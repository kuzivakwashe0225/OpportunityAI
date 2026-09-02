"""Build a submission-ready draft package for one shortlisted opportunity.

SOLUTION_DEFINITION.md §10 (MVP scope): "a drafted, submission-ready package —
tailored CV/cover-letter or essay draft, a requirement checklist, and every
generated claim linked back to a profile fact or source document." No LLM call
here on purpose — the cover note is assembled only from fields the profile
actually has, so nothing in it can be a fabricated claim (§3 non-goals). It is
a draft the owner edits and finishes by hand, not a final document.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, HttpUrl

from .models import MatchResult, Opportunity, PersonalProfile


class ChecklistItem(BaseModel):
    requirement: str
    status: Literal["met", "missing", "unknown"]


class ApplicationPackage(BaseModel):
    opportunity_url: HttpUrl
    cover_note: str
    checklist: list[ChecklistItem]
    evidence: list[str]
    warnings: list[str] = []


def build_application_package(
    profile: PersonalProfile,
    opportunity: Opportunity,
    match: MatchResult,
) -> ApplicationPackage:
    checklist = (
        [ChecklistItem(requirement=r, status="met") for r in match.matched_requirements]
        + [ChecklistItem(requirement=r, status="missing") for r in match.failed_requirements]
        + [ChecklistItem(requirement=r, status="unknown") for r in match.unknown_requirements]
    )

    return ApplicationPackage(
        opportunity_url=match.opportunity_url,
        cover_note=_build_cover_note(profile, opportunity, match),
        checklist=checklist,
        evidence=list(opportunity.evidence),
        warnings=_build_warnings(match),
    )


def _build_cover_note(profile: PersonalProfile, opportunity: Opportunity, match: MatchResult) -> str:
    level_and_field = " ".join(part for part in [profile.study_level, profile.field] if part)
    candidacy = f", a {level_and_field} candidate" if level_and_field else ""

    lines = [
        f"Draft application — {opportunity.title} ({opportunity.source})",
        "",
        "Dear Selection Committee,",
        "",
        f"My name is {profile.name}{candidacy}. I am writing to apply for {opportunity.title}.",
    ]

    if profile.goals:
        lines.append(f"My goal is to {'; '.join(profile.goals)}.")
    if profile.certificates:
        lines.append(f"Relevant qualifications: {'; '.join(profile.certificates)}.")
    if profile.work_history:
        lines.append(f"Relevant experience: {'; '.join(profile.work_history)}.")
    if match.matched_requirements:
        lines.append(
            "Based on the published requirements, this profile meets: "
            f"{', '.join(match.matched_requirements)}."
        )

    lines += ["", "[Draft — review, personalize, and verify before submitting.]"]
    return "\n".join(lines)


def _build_warnings(match: MatchResult) -> list[str]:
    if match.status == "ineligible":
        return ["This opportunity did not pass eligibility checks — review the failed requirements before applying."]
    if match.status == "needs_review":
        return ["Some eligibility information is missing or unverified — confirm it before applying."]
    return []
