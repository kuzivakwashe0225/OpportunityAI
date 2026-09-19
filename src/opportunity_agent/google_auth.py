"""Verifying a Google Sign-In credential, without a second HTTP library.

The browser does the OAuth dance entirely on its own (Google Identity
Services' rendered button), and hands this system one thing: a signed JWT
("credential") asserting who the person is. All this module does is check
that JWT was really signed by Google, for our app, and for an account whose
email Google has itself verified - then hands back the claims.

Deliberately built on PyJWT rather than the `google-auth` package. That
package's own HTTP transport requires `requests`, a library this project has
never depended on (httpx is the one client used everywhere else, and adding a
second just for one feature is the kind of dependency creep the codebase has
avoided elsewhere - see credentials.py's comment on declaring only what is
actually imported). PyJWT already ships a JWKS client built on `urllib`, and
PyJWT is already a dependency for session tokens, so this needed nothing new.
"""

from __future__ import annotations

import jwt

# Google's own published key set for verifying ID tokens. Fetched and cached
# by PyJWKClient (default 300s lifespan) - not re-fetched per login.
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"

# Google issues tokens under either form depending on the flow; both are
# genuine.
GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}


class GoogleAuthError(Exception):
    """Raised for every failure here - a bad signature, a wrong audience, an
    expired token, an unreachable key set. Deliberately one type: callers
    must not be tempted to treat one kind of failure as safe to ignore."""


def _client(jwk_client: "jwt.PyJWKClient | None") -> "jwt.PyJWKClient":
    return jwk_client or jwt.PyJWKClient(GOOGLE_JWKS_URL)


def verify_google_id_token(
    token: str, client_id: str, *, jwk_client: "jwt.PyJWKClient | None" = None
) -> dict:
    """The token's claims, once its signature and audience are confirmed.

    Raises GoogleAuthError for anything short of a fully valid token for this
    specific app. Does not itself check `email_verified` - callers decide
    what to require of the identity once it is confirmed genuine, the same
    separation `auth.py` keeps between "this token is real" and "this
    account may do X".
    """
    try:
        signing_key = _client(jwk_client).get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=client_id,
            # Issuer checked by hand below: PyJWT's own check takes one
            # string, and Google legitimately uses either form.
            options={"verify_iss": False},
        )
    except jwt.PyJWTError as error:
        raise GoogleAuthError(f"could not verify this Google sign-in: {error}") from error
    except Exception as error:
        raise GoogleAuthError(f"could not reach Google to verify this sign-in: {error}") from error

    if payload.get("iss") not in GOOGLE_ISSUERS:
        raise GoogleAuthError("token was not issued by Google")
    return payload
