"""The autonomous per-profile agent cycle.

This is the loop that runs without the owner present (SOLUTION_DEFINITION.md
§16): find opportunities from the profile's own intent, verify them, decide
eligibility, and - for the ones that pass - draft a complete application and
notify the owner that something is waiting for them.

Gate model (from the agentic-systems research in §16):

  discover / fetch / verify / match   auto     no gate, fully unattended
  shortlist + draft an application    notify   agent proceeds, owner is told
  submit                              HARD     never here - a human approves

Nothing in this module submits anything anywhere. Drafting stops at a package
sitting in the review queue; `stage` never advances past "drafted" without a
human acting.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from . import models_db
from .connector import fetch_public_page
from .discovery import build_search_queries, canonicalize_url, discover
from .drafting import build_application_package
from .extraction import page_to_opportunity
from .matching import match_opportunity
from .models import PersonalProfile
from .search import SearchResult

DRAFTABLE_STATUSES = ("eligible",)


def profile_to_personal_profile(profile: models_db.Profile) -> PersonalProfile | None:
    """The stored JSON blob back into the model matching/drafting expect.

    Returns None when there's nothing to work from - a profile with no name
    has not really been set up, and running discovery on it would just burn
    search quota on generic queries.
    """
    fields = profile.fields or {}
    if not fields.get("name"):
        return None
    try:
        return PersonalProfile.model_validate(fields)
    except Exception:
        return None


def _draft_package(personal: PersonalProfile, stored: models_db.StoredOpportunity) -> dict:
    from .models import Opportunity

    opportunity = Opportunity.model_validate(stored.payload)
    match = match_opportunity(opportunity, personal)
    package = build_application_package(personal, opportunity, match)
    return package.model_dump(mode="json")


def run_profile_cycle(
    session: Session,
    profile: models_db.Profile,
    *,
    api_key: str,
    search_fn: Callable[..., list[SearchResult]] = discover,
    fetch_fn: Callable[[str], object] = fetch_public_page,
) -> models_db.ProfileDiscoveryRun | None:
    """One unattended pass for one profile. Returns the run record, or None if
    the profile isn't set up enough to act on yet."""
    personal = profile_to_personal_profile(profile)
    if personal is None:
        return None

    started_at = datetime.now(timezone.utc)
    queries = build_search_queries(personal)
    failures: list[str] = []

    try:
        results = search_fn(personal, api_key=api_key)
    except Exception as error:
        return _record_run(
            session, profile, started_at, queries, found=0, added=0, drafted=0,
            failures=[f"search: {error}"],
        )

    known_urls = {
        row.canonical_url
        for row in session.query(models_db.StoredOpportunity).filter_by(profile_id=profile.id).all()
    }

    added = 0
    drafted = 0
    for result in results:
        try:
            page = fetch_fn(result.url)
            opportunity = page_to_opportunity(page, title=result.title or "Untitled opportunity")
            canonical = canonicalize_url(str(opportunity.url))
        except Exception as error:
            failures.append(f"{result.url}: {error}")
            continue

        if canonical in known_urls:
            continue
        known_urls.add(canonical)

        match = match_opportunity(opportunity, personal)
        stored = models_db.StoredOpportunity(
            profile_id=profile.id,
            canonical_url=canonical,
            payload=opportunity.model_dump(mode="json"),
            match_status=match.status,
            match_score=match.score,
            match_reasons={
                "matched": match.matched_requirements,
                "failed": match.failed_requirements,
                "unknown": match.unknown_requirements,
            },
        )
        session.add(stored)
        session.flush()
        added += 1

        # notify-gate: eligible work gets drafted unattended, the owner is told
        if match.status in DRAFTABLE_STATUSES:
            stored.package = _draft_package(personal, stored)
            stored.stage = "drafted"
            drafted += 1

    if drafted:
        session.add(models_db.Notification(
            account_id=profile.account_id,
            profile_id=profile.id,
            kind="applications_drafted",
            message=(
                f"{drafted} application{'s are' if drafted != 1 else ' is'} drafted and "
                f"waiting for your review on {profile.display_name}."
            ),
        ))

    return _record_run(
        session, profile, started_at, queries, found=len(results), added=added,
        drafted=drafted, failures=failures,
    )


def escalate_opportunity(session: Session, stored: models_db.StoredOpportunity) -> models_db.StoredOpportunity:
    """The owner overrode the eligibility verdict - draft it anyway.

    Deliberately does not rewrite `match_status`: the engine's verdict and the
    warnings that come with it stay visible on the draft, so approving it later
    is an informed decision rather than one where the disagreement was erased.
    """
    personal = profile_to_personal_profile(stored.profile)
    if personal is None:
        raise ValueError("profile is not set up")

    stored.escalated = True
    stored.package = _draft_package(personal, stored)
    stored.stage = "drafted"
    session.commit()
    return stored


def _record_run(
    session: Session,
    profile: models_db.Profile,
    started_at: datetime,
    queries: list[str],
    *,
    found: int,
    added: int,
    drafted: int,
    failures: list[str],
) -> models_db.ProfileDiscoveryRun:
    run = models_db.ProfileDiscoveryRun(
        profile_id=profile.id,
        started_at=started_at,
        completed_at=datetime.now(timezone.utc),
        queries=queries,
        found=found,
        added=added,
        drafted=drafted,
        failures=failures,
    )
    session.add(run)
    session.commit()
    return run
