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

import re

import httpx

from .application_spec import SectionSpec, SubmissionSpec
from .extraction_llm import (
    DEFAULT_MODEL,
    DEFAULT_OLLAMA_URL,
    _TIMEOUT_SECONDS,
)

# What one page of Times New Roman 12 at 1.5 spacing actually holds. Used to
# turn a call's page limit into a word budget per section, because "3.5 pages
# for nine sections" is a real constraint and a section that ignores it is a
# section the owner has to cut by hand.
_WORDS_PER_PAGE = 450

# When the call states no limit. Long enough to say something, short enough
# that a small model does not wander into padding.
_DEFAULT_SECTION_WORDS = 220

# Floor and ceiling whatever the arithmetic says. Below the floor a section is
# a sentence and answers nothing; above the ceiling a 3B model loses the thread
# and starts repeating itself.
_MIN_SECTION_WORDS = 110
_MAX_SECTION_WORDS = 600

# What a section is actually asking for. Read off its heading, because the
# heading is all a call gives you and it is nearly always enough.
#
# This exists because the first version of the rewritten prompt put the
# applicant's whole CV in front of the model and asked for a "Policy Problem
# Statement" - and got three paragraphs of biography. The model was doing the
# reasonable thing with what it was given: the CV was the most concrete
# material in the prompt, so it wrote about the CV. A section that asks what
# is wrong with national ICT policy is not asking who the applicant is, and
# nothing in the prompt had said so.
_TITLE_HEADINGS = (
    "title of", "title:", "proposed title", "project title", "research title",
    "name of the proposed",
)

_ABOUT_THE_APPLICANT = (
    "about you", "about the applicant", "personal statement", "personal details",
    "biography", "profile", "experience", "work history", "employment",
    "qualification", "education", "capability", "capacity", "track record",
    "past performance", "references", "key personnel", "team", "motivation",
    "why you", "why do you", "statement of purpose", "career", "skills",
    "company profile", "organisational", "organizational",
)

_ANALYTICAL = (
    "problem", "background", "justification", "rationale", "objective",
    "aim", "question", "methodology", "method", "approach", "workplan",
    "work plan", "timeline", "budget", "outcome", "impact", "deliverable",
    "risk", "sustainability", "literature", "scope", "technical proposal",
    "implementation", "monitoring", "evaluation", "dissemination",
)


def section_kind(title: str) -> str:
    """One of "title", "applicant" or "analysis".

    Deliberately keyword-based rather than a model call: it runs once per
    section, it has to be predictable, and a heading is short enough that
    keywords read it about as well as anything would. Anything unrecognised
    is treated as analysis, which is the safer default - a section wrongly
    told to analyse still produces content about the call's subject, while a
    section wrongly told to recite the CV produces the biography this exists
    to stop.
    """
    lowered = (title or "").strip().lower()
    if any(word in lowered for word in _TITLE_HEADINGS) or lowered in ("title", "titles"):
        return "title"
    if any(word in lowered for word in _ABOUT_THE_APPLICANT):
        return "applicant"
    if any(word in lowered for word in _ANALYTICAL):
        return "analysis"
    return "analysis"


_KIND_INSTRUCTIONS = {
    "title": (
        "This section is a TITLE. Write one single line - a specific, concrete "
        "title for the work being proposed. No sentences, no paragraph, no "
        "explanation, no biography. Just the title itself."
    ),
    "applicant": (
        "This section IS about the applicant. Write it in the first person "
        "(\"I\", or \"we\" for an organisation) and fill it with their real "
        "specifics from the evidence - named projects, employers, "
        "qualifications, dates, results."
    ),
    "analysis": (
        "This section asks about the subject matter, not about the applicant. "
        "Write substantive content on the call's own subject: the problem, "
        "the approach, the plan.\n"
        "  Write about the work in the first person future - \"I will\", "
        "\"this research will\" - describing what is proposed.\n"
        "  Say NOTHING about the applicant's history. Do not name them. Do "
        "not mention an employer, a qualification, a past project or a past "
        "result. You have not been told any of those and anything you write "
        "about them would be invented. If a sentence starts to describe who "
        "the applicant is or what they have done, delete it and write about "
        "the proposed work instead."
    ),
}

