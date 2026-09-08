from datetime import date

import pytest
from sqlalchemy.orm import Session

from opportunity_agent import egp_awards, models_db, pipeline
from opportunity_agent.connector import PublicPage
from opportunity_agent.db import init_db, make_engine
from opportunity_agent.search import SearchResult


@pytest.fixture
def session():
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(engine) as db_session:
        yield db_session


@pytest.fixture
def profile(session):
    account = models_db.Account(email="owner@example.com", password_hash="x")
    session.add(account)
    session.commit()
    profile = models_db.Profile(
        account_id=account.id,
        profile_type="scholarship",
        display_name="Scholarships",
        fields={
            "name": "Tendai Moyo",
            "country": "Zimbabwe",
            "study_level": "masters",
            "field": "Computer Science",
            "documents": ["transcript", "cv"],
        },
    )
    session.add(profile)
    session.commit()
    return profile


def _eligible_page(url="https://example.org/award"):
    return PublicPage(
        url=url,
        content=(
            "Open to citizens of Zimbabwe. Applicants must be enrolled in a master's "
            "programme in computer science. Required documents: CV, transcript."
        ),
        retrieved_at="2026-01-01T00:00:00Z",
        sha256="abc",
        content_type="text/html",
    )


def _ineligible_page(url="https://example.org/kenya"):
    return PublicPage(
        url=url,
        content="Open to citizens of Kenya only. Bachelor's degree required in agriculture.",
        retrieved_at="2026-01-01T00:00:00Z",
        sha256="def",
        content_type="text/html",
    )


def test_cycle_with_no_profile_fields_records_nothing(session):
    account = models_db.Account(email="empty@example.com", password_hash="x")
    session.add(account)
    session.commit()
    empty = models_db.Profile(
        account_id=account.id, profile_type="job", display_name="Empty", fields={}
    )
    session.add(empty)
    session.commit()

    run = pipeline.run_profile_cycle(session, empty, api_key="k")

    assert run is None


def test_cycle_stores_discovered_opportunities_scoped_to_the_profile(session, profile):
    run = pipeline.run_profile_cycle(
        session, profile, api_key="k",
        search_fn=lambda p, api_key, **kw: [SearchResult(title="Award", url="https://example.org/award", content="x")],
        fetch_fn=lambda url: _eligible_page(url),
    )

    stored = session.query(models_db.StoredOpportunity).all()
    assert len(stored) == 1
    assert stored[0].profile_id == profile.id
    assert run.found == 1
    assert run.added == 1


def test_eligible_opportunities_are_auto_drafted_without_human_input(session, profile):
    pipeline.run_profile_cycle(
        session, profile, api_key="k",
        search_fn=lambda p, api_key, **kw: [SearchResult(title="Award", url="https://example.org/award", content="x")],
        fetch_fn=lambda url: _eligible_page(url),
    )

    stored = session.query(models_db.StoredOpportunity).one()
    assert stored.match_status == "eligible"
    assert stored.stage == "drafted"
    assert stored.package is not None
    assert "cover_note" in stored.package


def test_ineligible_opportunities_are_kept_but_not_drafted(session, profile):
    pipeline.run_profile_cycle(
        session, profile, api_key="k",
        search_fn=lambda p, api_key, **kw: [SearchResult(title="Kenya Award", url="https://example.org/kenya", content="x")],
        fetch_fn=lambda url: _ineligible_page(url),
    )

    stored = session.query(models_db.StoredOpportunity).one()
    assert stored.match_status == "ineligible"
    assert stored.stage == "discovered"
    assert stored.package is None


def test_a_notification_is_created_when_applications_are_drafted(session, profile):
    pipeline.run_profile_cycle(
        session, profile, api_key="k",
        search_fn=lambda p, api_key, **kw: [SearchResult(title="Award", url="https://example.org/award", content="x")],
        fetch_fn=lambda url: _eligible_page(url),
    )

    notifications = session.query(models_db.Notification).all()
    assert len(notifications) == 1
    assert notifications[0].kind == "applications_drafted"
    assert notifications[0].account_id == profile.account_id
    assert notifications[0].profile_id == profile.id


