"""Security, checked against the route table rather than against a list.

This system is about to be opened to the public, where the people using it are
strangers to each other. Two of them holding accounts on the same box must not
be able to reach each other's tax clearance certificate, their eGP password,
or the tender they are bidding against.

Every test here derives its cases from `app.routes` at run time. That is the
point: a list written by hand goes stale the moment someone adds an endpoint,
and the endpoint that gets forgotten is exactly the one that leaks. Add a
route to api.py and these tests will start exercising it without being
touched. If a new route needs to be public, it has to be named in
PUBLIC_ROUTES below - a deliberate, reviewable act.
"""

import re

import pytest
from fastapi.testclient import TestClient

from conftest import sign_up
from opportunity_agent import credentials as credentials_module
from opportunity_agent import db as db_module
from opportunity_agent import models_db
from opportunity_agent.api import app

client = TestClient(app)


class _FakeStorage:
    """Object storage in a dict. Uploads must actually succeed here - a helper
    that failed to store a document would make every authorisation test below
    pass for the wrong reason."""

    def __init__(self):
        self.objects = {}

    def bucket_exists(self, bucket_name):
        return True

    def make_bucket(self, bucket_name, location=None, object_lock=False):
        pass

    def put_object(self, bucket_name, object_name, data, length, **kwargs):
        self.objects[(bucket_name, object_name)] = data.read()

    def get_object(self, bucket_name, object_name, **kwargs):
        blob = self.objects[(bucket_name, object_name)]

        class _Resp:
            def read(self_inner):
                return blob

            def close(self_inner):
                pass

            def release_conn(self_inner):
                pass

        return _Resp()

    def remove_object(self, bucket_name, object_name, version_id=None):
        self.objects.pop((bucket_name, object_name), None)


@pytest.fixture(autouse=True)
def storage_and_keys(monkeypatch):
    from opportunity_agent import api as api_module

    monkeypatch.setattr(api_module, "_minio_client", lambda: _FakeStorage())
    monkeypatch.setenv(credentials_module.KEY_ENV_VAR, credentials_module.generate_key())


# Endpoints that are meant to be reachable without signing in. Everything
# else must refuse an anonymous caller.
PUBLIC_ROUTES = {
    ("GET", "/"), ("GET", "/health"), ("GET", "/ui"),
    ("GET", "/robots.txt"), ("GET", "/sitemap.xml"),
    ("POST", "/register"), ("POST", "/login"), ("POST", "/logout"),
    ("POST", "/forgot-password"),
    ("GET", "/profile-types"),
    # FastAPI's own documentation pages.
    ("GET", "/docs"), ("GET", "/docs/oauth2-redirect"), ("GET", "/redoc"),
    ("GET", "/openapi.json"),
}

# A request body that is structurally plausible for whichever endpoint gets
# it, so a 422 cannot be mistaken for a refusal.
SAMPLE_BODY = {
    "email": "intruder@example.com", "password": "hunter2hunter2",
    "current_password": "hunter2hunter2", "new_password": "hunter2hunter2",
    "display_name": "x", "profile_type": "scholarship",
    "fields": {}, "sections": [], "username": "u", "steer": None,
    "text": "I have a BSc in Computer Science.",
}


def _request(caller, method, url):
    """A request that is valid in every respect except whose data it is
    reaching for.

    This matters more than it looks. If the body is wrong the endpoint answers
    422 - and a 422 proves nothing about whether the ownership check would
    have run. Every case has to get far enough in to be refused on ownership
    alone.
    """
    if url.endswith("/documents") and method == "POST":
        return caller.post(
            url,
            files={"file": ("cv.txt", b"not mine", "text/plain")},
            data={"doc_type": "cv"},
        )
    return caller.request(method, url, json=SAMPLE_BODY)


def _routes():
    for route in app.routes:
        for method in sorted(getattr(route, "methods", None) or []):
            if method in ("HEAD", "OPTIONS"):
                continue
            yield method, route.path


def _fill(path, **values):
    """Turn "/profiles/{profile_id}/documents/{document_id}" into a real URL."""
    def replace(match):
        return str(values.get(match.group(1), "00000000-0000-0000-0000-000000000000"))
    return re.sub(r"\{([a-z_]+)\}", replace, path)


PROTECTED = sorted(set(_routes()) - PUBLIC_ROUTES)
PROFILE_SCOPED = [r for r in PROTECTED if "{profile_id}" in r[1]]


