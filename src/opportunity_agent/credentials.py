"""Encryption for portal passwords the owner has entrusted to this system.

These are real credentials to a real government procurement account. Three
rules, all of them enforced here rather than left to whoever calls this next:

1. **Never stored in clear.** Fernet (AES-128-CBC with an HMAC) with a key
   that lives in the environment, never in the database and never in git.
2. **A missing key stops the feature.** It does not fall back to storing the
   password in plaintext. That is the failure mode that silently turns a
   database backup into a credential dump, and it is the one worth being
   inflexible about.
3. **Decryption fails loudly.** A wrong key or a tampered value raises rather
   than returning something that might be mistaken for a password.

The key is per-deployment. Rotating it makes every stored credential
undecryptable - which is a deliberate property: it is how the owner revokes
everything at once if this machine is ever suspect. There is no recovery path
and there should not be one; the owner re-enters the password.

Two further protections live outside this module because they belong to their
own layers: the API never returns a stored secret (see api.py's
CredentialOut), and the secret is kept in its own table rather than in
Profile.fields, which is serialised to the browser wholesale.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

KEY_ENV_VAR = "CREDENTIALS_SECRET_KEY"


class CredentialError(Exception):
    """Raised for every failure here - a missing key, a bad key, a tampered
    value. Deliberately one type: callers must not be tempted to treat
    "couldn't decrypt" as recoverable and carry on with a partial value."""


def generate_key() -> str:
    """A new deployment key. Print it once, put it in the environment, keep it
    out of git."""
    return Fernet.generate_key().decode("ascii")


def _cipher(key: str | None) -> Fernet:
    resolved = key or os.getenv(KEY_ENV_VAR)
    if not resolved:
        raise CredentialError(
            f"{KEY_ENV_VAR} is not set. Portal credentials are never stored "
            f"unencrypted, so this feature stays off until a key is configured. "
            f"Generate one with: python -c \"from opportunity_agent.credentials "
            f"import generate_key; print(generate_key())\""
        )
    try:
        return Fernet(resolved.encode("ascii") if isinstance(resolved, str) else resolved)
    except (ValueError, TypeError) as error:
        raise CredentialError(f"{KEY_ENV_VAR} is not a valid Fernet key") from error


def encrypt_secret(secret: str, *, key: str | None = None) -> str:
    if not secret or not secret.strip():
        # An empty password is never deliberate, and storing one would show up
        # in the UI as "credentials configured" while failing every login.
        raise CredentialError("refusing to store an empty secret")
    return _cipher(key).encrypt(secret.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str, *, key: str | None = None) -> str:
    try:
        return _cipher(key).decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError) as error:
        raise CredentialError(
            "stored credential could not be decrypted - the encryption key has "
            "probably changed, and the password needs entering again"
        ) from error
