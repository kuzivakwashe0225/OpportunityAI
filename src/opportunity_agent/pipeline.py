"""The autonomous per-profile agent cycle.

This is the loop that runs without the owner present (SOLUTION_DEFINITION.md
§16): find opportunities from the profile's own intent, verify them, decide
eligibility, and - for the ones that pass - draft a complete application and
notify the owner that something is waiting for them.

Gate model (from the agentic-systems research in §16):

  discover / fetch / verify / match   auto     no gate, fully unattended
  shortlist + draft an application    notify   agent proceeds, owner is told
  ask for a missing document          notify   agent proceeds, owner is told
  submit                              HARD     never here - a human approves

Nothing in this module submits anything anywhere. Drafting stops at a package
sitting in the review queue; `stage` never advances past "drafted" without a
human acting.

Two sources, chosen by profile type. Scholarships, jobs and grants are found by
web search, because there is no register of them. Tenders are read straight off
the PRAZ eGP bulletin board (egp.py) - structured records from the procurement
regulator, which beats searching the open web for them by a wide margin.

For tenders specifically, every cycle also polls the award notices
(egp_awards.py) and does two things with them before drafting anything: newly
found tenders already awarded to someone else are dropped rather than stored,
and previously stored ones the owner has not submitted or dismissed are
flagged (StoredOpportunity.awarded_to/awarded_at) rather than left to sit in
the review queue looking actionable. Both are best-effort - a failed poll
degrades to "nothing filtered or flagged this cycle", not a failed cycle.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from . import compliance, egp, egp_awards, models_db, profile_schema
from .connector import fetch_public_page
from .discovery import build_search_queries, canonicalize_url, discover
from .drafting import build_application_package
from .extraction import page_to_opportunity
from .matching import match_opportunity
from .models import Opportunity, OrganisationProfile, PersonalProfile
from .search import SearchResult

DRAFTABLE_STATUSES = ("eligible",)

# How many board pages to read per tender cycle. 20 tenders a page; the whole
# board is ~45 pages. Deliberately modest per run - the board is a government
# server, the same tenders stay live for weeks, and dedup means a later run
# picks up what this one didn't reach.
TENDER_PAGES_PER_CYCLE = 3


def profile_to_personal_profile(profile: models_db.Profile):
    """The stored JSON blob back into the model matching/drafting expect.

    Returns a PersonalProfile or an OrganisationProfile depending on what the
    profile type says the applicant *is* - a company being validated against
    the personal model would silently drop its supplier categories, which are
    the whole basis of tender eligibility.

    Returns None when there's nothing to work from: a profile with no name has
    not really been set up, and running discovery on it would just burn search
    quota on generic queries.
    """
    fields = profile.fields or {}
    if not fields.get("name"):
        return None
    subject = profile_schema.resolve_subject(profile.profile_type, fields)
    model = OrganisationProfile if subject == profile_schema.ORGANISATION else PersonalProfile
    try:
        return model.model_validate(fields)
    except Exception:
        return None


def held_document_keys(profile: models_db.Profile) -> set[str]:
    """Which kinds of paper this profile actually has in the vault.

    Two sources, because the owner shouldn't have to enter the same fact
    twice: files genuinely uploaded (Document.doc_type) and anything they
    typed into the profile's own "documents on hand" list.
    """
    uploaded = {
        (document.doc_type or "other")
        for document in profile.documents
        if (document.doc_type or "other") != "other"
    }
    declared = {str(item).strip() for item in (profile.fields or {}).get("documents", []) if item}
    return uploaded | declared


def _subject_profile_with_documents(profile: models_db.Profile):
    """The matching-ready profile, with the vault's contents folded in.

    Matching decides missing documents from `profile.documents`, so an
    uploaded tax clearance has to reach it - otherwise the agent keeps asking
    for a file the owner already sent.
    """
    subject = profile_to_personal_profile(profile)
    if subject is None:
        return None
    combined = sorted(set(subject.documents) | held_document_keys(profile))
    return subject.model_copy(update={"documents": combined})


def _draft_package(subject, stored: models_db.StoredOpportunity) -> dict:
    opportunity = Opportunity.model_validate(stored.payload)
    match = match_opportunity(opportunity, subject)
    package = build_application_package(subject, opportunity, match)
    return package.model_dump(mode="json")


def _compliance_for(
    profile: models_db.Profile,
    opportunity: Opportunity,
    details: egp.TenderDetails | None = None,
) -> dict:
    return compliance.build_report(
        profile_type=profile.profile_type,
        fields=profile.fields or {},
        held_documents=held_document_keys(profile),
        opportunity=opportunity,
        details=details,
    ).to_dict()


def _tender_opportunities(fetch_details: bool = True) -> list[tuple[Opportunity, egp.TenderDetails | None]]:
    """Live tenders off the PRAZ board, each with its detail page read.

    The detail page is where the bid security, the fees and the addendum count
    live, and those are the things that decide whether a bid is even worth
    starting - so they are fetched here rather than left for the owner.
    """
    found: list[tuple[Opportunity, egp.TenderDetails | None]] = []
    for tender in egp.fetch_live_tenders(max_pages=TENDER_PAGES_PER_CYCLE, pause_seconds=1.0):
        opportunity = egp.tender_to_opportunity(tender)
        details = None
        if fetch_details:
            try:
                page = fetch_public_page(tender.url)
                details = egp.parse_tender_details(page.content)
            except Exception:
                # A detail page that won't load costs us the fees and addenda
                # for that one tender, not the tender itself.
                details = None
        found.append((opportunity, details))
    return found


def _persist_new_award_notices(session: Session, notices: list[egp_awards.AwardNotice]) -> None:
    """Accumulate award history past the live page's own rolling window.

    Insert-only, keyed on `award_number`: a later cycle re-polling the same
    ~100-row window must not duplicate rows it already recorded. This is
    global reference data, not scoped to the profile doing the polling - see
    StoredAwardNotice's docstring.
    """
    if not notices:
        return
    existing = {
        row.award_number
        for row in session.query(models_db.StoredAwardNotice)
        .filter(models_db.StoredAwardNotice.award_number.in_([n.award_number for n in notices]))
        .all()
    }
    for notice in notices:
        if notice.award_number in existing:
            continue
        session.add(models_db.StoredAwardNotice(
            award_number=notice.award_number,
            tender_id=notice.tender_id,
            title=notice.title,
            awardee=notice.awardee,
            award_date=notice.award_date,
        ))


def _mark_awarded_elsewhere(
    session: Session, profile: models_db.Profile, notices: list[egp_awards.AwardNotice]
) -> int:
    """Stored tender opportunities the owner has not acted on, but which have
    since been awarded to someone else, stop being actionable.

    Submitted and dismissed rows are left alone - the owner already knows the
    outcome of one and chose to ignore the other. Everything else (discovered,
    needs_documents, drafted, even approved) can still be sitting in the
    review queue asking the owner to act on a bid that is already decided,
    which is exactly what this is for.
    """
    awarded = egp_awards.awarded_tender_ids(notices)
    if not awarded:
        return 0
    by_tender_id = {n.tender_id: n for n in notices}
    rows = (
        session.query(models_db.StoredOpportunity)
        .filter_by(profile_id=profile.id)
        .filter(models_db.StoredOpportunity.awarded_to.is_(None))
        .filter(~models_db.StoredOpportunity.stage.in_(("submitted", "dismissed")))
        .all()
    )
    marked = 0
    for row in rows:
        tender_id = egp.tender_id_from_url(row.canonical_url)
        notice = by_tender_id.get(tender_id) if tender_id else None
        if notice is None:
            continue
        row.awarded_to = notice.awardee
        row.awarded_at = notice.award_date
        marked += 1
    return marked


def run_profile_cycle(
    session: Session,
    profile: models_db.Profile,
    *,
    api_key: str,
    search_fn: Callable[..., list[SearchResult]] = discover,
    fetch_fn: Callable[[str], object] = fetch_public_page,
    tender_fn: Callable[..., list] = _tender_opportunities,
    award_fn: Callable[[], list[egp_awards.AwardNotice]] = egp_awards.fetch_award_notices,
) -> models_db.ProfileDiscoveryRun | None:
    """One unattended pass for one profile. Returns the run record, or None if
    the profile isn't set up enough to act on yet."""
    subject = _subject_profile_with_documents(profile)
    if subject is None:
        return None

    started_at = datetime.now(timezone.utc)
    queries = build_search_queries(subject, profile.profile_type)
    failures: list[str] = []
    is_tender = profile.profile_type == "tender"

    # ---- award notices ------------------------------------------------------
    # Best-effort and deliberately not fatal to the cycle: award filtering is
    # an enhancement on top of tender discovery, not a dependency of it. A
    # failed fetch here means "nothing gets filtered or flagged this cycle",
    # which is the same safe default egp_awards.still_open() already commits
    # to when it has no notices to work with.
    notices: list[egp_awards.AwardNotice] = []
    if is_tender:
        try:
            notices = award_fn()
        except Exception:
            notices = []
        _persist_new_award_notices(session, notices)
        _mark_awarded_elsewhere(session, profile, notices)

    # ---- gather -----------------------------------------------------------
    # `found` counts what the source turned up, not what we managed to fetch -
    # the gap between the two is exactly the signal the failures list exists
    # to explain, and collapsing them hides a source going bad.
    candidates: list[tuple[Opportunity, egp.TenderDetails | None]] = []
    if is_tender:
        try:
            candidates = list(tender_fn())
            queries = [f"PRAZ eGP bulletin board (top {TENDER_PAGES_PER_CYCLE} pages)"]
        except Exception as error:
            return _record_run(session, profile, started_at, queries,
                               found=0, added=0, drafted=0, failures=[f"eGP board: {error}"])
        found = len(candidates)
        if notices:
            # A tender already awarded is not a new opportunity to store or
            # draft against, however it still reads on the live board - only
            # `found` reflects the board's own count; what goes on to be
            # stored is the filtered list.
            awarded = egp_awards.awarded_tender_ids(notices)
            candidates = [
                (opportunity, details) for opportunity, details in candidates
                if egp.tender_id_from_url(str(opportunity.url)) not in awarded
            ]
    else:
        try:
            results = search_fn(subject, api_key=api_key, profile_type=profile.profile_type)
        except Exception as error:
            return _record_run(session, profile, started_at, queries,
                               found=0, added=0, drafted=0, failures=[f"search: {error}"])
        found = len(results)
        for result in results:
            try:
                page = fetch_fn(result.url)
                candidates.append(
                    (page_to_opportunity(page, title=result.title or "Untitled opportunity"), None)
                )
            except Exception as error:
                failures.append(f"{result.url}: {error}")

    # ---- store, match, draft ---------------------------------------------
    known_urls = {
        row.canonical_url
        for row in session.query(models_db.StoredOpportunity).filter_by(profile_id=profile.id).all()
    }

    added = 0
    drafted = 0
    awaiting_documents: list[str] = []

    for opportunity, details in candidates:
        try:
            canonical = canonicalize_url(str(opportunity.url))
        except Exception as error:
            failures.append(f"{opportunity.url}: {error}")
            continue
        if canonical in known_urls:
            continue
        known_urls.add(canonical)

        match = match_opportunity(opportunity, subject)
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
                "missing_documents": match.missing_documents,
            },
            compliance=_compliance_for(profile, opportunity, details),
        )
        session.add(stored)
        session.flush()
        added += 1

        if match.status not in DRAFTABLE_STATUSES:
            continue

        # notify-gate: eligible work gets drafted unattended, the owner is told
        stored.package = _draft_package(subject, stored)
        if match.missing_documents:
            # Qualified, drafted, and not submittable - because a file is
            # missing, which is the one kind of blocker the owner can clear in
            # two minutes. Say so instead of burying it in the review queue.
            stored.stage = "needs_documents"
            awaiting_documents.extend(match.missing_documents)
        else:
            stored.stage = "drafted"
            drafted += 1

    _notify(session, profile, drafted, awaiting_documents)

    return _record_run(session, profile, started_at, queries, found=found,
                       added=added, drafted=drafted, failures=failures)


