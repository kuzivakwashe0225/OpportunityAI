"""Deleting a profile, resetting a lost password, and what the account screen
is allowed to show.

The password tests are mostly about *ordering*. A reset that overwrites the
stored hash and then fails to deliver the new password locks the owner out
using a password that exists nowhere - strictly worse than the state they
were already in - so the send has to come first.
"""

from fastapi.testclient import TestClient

from conftest import _password_from, sign_up
from opportunity_agent import api, mailer
from opportunity_agent.api import app
from test_documents_api import FakeMinioClient

client = TestClient(app)


def setup_function():
    client.cookies.clear()


def _make_profile(profile_type="job", name="Jobs"):
    return client.post(
        "/profiles", json={"profile_type": profile_type, "display_name": name}
    ).json()


# --------------------------------------------------------------------------
# Forgot password
# --------------------------------------------------------------------------

def test_forgot_password_emails_a_working_replacement(outbox):
    sign_up(client, outbox=outbox)
    client.post("/logout")

    response = client.post("/forgot-password", json={"email": "owner@example.com"})

    assert response.status_code == 200
    new_password = _password_from(outbox[-1]["body"])
    assert client.post(
        "/login", json={"email": "owner@example.com", "password": new_password}
    ).status_code == 200


def test_the_old_password_stops_working_after_a_reset(outbox):
    old_password = sign_up(client, outbox=outbox)
    client.post("/logout")

    client.post("/forgot-password", json={"email": "owner@example.com"})

    assert client.post(
        "/login", json={"email": "owner@example.com", "password": old_password}
    ).status_code == 401


def test_a_reset_whose_email_fails_leaves_the_existing_password_working(outbox, monkeypatch):
    """The whole point of sending before storing.

    If delivery fails the owner must be exactly where they started, not
    holding an account whose password was replaced by one nobody received.
    """
    old_password = sign_up(client, outbox=outbox)
    client.post("/logout")

    def refuse(**kwargs):
        raise mailer.MailError("could not send mail: SMTPException")

    monkeypatch.setattr(api, "_send_mail", refuse)

    response = client.post("/forgot-password", json={"email": "owner@example.com"})

    assert response.status_code == 503
    assert client.post(
        "/login", json={"email": "owner@example.com", "password": old_password}
    ).status_code == 200


def test_a_reset_forces_the_password_to_be_replaced_at_next_sign_in(outbox):
    sign_up(client, outbox=outbox)
    client.post("/logout")
    client.post("/forgot-password", json={"email": "owner@example.com"})

    new_password = _password_from(outbox[-1]["body"])
    body = client.post(
        "/login", json={"email": "owner@example.com", "password": new_password}
    ).json()

    assert body["must_change_password"] is True


def test_forgot_password_for_an_unknown_address_is_a_404(outbox):
    response = client.post("/forgot-password", json={"email": "nobody@example.com"})

    assert response.status_code == 404
    assert outbox == []


# --------------------------------------------------------------------------
# The account screen
# --------------------------------------------------------------------------

def test_me_reports_password_state_but_never_a_password(outbox):
    sign_up(client, outbox=outbox)

    body = client.get("/me").json()

    assert body["email"] == "owner@example.com"
    assert "must_change_password" in body
    assert "temp_password_expires_at" in body
    # There is no way to show someone their password and no attempt to:
    # auth.py stores bcrypt hashes, so the plaintext does not exist here.
    assert not any("password" == k or "password_hash" == k for k in body)
    assert "hash" not in repr(body).lower()


# --------------------------------------------------------------------------
# Deleting a profile
# --------------------------------------------------------------------------

def test_deleting_a_profile_removes_it(outbox):
    sign_up(client, outbox=outbox)
    profile = _make_profile()

    assert client.delete(f"/profiles/{profile['id']}").status_code == 204
    assert client.get("/profiles").json() == []


def test_deleting_a_profile_also_deletes_its_files_from_the_object_store(outbox, monkeypatch):
    """The rows are not the thing the owner wants gone - the CV is."""
    sign_up(client, outbox=outbox)
    fake = FakeMinioClient()
    monkeypatch.setattr(api, "_minio_client", lambda: fake)
    profile = _make_profile()
    client.post(
        f"/profiles/{profile['id']}/documents",
        files={"file": ("cv.txt", b"private", "text/plain")},
    )
    assert fake.objects, "precondition: the upload actually stored something"

    client.delete(f"/profiles/{profile['id']}")

    assert fake.objects == {}


def test_a_missing_object_does_not_block_deleting_the_profile(outbox, monkeypatch):
    sign_up(client, outbox=outbox)
    fake = FakeMinioClient()
    monkeypatch.setattr(api, "_minio_client", lambda: fake)
    profile = _make_profile()
    client.post(
        f"/profiles/{profile['id']}/documents",
        files={"file": ("cv.txt", b"private", "text/plain")},
    )

    def blow_up(bucket_name, object_name, version_id=None):
        raise RuntimeError("object already gone")

    monkeypatch.setattr(fake, "remove_object", blow_up)

    assert client.delete(f"/profiles/{profile['id']}").status_code == 204
    assert client.get("/profiles").json() == []


def test_deleting_a_profile_clears_notifications_that_point_at_it(outbox):
    """Notifications hang off the account but carry a profile_id foreign key,
    so they are not covered by the profile's ORM cascade."""
    sign_up(client, outbox=outbox)
    profile = _make_profile()

    from opportunity_agent import db as db_module, models_db

    session = db_module.SessionLocal()
    account_id = session.query(models_db.Account).first().id
    session.add(models_db.Notification(
        account_id=account_id, profile_id=profile["id"],
        kind="ready", message="something is ready",
    ))
    session.commit()
    session.close()

    assert len(client.get("/notifications").json()) == 1

    assert client.delete(f"/profiles/{profile['id']}").status_code == 204
    assert client.get("/notifications").json() == []


def test_another_account_cannot_delete_your_profile(outbox):
    sign_up(client, outbox=outbox)
    profile = _make_profile()
    client.cookies.clear()
    sign_up(client, email="someone-else@example.com", outbox=outbox)

    assert client.delete(f"/profiles/{profile['id']}").status_code == 404


def test_deleting_a_profile_requires_authentication(outbox):
    sign_up(client, outbox=outbox)
    profile = _make_profile()
    client.cookies.clear()

    assert client.delete(f"/profiles/{profile['id']}").status_code == 401