_SECTION_PROMPT = """You are helping an applicant write one section of a \
{document_kind}. Write it as they would write it about themselves: specific, \
evidenced, and in the first person where the section is about them.

THE CALL: {title}
{call_context}

THE WHOLE APPLICATION MUST COVER THESE, IN ORDER:
{all_sections}

{written_so_far}YOU ARE WRITING SECTION {position}: {section_title}
{section_guidance}
{kind_instruction}

EVERYTHING KNOWN ABOUT THE APPLICANT - the only source you may draw on:
{profile_facts}

How to write it:
- Answer this section's question fully and directly. Do not restate the \
question, and do not write an introduction to your answer.
- Write the applicant's name exactly as it is spelled in the evidence, or \
not at all.
- Use the applicant's real specifics from the evidence above - actual project \
names, employers, qualifications, dates, results. A sentence naming a real \
project is worth a paragraph of "the applicant is passionate about".
- Never invent a qualification, employer, institution, date or result. If the \
evidence does not support a claim, leave the claim out.
- Where the section asks for analysis or proposed work rather than facts about \
the applicant, write substantive content on the call's subject and connect it \
to what the applicant has actually done.
- Do not repeat what the earlier sections already said. This section has its \
own job.
- About {max_words} words. Plain professional prose. British English.
- Output the section body only: no heading, no title, no markdown, no \
sign-off, no note about being an AI.

Write it now:"""

_TITLE_PROMPT = """Propose a title for a {document_kind} answering this call.

THE CALL: {title}
{call_context}

THE PROPOSAL WILL COVER:
{all_sections}

{profile_facts}

Output exactly one line: the title itself. A specific, concrete title naming \
the subject and the approach - for example "Regulating AI-Assisted Fraud \
Detection in Zimbabwe's Mobile Money Sector". No name, no quotation marks, no \
label, no explanation, nothing before or after it.

The title:"""

_COVER_LETTER_PROMPT = """Write a covering letter for this application, in the \
applicant's own voice.

THE CALL: {title}
{call_context}

{addressee}

EVERYTHING KNOWN ABOUT THE APPLICANT - the only source you may draw on:
{profile_facts}

How to write it:
- Structure it exactly like this, and do not deviate:
  line 1: the salutation.
  line 2 onwards: a first sentence of the form "I should like to be \
considered for <the call>." or "I am submitting a proposal for <the call>."
  then two or three paragraphs of substance.
  then a closing line and the applicant's name.
- Never write a placeholder in brackets. No "[To Whom It May Concern]", no \
"[Your Name]", no "[insert date]". If you do not have something, leave it \
out entirely.
- Write the applicant's name exactly as it is spelled in the evidence.
- Two or three short paragraphs of substance: the specific qualification, \
project or contract that makes them a credible applicant for THIS call, named \
exactly as it appears in their evidence.
- Close with what is enclosed and how to reach them.
- Every fact you state about the applicant must appear in the evidence above. Do not add a university, an employer, a project or a result that is not written there, and do not upgrade what it says - if it says they attended a symposium, they attended it; they did not organise it.
- Around {max_words} words. British English. No markdown, no placeholders \
like [Your Name] - use their real name, and leave out anything you do not \
have.

Write the letter now:"""


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


def words_for_each_section(spec, section_count: int) -> int:
    """Turn the call's page limit into a per-section word budget.

    A call that says "not more than 3.5 pages" and lists nine sections is
    asking for roughly 175 words each, and nine 220-word sections overshoot it
    by half a page - which the owner then has to cut by hand, section by
    section, which is the work this was supposed to save.
    """
    max_pages = getattr(getattr(spec, "format_rules", None), "max_pages", None)
    if not max_pages or section_count < 1:
        return _DEFAULT_SECTION_WORDS
    try:
        total = float(max_pages) * _WORDS_PER_PAGE
    except (TypeError, ValueError):
        return _DEFAULT_SECTION_WORDS
    return max(_MIN_SECTION_WORDS, min(_MAX_SECTION_WORDS, int(total / section_count)))


