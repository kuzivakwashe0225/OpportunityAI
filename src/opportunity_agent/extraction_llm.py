"""LLM-based extraction of profile facts from uploaded documents (CVs, etc).

Uses a local Ollama instance - SOLUTION_DEFINITION.md §14's deferred
"document extraction," now unblocked. Client is injectable, same
testability pattern as search.py/documents.py: tests never need a real
Ollama instance running.

Model choice is a real operational constraint, not a preference: the
deployment server has 11GB RAM shared with several other live projects and
no GPU. The installed 27B model OOM-killed the Ollama process on first live
test; qwen2.5:0.5b (397MB) is what's actually verified safe to run there
(confirmed live: memory barely moved across a real call). Extraction quality
is correspondingly modest for a model this small - conservative parsing
(returning less rather than guessing) matters more than usual here.

We talk to ``/api/chat``, not ``/api/generate``, so that a *reasoning* model
can be swapped in via ``OLLAMA_MODEL`` without changing this file. On
``/api/generate`` a reasoning model streams its raw chain-of-thought into
``response`` and the JSON never arrives - verified against ``gpt-oss:20b``,
which returned ``"The user says: ..."`` instead of an object. ``/api/chat``
splits that off into ``message.thinking`` and leaves ``message.content``
clean. Non-reasoning models answer the same way on both endpoints, so
qwen2.5:0.5b is unaffected. (Do not "help" by sending ``think: false`` -
against gpt-oss:20b that produced an empty response and zero eval tokens.)
"""

from __future__ import annotations

import json

import httpx
from pydantic import BaseModel, Field

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5:0.5b"
_MAX_INPUT_CHARS = 6000
# Generous because this call is synchronous inside the extract request, and a
# reasoning model spends tokens thinking before it emits any JSON: one real
# CV through gpt-oss:20b took 185s on a contended box, which the previous
# 120s would have cut off mid-answer.
_TIMEOUT_SECONDS = 300.0

_PROMPT_TEMPLATE = """You extract facts from a CV or personal document. Read the text below and \
return ONLY a JSON object with these fields. Use [] or null for anything not clearly present - \
never invent a value that isn't in the text.

{{
  "work_history": [one string per role, e.g. "Software Engineer, Acme Corp (2021-2024): built payments infrastructure"],
  "certificates": [one string per qualification or certificate],
  "study_level": one of "undergraduate", "bachelors", "masters", "doctoral", or null,
  "field": string (field of study) or null
}}

Document text:
---
{text}
---

JSON:"""


class ExtractedFacts(BaseModel):
    work_history: list[str] = Field(default_factory=list)
    certificates: list[str] = Field(default_factory=list)
    study_level: str | None = None
    field: str | None = None


def _text_list_from_model(value: object) -> list[str]:
    """Keep text from a model-produced list without trusting its shape.

    Small local models occasionally emit a role as a nested JSON array, for
    example ``[[\"Software Engineer\", \"Acme (2021-2024)\"]]``.  That is
    still document-derived information, but it is not valid for our
    ``list[str]`` contract.  Flatten each nested entry into one readable item
    rather than failing extraction of the entire uploaded document.  Other
    JSON types are deliberately ignored: we do not stringify objects or
    numbers into profile facts.
    """
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return []

    items: list[str] = []
    for entry in value:
        if isinstance(entry, str):
            cleaned = entry.strip()
            if cleaned:
                items.append(cleaned)
        elif isinstance(entry, list):
            parts = _text_list_from_model(entry)
            if parts:
                items.append(" ".join(parts))
    return items


def extract_facts_from_text(
    text: str,
    *,
    base_url: str = DEFAULT_OLLAMA_URL,
    model: str = DEFAULT_MODEL,
    client: httpx.Client | None = None,
) -> ExtractedFacts:
    prompt = _PROMPT_TEMPLATE.format(text=text[:_MAX_INPUT_CHARS])

    owns_client = client is None
    http_client = client or httpx.Client(timeout=_TIMEOUT_SECONDS)
    try:
        response = http_client.post(
            f"{base_url}/api/chat",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "format": "json",
            },
        )
        response.raise_for_status()
        payload = response.json()
    finally:
        if owns_client:
            http_client.close()

    # Only message.content is the answer. A reasoning model's message also
    # carries a "thinking" field, which is deliberately ignored.
    message = payload.get("message")
    content = message.get("content") if isinstance(message, dict) else None

    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return ExtractedFacts()

    if not isinstance(parsed, dict):
        return ExtractedFacts()

    return ExtractedFacts(
        work_history=_text_list_from_model(parsed.get("work_history")),
        certificates=_text_list_from_model(parsed.get("certificates")),
        study_level=parsed.get("study_level") or None,
        field=parsed.get("field") or None,
    )


