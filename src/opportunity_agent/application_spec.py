"""What a particular call actually asks you to submit.

The gap this closes, in the owner's own words after reading what the system
produced for a POTRAZ research call: "it gave me an inadequate application,
just an unprofessional email".

He was right, and drafting.py's docstring explains why it happened. It
assembles a cover note from profile fields and makes no model call at all -
a deliberate choice to guarantee nothing is fabricated. That guarantee is
worth keeping, but it silently assumed every opportunity wants the same
thing: a letter. The POTRAZ call wanted a 3.5-page research proposal, Times
New Roman 12, 1.5 spacing, as a PDF, with nine named sections in a fixed
order, submitted to a specific address by a specific date. A letter is not a
worse version of that. It is the wrong artefact.

So this module reads the call itself and produces the *shape* of what is
being asked for: which sections, in what order, what each must contain, and
the format rules the submission is judged against before anyone reads it.
drafting.py then fills that shape in.

Two deliberate limits:

* **It reports only what the call states.** Every field is optional and
  absent when the page does not say - an invented page limit is worse than
  no page limit, because the owner would trust it.
* **It never invents a section.** If no structure can be read off the page,
  `sections` is empty and drafting falls back to the cover note it has always
  produced. A confidently wrong nine-section skeleton for a call that wanted
  a two-page letter helps nobody.
"""

from __future__ import annotations

import json

import httpx
from pydantic import BaseModel, Field

from .extraction_llm import (
    DEFAULT_MODEL,
    DEFAULT_OLLAMA_URL,
    _MAX_INPUT_CHARS,
    _TIMEOUT_SECONDS,
)


class FormatRules(BaseModel):
    """How the submission must be presented.

    These are not cosmetic. A call that says "maximum 3.5 pages" and "below
    10% AI-generated text" is stating grounds for rejection before the
    content is read at all.
    """

    max_pages: float | None = None
    font: str | None = None
    font_size: int | None = None
    line_spacing: str | None = None
    file_format: str | None = None
    language: str | None = None
    # Things like "AI-generated text below 10%" that do not fit the fields
    # above but that the owner must not discover after submitting.
    other: list[str] = Field(default_factory=list)


class SectionSpec(BaseModel):
    title: str
    # What the call says this section should contain, in the call's own terms.
    guidance: str = ""


class SubmissionSpec(BaseModel):
    """The shape of what this particular call wants submitted."""

    # "research proposal", "cover letter", "bid", "concept note"...
    document_kind: str = ""
    sections: list[SectionSpec] = Field(default_factory=list)
    format_rules: FormatRules = Field(default_factory=FormatRules)
    submit_to: str | None = None
    deadline: str | None = None
    eligibility: list[str] = Field(default_factory=list)

    @property
    def is_structured(self) -> bool:
        """Whether this is worth drafting section-by-section at all."""
        return len(self.sections) >= 2


_PROMPT = """You are reading a call for applications, proposals or tenders. Report ONLY what \
the text below actually states about how to apply. Return a JSON object.

{{
  "document_kind": what must be produced, e.g. "research proposal", "cover letter", "bid" - "" if unstated,
  "sections": [ {{"title": exact section heading the call asks for, "guidance": what the call says it must contain}} ],
  "format_rules": {{
    "max_pages": number or null,
    "font": string or null,
    "font_size": number or null,
    "line_spacing": string or null,
    "file_format": string or null,
    "language": string or null,
    "other": [any other stated presentation rule, e.g. limits on AI-generated text]
  }},
  "submit_to": email address or submission portal stated, or null,
  "deadline": the stated closing date exactly as written, or null,
  "eligibility": [who the call says may apply]
}}

Rules that matter more than completeness:
- Report only what the text states. Use null or [] for anything it does not.
- Never invent a section, a page limit or a deadline. A wrong one is worse than none.
- Keep section titles in the call's own words, in the order the call gives them.

Call text:
---
{text}
---

JSON:"""


def build_spec_prompt(text: str) -> str:
    return _PROMPT.format(text=text[:_MAX_INPUT_CHARS])


def _clean_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _clean_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [v.strip() for v in value if isinstance(v, str) and v.strip()]


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_spec(payload: dict) -> SubmissionSpec:
    """Turn a model answer into a spec, keeping only what holds its shape."""
    raw_format = payload.get("format_rules")
    raw_format = raw_format if isinstance(raw_format, dict) else {}

    size = _number(raw_format.get("font_size"))
    rules = FormatRules(
        max_pages=_number(raw_format.get("max_pages")),
        font=_clean_str(raw_format.get("font")),
        font_size=int(size) if size else None,
        line_spacing=_clean_str(raw_format.get("line_spacing")),
        file_format=_clean_str(raw_format.get("file_format")),
        language=_clean_str(raw_format.get("language")),
        other=_clean_list(raw_format.get("other")),
    )

    sections: list[SectionSpec] = []
    for item in payload.get("sections") or []:
        if isinstance(item, dict):
            title = _clean_str(item.get("title"))
            if title:
                sections.append(SectionSpec(
                    title=title, guidance=_clean_str(item.get("guidance")) or ""
                ))
        elif isinstance(item, str) and item.strip():
            # Some models answer with a bare list of headings. That is still
            # the ordered structure we asked for, just without the guidance.
            sections.append(SectionSpec(title=item.strip()))

    return SubmissionSpec(
        document_kind=_clean_str(payload.get("document_kind")) or "",
        sections=sections,
        format_rules=rules,
        submit_to=_clean_str(payload.get("submit_to")),
        deadline=_clean_str(payload.get("deadline")),
        eligibility=_clean_list(payload.get("eligibility")),
    )


def extract_submission_spec(
    text: str,
    *,
    base_url: str = DEFAULT_OLLAMA_URL,
    model: str = DEFAULT_MODEL,
    client: httpx.Client | None = None,
) -> SubmissionSpec:
    """Read the call and report what it asks for.

    Returns an empty spec rather than raising when the page cannot be read or
    the model answers with nonsense - an unreadable call should cost the
    tailored draft, not the whole discovery cycle.
    """
    if not (text or "").strip():
        return SubmissionSpec()

    owns_client = client is None
    http_client = client or httpx.Client(timeout=_TIMEOUT_SECONDS)
    try:
        response = http_client.post(
            f"{base_url}/api/chat",
            json={
                "model": model,
                "messages": [{"role": "user", "content": build_spec_prompt(text)}],
                "stream": False,
                "format": "json",
            },
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return SubmissionSpec()
    finally:
        if owns_client:
            http_client.close()

    message = payload.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return SubmissionSpec()
    if not isinstance(parsed, dict):
        return SubmissionSpec()

    return parse_spec(parsed)