def strip_echoed_guidance(body: str, guidance: str) -> str:
    """Drop an opening sentence that is just the call's instruction repeated.

    A real run opened a Policy Problem Statement with "Present a brief
    overview of the problem in the ICT policy area that your research will
    address." - which is what the call asked for, not an answer to it. The
    prompt already says not to restate the question; this removes it when the
    model does so anyway.
    """
    text = (body or "").strip()
    guide = " ".join((guidance or "").split()).strip().lower().rstrip(".")
    if not text or len(guide) < 15:
        return text
    first, _, rest = text.partition(".")
    if " ".join(first.split()).lower().strip().rstrip(".") == guide and rest.strip():
        return rest.strip()
    # Also catch a near-match: the model often reproduces the guidance with a
    # word changed, which a strict comparison misses.
    words_first = set(" ".join(first.split()).lower().split())
    words_guide = set(guide.split())
    if words_guide and rest.strip() and len(words_guide) >= 6:
        overlap = len(words_first & words_guide) / len(words_guide)
        if overlap > 0.8:
            return rest.strip()
    return text


def strip_leaked_heading(body: str, section_title: str) -> str:
    """Remove a heading the model added despite being told not to.

    Small models restate the section title as a markdown heading roughly a
    third of the time. Left in, it lands in the .docx underneath the real
    heading, so every section appears titled twice.
    """
    text = (body or "").strip()
    if not text:
        return ""
    lines = text.split("\n")
    first = lines[0].strip()
    looks_like_heading = (
        first.startswith("#")
        or (first.rstrip(":").strip().lower() == (section_title or "").strip().lower())
        or (first.startswith("**") and first.endswith("**") and len(first) < 120)
    )
    if looks_like_heading:
        lines = lines[1:]
    # Markdown in a Word document is noise, not formatting. A real run put
    # "the **POTRAZ Call for Research Proposals 2026**" in a covering letter.
    cleaned = "\n".join(lines).strip()
    for marker in ("### ", "## ", "# "):
        cleaned = cleaned.replace(marker, "")
    cleaned = re.sub(r"\*\*(.+?)\*\*", r"\1", cleaned, flags=re.S)
    cleaned = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", cleaned, flags=re.S)
    return cleaned.strip()


def _call_context(spec: SubmissionSpec, opportunity) -> str:
    bits = []
    if spec.eligibility:
        bits.append("Who may apply: " + "; ".join(spec.eligibility))
    if spec.deadline:
        bits.append("Closes: " + spec.deadline)
    summary = (getattr(opportunity, "summary", "") or "")[:1400]
    if summary:
        bits.append("From the call: " + summary)
    return "\n".join(bits)


def _already_written(written: list[dict] | None) -> str:
    """A digest of the sections drafted before this one.

    Without it each section is written by something that has never seen the
    others, and it shows: the objectives contradict the problem statement, and
    the same sentence about the applicant appears in four places. A person
    drafting by hand has the earlier sections in front of them, so this puts
    them there.

    The opening line of each is enough to convey what ground it covered -
    passing the full text would crowd out the applicant's own evidence, which
    is the thing that must not be crowded out.
    """
    if not written:
        return ""
    lines = []
    for item in written:
        title = str(item.get("title") or "").strip()
        body = " ".join(str(item.get("body") or "").split())
        if not title:
            continue
        lines.append(f"- {title}: {body[:180]}{'...' if len(body) > 180 else ''}")
    if not lines:
        return ""
    return (
        "ALREADY WRITTEN - do not repeat this ground:\n"
        + "\n".join(lines)
        + "\n\n"
    )


def build_section_prompt(
    section: SectionSpec,
    spec: SubmissionSpec,
    opportunity,
    facts: str,
    max_words: int = _DEFAULT_SECTION_WORDS,
    *,
    all_sections: list[SectionSpec] | None = None,
    written: list[dict] | None = None,
) -> str:
    sections = list(all_sections or spec.sections or [section])
    titles = "\n".join(
        f"{n}. {sec.title}" for n, sec in enumerate(sections, start=1)
    ) or f"1. {section.title}"
    try:
        position = next(
            n for n, sec in enumerate(sections, start=1) if sec.title == section.title
        )
    except StopIteration:
        position = 1

    kind = section_kind(section.title)
    if kind == "title":
        return _TITLE_PROMPT.format(
            document_kind=spec.document_kind or "application",
            title=getattr(opportunity, "title", "this opportunity"),
            call_context=_call_context(spec, opportunity),
            all_sections=titles,
            profile_facts=facts,
        )
    return _SECTION_PROMPT.format(
        document_kind=spec.document_kind or "application",
        title=getattr(opportunity, "title", "this opportunity"),
        call_context=_call_context(spec, opportunity),
        all_sections=titles,
        written_so_far=_already_written(written),
        position=position,
        section_title=section.title,
        section_guidance=(f"The call says this section should cover: {section.guidance}"
                          if section.guidance else ""),
        kind_instruction=_KIND_INSTRUCTIONS[kind],
        profile_facts=facts,
        # A title is one line however many pages the call allows.
        max_words=25 if kind == "title" else max_words,
    )


