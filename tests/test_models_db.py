import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from opportunity_agent.db import init_db, make_engine
from opportunity_agent import models_db


@pytest.fixture
def session():
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    with Session(engine) as db_session:
        yield db_session


def test_init_db_creates_usable_tables(session):
    account = models_db.Account(email="owner@example.com", password_hash="hashed")
    session.add(account)
    session.commit()

    assert session.query(models_db.Account).count() == 1


def test_account_can_hold_up_to_three_profile_types(session):
    account = models_db.Account(email="owner@example.com", password_hash="hashed")
    session.add(account)
    session.commit()

    for profile_type in ["scholarship", "job", "grant"]:
        session.add(models_db.Profile(
            account_id=account.id, profile_type=profile_type,
            display_name=profile_type.title(), fields={},
        ))
    session.commit()

    assert len(account.profiles) == 3


def test_account_cannot_hold_two_profiles_of_the_same_type(session):
    account = models_db.Account(email="owner@example.com", password_hash="hashed")
    session.add(account)
    session.commit()
    session.add(models_db.Profile(
        account_id=account.id, profile_type="scholarship", display_name="First", fields={},
    ))
    session.commit()

    session.add(models_db.Profile(
        account_id=account.id, profile_type="scholarship", display_name="Second", fields={},
    ))
    with pytest.raises(IntegrityError):
        session.commit()


def test_profile_fields_round_trip_as_json(session):
    account = models_db.Account(email="owner@example.com", password_hash="hashed")
    session.add(account)
    session.commit()
    profile = models_db.Profile(
        account_id=account.id, profile_type="job", display_name="Job search",
        fields={"name": "Tendai Moyo", "goals": ["climate technology"]},
    )
    session.add(profile)
    session.commit()
    session.expire_all()

    reloaded = session.get(models_db.Profile, profile.id)
    assert reloaded.fields["name"] == "Tendai Moyo"
    assert reloaded.fields["goals"] == ["climate technology"]


def test_document_belongs_to_one_profile_and_records_upload_metadata(session):
    account = models_db.Account(email="owner@example.com", password_hash="hashed")
    session.add(account)
    session.commit()
    profile = models_db.Profile(
        account_id=account.id, profile_type="scholarship", display_name="Scholarships", fields={},
    )
    session.add(profile)
    session.commit()

    document = models_db.Document(
        profile_id=profile.id, object_key="acct/profile/doc/cv.pdf",
        original_filename="cv.pdf", content_type="application/pdf", size_bytes=1024,
    )
    session.add(document)
    session.commit()

    assert profile.documents[0].original_filename == "cv.pdf"
    assert document.extraction_status == "skipped"


def test_deleting_a_profile_cascades_to_its_documents(session):
    account = models_db.Account(email="owner@example.com", password_hash="hashed")
    session.add(account)
    session.commit()
    profile = models_db.Profile(
        account_id=account.id, profile_type="scholarship", display_name="Scholarships", fields={},
    )
    session.add(profile)
    session.commit()
    session.add(models_db.Document(
        profile_id=profile.id, object_key="k", original_filename="cv.pdf",
        content_type="application/pdf", size_bytes=10,
    ))
    session.commit()

    session.delete(profile)
    session.commit()

    assert session.query(models_db.Document).count() == 0


def test_notification_belongs_to_an_account_and_starts_unread(session):
    account = models_db.Account(email="owner@example.com", password_hash="hashed")
    session.add(account)
    session.commit()

    session.add(models_db.Notification(
        account_id=account.id, kind="discovery_complete", message="Found 3 new opportunities.",
    ))
    session.commit()

    assert account.notifications[0].read_at is None


def test_two_accounts_can_use_the_same_profile_type_independently(session):
    a = models_db.Account(email="a@example.com", password_hash="x")
    b = models_db.Account(email="b@example.com", password_hash="y")
    session.add_all([a, b])
    session.commit()

    session.add(models_db.Profile(account_id=a.id, profile_type="job", display_name="A's job search", fields={}))
    session.add(models_db.Profile(account_id=b.id, profile_type="job", display_name="B's job search", fields={}))
    session.commit()

    assert len(a.profiles) == 1
    assert len(b.profiles) == 1
