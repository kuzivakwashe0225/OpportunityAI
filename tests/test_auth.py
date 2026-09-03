from datetime import datetime, timedelta, timezone

import jwt
import pytest

from opportunity_agent.auth import (
    create_session_token,
    decode_session_token,
    hash_password,
    verify_password,
)


def test_hashed_password_is_not_the_plaintext():
    hashed = hash_password("correct horse battery staple")

    assert hashed != "correct horse battery staple"
    assert hashed.startswith("$2b$") or hashed.startswith("$2a$")


def test_verify_password_accepts_the_correct_password():
    hashed = hash_password("correct horse battery staple")

    assert verify_password("correct horse battery staple", hashed) is True


def test_verify_password_rejects_the_wrong_password():
    hashed = hash_password("correct horse battery staple")

    assert verify_password("wrong password", hashed) is False


def test_two_hashes_of_the_same_password_differ():
    # bcrypt salts each hash - equal hashes would mean a constant/missing salt
    assert hash_password("same password") != hash_password("same password")


def test_session_token_round_trips_the_account_id():
    token = create_session_token("account-123", secret_key="test-secret")

    assert decode_session_token(token, secret_key="test-secret") == "account-123"


def test_session_token_rejects_the_wrong_secret():
    token = create_session_token("account-123", secret_key="test-secret")

    with pytest.raises(jwt.PyJWTError):
        decode_session_token(token, secret_key="a-different-secret")


def test_session_token_rejects_tampering():
    token = create_session_token("account-123", secret_key="test-secret")
    header, payload, signature = token.split(".")
    # Flip a character in the middle of the signature, not the edge - the
    # outermost base64url character of a segment can sit on a padding
    # boundary where more than one character decodes to the same bytes.
    middle = len(signature) // 2
    flipped_char = "A" if signature[middle] != "A" else "B"
    tampered_signature = signature[:middle] + flipped_char + signature[middle + 1:]
    tampered = f"{header}.{payload}.{tampered_signature}"

    with pytest.raises(jwt.PyJWTError):
        decode_session_token(tampered, secret_key="test-secret")


def test_expired_session_token_is_rejected():
    issued = datetime.now(timezone.utc) - timedelta(days=30)
    token = create_session_token("account-123", secret_key="test-secret", now=issued)

    with pytest.raises(jwt.ExpiredSignatureError):
        decode_session_token(token, secret_key="test-secret")


def test_missing_secret_key_env_var_raises_a_clear_error(monkeypatch):
    monkeypatch.delenv("SESSION_SECRET_KEY", raising=False)

    with pytest.raises(RuntimeError, match="SESSION_SECRET_KEY"):
        create_session_token("account-123")
