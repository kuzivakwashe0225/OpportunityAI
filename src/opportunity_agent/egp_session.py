"""Signing in to eGP as the account owner, with their explicit permission.

The public bulletin board (egp.py) lists tenders. It does not carry the bid
pack - the bid data sheet, technical specifications, pricing schedules and
forms - which is what a bidder actually needs. Those sit behind a registered
supplier login. So the owner supplying their own eGP credentials is the only
way for this system to see them, and this module is what uses them.

Scope and stance, deliberately narrow:

* This acts **as the owner, on their own account, at their request**. It is
  the same thing a password manager or a browser profile does - not access to
  anybody else's data.
* It **reads**. It downloads documents the owner is entitled to download. It
  does not submit a bid; see SOLUTION_DEFINITION.md §17.4.
* It identifies itself honestly in the User-Agent, like the rest of this
  codebase, rather than impersonating a browser.

On the captcha: the login page draws one, but `createCaptcha()` generates the
string in JavaScript, renders it to a canvas, and jQuery Validate compares it
to the typed value **in the browser**. It is never posted to the server - the
form sends only `_method`, `_csrfToken`, `checkCount`, `username`, `password`
and `redirect`. So there is nothing here that defeats a server-side control;
there is no server-side control. Worth stating plainly rather than leaving
someone to assume this module cracks a captcha, because if PRAZ ever makes the
captcha real, this module must start failing rather than start guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from .connector import USER_AGENT
from .egp import BASE_URL

LOGIN_PATH = "/Indexes/login"
LOGIN_POST_PATH = "/users/login"
DOCUMENTS_PATH = "/Tenders/tender_doc_view"

_CSRF_RE = re.compile(r'name="_csrfToken"[^>]*value="([^"]+)"')
_DOC_ROW_RE = re.compile(
    r"<tr[^>]*>\s*<td[^>]*>(.*?)</td>.*?href=\"(/Tenders/downloadTenderDoc/[^\"]+)\"",
    re.S | re.I,
)
# Markers that mean "you are looking at the login form", used to catch an
# expired session that returns 200 with a login page instead of the content.
_LOGIN_MARKERS = ('name="_csrfToken"', 'id="login_box"')


class LoginError(Exception):
    """Any failure to establish or use an authenticated session.

    Never carries the password: this string reaches logs, API responses and
    the browser.
    """


@dataclass
class TenderDocument:
    name: str
    url: str


def parse_csrf_token(html: str) -> str:
    match = _CSRF_RE.search(html)
    if not match:
        raise LoginError(
            "could not find the CSRF token on the eGP login page - the portal's "
            "login form has probably changed"
        )
    return match.group(1)


def parse_tender_documents(html: str) -> list[TenderDocument]:
    documents = []
    for name, href in _DOC_ROW_RE.findall(html):
        clean = " ".join(re.sub(r"<[^>]+>", " ", name).replace("&nbsp;", " ").split())
        if clean:
            documents.append(TenderDocument(name=clean, url=f"{BASE_URL}{href}"))
    return documents


@dataclass
class EgpSession:
    client: httpx.Client
    authenticated: bool = False

    def tender_documents(self, tender_id: str) -> list[TenderDocument]:
        if not self.authenticated:
            # Guarding the worst bug available here: fetching without a session
            # returns the *public* page with a 200, which would then be reported
            # to the owner as "this tender has no documents".
            raise LoginError("not logged in to eGP")

        try:
            response = self.client.get(
                f"{BASE_URL}{DOCUMENTS_PATH}/{tender_id}/{tender_id}",
                headers={"User-Agent": USER_AGENT},
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise LoginError(f"could not read the eGP bid pack: {error}") from error

        if any(marker in response.text for marker in _LOGIN_MARKERS):
            self.authenticated = False
            raise LoginError("the eGP session has expired - log in again")

        return parse_tender_documents(response.text)

    def close(self) -> None:
        self.client.close()


def log_in(username: str, password: str, *, client: httpx.Client | None = None) -> EgpSession:
    """Establish an authenticated eGP session.

    Raises LoginError on anything other than a confirmed success. In
    particular it does *not* return an unauthenticated session on bad
    credentials - a caller that treated one as usable would silently read
    public pages and present them as privileged content.
    """
    owns_client = client is None
    http = client or httpx.Client(timeout=30.0, follow_redirects=False)

    try:
        try:
            page = http.get(f"{BASE_URL}{LOGIN_PATH}", headers={"User-Agent": USER_AGENT})
            page.raise_for_status()
        except httpx.HTTPError as error:
            raise LoginError(f"could not reach the eGP login page: {error}") from error

        token = parse_csrf_token(page.text)

        try:
            response = http.post(
                f"{BASE_URL}{LOGIN_POST_PATH}",
                data={
                    "_method": "POST",
                    "_csrfToken": token,
                    "checkCount": "",
                    "username": username,
                    "password": password,
                    "redirect": "",
                },
                headers={
                    "User-Agent": USER_AGENT,
                    "Referer": f"{BASE_URL}{LOGIN_PATH}",
                },
            )
        except httpx.HTTPError as error:
            # Deliberately does not interpolate the response body or the form
            # data - the password is in that dict.
            raise LoginError(f"eGP login request failed: {error}") from error

        # A CakePHP login that works redirects away from the form. One that
        # fails re-renders the form with an error, at status 200 - so "did we
        # get a 200" is exactly the wrong test.
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location", "")
            if LOGIN_PATH.lower() not in location.lower():
                return EgpSession(client=http, authenticated=True)
            raise LoginError("eGP rejected the credentials")

        if response.status_code >= 400:
            raise LoginError(f"eGP login failed with status {response.status_code}")

        if any(marker in response.text for marker in _LOGIN_MARKERS):
            raise LoginError("eGP rejected the credentials")

        return EgpSession(client=http, authenticated=True)
    except Exception:
        if owns_client:
            http.close()
        raise


def verify_credentials(username: str, password: str) -> tuple[bool, str]:
    """Try the credentials once and report the outcome in words fit for the UI.

    Returns (ok, message). Never raises and never echoes the password, because
    the message is written straight to the database and shown on the page.
    """
    try:
        session = log_in(username, password)
    except LoginError as error:
        return False, str(error)[:400]
    except Exception as error:  # noqa: BLE001 - last-resort guard, see docstring
        return False, f"unexpected error contacting eGP: {type(error).__name__}"
    session.close()
    return True, "eGP accepted these credentials"