def test_no_notification_when_nothing_was_drafted(session, profile):
    pipeline.run_profile_cycle(
        session, profile, api_key="k",
        search_fn=lambda p, api_key, **kw: [SearchResult(title="Kenya", url="https://example.org/kenya", content="x")],
        fetch_fn=lambda url: _ineligible_page(url),
    )

    assert session.query(models_db.Notification).count() == 0


def test_rerunning_does_not_duplicate_the_same_opportunity(session, profile):
    for _ in range(2):
        pipeline.run_profile_cycle(
            session, profile, api_key="k",
            search_fn=lambda p, api_key, **kw: [SearchResult(title="Award", url="https://example.org/award", content="x")],
            fetch_fn=lambda url: _eligible_page(url),
        )

    assert session.query(models_db.StoredOpportunity).count() == 1


def test_a_fetch_failure_is_recorded_without_killing_the_cycle(session, profile):
    def failing_fetch(url):
        if "bad" in url:
            raise ValueError("blocked by robots.txt")
        return _eligible_page(url)

    run = pipeline.run_profile_cycle(
        session, profile, api_key="k",
        search_fn=lambda p, api_key, **kw: [
            SearchResult(title="Bad", url="https://example.org/bad", content="x"),
            SearchResult(title="Good", url="https://example.org/award", content="x"),
        ],
        fetch_fn=failing_fetch,
    )

    assert run.found == 2
    assert run.added == 1
    assert len(run.failures) == 1
    assert "robots.txt" in run.failures[0]


def test_escalating_an_ineligible_opportunity_drafts_it_anyway(session, profile):
    pipeline.run_profile_cycle(
        session, profile, api_key="k",
        search_fn=lambda p, api_key, **kw: [SearchResult(title="Kenya", url="https://example.org/kenya", content="x")],
        fetch_fn=lambda url: _ineligible_page(url),
    )
    stored = session.query(models_db.StoredOpportunity).one()

    pipeline.escalate_opportunity(session, stored)

    assert stored.escalated is True
    assert stored.stage == "drafted"
    assert stored.package is not None
    # the eligibility verdict itself is untouched - the human overrode acting on
    # it, they didn't change what the engine found
    assert stored.match_status == "ineligible"
    assert stored.package["warnings"]


def test_search_failure_records_a_run_rather_than_raising(session, profile):
    def failing_search(p, api_key, **kw):
        raise RuntimeError("search API unreachable")

    run = pipeline.run_profile_cycle(session, profile, api_key="k", search_fn=failing_search)

    assert run is not None
    assert run.found == 0
    assert "search API unreachable" in run.failures[0]


# ---------------------------------------------------------------------------
# "Please may I have these documents" - and noticing when they arrive
# ---------------------------------------------------------------------------

def _page_needing_a_reference(url="https://example.org/award"):
    return PublicPage(
        url=url,
        content=(
            "Open to citizens of Zimbabwe. Applicants must be enrolled in a master's "
            "programme in computer science. Required documents: CV, transcript, "
            "reference letter."
        ),
        retrieved_at="2026-01-01T00:00:00Z",
        sha256="ghi",
        content_type="text/html",
    )


def test_a_qualifying_opportunity_missing_a_file_waits_instead_of_being_dropped(session, profile):
    """The owner qualifies. One file is missing. That is a request, not a
    rejection, and the draft still gets written so it's ready the moment the
    file lands."""
    pipeline.run_profile_cycle(
        session, profile, api_key="k",
        search_fn=lambda p, api_key, **kw: [
            SearchResult(title="Award", url="https://example.org/award", content="x")
        ],
        fetch_fn=_page_needing_a_reference,
    )

    stored = session.query(models_db.StoredOpportunity).one()
    assert stored.stage == "needs_documents"
    assert stored.match_status == "eligible"
    assert stored.package, "the draft is written up front, not after the upload"