def _notify(
    session: Session,
    profile: models_db.Profile,
    drafted: int,
    awaiting_documents: list[str],
) -> None:
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

    if awaiting_documents:
        # Named the way the owner's filing cabinet names them, not by database
        # key - they are about to go and look for these.
        labels = sorted({
            profile_schema.document_label(profile.profile_type, profile.fields, key)
            for key in awaiting_documents
        })
        session.add(models_db.Notification(
            account_id=profile.account_id,
            profile_id=profile.id,
            kind="documents_requested",
            message=(
                "I found work you qualify for but can't finish it without "
                + ("this document" if len(labels) == 1 else "these documents")
                + ": " + ", ".join(labels)[:800]
                + ". Upload them and I'll carry on."
            )[:1000],
        ))


def resume_after_documents(session: Session, profile: models_db.Profile) -> int:
    """The owner uploaded something. Re-check everything that was waiting.

    This is the other half of "please may I have these documents": having
    asked, the agent has to notice the answer without being told, or the
    opportunity sits blocked until somebody happens to look at it.

    Returns how many drafts became submittable.
    """
    subject = _subject_profile_with_documents(profile)
    if subject is None:
        return 0

    waiting = session.query(models_db.StoredOpportunity).filter_by(
        profile_id=profile.id, stage="needs_documents"
    ).all()

    unblocked = 0
    for stored in waiting:
        opportunity = Opportunity.model_validate(stored.payload)
        match = match_opportunity(opportunity, subject)
        stored.compliance = _compliance_for(profile, opportunity)
        reasons = dict(stored.match_reasons or {})
        reasons["missing_documents"] = match.missing_documents
        stored.match_reasons = reasons
        if not match.missing_documents:
            stored.package = _draft_package(subject, stored)
            stored.stage = "drafted"
            unblocked += 1

    if unblocked:
        session.add(models_db.Notification(
            account_id=profile.account_id,
            profile_id=profile.id,
            kind="applications_drafted",
            message=(
                f"Thanks - that unblocked {unblocked} application"
                f"{'s' if unblocked != 1 else ''} on {profile.display_name}. "
                "They're ready for your review."
            ),
        ))
    session.commit()
    return unblocked


def escalate_opportunity(session: Session, stored: models_db.StoredOpportunity) -> models_db.StoredOpportunity:
    """The owner overrode the eligibility verdict - draft it anyway.

    Deliberately does not rewrite `match_status`: the engine's verdict and the
    warnings that come with it stay visible on the draft, so approving it later
    is an informed decision rather than one where the disagreement was erased.
    """
    subject = _subject_profile_with_documents(stored.profile)
    if subject is None:
        raise ValueError("profile is not set up")

    stored.escalated = True
    stored.package = _draft_package(subject, stored)
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
