"""What each opportunity type needs from the owner.

Choosing "tenders" and choosing "scholarships" are not the same product with a
different word on the button. A tender bid is submitted by a *company* and
stands or falls on compliance paperwork - certificate of incorporation, a
current tax clearance, PRAZ supplier registration. A scholarship application is
submitted by a *person* and stands or falls on transcripts and a CV. Asking a
company for its study level, or a student for its VAT certificate, is the
failure this module exists to prevent.

So the profile type is not a label on one universal form. It selects:

  subject      - is the applicant a person or an organisation
  fields       - which questions the setup screen asks
  documents    - which papers the vault expects, and which are non-negotiable
  search_terms - the vocabulary discovery searches with

Everything downstream (onboarding UI, the document checklist, query building,
the "please upload X" notification) reads this one table, so adding a new
opportunity type is a data change here rather than a hunt through five modules.

The Zimbabwe compliance list below is the set commonly demanded by procuring
entities on the PRAZ eGP bulletin board; it is not a legal minimum, and
individual tenders ask for their own extras. `required=True` here means "you
will almost certainly be asked for this", not "the law says so".
"""

from __future__ import annotations

from dataclasses import dataclass

INDIVIDUAL = "individual"
ORGANISATION = "organisation"
EITHER = "either"


@dataclass(frozen=True)
class DocumentKind:
    key: str
    label: str
    required: bool
    hint: str = ""


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    kind: str = "text"  # text | number | textarea | select | list
    hint: str = ""
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProfileTypeSpec:
    key: str
    label: str
    subject: str
    blurb: str
    search_terms: tuple[str, ...]


# --------------------------------------------------------------------------
# Documents
# --------------------------------------------------------------------------

_PERSON_DOCUMENTS: tuple[DocumentKind, ...] = (
    DocumentKind("cv", "CV / résumé", True,
                 "Also the best single upload - work history and certificates get read out of it."),
    DocumentKind("transcript", "Academic transcript", True, "Most recent completed qualification."),
    DocumentKind("id", "National ID or passport", True, ""),
    DocumentKind("certificate", "Degree / diploma certificates", False, "One file per qualification is fine."),
    DocumentKind("reference_letter", "Reference letters", False, "Academic or professional referees."),
    DocumentKind("proof_of_address", "Proof of address", False, ""),
)

# The Zimbabwe tender compliance pack. Named the way procuring entities name
# them on the eGP bulletin board, so the request the owner receives matches the
# words on the document they go looking for.
_COMPANY_DOCUMENTS: tuple[DocumentKind, ...] = (
    DocumentKind("company_profile", "Company profile", True,
                 "Capability statement: what you do, who you've done it for."),
    DocumentKind("certificate_of_incorporation", "Certificate of Incorporation", True, ""),
    DocumentKind("tax_clearance", "Tax Clearance Certificate (ITF263)", True,
                 "Expires - procuring entities reject an out-of-date one outright."),
    DocumentKind("praz_registration", "PRAZ supplier registration certificate", True,
                 "Category-specific: it must cover the category the tender asks for."),
    DocumentKind("cr14", "CR14 / return of directors", True, ""),
    DocumentKind("cr6", "CR6 / notice of registered office", False, ""),
    DocumentKind("nssa_clearance", "NSSA compliance certificate", False, ""),
    DocumentKind("vat_certificate", "VAT registration certificate", False, "If you are VAT registered."),
    DocumentKind("audited_financials", "Audited financial statements", False, "Usually the last two years."),
    DocumentKind("bank_statement", "Bank statement or bank rating letter", False, ""),
    DocumentKind("past_performance", "Reference letters / past performance", False,
                 "Traceable references for comparable work."),
    DocumentKind("key_personnel_cvs", "CVs of key personnel", False, ""),
    DocumentKind("trade_licence", "Trade or industry licence", False,
                 "ZERA, Construction Industry Council, NEC - whatever your sector requires."),
)


# --------------------------------------------------------------------------
# Fields
# --------------------------------------------------------------------------

_COMMON_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("country", "Country", "text"),
    FieldSpec("interests", "Areas of interest", "list", "What should it be looking for?"),
    FieldSpec("goals", "Goals", "list", "What are you trying to achieve?"),
    FieldSpec("preferred_countries", "Preferred countries", "list"),
)

_PERSON_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("name", "Your name", "text"),
    *_COMMON_FIELDS,
    FieldSpec("age", "Age", "number"),
    FieldSpec("study_level", "Study level", "select", options=(
        "", "undergraduate", "bachelors", "masters", "doctoral")),
    FieldSpec("field", "Field of study or work", "text"),
    FieldSpec("history", "About you", "textarea"),
    FieldSpec("certificates", "Certificates", "list", "e.g. BSc Computer Science (2021)"),
    FieldSpec("work_history", "Work history", "list", "e.g. Software Engineer, Acme (2021-2024)"),
    FieldSpec("achievements", "Achievements", "list"),
    FieldSpec("preferred_funding", "Preferred funding", "list"),
    # Free text, deliberately last and deliberately vague: the boxes above
    # cannot anticipate everything, and an owner who has something relevant
    # to say should not have to leave it out because there is no field for
    # it. Read by discovery and drafting like any other stated fact.
    FieldSpec("notes", "Anything else", "textarea",
              "Anything that does not fit the boxes above."),
)