def test_the_owner_is_told_exactly_which_document_is_wanted(session, profile):
    pipeline.run_profile_cycle(
        session, profile, api_key="k",
        search_fn=lambda p, api_key, **kw: [
            SearchResult(title="Award", url="https://example.org/award", content="x")
        ],
        fetch_fn=_page_needing_a_reference,
    )

    notification = session.query(models_db.Notification).filter_by(
        kind="documents_requested"
    ).one()
    assert "reference" in notification.message.lower()
    assert "upload" in notification.message.lower()


def test_uploading_the_document_unblocks_the_draft_without_being_asked(session, profile):
    """The other half of asking: having asked, the agent has to notice the
    answer on its own, or the opportunity sits blocked until somebody looks."""
    pipeline.run_profile_cycle(
        session, profile, api_key="k",
        search_fn=lambda p, api_key, **kw: [
            SearchResult(title="Award", url="https://example.org/award", content="x")
        ],
        fetch_fn=_page_needing_a_reference,
    )
    assert session.query(models_db.StoredOpportunity).one().stage == "needs_documents"

    session.add(models_db.Document(
        profile_id=profile.id, object_key="k", doc_type="reference letter",
        original_filename="ref.pdf", content_type="application/pdf", size_bytes=10,
    ))
    session.commit()
    session.refresh(profile)

    unblocked = pipeline.resume_after_documents(session, profile)

    assert unblocked == 1
    assert session.query(models_db.StoredOpportunity).one().stage == "drafted"


def test_resuming_with_the_document_still_absent_changes_nothing(session, profile):
    pipeline.run_profile_cycle(
        session, profile, api_key="k",
        search_fn=lambda p, api_key, **kw: [
            SearchResult(title="Award", url="https://example.org/award", content="x")
        ],
        fetch_fn=_page_needing_a_reference,
    )

    assert pipeline.resume_after_documents(session, profile) == 0
    assert session.query(models_db.StoredOpportunity).one().stage == "needs_documents"


def test_an_uploaded_file_counts_as_a_document_the_owner_holds(session, profile):
    session.add(models_db.Document(
        profile_id=profile.id, object_key="k", doc_type="tax_clearance",
        original_filename="itf263.pdf", content_type="application/pdf", size_bytes=10,
    ))
    session.add(models_db.Document(
        profile_id=profile.id, object_key="k2", doc_type="other",
        original_filename="notes.txt", content_type="text/plain", size_bytes=3,
    ))
    session.commit()
    session.refresh(profile)

    held = pipeline.held_document_keys(profile)

    assert "tax_clearance" in held
    assert "transcript" in held, "typed-in documents count too"
    assert "other" not in held, "an unclassified upload proves nothing about what it is"


# ---------------------------------------------------------------------------
# Tenders come off the PRAZ board, not out of a web search
# ---------------------------------------------------------------------------

@pytest.fixture
def company_profile(session):
    account = models_db.Account(email="co@example.com", password_hash="x")
    session.add(account)
    session.commit()
    company = models_db.Profile(
        account_id=account.id, profile_type="tender", display_name="Tenders",
        fields={
            "name": "Meshcloud Zimbabwe",
            "country": "Zimbabwe",
            "praz_categories": ["GE001"],
            "categories": ["GE001"],
            "sectors": ["ICT hardware"],
        },
    )
    session.add(company)
    session.commit()
    return company


