from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl


class PersonalProfile(BaseModel):
    name: str = Field(min_length=1)
    country: str | None = None
    age: int | None = Field(default=None, ge=0, le=120)
    study_level: str | None = None
    field: str | None = None
    documents: list[str] = Field(default_factory=list)
    interests: list[str] = Field(default_factory=list)
    goals: list[str] = Field(default_factory=list)
    history: str | None = None
    achievements: list[str] = Field(default_factory=list)
    preferred_countries: list[str] = Field(default_factory=list)
    preferred_funding: list[str] = Field(default_factory=list)
    certificates: list[str] = Field(default_factory=list)
    work_history: list[str] = Field(default_factory=list)
    social_links: dict[str, str] = Field(default_factory=dict)


class OrganisationProfile(BaseModel):
    """A company bidding for tenders or applying for organisational grants.

    Deliberately a separate model rather than extra optional fields on
    PersonalProfile: almost nothing overlaps. A company has no study level and
    a person has no VAT number, and a single model carrying both would let the
    UI, the query builder and the drafting templates all quietly ask the wrong
    entity for the wrong thing. What the two share - a name, a country, things
    it is interested in, documents on hand - is the small set the matching
    engine reads, so both satisfy it structurally.

    `categories` is the important one: PRAZ supplier registration codes. It is
    what turns tender matching from text-similarity guesswork into a rule.
    """

    name: str = Field(min_length=1)
    country: str | None = None
    registration_number: str | None = None
    tax_number: str | None = None
    vat_number: str | None = None
    categories: list[str] = Field(default_factory=list)
    sectors: list[str] = Field(default_factory=list)
    documents: list[str] = Field(default_factory=list)
    interests: list[str] = Field(default_factory=list)
    goals: list[str] = Field(default_factory=list)
    history: str | None = None
    years_trading: int | None = Field(default=None, ge=0, le=200)
    employee_count: int | None = Field(default=None, ge=0)
    past_contracts: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    preferred_countries: list[str] = Field(default_factory=list)
    social_links: dict[str, str] = Field(default_factory=dict)


class Opportunity(BaseModel):
    source: str = Field(min_length=1)
    title: str = Field(min_length=1)
    url: HttpUrl
    deadline: date | None = None
    eligible_countries: list[str] = Field(default_factory=list)
    required_levels: list[str] = Field(default_factory=list)
    required_fields: list[str] = Field(default_factory=list)
    required_age_max: int | None = Field(default=None, ge=0, le=120)
    # Supplier category codes (e.g. PRAZ "GE001"). Tenders are the one place
    # this system has a hard, checkable eligibility rule rather than an
    # inference from prose: you either hold the registration category or you
    # do not. Empty for scholarships/jobs, which have no equivalent.
    required_categories: list[str] = Field(default_factory=list)
    required_documents: list[str] = Field(default_factory=list)
    interests: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    retrieved_at: str | None = None
    content_sha256: str | None = None
    requirements_verified: bool = False
    content_type: str | None = None
    parser_version: str | None = None


class MatchResult(BaseModel):
    opportunity_url: HttpUrl
    status: Literal["eligible", "ineligible", "needs_review"]
    score: int = Field(ge=0, le=100)
    matched_requirements: list[str] = Field(default_factory=list)
    failed_requirements: list[str] = Field(default_factory=list)
    unknown_requirements: list[str] = Field(default_factory=list)
    # Documents the applicant qualifies without but cannot *submit* without.
    # Deliberately its own dimension rather than a failed requirement: "you
    # don't qualify" and "you qualify but I need your tax clearance" are
    # different answers, and only one of them is fixable by the owner in two
    # minutes. Keeping them apart is what lets the agent ask for the file
    # instead of silently discarding the opportunity.
    missing_documents: list[str] = Field(default_factory=list)
