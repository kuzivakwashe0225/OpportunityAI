"""Password hashing and session tokens for account authentication.

See SOLUTION_DEFINITION.md §14: password auth, not magic-link - avoids a
second new external dependency (SMTP) on top of the document/auth work
already in this phase. SESSION_SECRET_KEY must be set wherever this runs
(no fallback default - a guessable default secret would make every session
token forgeable).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

ALGORITHM = "HS256"
TOKEN_TTL = timedelta(days=14)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def _secret_key() -> str:
    key = os.getenv("SESSION_SECRET_KEY")
    if not key:
        raise RuntimeError("SESSION_SECRET_KEY is not configured")
    return key


def create_session_token(
    account_id: str,
    *,
    secret_key: str | None = None,
    now: datetime | None = None,
) -> str:
    issued_at = now or datetime.now(timezone.utc)
    payload = {"sub": account_id, "iat": issued_at, "exp": issued_at + TOKEN_TTL}
    return jwt.encode(payload, secret_key or _secret_key(), algorithm=ALGORITHM)


def decode_session_token(token: str, *, secret_key: str | None = None) -> str:
    """Return the account id encoded in the token.

    Raises jwt.PyJWTError (or a subclass, e.g. ExpiredSignatureError,
    InvalidSignatureError) if the token is invalid, tampered, or expired.
    """
    payload = jwt.decode(token, secret_key or _secret_key(), algorithms=[ALGORITHM])
    return payload["sub"]