def test_a_tender_profile_reads_the_board_and_never_calls_web_search(session, company_profile):
    from opportunity_agent import egp
    from opportunity_agent.models import Opportunity

    searched = []

    def spy_search(p, api_key, **kw):
        searched.append(kw)
        return []

    tender = Opportunity(
        source="PRAZ eGP", title="Supply of transformers",
        url="https://egp.praz.org.zw/Indexes/viewLiveTenderDetails/1",
        eligible_countries=["Zimbabwe"], required_categories=["GE001"],
        required_documents=[], evidence=["PRAZ eGP bulletin board listing"],
        requirements_verified=True,
    )
    details = egp.TenderDetails(bid_security_domestic=25000, addendum_count=7)

    run = pipeline.run_profile_cycle(
        session, company_profile, api_key="k",
        search_fn=spy_search, tender_fn=lambda: [(tender, details)],
        award_fn=lambda: [],  # no test may reach the live award notices page either
    )

    assert searched == [], "tenders must not go through the search API"
    assert run.found == 1
    stored = session.query(models_db.StoredOpportunity).one()
    assert stored.match_status == "eligible"
    assert stored.stage == "drafted"


def test_a_tender_draft_carries_the_advice_the_bidder_needs(session, company_profile):
    from opportunity_agent import egp
    from opportunity_agent.models import Opportunity

    tender = Opportunity(
        source="PRAZ eGP", title="Supply of transformers",
        url="https://egp.praz.org.zw/Indexes/viewLiveTenderDetails/1",
        eligible_countries=["Zimbabwe"], required_categories=["GE001"],
        required_documents=["tax_clearance"],
        evidence=["PRAZ eGP bulletin board listing"], requirements_verified=True,
    )
    details = egp.TenderDetails(bid_security_domestic=25000, addendum_count=7)

    pipeline.run_profile_cycle(
        session, company_profile, api_key="k",
        search_fn=lambda p, api_key, **kw: [], tender_fn=lambda: [(tender, details)],
        award_fn=lambda: [],
    )

    stored = session.query(models_db.StoredOpportunity).one()
    advice = " ".join(stored.compliance["actions"])
    assert "25,000" in advice
    assert "addend" in advice.lower()
    assert "Tax Clearance" in advice
    assert stored.compliance["ready_to_submit"] is False


# ---------------------------------------------------------------------------
# Award notices: filtering new candidates, flagging existing ones, and
# accumulating history past the live page's own rolling window.
# ---------------------------------------------------------------------------

def _awarded(tender_id: str, awardee: str = "Some Other Company") -> egp_awards.AwardNotice:
    return egp_awards.AwardNotice(
        award_number="9001", tender_id=tender_id, title="Something",
        awardee=awardee, award_date=date(2026, 9, 1),
    )


def test_a_newly_found_tender_already_awarded_is_not_stored(session, company_profile):
    from opportunity_agent.models import Opportunity

    tender = Opportunity(
        source="PRAZ eGP", title="Supply of transformers",
        url="https://egp.praz.org.zw/Indexes/viewLiveTenderDetails/1",
        eligible_countries=["Zimbabwe"], required_categories=["GE001"],
        required_documents=[], evidence=["PRAZ eGP bulletin board listing"],
        requirements_verified=True,
    )

    run = pipeline.run_profile_cycle(
        session, company_profile, api_key="k",
        search_fn=lambda p, api_key, **kw: [], tender_fn=lambda: [(tender, None)],
        award_fn=lambda: [_awarded("1")],
    )

    assert session.query(models_db.StoredOpportunity).count() == 0
    # the board still reported one tender - the exclusion is deliberate, not a failure
    assert run.found == 1
    assert run.added == 0


