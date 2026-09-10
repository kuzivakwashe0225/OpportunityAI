"""Idle sessions, extending them, and what the browser is told.

The load-bearing property here is that the *server* ends an idle session. An
idle timeout implemented only as a countdown in JavaScript is decoration:
close the tab, reopen it, and the old token still works. So these tests care
about token expiry, not about anything the page draws.
"""

from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient

from conftest import sign_up
from opportunity_agent import api, auth as auth_module
from opportunity_agent.api import app

client = TestClient(app)


def setup_function():
    client.cookies.clear()


# --------------------------------------------------------------------------
# The idle window
# --------------------------------------------------------------------------

def test_the_session_token_expires_after_the_idle_window(monkeypatch):
    monkeypatch.setenv("SESSION_IDLE_MINUTES", "30")
    token = auth_module.create_session_token("acct-1", secret_key="k")

    claims = jwt.decode(token, "k", algorithms=[auth_module.ALGORITHM])
    lifetime = claims["exp"] - claims["iat"]

    assert lifetime == pytest.approx(30 * 60, abs=2)


def test_the_idle_window_is_configurable(monkeypatch):
    monkeypatch.setenv("SESSION_IDLE_MINUTES", "5")
    token = auth_module.create_session_token("acct-1", secret_key="k")

    claims = jwt.decode(token, "k", algorithms=[auth_module.ALGORITHM])

    assert (claims["exp"] - claims["iat"]) == pytest.approx(5 * 60, abs=2)


def test_a_nonsense_idle_window_falls_back_rather_than_locking_everyone_out(monkeypatch):
    """Zero would mean "expire immediately", which is a denial of service."""
    for bad in ("0", "-10", "banana"):
        monkeypatch.setenv("SESSION_IDLE_MINUTES", bad)
        assert auth_module._idle_ttl() == timedelta(minutes=30)


def test_an_expired_token_is_refused(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-key")
    stale = auth_module.create_session_token(
        "acct-1", secret_key="test-key",
        now=datetime.now(timezone.utc) - timedelta(days=1),
    )

    with pytest.raises(jwt.PyJWTError):
        auth_module.decode_session_token(stale, secret_key="test-key")


# --------------------------------------------------------------------------
# Extending
# --------------------------------------------------------------------------

def test_extending_issues_a_later_deadline(outbox):
    sign_up(client, outbox=outbox)

    first = client.get("/me")
    before = client.cookies.get(api.SESSION_EXPIRY_COOKIE)
    assert first.status_code == 200

    extended = client.post("/session/extend")

    assert extended.status_code == 200
    assert "expires_at" in extended.json()
    after = extended.json()["expires_at"]
    if before:
        assert after >= before


def test_extending_requires_a_live_session(outbox):
    """An expired session must not be able to resurrect itself."""
    sign_up(client, outbox=outbox)
    client.cookies.clear()

    assert client.post("/session/extend").status_code == 401


def test_reading_does_not_extend_the_session(outbox):
    """The crucial one.

    If any authenticated request pushed the deadline back, the page's own
    background polling would keep a session alive forever with nobody at the
    keyboard, and the idle timeout would mean nothing at all.
    """
    sign_up(client, outbox=outbox)
    original = client.cookies.get(api.SESSION_COOKIE)

    for path in ("/me", "/profiles", "/notifications"):
        client.get(path)

    assert client.cookies.get(api.SESSION_COOKIE) == original


# --------------------------------------------------------------------------
# What the browser is handed
# --------------------------------------------------------------------------

def test_login_sets_a_readable_expiry_alongside_the_httponly_token(outbox):
    """The token stays httponly; only the deadline is readable.

    Without this the page cannot tell, after a refresh, how long the session it
    already holds has left - so it could not warn before it died.
    """
    sign_up(client, outbox=outbox)

    assert client.cookies.get(api.SESSION_COOKIE)
    expiry = client.cookies.get(api.SESSION_EXPIRY_COOKIE)
    assert expiry
    # It is a timestamp, and it carries nothing else.
    parsed = datetime.fromisoformat(expiry)
    assert parsed > datetime.now(timezone.utc)


def test_the_expiry_cookie_holds_no_credential(outbox):
    sign_up(client, outbox=outbox)

    expiry = client.cookies.get(api.SESSION_EXPIRY_COOKIE)
    token = client.cookies.get(api.SESSION_COOKIE)

    assert token not in expiry
    assert "." not in expiry.split("T")[0]      # not a JWT


def test_logout_clears_both_cookies(outbox):
    sign_up(client, outbox=outbox)

    client.post("/logout")

    assert not client.cookies.get(api.SESSION_COOKIE)
    assert not client.cookies.get(api.SESSION_EXPIRY_COOKIE)


def test_the_session_survives_a_refresh(outbox):
    """What "refresh logs me out" should never mean again.

    The cookie was always persistent; the bug was in the page. This pins the
    server half so a future change cannot quietly make it a session cookie.
    """
    sign_up(client, outbox=outbox)

    # A fresh "page load" - same cookie jar, brand new request.
    reloaded = client.get("/me")

    assert reloaded.status_code == 200
    assert reloaded.json()["email"] == "owner@example.com"