def build_cover_letter_prompt(
    spec: SubmissionSpec, opportunity, facts: str, max_words: int = 320
) -> str:
    submit_to = (spec.submit_to or "").strip()
    if submit_to and "@" not in submit_to:
        # A named office or person: address them.
        addressee = f'ADDRESSED TO: {submit_to}. Open with "Dear {submit_to},".'
    elif submit_to:
        # Only an email address, which is not a salutation.
        addressee = (
            f"SUBMITTED TO: {submit_to} (an email address, not a person - open "
            'with "Dear Sir or Madam," and mention the address only at the end '
            "if at all)."
        )
    else:
        addressee = 'ADDRESSED TO: no recipient was named. Open with "Dear Sir or Madam,".'

    return _COVER_LETTER_PROMPT.format(
        title=getattr(opportunity, "title", "this opportunity"),
        call_context=_call_context(spec, opportunity),
        addressee=addressee,
        profile_facts=facts,
        max_words=max_words,
    )


# One retry, no more. Empty answers do happen - a small model under memory
# pressure returns nothing, and a request can time out while another model is
# being swapped in - and a blank section in a finished application is a bad
# outcome the owner has to notice and fix. Two attempts costs at most one
# extra minute; three would double the time of a nine-section proposal for
# very little more.
_ATTEMPTS = 2


def _ask(prompt: str, *, base_url: str, model: str, client: httpx.Client | None) -> str:
    """One model call, retried once if it comes back empty.

    Never raises. Losing a whole nine-section package because section six
    timed out would be far worse than one empty section the owner fills in.
    """
    for attempt in range(_ATTEMPTS):
        answer = _ask_once(prompt, base_url=base_url, model=model, client=client)
        if answer:
            return answer
    return ""


def _ask_once(prompt: str, *, base_url: str, model: str, client: httpx.Client | None) -> str:
    owns_client = client is None
    http_client = client or httpx.Client(timeout=_TIMEOUT_SECONDS)
    try:
        response = http_client.post(
            f"{base_url}/api/chat",
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                # Low but not zero: an application should read as considered
                # prose, not as the single most probable continuation, which
                # at this size is where the "passionate and dedicated" filler
                # comes from.
                "options": {"temperature": 0.4},
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


def draft_section(
    section: SectionSpec,
    spec: SubmissionSpec,
    opportunity,
    facts: str,
    *,
    all_sections: list[SectionSpec] | None = None,
    written: list[dict] | None = None,
    max_words: int | None = None,
    base_url: str = DEFAULT_OLLAMA_URL,
    model: str = DEFAULT_MODEL,
    client: httpx.Client | None = None,
) -> str:
    """One section's body, or "" if it could not be written."""
    sections = list(all_sections or spec.sections or [section])
    budget = max_words or words_for_each_section(spec, len(sections))
    prompt = build_section_prompt(
        section, spec, opportunity, facts, budget,
        all_sections=sections, written=written,
    )
    answer = strip_leaked_heading(
        _ask(prompt, base_url=base_url, model=model, client=client), section.title
    )
    answer = strip_echoed_guidance(answer, section.guidance)
    if section_kind(section.title) == "title":
        # One line means one line. A small model asked for a title will often
        # add an explanation underneath it, and the first line is the title.
        answer = answer.split("\n")[0].strip().strip('"').strip()
    return answer


def draft_cover_letter(
    spec: SubmissionSpec,
    opportunity,
    facts: str,
    *,
    max_words: int = 320,
    base_url: str = DEFAULT_OLLAMA_URL,
    model: str = DEFAULT_MODEL,
    client: httpx.Client | None = None,
) -> str:
    """A covering letter in the applicant's voice, or "" if it failed.

    Written rather than assembled from a template. The template version -
    still in drafting.py, and still the right answer when there is no model -
    cannot say why *this* applicant suits *this* call, and a letter that
    cannot say that is the "unprofessional email" the owner objected to.
    """
    prompt = build_cover_letter_prompt(spec, opportunity, facts, max_words)
    return strip_leaked_heading(
        _ask(prompt, base_url=base_url, model=model, client=client), "Covering letter"
    )
