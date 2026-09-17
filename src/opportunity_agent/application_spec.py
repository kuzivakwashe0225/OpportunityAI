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

import re

import json

import httpx
from pydantic import BaseModel, Field

from .extraction_llm import (
    DEFAULT_MODEL,
    DEFAULT_OLLAMA_URL,
    _TIMEOUT_SECONDS,
)

# Deliberately far larger than extraction_llm's 6000-character window, and not
# shared with it. That limit suits a CV, where everything that matters is near
# the top. A call for proposals is the opposite shape: the submission address,
# the closing date and the formatting rules are almost always in the last page,
# after several pages of programme background. Truncating the head of a real
# 7,401-character POTRAZ PDF at 6000 dropped precisely the deadline and the
# email - the extraction reported them as absent, which reads identically to
# a call that never stated them.
_SPEC_MAX_CHARS = 18000
# When even that is not enough, the head and the tail both matter more than
# the middle, for the same reason.
_SPEC_HEAD_CHARS = 11000
_SPEC_TAIL_CHARS = 6000


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


def trim_for_spec(text: str) -> str:
    """Keep the parts of a call that carry the requirements.

    Head and tail rather than the first N characters: a call opens with the
    programme background and closes with how, where and by when to submit.
    Given a choice, the middle is what to lose.
    """
    if len(text) <= _SPEC_MAX_CHARS:
        return text
    head = text[:_SPEC_HEAD_CHARS]
    tail = text[-_SPEC_TAIL_CHARS:]
    return head + "\n\n[...]\n\n" + tail


def build_spec_prompt(text: str) -> str:
    return _PROMPT.format(text=trim_for_spec(text))


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



# A call that lists its required structure almost always numbers it:
#
#   The proposal must be structured as follows:
#   1. Title of the Proposed Policy Research
#   2. Policy Problem Statement
#   ...
#
# Reading that with a regular expression is completely reliable, and asking a
# 3B model to do it is not. A real run returned all nine sections one day and
# none at all the next, from the same text - and "none" collapses the whole
# application to a covering letter, which is the failure the owner reported
# in the first place.
#
# So the model still goes first, because it handles prose that does not
# number itself. This catches it when it comes back empty-handed.
_NUMBERED_HEADING = re.compile(
    r"^\s{0,8}(?:\(?(?:\d{1,2}|[ivx]{1,4}|[a-z])[\.\)]|[-\u2022])\s+"
    r"([A-Z][^\n]{3,90}?)\s*$",
    re.MULTILINE,
)

# Phrases that introduce the list of required sections. Looking for the list
# *after* one of these avoids picking up a numbered list of eligibility rules
# or of documents to attach.
_STRUCTURE_CUES = (
    "structured as follows", "must be structured", "should be structured",
    "must contain the following", "should contain the following",
    "must include the following", "should include the following",
    "the following sections", "following structure", "proposal structure",
    "format of the proposal", "sections:", "structure:",
)

# Fewer than this and it is probably not a section list at all.
_MIN_HEADINGS = 3
_MAX_HEADINGS = 20


def sections_from_text(text: str) -> list[SectionSpec]:
    """The call's own numbered section list, or [] if it does not have one."""
    body = text or ""
    lowered = body.lower()

    start = -1
    for cue in _STRUCTURE_CUES:
        found = lowered.find(cue)
        if found != -1 and (start == -1 or found < start):
            start = found
    if start == -1:
        return []

    # Everything after the cue, capped: a section list runs to a few hundred
    # characters, and reading further starts picking up unrelated lists.
    window = body[start:start + 2500]
    headings = [" ".join(m.group(1).split()).strip(" .:-") for m in _NUMBERED_HEADING.finditer(window)]

    cleaned: list[str] = []
    for heading in headings:
        if not (4 <= len(heading) <= 90):
            continue
        if heading.lower() in (h.lower() for h in cleaned):
            continue
        cleaned.append(heading)

    if len(cleaned) < _MIN_HEADINGS:
        return []
    return [SectionSpec(title=h) for h in cleaned[:_MAX_HEADINGS]]

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

    When the model reports no sections, the call's own numbered list is read
    off the text instead. The same POTRAZ text gave nine sections on one run
    and none on the next; with no sections there is nothing to draft, and the
    whole application collapses back to a covering letter - which is the
    complaint this was all meant to answer.
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

    spec = parse_spec(parsed)
    if not spec.sections:
        fallback = sections_from_text(text)
        if fallback:
            spec = spec.model_copy(update={"sections": fallback})
    return spec