_COMPANY_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("name", "Registered company name", "text"),
    *_COMMON_FIELDS,
    FieldSpec("registration_number", "Company registration number", "text"),
    FieldSpec("tax_number", "BP number / TIN", "text"),
    FieldSpec("vat_number", "VAT number", "text", "Leave blank if not VAT registered."),
    FieldSpec("praz_categories", "PRAZ supplier category codes", "list",
              "e.g. GE001. This is what tenders are matched against - the codes on your "
              "PRAZ certificate."),
    FieldSpec("sectors", "Sectors you supply", "list", "e.g. civil works, ICT hardware, catering"),
    FieldSpec("history", "About the company", "textarea", "Used as the basis of the capability statement."),
    FieldSpec("years_trading", "Years trading", "number"),
    FieldSpec("employee_count", "Number of employees", "number"),
    FieldSpec("past_contracts", "Notable past contracts", "list", "Client, value, year."),
    FieldSpec("certifications", "Certifications and accreditations", "list", "e.g. ISO 9001, CIFOZ grade"),
    # Free text, deliberately last and deliberately vague: the boxes above
    # cannot anticipate everything, and an owner who has something relevant
    # to say should not have to leave it out because there is no field for
    # it. Read by discovery and drafting like any other stated fact.
    FieldSpec("notes", "Anything else", "textarea",
              "Anything that does not fit the boxes above."),
)


# --------------------------------------------------------------------------
# The table
# --------------------------------------------------------------------------

_SPECS: dict[str, ProfileTypeSpec] = {
    "scholarship": ProfileTypeSpec(
        "scholarship", "Scholarships", INDIVIDUAL,
        "Degrees, fellowships, funded study.",
        ("scholarship", "fellowship", "funded masters", "bursary"),
    ),
    "job": ProfileTypeSpec(
        "job", "Jobs", INDIVIDUAL,
        "Employment and career openings.",
        ("job vacancy", "job opening", "hiring", "career opportunity"),
    ),
    "grant": ProfileTypeSpec(
        "grant", "Grants", EITHER,
        "Funding calls - for you or for your organisation.",
        ("grant", "call for proposals", "funding opportunity", "request for applications"),
    ),
    "tender": ProfileTypeSpec(
        "tender", "Tenders", ORGANISATION,
        "Public and private contracts for your company to bid on.",
        ("tender", "request for quotation", "invitation to bid", "request for proposals",
         "procurement notice"),
    ),
}


def spec_for(profile_type: str) -> ProfileTypeSpec:
    """The spec for a type. Raises rather than guessing - an unknown type
    silently falling back to the scholarship shape is how a company ends up
    being asked for its transcript."""
    return _SPECS[profile_type]


def all_specs() -> tuple[ProfileTypeSpec, ...]:
    return tuple(_SPECS.values())


def resolve_subject(profile_type: str, fields: dict | None) -> str:
    """Person or organisation, for this profile as it is actually configured.

    Only "grant" consults the stored value; for the others the type decides,
    so a stale or hand-edited `subject` in the JSON blob can't turn a tender
    profile into a personal one and start demanding transcripts.
    """
    spec = spec_for(profile_type)
    if spec.subject != EITHER:
        return spec.subject
    stored = (fields or {}).get("subject")
    return ORGANISATION if stored == ORGANISATION else INDIVIDUAL


def documents_for(profile_type: str, fields: dict | None) -> tuple[DocumentKind, ...]:
    return (
        _COMPANY_DOCUMENTS
        if resolve_subject(profile_type, fields) == ORGANISATION
        else _PERSON_DOCUMENTS
    )


def fields_for(profile_type: str, fields: dict | None) -> tuple[FieldSpec, ...]:
    return (
        _COMPANY_FIELDS
        if resolve_subject(profile_type, fields) == ORGANISATION
        else _PERSON_FIELDS
    )


def required_document_keys(profile_type: str, fields: dict | None) -> set[str]:
    return {d.key for d in documents_for(profile_type, fields) if d.required}


def missing_document_keys(profile_type: str, fields: dict | None, held: set[str]) -> set[str]:
    """Which required papers the vault does not have yet.

    This is the thing the agent turns into "please may I have these documents"
    - so it deals in keys the UI can render an upload slot for, not prose.
    """
    return required_document_keys(profile_type, fields) - set(held)


def document_label(profile_type: str, fields: dict | None, key: str) -> str:
    for kind in documents_for(profile_type, fields):
        if kind.key == key:
            return kind.label
    return key
