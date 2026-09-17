"""Everything true about the applicant, gathered in one place for drafting.

The owner's complaint, in their words: "if I am to apply for an opportunity
manually I first list down the requirements then I start drafting each
requirement step by step according to my strengths, qualifications and
trainings."

Their strengths, qualifications and trainings are not in their profile. Six
one-line fields cannot hold them. They are in the CV they uploaded, the
transcript, the certificates - and until now those were read once, mined for
a handful of profile fields, and thrown away. Drafting was then given the
fields alone, which is precisely why what came out read like a stranger had
written it: it *was* written by something that had never seen their CV.

This module hands drafting the same thing the applicant would have in front of
them. Two ordering decisions matter:

**Stated facts first, documents second.** What the owner typed is what they
chose to say about themselves, and it is unambiguous. The CV is richer but
messier - dates, headings and page furniture all mixed together - so it goes
underneath as supporting detail rather than replacing the clean list.

**The CV before everything else.** When the budget runs out, a transcript's
module marks are the first thing worth losing and the CV is the last.
"""

from __future__ import annotations

# The whole dossier. Sized against what a small model can actually attend to:
# past roughly this much, a 3B model starts losing the middle, and the middle
# is where the work history sits.
MAX_DOSSIER_CHARS = 9_000

# Per document, so one long CV cannot crowd out the transcript entirely.
MAX_PER_DOCUMENT_CHARS = 4_000

# Most useful first. Anything not named here comes after these, in whatever
# order it was uploaded.
_DOCUMENT_PRIORITY = (
    "cv", "company_profile", "transcript", "certificate", "key_personnel_cvs",
    "past_performance", "reference_letter", "certifications",
)

_DOCUMENT_LABELS = {
    "cv": "CV",
    "transcript": "Academic transcript",
    "certificate": "Qualification certificate",
    "reference_letter": "Reference letter",
    "company_profile": "Company profile",
    "past_performance": "Past performance record",
    "key_personnel_cvs": "CVs of key personnel",
    "audited_financials": "Audited financial statements",
}


def _label(doc_type: str) -> str:
    return _DOCUMENT_LABELS.get(doc_type, (doc_type or "document").replace("_", " ").title())


def stated_facts(profile) -> str:
    """What the owner typed about themselves, as a flat list.

    Deliberately flat and readable: someone reading the prompt afterwards can
    see exactly what the model was permitted to assert, which is also what
    makes a fabricated claim identifiable as one.
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
    add("Employees", getattr(profile, "employee_count", None))
    # Shared
    add("Interests", getattr(profile, "interests", None))
    add("Goals", getattr(profile, "goals", None))
    add("Background", getattr(profile, "history", None))
    add("Other notes", getattr(profile, "notes", None))

    return "\n".join(bits) if bits else "- (the applicant has not filled in their profile)"


def document_excerpts(documents, budget: int = MAX_DOSSIER_CHARS) -> str:
    """The uploaded documents' own words, most useful first.

    `documents` is any iterable of objects with `doc_type` and
    `extracted_text` - the ORM rows, in practice. Documents with no readable
    text (a scanned certificate, say) are skipped silently: their absence is
    normal and is already reported elsewhere as the extraction status.
    """
    usable = [
        d for d in documents
        if (getattr(d, "extracted_text", None) or "").strip()
    ]
    if not usable:
        return ""

    def rank(document):
        doc_type = getattr(document, "doc_type", "") or ""
        return _DOCUMENT_PRIORITY.index(doc_type) if doc_type in _DOCUMENT_PRIORITY else 99

    parts: list[str] = []
    spent = 0
    for document in sorted(usable, key=rank):
        remaining = budget - spent
        if remaining < 400:
            # Less than this is a fragment nobody can use, and a fragment of a
            # transcript is worse than no transcript: it reads as if that is
            # all the applicant has.
            break
        text = (document.extracted_text or "").strip()[:min(MAX_PER_DOCUMENT_CHARS, remaining)]
        header = f"--- From the applicant's {_label(getattr(document, 'doc_type', ''))} ---"
        parts.append(f"{header}\n{text}")
        spent += len(text) + len(header)

    return "\n\n".join(parts)


def dossier(profile, documents=()) -> str:
    """Stated facts plus the documents, as one block for a prompt.

    This is the whole of what a draft may claim. Nothing outside it is
    permitted, and the prompts say so.
    """
    facts = stated_facts(profile)
    excerpts = document_excerpts(documents, budget=MAX_DOSSIER_CHARS - len(facts))
    if not excerpts:
        return facts
    return (
        f"{facts}\n\n"
        "The applicant's own documents follow. Use them for specifics - real "
        "project names, employers, dates, results - rather than writing in "
        "generalities.\n\n"
        f"{excerpts}"
    )


def has_real_evidence(documents=()) -> bool:
    """Whether anything was actually uploaded and read.

    Used to tell the owner why a draft is thin: a draft written from six
    profile fields and no documents is doing the best it can with what it was
    given, and saying so is more useful than letting them conclude the
    system cannot write.
    """
    return any((getattr(d, "extracted_text", None) or "").strip() for d in documents)