# --------------------------------------------------------------------------
# Authentication: every endpoint, with no session at all
# --------------------------------------------------------------------------

@pytest.mark.parametrize("method,path", PROTECTED, ids=lambda v: str(v))
def test_no_protected_endpoint_answers_an_anonymous_caller(method, path):
    anonymous = TestClient(app)

    response = _request(anonymous, method, _fill(path))

    assert response.status_code == 401, (
        f"{method} {path} answered {response.status_code} without a session"
    )


def test_the_public_list_is_not_quietly_growing():
    """A guard on the guard.

    Someone adding a public route has to come here and say so. Without this,
    the exemption list above is a place things can be slipped into.
    """
    assert len(PUBLIC_ROUTES) == 14
    assert len(PROTECTED) == 37


# --------------------------------------------------------------------------
# Authorisation: another account's data, with a perfectly valid session
# --------------------------------------------------------------------------

def _account_with_everything(email):
    """One account holding a profile, a document and an opportunity."""
    sub = TestClient(app)
    sign_up(sub, email=email)
    profile = sub.post(
        "/profiles", json={"profile_type": "tender", "display_name": "Mine"}
    ).json()
    sub.put(f"/profiles/{profile['id']}", json={"fields": {"name": "Private Ltd"}})

    document = sub.post(
        f"/profiles/{profile['id']}/documents",
        files={"file": ("secret.txt", b"my tax clearance", "text/plain")},
        data={"doc_type": "tax_clearance"},
    ).json()

    session = db_module.SessionLocal()
    row = models_db.StoredOpportunity(
        profile_id=profile["id"], canonical_url="https://example.org/private-tender",
        payload={"title": "A tender I am bidding for", "evidence": ["secret plans"]},
        match_status="eligible", match_score=10, match_reasons={},
        package={"status": "ready", "sections": [{"title": "Price", "body": "USD 40,000"}]},
    )
    session.add(row)
    session.commit()
    opportunity_id = row.id
    session.close()

    return sub, profile["id"], document["id"], opportunity_id


@pytest.mark.parametrize("method,path", PROFILE_SCOPED, ids=lambda v: str(v))
def test_a_signed_in_stranger_cannot_reach_another_accounts_profile(method, path):
    """The multi-tenant test that matters once this is public.

    A real session, a real profile id - just not theirs. Every one of these
    must be a 404: not a 403, which would confirm the id exists.
    """
    _, victim_profile, victim_document, victim_opportunity = _account_with_everything(
        "victim@example.com"
    )

    intruder = TestClient(app)
    sign_up(intruder, email="intruder@example.com")

    url = _fill(path, profile_id=victim_profile, document_id=victim_document,
                opportunity_id=victim_opportunity, portal="egp")
    response = _request(intruder, method, url)

    assert response.status_code == 404, (
        f"{method} {path} answered {response.status_code} for another account's data"
    )


def test_an_intruder_is_told_nothing_about_what_exists():
    """404 and not 403, so the reply cannot be used to enumerate ids."""
    _, victim_profile, _, _ = _account_with_everything("victim@example.com")
    intruder = TestClient(app)
    sign_up(intruder, email="intruder@example.com")

    real = intruder.get(f"/profiles/{victim_profile}")
    invented = intruder.get("/profiles/00000000-0000-0000-0000-000000000000")

    assert real.status_code == invented.status_code == 404
    assert real.json() == invented.json()


def test_one_accounts_opportunities_never_appear_in_anothers_list():
    victim, victim_profile, _, _ = _account_with_everything("victim@example.com")
    intruder = TestClient(app)
    sign_up(intruder, email="intruder@example.com")
    mine = intruder.post(
        "/profiles", json={"profile_type": "tender", "display_name": "Mine too"}
    ).json()

    assert intruder.get(f"/profiles/{mine['id']}/opportunities").json() == []
    assert intruder.get("/profiles").json() != victim.get("/profiles").json()
    assert len(intruder.get("/profiles").json()) == 1


def test_notifications_are_scoped_to_the_account_that_owns_them():
    """Notifications are the one list not fetched under a profile id, so the
    scoping has to come from the session instead."""
    victim, victim_profile, _, _ = _account_with_everything("victim@example.com")
    session = db_module.SessionLocal()
    owner = session.get(models_db.Profile, victim_profile).account_id
    session.add(models_db.Notification(
        account_id=owner, profile_id=victim_profile, kind="documents_needed",
        message="Upload your CR14 for the Bulawayo tender",
    ))
    session.commit()
    session.close()

    intruder = TestClient(app)
    sign_up(intruder, email="intruder@example.com")

    assert victim.get("/notifications").json() != []
    assert intruder.get("/notifications").json() == []


