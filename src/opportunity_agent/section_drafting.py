"""Writing the sections a call actually asked for.

drafting.py assembles a cover note from profile fields with no model call,
which is what keeps it incapable of fabricating a claim. That is the right
guarantee for a letter that asserts who you are and what you have done.

It is not sufficient for a call that asks for a "Policy Problem Statement"
and "Key Research Questions". Those sections are not assertions about the
applicant at all - they are the applicant's thinking, and refusing to write
them does not produce a safer application, it produces no application.

So this module writes them, under two constraints that keep the original
guarantee where it still applies:

* **Facts come from the profile, prose comes from the model.** The profile's
  stated facts are given to the model as the only permitted source for any
  claim about the applicant. It is told, explicitly, not to invent
  qualifications, employers, institutions or results.
* **Every section is marked as a draft the owner must edit.** The package
  returned here is a starting point that saves an hour of blank-page work,
  not something to submit unread. The UI makes every section editable for
  exactly this reason.
"""

from __future__ import annotations

import httpx

from .application_spec import SectionSpec, SubmissionSpec
from .extraction_llm import (
    DEFAULT_MODEL,
    DEFAULT_OLLAMA_URL,
    _TIMEOUT_SECONDS,
)

# Long enough for a real section, short enough that a small model does not
# wander. The page limits these calls impose are tight - POTRAZ allowed 3.5
# pages for nine sections - so verbosity is a defect here, not a feature.
_MAX_SECTION_WORDS = 220

_SECTION_PROMPT = """You are drafting one section of a {document_kind} for this call.

CALL: {title}
{call_context}

SECTION TO WRITE: {section_title}
{section_guidance}

ABOUT THE APPLICANT - this is the only information you may use about them:
{profile_facts}

Rules:
- Write only the body of this section. No heading, no preamble, no sign-off.
- Do not invent qualifications, employers, institutions, dates or results. If \
the applicant information above does not support a claim, do not make it.
- Where the section calls for analysis or proposed work rather than facts about \
the applicant, write substantive content addressing the call's subject.
- Around {max_words} words. Plain professional prose, no bullet lists unless \
the section is naturally a list.
- British English.

Write the section now:"""


def profile_facts(profile) -> str:
    """The applicant's stated facts, as the only permitted source.

    Deliberately a flat list of what the owner actually entered - the point
    is that a reader of the prompt can see exactly what the model was allowed
    to assert, which is also what makes a fabricated claim identifiable as
    one afterwards.
    """
    bits: list[str] = []

    def add(label: str, value) -> None:
        if not value:
            return
        if isinstance(value, (list, tuple)):
            items = [str(v).strip() for v in value if str(v).strip()]
            if items:
                bits.append(f"- {label}: {'; '.join(items)}")
        else:
            text = str(value).strip()
            if text:
                bits.append(f"- {label}: {text}")

    add("Name", getattr(profile, "name", None))
    add("Country", getattr(profile, "country", None))
    # Person-shaped
    add("Field", getattr(profile, "field", None))
    add("Study level", getattr(profile, "study_level", None))
    add("Qualifications", getattr(profile, "certificates", None))
    add("Work history", getattr(profile, "work_history", None))
    add("Achievements", getattr(profile, "achievements", None))
    # Organisation-shaped
    add("Sectors", getattr(profile, "sectors", None))
    add("Past contracts", getattr(profile, "past_contracts", None))
    add("Certifications", getattr(profile, "certifications", None))
    add("Years trading", getattr(profile, "years_trading", None))
    # Shared
    add("Interests", getattr(profile, "interests", None))
    add("Goals", getattr(profile, "goals", None))
    add("Background", getattr(profile, "history", None))
    add("Other notes", getattr(profile, "notes", None))

    return "\n".join(bits) if bits else "- (the applicant has not filled in their profile)"


def build_section_prompt(
    section: SectionSpec,
    spec: SubmissionSpec,
    opportunity,
    facts: str,
    max_words: int = _MAX_SECTION_WORDS,
) -> str:
    context_bits = []
    if spec.eligibility:
        context_bits.append("Who may apply: " + "; ".join(spec.eligibility))
    summary = (getattr(opportunity, "summary", "") or "")[:900]
    if summary:
        context_bits.append("From the call: " + summary)

    return _SECTION_PROMPT.format(
        document_kind=spec.document_kind or "application",
        title=getattr(opportunity, "title", "this opportunity"),
        call_context="\n".join(context_bits),
        section_title=section.title,
        section_guidance=(f"The call says this section should cover: {section.guidance}"
                          if section.guidance else ""),
        profile_facts=facts,
        max_words=max_words,
    )


def draft_section(
    section: SectionSpec,
    spec: SubmissionSpec,
    opportunity,
    facts: str,
    *,
    base_url: str = DEFAULT_OLLAMA_URL,
    model: str = DEFAULT_MODEL,
    client: httpx.Client | None = None,
) -> str:
    """One section's body, or "" if it could not be written.

    Never raises: a section the model could not produce becomes an empty one
    for the owner to fill, which is still a usable skeleton. Losing the whole
    package because section 6 of 9 timed out would not be.
    """
    owns_client = client is None
    http_client = client or httpx.Client(timeout=_TIMEOUT_SECONDS)
    try:
        response = http_client.post(
            f"{base_url}/api/chat",
            json={
                "model": model,
                "messages": [{
                    "role": "user",
                    "content": build_section_prompt(section, spec, opportunity, facts),
                }],
                "stream": False,
            },
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return ""
    finally:
        if owns_client:
            http_client.close()

    message = payload.get("message")
    if not isinstance(message, dict):
        return ""
    return (message.get("content") or "").strip()
