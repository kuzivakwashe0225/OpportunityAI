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
