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

# The token's lifetime is the *idle* window, not a fixed session length. The
# client re-issues it whenever the owner does something meaningful, so this is
# how long a session survives with nobody doing anything.
#
# Short on purpose, and enforced here rather than in the browser: an idle
# timeout that only exists as a countdown in JavaScript is decoration - close
# the tab, reopen it, and the old token still works. Expiring the token is
# what actually ends the session, whatever the page does or does not draw.
def _idle_ttl() -> timedelta:
    raw = os.getenv("SESSION_IDLE_MINUTES", "30")
    try:
        minutes = int(raw)
    except ValueError:
        minutes = 30
    # A zero or negative window would mean "expire immediately", which locks
    # everyone out; a misconfiguration should not be a denial of service.
    return timedelta(minutes=minutes if minutes > 0 else 30)


TOKEN_TTL = _idle_ttl()


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
    payload = {"sub": account_id, "iat": issued_at, "exp": issued_at + _idle_ttl()}
    return jwt.encode(payload, secret_key or _secret_key(), algorithm=ALGORITHM)


def decode_session_token(token: str, *, secret_key: str | None = None) -> str:
    """Return the account id encoded in the token.

    Raises jwt.PyJWTError (or a subclass, e.g. ExpiredSignatureError,
    InvalidSignatureError) if the token is invalid, tampered, or expired.
    """
    payload = jwt.decode(token, secret_key or _secret_key(), algorithms=[ALGORITHM])
    return payload["sub"]


def session_expires_at(now: datetime | None = None) -> datetime:
    """When a token issued right now would stop working.

    Handed to the browser so it can warn before the session dies rather than
    discovering it on the next click. It is only a *copy* of the deadline -
    the token's own `exp` is the one that decides.
    """
    return (now or datetime.now(timezone.utc)) + _idle_ttl()
