"""Registration by emailed password, and replacing it at first login.

The password in these tests travels over email in clear, which is the owner's
explicit design choice. What the tests below hold the implementation to is the
set of things that make that choice survivable: the password is generated
rather than chosen, it expires, it is never returned in an HTTP response, the
account is not created at all if the mail cannot be delivered, and the user is
told to replace it the moment they arrive.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from opportunity_agent import api, models_db
from opportunity_agent import db as db_module

client = TestClient(api.app)


@pytest.fixture
def sent(monkeypatch):
    """Capture outgoing mail instead of sending it."""
    outbox = []
    monkeypatch.setattr(api, "_send_mail",
                        lambda **kwargs: outbox.append(kwargs))
    return outbox


def _register(email="new@example.com"):
    return client.post("/register", json={"email": email})


def _account(email="new@example.com"):
    with db_module.SessionLocal() as session:
        return session.query(models_db.Account).filter_by(email=email).one()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_registering_needs_only_an_email_address(sent):
    response = _register()

    assert response.status_code == 201
    assert response.json()["email"] == "new@example.com"


def test_the_password_is_emailed_to_the_new_user(sent):
    _register()

    assert len(sent) == 1
    assert sent[0]["to"] == "new@example.com"
    assert "password" in sent[0]["subject"].lower()
    assert "OpportunityAI" in sent[0]["body"]


def test_the_emailed_password_actually_works(sent):
    """The obvious thing that is easy to get wrong: mail one password and
    store the hash of a different one."""
    _register()
    password = _extract_password(sent[0]["body"])

    login = client.post("/login", json={"email": "new@example.com", "password": password})

    assert login.status_code == 200


def test_the_password_is_never_in_the_http_response(sent):
    """It goes to the mailbox, not to whoever submitted the form. Returning it
    would put it in browser history, proxy logs and the network tab."""
    response = _register()
    password = _extract_password(sent[0]["body"])

    assert password not in response.text


def test_registration_does_not_log_the_new_user_in(sent):
    """They have to prove they can read the mailbox. Auto-login would make the
    emailed password decorative and let anyone register an address they don't
    control and get straight in."""
    response = _register()

    assert "session" not in response.cookies
    client.cookies.clear()
    assert client.get("/me").status_code == 401


def test_generated_passwords_are_not_predictable(sent):
    _register("a@example.com")
    _register("b@example.com")

    first = _extract_password(sent[0]["body"])
    second = _extract_password(sent[1]["body"])

    assert first != second
    assert len(first) >= 12


def test_no_account_is_created_if_the_mail_cannot_be_delivered(monkeypatch):
    """An account whose password was never delivered is one nobody can log
    into. Reporting success for that is worse than refusing."""
    from opportunity_agent import mailer

    def explode(**kwargs):
        raise mailer.MailError("could not send mail: SMTPException")

    monkeypatch.setattr(api, "_send_mail", explode)

    response = _register()

    assert response.status_code == 503
    with db_module.SessionLocal() as session:
        assert session.query(models_db.Account).filter_by(email="new@example.com").count() == 0


def test_registering_an_existing_address_is_refused(sent):
    _register()
    again = _register()

    assert again.status_code == 409
    assert len(sent) == 1, "no second password is issued for an existing account"


def test_more_than_one_account_can_now_register(sent):
    """The single-account cap existed because the legacy endpoints shared one
    global store. That store is gone, so the cap goes with it."""
    assert _register("one@example.com").status_code == 201
    assert _register("two@example.com").status_code == 201
    assert _register("three@example.com").status_code == 201


# ---------------------------------------------------------------------------
# First login and changing the password
# ---------------------------------------------------------------------------

def test_a_new_account_is_flagged_to_change_its_password(sent):
    _register()

    assert _account().must_change_password is True


def test_the_flag_is_visible_to_the_frontend_so_it_can_prompt(sent):
    _register()
    password = _extract_password(sent[0]["body"])
    body = client.post("/login", json={"email": "new@example.com", "password": password}).json()

    assert body["must_change_password"] is True


def test_changing_the_password_clears_the_prompt_and_works(sent):
    _register()
    old = _extract_password(sent[0]["body"])
    client.post("/login", json={"email": "new@example.com", "password": old})

    changed = client.post("/change-password", json={
        "current_password": old, "new_password": "a much better passphrase"})

    assert changed.status_code == 200
    assert changed.json()["must_change_password"] is False
    client.cookies.clear()
    assert client.post("/login", json={
        "email": "new@example.com", "password": "a much better passphrase"}).status_code == 200


def test_the_old_password_stops_working_once_changed(sent):
    _register()
    old = _extract_password(sent[0]["body"])
    client.post("/login", json={"email": "new@example.com", "password": old})
    client.post("/change-password", json={
        "current_password": old, "new_password": "a much better passphrase"})
    client.cookies.clear()

    assert client.post("/login", json={
        "email": "new@example.com", "password": old}).status_code == 401


def test_changing_requires_the_current_password(sent):
    """Otherwise a stolen session cookie is enough to take the account over."""
    _register()
    old = _extract_password(sent[0]["body"])
    client.post("/login", json={"email": "new@example.com", "password": old})

    response = client.post("/change-password", json={
        "current_password": "not it", "new_password": "a much better passphrase"})

    assert response.status_code == 401


def test_a_too_short_new_password_is_refused(sent):
    _register()
    old = _extract_password(sent[0]["body"])
    client.post("/login", json={"email": "new@example.com", "password": old})

    response = client.post("/change-password", json={
        "current_password": old, "new_password": "short"})

    assert response.status_code == 422


def test_changing_the_password_requires_being_logged_in(sent):
    _register()
    client.cookies.clear()

    assert client.post("/change-password", json={
        "current_password": "x", "new_password": "a much better passphrase"}).status_code == 401


# ---------------------------------------------------------------------------
# The emailed password expires
# ---------------------------------------------------------------------------

def test_the_emailed_password_has_an_expiry(sent):
    _register()
    account = _account()

    assert account.temp_password_expires_at is not None


def test_an_expired_temporary_password_is_rejected(sent):
    """A temporary password that never expires is a permanent password with
    worse handling - it sat in an inbox in clear."""
    _register()
    password = _extract_password(sent[0]["body"])

    with db_module.SessionLocal() as session:
        account = session.query(models_db.Account).filter_by(email="new@example.com").one()
        account.temp_password_expires_at = datetime.now(timezone.utc) - timedelta(days=1)
        session.commit()

    response = client.post("/login", json={"email": "new@example.com", "password": password})

    assert response.status_code == 401
    assert "expired" in response.json()["detail"].lower()


def test_a_chosen_password_does_not_expire(sent):
    """Only the emailed one is temporary. Changing it must clear the clock,
    or the user is locked out a week later for no reason."""
    _register()
    old = _extract_password(sent[0]["body"])
    client.post("/login", json={"email": "new@example.com", "password": old})
    client.post("/change-password", json={
        "current_password": old, "new_password": "a much better passphrase"})

    assert _account().temp_password_expires_at is None


def _extract_password(body: str) -> str:
    """The email states the password on its own line after a marker."""
    for line in body.splitlines():
        if line.startswith("Password:"):
            return line.split("Password:", 1)[1].strip()
    raise AssertionError(f"no password line in email body:\n{body}")
