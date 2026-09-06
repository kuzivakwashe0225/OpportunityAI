"""Storing a portal password the owner has entrusted to us.

These are someone's real credentials to a government procurement system. The
tests below are mostly about the ways this can go quietly wrong: storing
plaintext because a key wasn't configured, leaking the secret back out through
an API response, or decrypting with the wrong key and getting something that
looks like success.
"""

import pytest

from opportunity_agent import credentials


KEY = credentials.generate_key()
OTHER_KEY = credentials.generate_key()


def test_a_secret_round_trips():
    token = credentials.encrypt_secret("hunter2", key=KEY)
    assert credentials.decrypt_secret(token, key=KEY) == "hunter2"


def test_the_stored_form_does_not_contain_the_secret():
    token = credentials.encrypt_secret("hunter2", key=KEY)

    assert "hunter2" not in token
    assert "hunter2" not in repr(token)


def test_the_same_secret_encrypts_differently_every_time():
    """Deterministic ciphertext would let anyone with database access tell
    that two profiles share a password, or spot when one hasn't changed."""
    a = credentials.encrypt_secret("hunter2", key=KEY)
    b = credentials.encrypt_secret("hunter2", key=KEY)

    assert a != b
    assert credentials.decrypt_secret(a, key=KEY) == credentials.decrypt_secret(b, key=KEY)


def test_the_wrong_key_fails_loudly_rather_than_returning_rubbish():
    token = credentials.encrypt_secret("hunter2", key=KEY)

    with pytest.raises(credentials.CredentialError):
        credentials.decrypt_secret(token, key=OTHER_KEY)


def test_tampered_ciphertext_is_rejected():
    token = credentials.encrypt_secret("hunter2", key=KEY)
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")

    with pytest.raises(credentials.CredentialError):
        credentials.decrypt_secret(tampered, key=KEY)


def test_no_configured_key_refuses_to_store_rather_than_storing_plaintext(monkeypatch):
    """The dangerous failure mode. A missing key must stop the feature, not
    silently downgrade it to writing the password to the database in clear."""
    monkeypatch.delenv(credentials.KEY_ENV_VAR, raising=False)

    with pytest.raises(credentials.CredentialError, match=credentials.KEY_ENV_VAR):
        credentials.encrypt_secret("hunter2")


def test_a_malformed_configured_key_is_rejected(monkeypatch):
    monkeypatch.setenv(credentials.KEY_ENV_VAR, "not-a-valid-fernet-key")

    with pytest.raises(credentials.CredentialError):
        credentials.encrypt_secret("hunter2")


def test_the_configured_key_is_used_when_none_is_passed(monkeypatch):
    monkeypatch.setenv(credentials.KEY_ENV_VAR, KEY)

    token = credentials.encrypt_secret("hunter2")
    assert credentials.decrypt_secret(token) == "hunter2"


def test_an_empty_secret_is_refused():
    """An empty password is never intentional, and storing one would present
    as "credentials configured" while failing every login."""
    with pytest.raises(credentials.CredentialError):
        credentials.encrypt_secret("   ", key=KEY)


def test_generated_keys_are_unique():
    assert credentials.generate_key() != credentials.generate_key()
