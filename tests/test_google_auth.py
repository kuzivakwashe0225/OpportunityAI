"""Verifying a Google credential, without ever calling Google.

A real RSA keypair signs the test tokens, and a stub stands in for
PyJWKClient - the same dependency-injection pattern the rest of this project
uses (`_send_mail`, `client: httpx.Client | None`) so no test here reaches a
real network.
"""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from opportunity_agent.google_auth import GoogleAuthError, verify_google_id_token

CLIENT_ID = "123-example.apps.googleusercontent.com"

_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_public_key = _private_key.public_key()


class _StubSigningKey:
    def __init__(self, key):
        self.key = key


class _StubJWKClient:
    """Stands in for jwt.PyJWKClient: hands back the one key every test
    signs with, rather than fetching Google's real key set."""

    def get_signing_key_from_jwt(self, token):
        return _StubSigningKey(_public_key)


def _token(**claims) -> str:
    payload = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "10769150350006150715113082367",
        "email": "student@example.com",
        "email_verified": True,
        "name": "A Student",
        "iat": int(time.time()),
        "exp": int(time.time()) + 3600,
    }
    payload.update(claims)
    return jwt.encode(payload, _private_key, algorithm="RS256")


def test_a_genuine_token_verifies():
    claims = verify_google_id_token(_token(), CLIENT_ID, jwk_client=_StubJWKClient())

    assert claims["email"] == "student@example.com"
    assert claims["sub"] == "10769150350006150715113082367"


def test_the_older_bare_issuer_form_is_accepted_too():
    """Google legitimately issues tokens under either form of its issuer."""
    claims = verify_google_id_token(
        _token(iss="accounts.google.com"), CLIENT_ID, jwk_client=_StubJWKClient())

    assert claims["iss"] == "accounts.google.com"


def test_a_token_for_a_different_app_is_refused():
    """The audience is the one thing that stops any Google-signed token from
    anywhere logging into this app."""
    with pytest.raises(GoogleAuthError):
        verify_google_id_token(
            _token(aud="someone-elses-app.apps.googleusercontent.com"),
            CLIENT_ID, jwk_client=_StubJWKClient())


def test_an_expired_token_is_refused():
    with pytest.raises(GoogleAuthError):
        verify_google_id_token(
            _token(exp=int(time.time()) - 60), CLIENT_ID, jwk_client=_StubJWKClient())


def test_a_token_claiming_a_different_issuer_is_refused():
    with pytest.raises(GoogleAuthError):
        verify_google_id_token(
            _token(iss="https://not-google.example.com"), CLIENT_ID,
            jwk_client=_StubJWKClient())


def test_a_tampered_signature_is_refused():
    genuine = _token()
    header, payload, signature = genuine.split(".")
    tampered = header + "." + payload + "." + signature[:-4] + "aaaa"

    with pytest.raises(GoogleAuthError):
        verify_google_id_token(tampered, CLIENT_ID, jwk_client=_StubJWKClient())


def test_a_token_signed_by_a_different_key_is_refused():
    """The scenario the whole check exists for: a forged token that merely
    claims to be from Google."""
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode({
        "iss": "https://accounts.google.com", "aud": CLIENT_ID,
        "sub": "attacker", "email": "attacker@example.com",
        "email_verified": True, "exp": int(time.time()) + 3600,
    }, other_key, algorithm="RS256")

    with pytest.raises(GoogleAuthError):
        verify_google_id_token(forged, CLIENT_ID, jwk_client=_StubJWKClient())


def test_being_unable_to_reach_googles_key_set_is_a_google_auth_error():
    """Any failure here is the same one type - callers must not be tempted
    to treat "couldn't check" as "must be fine"."""
    class _BrokenClient:
        def get_signing_key_from_jwt(self, token):
            raise OSError("network unreachable")

    with pytest.raises(GoogleAuthError):
        verify_google_id_token(_token(), CLIENT_ID, jwk_client=_BrokenClient())


def test_email_verification_is_the_callers_decision_not_this_modules():
    """This module confirms the token is genuine; it does not itself refuse
    an unverified email - the caller decides what identity guarantee it
    needs, the same separation auth.py keeps between "is this token real"
    and "may this account do X"."""
    claims = verify_google_id_token(
        _token(email_verified=False), CLIENT_ID, jwk_client=_StubJWKClient())

    assert claims["email_verified"] is False
