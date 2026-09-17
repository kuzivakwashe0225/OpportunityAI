"""The form the call tells you to complete: find it, read it, answer it, fill it.

The owner's words: "if there are forms to fill I fill them effectively
according to my profile, qualifications, achievements... the system should be
able to see the requirements, list them, and be able to tackle each
requirement one by one and answering it fully and able to download and fill in
the opportunity document."

Until now nothing here ever looked at an attachment. A call saying "complete
the attached application form" produced a cover letter that did not mention
it, and the form itself was never downloaded - which for a great many
Zimbabwean tenders and bursaries is the *entire* application.

Three steps, each of which can fail without taking the others down:

**Find.** The connector now keeps links to documents it saw on the page, and
`likely_form` picks the one whose name reads like an application form rather
than guidance notes or a specification.

**Read.** The file is fetched through the same guarded fetcher as any other
public URL - it gets the SSRF checks and the size limits for free - and its
text is extracted by the existing PDF/docx readers.

**Answer, and where possible fill.** Questions are pulled out of the text and
answered one at a time from the applicant's evidence. If the PDF carries real
AcroForm fields, those fields are written directly and the result is a filled
PDF. If it does not - and most scanned government forms do not - the answers
are still produced as a completed-form document the applicant can copy from
or attach. Pretending to fill a flat scan would be worse than saying plainly
that it cannot be typed into.
"""

from __future__ import annotations

import re

# Words in a filename that say "this is the thing you fill in".
_FORM_WORDS = (
    "form", "application", "applicationform", "app-form", "bid", "submission",
    "template", "annex", "appendix", "schedule", "questionnaire", "proposal",
    "entry",
)

# Words that say "this is background reading, not the form".
_NOT_FORM_WORDS = (
    "guidance", "guideline", "notes", "instructions", "faq", "terms",
    "conditions", "policy", "advert", "advertisement", "notice", "report",
    "minutes", "specification", "drawing", "boq", "brochure",
)

# A line ending in a colon, or one padded out with dots or underscores, is a
# form field. So is a numbered question. These are what a person filling the
# form in by hand would write on.
_FIELD_PATTERNS = (
    # "Full name: ______" or "Full name ........"
    re.compile(r"^\s*([A-Z][^:\n]{2,80}?)\s*[:\.]?\s*[_\.]{3,}\s*$"),
    # "Full name:" on its own line
    re.compile(r"^\s*([A-Z][^:\n]{2,80}?)\s*:\s*$"),
    # "3. Describe your relevant experience"
    re.compile(r"^\s*\(?\d{1,2}[\.\)]\s+([A-Z][^\n]{8,160}?)\s*[:\?]?\s*$"),
    # "(a) State your qualifications"
    re.compile(r"^\s*\(?[a-z]\)\s+([A-Z][^\n]{8,160}?)\s*[:\?]?\s*$"),
)

# Below this a "question" is a stray heading or a page number.
_MIN_QUESTION_CHARS = 6
# Above this it is a paragraph of instructions that happened to end in a colon.
_MAX_QUESTION_CHARS = 200

# A form with more than this many detected fields is almost certainly a
# false positive - a price schedule, or a table of contents read as questions.
MAX_QUESTIONS = 40


def likely_form(links) -> str | None:
    """The link most likely to be the form to complete, or None.

    Scored rather than pattern-matched, because a call often attaches four
    documents and only one of them is the form. A tie goes to the earlier
    link: calls list the form before the annexes.
    """
    best: tuple[int, str] | None = None
    for index, link in enumerate(links or ()):
        name = link.rsplit("/", 1)[-1].lower()
        if any(word in name for word in _NOT_FORM_WORDS):
            continue
        score = sum(2 for word in _FORM_WORDS if word in name)
        if not score:
            continue
        # A .docx or .doc is something you type into; a .pdf may be a scan.
        if name.endswith((".doc", ".docx", ".odt", ".rtf")):
            score += 3
        elif name.endswith((".xls", ".xlsx")):
            score += 1
        score -= index  # earlier links win ties
        if best is None or score > best[0]:
            best = (score, link)
    return best[1] if best else None


def questions_in(text: str) -> list[str]:
    """The things this form asks for, in the order it asks them.

    Text-based rather than structural on purpose: a form arrives as a PDF or
    a .docx and both flatten to lines, so the only reliable signal is how the
    line is written. A line ending in a colon, or trailed by a rule of dots or
    underscores, is somewhere a person writes.
    """
    found: list[str] = []
    seen: set[str] = set()
    for raw_line in (text or "").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        for pattern in _FIELD_PATTERNS:
            match = pattern.match(line)
            if not match:
                continue
            question = " ".join(match.group(1).split()).strip(" .:-")
            if not (_MIN_QUESTION_CHARS <= len(question) <= _MAX_QUESTION_CHARS):
                break
            key = question.lower()
            if key in seen:
                break
            seen.add(key)
            found.append(question)
            break
        if len(found) >= MAX_QUESTIONS:
            break
    return found


def pdf_form_fields(content: bytes) -> dict[str, str]:
    """The PDF's own fillable fields, empty dict if it has none.

    A PDF with AcroForm fields can be filled properly - typed into, saved,
    submitted. A flat scan cannot, and this returning {} is how the caller
    knows which of the two it is holding.
    """
    from io import BytesIO

    try:
        from pypdf import PdfReader

        fields = PdfReader(BytesIO(content)).get_fields() or {}
    except Exception:
        return {}
    out: dict[str, str] = {}
    for name, spec in fields.items():
        try:
            label = str(spec.get("/T") or name)
        except Exception:
            label = str(name)
        out[str(name)] = label
    return out


def fill_pdf_form(content: bytes, answers: dict[str, str]) -> bytes | None:
    """The same PDF with its fields filled in, or None if that is not possible.

    Returns None rather than a half-filled file when anything goes wrong: an
    application form that looks complete and is not is worse than one the
    applicant knows they must finish by hand.
    """
    from io import BytesIO

    try:
        from pypdf import PdfReader, PdfWriter

        reader = PdfReader(BytesIO(content))
        if not (reader.get_fields() or {}):
            return None
        writer = PdfWriter(clone_from=reader)
        # Without this, filled values exist in the file but many viewers show
        # the page blank until each field is clicked.
        writer.set_need_appearances_writer(True)
        for page in writer.pages:
            writer.update_page_form_field_values(page, answers)
        buffer = BytesIO()
        writer.write(buffer)
        return buffer.getvalue()
    except Exception:
        return None


def match_answers_to_fields(fields: dict[str, str], answers: dict[str, str]) -> dict[str, str]:
    """Line up answers we wrote against the field names the PDF actually uses.

    Forms name their fields anything from "Full Name" to "txtName1", so this
    matches on the words they share rather than on equality. An unmatched
    field is left empty, which is the honest outcome - a guess written into a
    government form is not a small mistake.
    """
    def words(value: str) -> set[str]:
        return {w for w in re.split(r"[^a-z0-9]+", value.lower()) if len(w) > 2}

    filled: dict[str, str] = {}
    for field_name, label in fields.items():
        target = words(label) | words(field_name)
        if not target:
            continue
        best_key, best_overlap = None, 0
        for question, answer in answers.items():
            overlap = len(target & words(question))
            if overlap > best_overlap:
                best_key, best_overlap = question, overlap
        if best_key and best_overlap >= 1:
            filled[field_name] = answers[best_key]
    return filled