# --------------------------------------------------------------------------
# Free-text profile assist
# --------------------------------------------------------------------------
# The CV path above answers one fixed question. This one answers whatever the
# profile schema currently asks, which is the difference between "extract a
# CV" and "help someone fill in this form": a tender profile wants a
# registration number and PRAZ categories, not a study level.
#
# The prompt is generated from the schema rather than written out, so adding a
# FieldSpec in profile_schema.py makes it extractable here with no change to
# this file. That is deliberate - the alternative is two lists that drift.

_ASSIST_HEADER = """You are helping someone fill in a form. Read their notes below and \
return ONLY a JSON object using exactly these keys.

Rules that matter more than completeness:
- Use null (or [] for lists) for anything the notes do not clearly state.
- Never invent, guess or infer a value that is not in the notes.
- Do not carry a value over from an example - the examples show format only.

Keys:
"""


# Field hints are deliberately NOT sent to the model.
#
# They are written for a human reading a form - "e.g. GE001", "Client, value,
# year." - and a small model reads them as data rather than as guidance. On
# qwen2.5:0.5b the "Client, value, year." hint came back as the past contracts
# ["Client #1", "Value A year ago", "Year B"]: invented bid history, which in
# a tender submission is considerably worse than an empty field.
#
# Stripping only the "e.g." half was tried first and is not enough, because a
# hint can be a bare template with no "e.g." in it at all. Telling a template
# from an explanation by shape is guesswork, and guessing wrong reintroduces
# fabricated data. The label alone ("Notable past contracts") is the signal
# the model actually needs, so that is all it gets.


def _field_line(spec) -> str:
    """One line of prompt per field, phrased by the field's own kind."""
    if spec.kind == "list":
        shape = "a JSON array of strings, [] if absent"
    elif spec.kind == "number":
        shape = "a number, or null"
    elif spec.kind == "select":
        allowed = [o for o in spec.options if o]
        shape = "one of " + ", ".join(repr(o) for o in allowed) + ", or null"
    else:
        shape = "a string, or null"
    return f'  "{spec.key}": {shape} - {spec.label}'


def build_assist_prompt(field_specs, text: str) -> str:
    lines = [_field_line(spec) for spec in field_specs]
    return (
        _ASSIST_HEADER
        + "\n".join(lines)
        + "\n\nTheir notes:\n---\n"
        + text[:_MAX_INPUT_CHARS]
        + "\n---\n\nJSON:"
    )


# Things a model says when it means "nothing here". Passing these through
# would put the literal string "N/A" into a profile field, which then travels
# into a search query and a drafted application.
_NON_ANSWERS = {
    "n/a", "na", "n.a.", "none", "null", "nil", "unknown", "not specified",
    "not stated", "not provided", "not applicable", "-", "--", "tbd", "unspecified",
}


def _is_non_answer(text: str) -> bool:
    return text.strip().casefold().strip(" .") in _NON_ANSWERS


def _coerce(spec, value: object) -> object | None:
    """Force a model answer into the shape the field actually accepts.

    Anything that cannot be coerced becomes None rather than being passed
    through: this output is offered to the owner as a suggestion, and a
    suggestion in the wrong shape is worse than no suggestion.
    """
    if value is None:
        return None
    if spec.kind == "list":
        items = [i for i in _text_list_from_model(value) if not _is_non_answer(i)]
        return items or None
    if spec.kind == "number":
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return value
        try:
            text = str(value).strip()
            return int(text) if text.isdigit() else float(text)
        except (TypeError, ValueError):
            return None
    if spec.kind == "select":
        allowed = {o.casefold(): o for o in spec.options if o}
        return allowed.get(str(value).strip().casefold())
    if isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    if not text or _is_non_answer(text):
        return None
    return text


def extract_profile_fields(
    text: str,
    *,
    field_specs,
    base_url: str = DEFAULT_OLLAMA_URL,
    model: str = DEFAULT_MODEL,
    client: httpx.Client | None = None,
) -> dict:
    """Suggest values for the profile's own fields from free-typed notes.

    Returns only the keys it actually found something for. Callers are
    expected to *offer* these, not apply them: the owner's typed value is
    always worth more than a small model's reading of their notes.
    """
    prompt = build_assist_prompt(field_specs, text)

    owns_client = client is None
    http_client = client or httpx.Client(timeout=_TIMEOUT_SECONDS)
    try:
        response = http_client.post(
            f"{base_url}/api/chat",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "format": "json",
            },
        )
        response.raise_for_status()
        payload = response.json()
    finally:
        if owns_client:
            http_client.close()

    message = payload.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}

    suggested: dict = {}
    for spec in field_specs:
        coerced = _coerce(spec, parsed.get(spec.key))
        if coerced is not None:
            suggested[spec.key] = coerced
    return suggested