def test_an_already_stored_tender_that_gets_awarded_is_flagged_not_deleted(session, company_profile):
    from opportunity_agent.models import Opportunity

    tender = Opportunity(
        source="PRAZ eGP", title="Supply of transformers",
        url="https://egp.praz.org.zw/Indexes/viewLiveTenderDetails/1",
        eligible_countries=["Zimbabwe"], required_categories=["GE001"],
        required_documents=[], evidence=["PRAZ eGP bulletin board listing"],
        requirements_verified=True,
    )
    # cycle 1: discovered while still open
    pipeline.run_profile_cycle(
        session, company_profile, api_key="k",
        search_fn=lambda p, api_key, **kw: [], tender_fn=lambda: [(tender, None)],
        award_fn=lambda: [],
    )
    stored = session.query(models_db.StoredOpportunity).one()
    assert stored.awarded_to is None
    assert stored.stage == "drafted"

    # cycle 2: someone else has now won it - board no longer lists it, so the
    # only way the owner finds out is this flag on the row they already have
    pipeline.run_profile_cycle(
        session, company_profile, api_key="k",
        search_fn=lambda p, api_key, **kw: [], tender_fn=lambda: [],
        award_fn=lambda: [_awarded("1", awardee="Acme Rivals Ltd")],
    )

    session.refresh(stored)
    assert stored.awarded_to == "Acme Rivals Ltd"
    assert stored.awarded_at == date(2026, 9, 1)
    assert stored.stage == "drafted", "the workflow stage itself is untouched"


def test_a_submitted_tender_is_not_touched_even_if_later_awarded_elsewhere(session, company_profile):
    """The owner already knows the outcome of a bid they submitted - this is
    for tenders sitting in the queue looking actionable when they no longer
    are, not for rewriting history on one that's already gone in."""
    from opportunity_agent.models import Opportunity

    tender = Opportunity(
        source="PRAZ eGP", title="Supply of transformers",
        url="https://egp.praz.org.zw/Indexes/viewLiveTenderDetails/1",
        eligible_countries=["Zimbabwe"], required_categories=["GE001"],
        required_documents=[], evidence=["PRAZ eGP bulletin board listing"],
        requirements_verified=True,
    )
    pipeline.run_profile_cycle(
        session, company_profile, api_key="k",
        search_fn=lambda p, api_key, **kw: [], tender_fn=lambda: [(tender, None)],
        award_fn=lambda: [],
    )
    stored = session.query(models_db.StoredOpportunity).one()
    stored.stage = "submitted"
    session.commit()

    pipeline.run_profile_cycle(
        session, company_profile, api_key="k",
        search_fn=lambda p, api_key, **kw: [], tender_fn=lambda: [],
        award_fn=lambda: [_awarded("1")],
    )

    session.refresh(stored)
    assert stored.awarded_to is None


def test_award_notices_are_persisted_past_the_rolling_window(session, company_profile):
    notice = _awarded("1")
    pipeline.run_profile_cycle(
        session, company_profile, api_key="k",
        search_fn=lambda p, api_key, **kw: [], tender_fn=lambda: [],
        award_fn=lambda: [notice],
    )

    row = session.query(models_db.StoredAwardNotice).one()
    assert row.award_number == "9001"
    assert row.tender_id == "1"
    assert row.awardee == "Some Other Company"

    # a later cycle re-polling the same window must not duplicate the row
    pipeline.run_profile_cycle(
        session, company_profile, api_key="k",
        search_fn=lambda p, api_key, **kw: [], tender_fn=lambda: [],
        award_fn=lambda: [notice],
    )
    assert session.query(models_db.StoredAwardNotice).count() == 1


def test_a_failed_award_poll_does_not_fail_the_cycle(session, company_profile):
    from opportunity_agent.models import Opportunity

    tender = Opportunity(
        source="PRAZ eGP", title="Supply of transformers",
        url="https://egp.praz.org.zw/Indexes/viewLiveTenderDetails/1",
        eligible_countries=["Zimbabwe"], required_categories=["GE001"],
        required_documents=[], evidence=["PRAZ eGP bulletin board listing"],
        requirements_verified=True,
    )

    def broken_award_fn():
        raise RuntimeError("PRAZ is down")

    run = pipeline.run_profile_cycle(
        session, company_profile, api_key="k",
        search_fn=lambda p, api_key, **kw: [], tender_fn=lambda: [(tender, None)],
        award_fn=broken_award_fn,
    )

    # degrades to "nothing filtered or flagged" - not a failed cycle
    assert run.found == 1
    assert run.added == 1
    assert session.query(models_db.StoredOpportunity).one().awarded_to is None