# --------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------

def test_a_portal_password_is_never_returned_by_the_api():
    """It is a real government procurement password. It goes in and it does
    not come back out - not to its owner either, because a screen that can
    display it is a screen that can be shoulder-surfed or cached."""
    client.cookies.clear()
    sign_up(client)
    profile = client.post(
        "/profiles", json={"profile_type": "tender", "display_name": "T"}
    ).json()

    client.put(f"/profiles/{profile['id']}/credentials/egp",
               json={"username": "isaiah@example.com", "password": "eGP-real-password"})
    listed = client.get(f"/profiles/{profile['id']}/credentials").text
    whole_profile = client.get(f"/profiles/{profile['id']}").text

    assert "eGP-real-password" not in listed
    assert "eGP-real-password" not in whole_profile
    assert "isaiah@example.com" in listed  # the username is not the secret


def test_a_portal_password_is_not_readable_in_the_database():
    """The failure mode this guards: a database backup becoming a credential
    dump. Encrypted at rest with a key that lives in the environment."""
    client.cookies.clear()
    sign_up(client)
    profile = client.post(
        "/profiles", json={"profile_type": "tender", "display_name": "T"}
    ).json()
    client.put(f"/profiles/{profile['id']}/credentials/egp",
               json={"username": "u", "password": "eGP-real-password"})

    session = db_module.SessionLocal()
    stored = session.query(models_db.PortalCredential).one()
    blob = stored.secret_ciphertext
    session.close()

    assert "eGP-real-password" not in str(blob)
    assert credentials_module.decrypt_secret(blob) == "eGP-real-password"


def test_an_account_password_is_not_readable_in_the_database():
    client.cookies.clear()
    password = sign_up(client, email="hashme@example.com")

    session = db_module.SessionLocal()
    account = session.query(models_db.Account).filter_by(email="hashme@example.com").one()
    stored = account.password_hash
    session.close()

    assert password not in stored
    assert stored.startswith("$2")  # bcrypt, not a hex digest and not the password


def test_the_session_cookie_cannot_be_read_by_script():
    """The agent scrapes opportunity titles off strangers' websites and the
    page renders them. If one of those ever carries working script, it must
    not be able to walk off with the session."""
    fresh = TestClient(app)
    password = sign_up(fresh, email="cookies@example.com")

    login = fresh.post("/login", json={"email": "cookies@example.com",
                                       "password": password})
    session_cookie = [c for c in login.headers.get_list("set-cookie")
                      if c.startswith("session=")]

    assert session_cookie, "no session cookie issued"
    assert "HttpOnly" in session_cookie[0]
    assert "SameSite=lax" in session_cookie[0]


def test_a_wrong_password_is_refused():
    fresh = TestClient(app)
    sign_up(fresh, email="wrongpw@example.com")
    fresh.cookies.clear()

    response = fresh.post("/login", json={"email": "wrongpw@example.com",
                                          "password": "not-the-password"})

    assert response.status_code == 401
    assert fresh.get("/me").status_code == 401


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------

def test_a_tampered_session_cookie_is_refused():
    fresh = TestClient(app)
    sign_up(fresh, email="tamper@example.com")
    good = fresh.cookies.get("session")

    fresh.cookies.set("session", good[:-4] + "aaaa")

    assert fresh.get("/me").status_code == 401


def test_a_session_from_a_different_secret_is_refused(monkeypatch):
    """Someone who guesses the token format but not the key gets nothing."""
    import jwt

    forged = jwt.encode({"sub": "some-account-id"}, "not-the-real-secret", algorithm="HS256")
    fresh = TestClient(app)
    fresh.cookies.set("session", forged)

    assert fresh.get("/me").status_code == 401


def test_logging_out_actually_ends_the_session():
    fresh = TestClient(app)
    sign_up(fresh, email="bye@example.com")
    assert fresh.get("/me").status_code == 200

    fresh.post("/logout")

    assert fresh.get("/me").status_code == 401


# --------------------------------------------------------------------------
# Input handling
# --------------------------------------------------------------------------

