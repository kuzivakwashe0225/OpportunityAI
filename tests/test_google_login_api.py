"""Signing in with Google, end to end through the API.

The credential itself is never real - `_verify_google_id_token` is swapped
out for a stub, the same pattern conftest.py uses for `_send_mail`. What is
tested here is everything api.py does once it trusts the claims: finding or
creating the account, linking a Google identity onto an existing password
account, and refusing what should be refused.
"""

import pytest
from fastapi.testclient import TestClient

from conftest import sign_up
from opportunity_agent import api, google_auth, models_db
from opportunity_agent import db as db_module

client = TestClient(api.app)


def setup_function():
    client.cookies.clear()


def _claims(**overrides):
    claims = {
        "sub": "10769150350006150715113082367",
        "email": "student@example.com",
        "email_verified": True,
        "name": "A Student",
    }
    claims.update(overrides)
    return claims


@pytest.fixture(autouse=True)
def stub_google(monkeypatch):
    """Returns a mutable dict so a test can change what "Google" answers with
    for the next call, and a list of every credential the endpoint was asked
    to verify."""
    seen = []
    current = {"claims": _claims()}

    def fake_verify(credential, client_id):
        seen.append((credential, client_id))
        if current.get("raises"):
            raise current["raises"]
        return current["claims"]

    monkeypatch.setattr(api, "_verify_google_id_token", fake_verify)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client-id.apps.googleusercontent.com")
    monkeypatch.setattr(api, "GOOGLE_CLIENT_ID", "test-client-id.apps.googleusercontent.com")
    return current, seen


# --------------------------------------------------------------------------
# The feature can be off
# --------------------------------------------------------------------------

def test_unconfigured_google_sign_in_answers_503_not_a_stack_trace(monkeypatch):
    monkeypatch.setattr(api, "GOOGLE_CLIENT_ID", "")

    response = client.post("/login/google", json={"credential": "whatever"})

    assert response.status_code == 503


def test_the_frontend_can_tell_whether_its_configured(monkeypatch):
    monkeypatch.setattr(api, "GOOGLE_CLIENT_ID", "")
    assert client.get("/auth/config").json()["google_client_id"] is None

    monkeypatch.setattr(api, "GOOGLE_CLIENT_ID", "abc.apps.googleusercontent.com")
    assert client.get("/auth/config").json()["google_client_id"] == "abc.apps.googleusercontent.com"


# --------------------------------------------------------------------------
# First sign-in creates the account; the token itself is never trusted blind
# --------------------------------------------------------------------------

def test_a_first_google_sign_in_creates_an_account(stub_google):
    response = client.post("/login/google", json={"credential": "token"})

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "student@example.com"
    assert body["must_change_password"] is False
    assert client.get("/me").status_code == 200


def test_the_credential_the_page_received_is_forwarded_untouched(stub_google):
    current, seen = stub_google

    client.post("/login/google", json={"credential": "the-exact-jwt-the-browser-got"})

    assert seen[0][0] == "the-exact-jwt-the-browser-got"
    assert seen[0][1] == "test-client-id.apps.googleusercontent.com"


def test_a_forged_or_expired_token_is_refused(stub_google):
    current, _ = stub_google
    current["raises"] = google_auth.GoogleAuthError("signature invalid")

    response = client.post("/login/google", json={"credential": "bad"})

    assert response.status_code == 401
    assert client.get("/me").status_code == 401


def test_an_unverified_email_is_refused():
    """A signed, genuine token is not enough on its own - Google itself must
    have confirmed the address."""
    from opportunity_agent import api as api_module

    api_module._verify_google_id_token = lambda credential, client_id: {
        "sub": "1", "email": "unverified@example.com", "email_verified": False,
    }

    response = client.post("/login/google", json={"credential": "token"})

    assert response.status_code == 401
    assert client.get("/me").status_code == 401


def test_signing_in_twice_reuses_the_same_account_not_a_second_one(stub_google):
    client.post("/login/google", json={"credential": "token"})
    first_id = client.get("/me").json()["id"]
    client.cookies.clear()

    client.post("/login/google", json={"credential": "token"})
    second_id = client.get("/me").json()["id"]

    assert first_id == second_id

    session = db_module.SessionLocal()
    count = session.query(models_db.Account).filter_by(email="student@example.com").count()
    session.close()
    assert count == 1


# --------------------------------------------------------------------------
# Linking onto an account that already has a password
# --------------------------------------------------------------------------

def test_google_links_onto_an_existing_password_account_with_the_same_email(stub_google):
    """Safe specifically because Google has verified the email - the same
    trust a password-reset link already relies on."""
    password = sign_up(client, email="student@example.com")
    client.cookies.clear()
    original_id = None
    session = db_module.SessionLocal()
    original_id = session.query(models_db.Account).filter_by(
        email="student@example.com").first().id
    session.close()

    response = client.post("/login/google", json={"credential": "token"})

    assert response.status_code == 200
    assert response.json()["id"] == original_id

    session = db_module.SessionLocal()
    count = session.query(models_db.Account).filter_by(email="student@example.com").count()
    linked = session.query(models_db.Account).filter_by(id=original_id).one()
    session.close()
    assert count == 1, "linking must not create a second account"
    assert linked.oauth_provider == "google"


def test_the_password_still_works_after_linking_a_google_account(stub_google):
    """Linking adds a way in - it must not take the old one away."""
    password = sign_up(client, email="student@example.com")
    client.cookies.clear()
    client.post("/login/google", json={"credential": "token"})
    client.cookies.clear()

    response = client.post("/login", json={"email": "student@example.com",
                                           "password": password})

    assert response.status_code == 200


# --------------------------------------------------------------------------
# Guessing is throttled here too
# --------------------------------------------------------------------------

def test_repeated_forged_tokens_from_one_address_are_throttled(stub_google):
    current, _ = stub_google
    current["raises"] = google_auth.GoogleAuthError("bad signature")

    codes = [client.post("/login/google", json={"credential": "bad"}).status_code
             for _ in range(25)]

    assert 429 in codes
