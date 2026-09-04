import pytest
from sqlalchemy.orm import Session

from opportunity_agent import models_db
from opportunity_agent.db import init_db, make_engine


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
        account_id=account.id, profile_type="scholarship", display_name="Scholarships", fields={}
    )
    session.add(profile)
    session.commit()
    return profile


def test_opportunity_belongs_to_a_profile_and_starts_discovered(session, profile):
    opportunity = models_db.StoredOpportunity(
        profile_id=profile.id,
        canonical_url="https://example.org/award",
        payload={"title": "STEM Award"},
        match_status="eligible",
    )
    session.add(opportunity)
    session.commit()

    assert profile.opportunities[0].canonical_url == "https://example.org/award"
    assert opportunity.stage == "discovered"
    assert opportunity.escalated is False


def test_the_same_url_can_exist_under_two_different_profiles(session, profile):
    """Two profiles (say scholarship and grant) may legitimately both track the
    same opportunity - dedup is per profile, not global."""
    other = models_db.Profile(
        account_id=profile.account_id, profile_type="grant", display_name="Grants", fields={}
    )
    session.add(other)
    session.commit()

    for target in (profile, other):
        session.add(models_db.StoredOpportunity(
            profile_id=target.id, canonical_url="https://example.org/same", payload={}, match_status="eligible",
        ))
    session.commit()

    assert session.query(models_db.StoredOpportunity).count() == 2


def test_deleting_a_profile_cascades_to_its_opportunities(session, profile):
    session.add(models_db.StoredOpportunity(
        profile_id=profile.id, canonical_url="https://example.org/x", payload={}, match_status="eligible",
    ))
    session.commit()

    session.delete(profile)
    session.commit()

    assert session.query(models_db.StoredOpportunity).count() == 0


def test_discovery_run_is_scoped_to_a_profile(session, profile):
    run = models_db.ProfileDiscoveryRun(
        profile_id=profile.id, queries=["scholarship zimbabwe"], found=5, added=3, failures=[],
    )
    session.add(run)
    session.commit()

    assert profile.discovery_runs[0].found == 5
    assert run.completed_at is not None


def test_notification_can_reference_a_profile_and_opportunity(session, profile):
    opportunity = models_db.StoredOpportunity(
        profile_id=profile.id, canonical_url="https://example.org/n", payload={}, match_status="eligible",
    )
    session.add(opportunity)
    session.commit()

    notification = models_db.Notification(
        account_id=profile.account_id,
        profile_id=profile.id,
        opportunity_id=opportunity.id,
        kind="application_drafted",
        message="1 application is ready for your review.",
    )
    session.add(notification)
    session.commit()

    assert notification.profile_id == profile.id
    assert notification.opportunity_id == opportunity.id
    assert notification.read_at is None