def test_an_id_that_is_a_path_traversal_attempt_is_just_a_miss():
    client.cookies.clear()
    sign_up(client)

    for nasty in ("../../etc/passwd", "..%2f..%2fetc%2fpasswd", "'; DROP TABLE profiles;--"):
        response = client.get(f"/profiles/{nasty}")
        assert response.status_code in (404, 422), nasty

    # And the table is still there.
    assert client.get("/profiles").status_code == 200


def test_an_executable_upload_is_refused_by_type():
    client.cookies.clear()
    sign_up(client)
    profile = client.post(
        "/profiles", json={"profile_type": "scholarship", "display_name": "S"}
    ).json()

    response = client.post(
        f"/profiles/{profile['id']}/documents",
        files={"file": ("payload.exe", b"MZ\x90\x00", "application/x-msdownload")},
        data={"doc_type": "cv"},
    )

    assert response.status_code == 415


def test_an_oversized_upload_is_refused_before_it_is_stored():
    client.cookies.clear()
    sign_up(client)
    profile = client.post(
        "/profiles", json={"profile_type": "scholarship", "display_name": "S"}
    ).json()

    response = client.post(
        f"/profiles/{profile['id']}/documents",
        files={"file": ("big.pdf", b"x" * (11 * 1024 * 1024), "application/pdf")},
        data={"doc_type": "cv"},
    )

    assert response.status_code == 413
    assert client.get(f"/profiles/{profile['id']}/documents").json() == []


def test_a_profile_type_that_does_not_exist_is_refused():
    client.cookies.clear()
    sign_up(client)

    response = client.post(
        "/profiles", json={"profile_type": "../../admin", "display_name": "X"}
    )

    assert response.status_code in (400, 422)


# --------------------------------------------------------------------------
# Known gaps, pinned rather than described
# --------------------------------------------------------------------------
# The three tests below assert what the system *currently does*, not what it
# ought to do, and each one says so. They exist because a finding written only
# in a report goes stale silently, whereas a test that starts failing is the
# system telling you the gap was closed. When one of these is fixed, invert
# the assertion rather than deleting it.

def test_guessing_passwords_stops_being_free():
    """Was a pinned gap: twelve wrong passwords in 3.7 seconds, no lockout.

    Now the door closes, and the answer changes from 401 to 429 carrying a
    Retry-After the caller can actually read.
    """
    fresh = TestClient(app)
    sign_up(fresh, email="guessme@example.com")
    fresh.cookies.clear()

    codes = [
        fresh.post("/login", json={"email": "guessme@example.com",
                                   "password": f"guess-{n}"}).status_code
        for n in range(12)
    ]

    assert codes[0] == 401, "the first wrong password is just wrong"
    assert 429 in codes, "twelve wrong passwords were all accepted"
    assert codes[-1] == 429, "and it stays shut"

    locked = fresh.post("/login", json={"email": "guessme@example.com",
                                        "password": "guess-again"})
    assert int(locked.headers["Retry-After"]) > 0


def test_the_lockout_does_not_punish_someone_who_then_remembers():
    """A person who mistypes twice and then gets it right is not an attacker,
    and must not meet a lockout a minute later."""
    fresh = TestClient(app)
    password = sign_up(fresh, email="fumble@example.com")
    fresh.cookies.clear()

    for _ in range(3):
        fresh.post("/login", json={"email": "fumble@example.com", "password": "nope"})
    assert fresh.post("/login", json={"email": "fumble@example.com",
                                      "password": password}).status_code == 200

    fresh.cookies.clear()
    for _ in range(3):
        fresh.post("/login", json={"email": "fumble@example.com", "password": "nope"})

    assert fresh.post("/login", json={"email": "fumble@example.com",
                                      "password": password}).status_code == 200


def test_locking_one_account_does_not_lock_everyone_else_out():
    """The per-address limit is deliberately looser than the per-account one.
    A campus or an office is one address to us, and one person forgetting
    their password there must not shut out the rest."""
    attacker = TestClient(app)
    sign_up(attacker, email="target@example.com")
    victim = TestClient(app)
    bystander_password = sign_up(victim, email="bystander@example.com")
    attacker.cookies.clear()
    victim.cookies.clear()

    for n in range(12):
        attacker.post("/login", json={"email": "target@example.com",
                                      "password": f"guess-{n}"})

    assert victim.post("/login", json={"email": "bystander@example.com",
                                       "password": bystander_password}).status_code == 200


