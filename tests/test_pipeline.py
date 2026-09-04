import pytest
from sqlalchemy.orm import Session

from opportunity_agent import models_db, pipeline
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
        search_fn=lambda p, api_key: [SearchResult(title="Award", url="https://example.org/award", content="x")],
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
        search_fn=lambda p, api_key: [SearchResult(title="Award", url="https://example.org/award", content="x")],
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
        search_fn=lambda p, api_key: [SearchResult(title="Kenya Award", url="https://example.org/kenya", content="x")],
        fetch_fn=lambda url: _ineligible_page(url),
    )

    stored = session.query(models_db.StoredOpportunity).one()
    assert stored.match_status == "ineligible"
    assert stored.stage == "discovered"
    assert stored.package is None


def test_a_notification_is_created_when_applications_are_drafted(session, profile):
    pipeline.run_profile_cycle(
        session, profile, api_key="k",
        search_fn=lambda p, api_key: [SearchResult(title="Award", url="https://example.org/award", content="x")],
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
        search_fn=lambda p, api_key: [SearchResult(title="Kenya", url="https://example.org/kenya", content="x")],
        fetch_fn=lambda url: _ineligible_page(url),
    )

    assert session.query(models_db.Notification).count() == 0


def test_rerunning_does_not_duplicate_the_same_opportunity(session, profile):
    for _ in range(2):
        pipeline.run_profile_cycle(
            session, profile, api_key="k",
            search_fn=lambda p, api_key: [SearchResult(title="Award", url="https://example.org/award", content="x")],
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
        search_fn=lambda p, api_key: [
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
        search_fn=lambda p, api_key: [SearchResult(title="Kenya", url="https://example.org/kenya", content="x")],
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
    def failing_search(p, api_key):
        raise RuntimeError("search API unreachable")

    run = pipeline.run_profile_cycle(session, profile, api_key="k", search_fn=failing_search)

    assert run is not None
    assert run.found == 0
    assert "search API unreachable" in run.failures[0]
