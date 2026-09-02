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


class Opportunity(BaseModel):
    source: str = Field(min_length=1)
    title: str = Field(min_length=1)
    url: HttpUrl
    deadline: date | None = None
    eligible_countries: list[str] = Field(default_factory=list)
    required_levels: list[str] = Field(default_factory=list)
    required_fields: list[str] = Field(default_factory=list)
    required_age_max: int | None = Field(default=None, ge=0, le=120)
    required_documents: list[str] = Field(default_factory=list)
    interests: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)


class MatchResult(BaseModel):
    opportunity_url: HttpUrl
    status: Literal["eligible", "ineligible", "needs_review"]
    score: int = Field(ge=0, le=100)
    matched_requirements: list[str] = Field(default_factory=list)
    failed_requirements: list[str] = Field(default_factory=list)
    unknown_requirements: list[str] = Field(default_factory=list)