def test_registering_gives_nothing_away_about_who_already_has_an_account():
    """Was a pinned gap: 409 for a known address, 201 for an unknown one, so
    anyone could test a list of emails against a system holding tax
    certificates and procurement logins."""
    fresh = TestClient(app)
    sign_up(fresh, email="taken@example.com")

    known = fresh.post("/register", json={"email": "taken@example.com"})
    unknown = fresh.post("/register", json={"email": "free@example.com"})

    assert known.status_code == unknown.status_code == 201
    assert known.json() == {"status": "sent", "email": "taken@example.com"}
    assert unknown.json() == {"status": "sent", "email": "free@example.com"}


def test_password_reset_gives_nothing_away_either():
    """The same enumeration on the endpoint that needs no session."""
    fresh = TestClient(app)
    sign_up(fresh, email="resetme@example.com")

    known = fresh.post("/forgot-password", json={"email": "resetme@example.com"})
    unknown = fresh.post("/forgot-password", json={"email": "nobody@example.com"})

    assert known.status_code == unknown.status_code == 200
    assert known.json()["status"] == unknown.json()["status"] == "sent"


def test_mailing_the_same_address_over_and_over_is_refused():
    """Otherwise this endpoint is a way to have us post mail to a stranger
    repeatedly, at the cost of our own sending reputation."""
    fresh = TestClient(app)

    codes = [
        fresh.post("/forgot-password", json={"email": "pester@example.com"}).status_code
        for _ in range(8)
    ]

    assert 429 in codes


def test_the_session_cookie_is_only_marked_secure_when_told_to():
    """GAP for public deployment: SESSION_COOKIE_SECURE defaults to off, and
    the server is currently reached over plain HTTP on port 8000. The session
    token therefore travels in clear. This is correct for local development
    and wrong the moment strangers sign in over the internet - the fix is TLS
    plus SESSION_COOKIE_SECURE=1, not a code change here."""
    from opportunity_agent import api as api_module

    assert api_module.COOKIES_SECURE is False


# --------------------------------------------------------------------------
# The front end renders text scraped from strangers' websites
# --------------------------------------------------------------------------

def test_every_rendered_value_in_the_page_goes_through_the_escaper():
    """Opportunity titles come off other people's sites and are written
    straight into the DOM. One that carried markup must not become script.

    Checked as a property of the source rather than in a browser: every
    innerHTML assignment that builds a string must escape what it
    interpolates. A new one that forgets fails here.
    """
    from pathlib import Path

    page = Path("src/opportunity_agent/web/index.html").read_text(encoding="utf-8")
    offenders = [
        line.strip()
        for line in page.splitlines()
        if "innerHTML" in line and "+" in line and "esc(" not in line
        and "draftBody(" not in line  # builds its own escaped string
    ]

    assert offenders == [], offenders


# --------------------------------------------------------------------------
# Headers every response carries
# --------------------------------------------------------------------------

def test_uploaded_documents_cannot_be_reinterpreted_as_pages():
    """Without nosniff, a browser may decide a document the owner uploaded is
    really HTML and run it - on our origin, with the session cookie
    attached."""
    assert client.get("/ui").headers["X-Content-Type-Options"] == "nosniff"


def test_the_app_cannot_be_framed_and_clicked_through():
    assert client.get("/ui").headers["X-Frame-Options"] == "DENY"


def test_the_page_someone_came_from_does_not_travel_to_the_sites_they_open():
    """Every opportunity is a link out to somebody else's site."""
    assert client.get("/ui").headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


def test_nothing_here_may_ask_for_a_camera_or_a_location():
    policy = client.get("/ui").headers["Permissions-Policy"]

    assert "camera=()" in policy
    assert "geolocation=()" in policy


def test_hsts_is_off_unless_the_deployment_turns_it_on(monkeypatch):
    """Sending HSTS from a deployment that cannot do HTTPS bricks that
    hostname in every browser that saw it, for the length of the max-age. So
    it is opt-in, set alongside TLS rather than shipped on by default."""
    from opportunity_agent import api as api_module

    assert api_module.HSTS_ENABLED is False
    assert "Strict-Transport-Security" not in client.get("/ui").headers


def test_the_headers_are_on_api_answers_too_not_just_the_page():
    """Middleware, not a decorator on one route - so a route added tomorrow
    is covered without anyone remembering to cover it."""
    headers = client.get("/health").headers

    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
