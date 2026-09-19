import os
import secrets
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import jwt
from dotenv import load_dotenv
from fastapi import BackgroundTasks, Cookie, Depends, FastAPI, File, Form, Request, Response, UploadFile
from fastapi import HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session

load_dotenv()

from . import auth as auth_module
from . import db as db_module
from . import credentials as credentials_module
from . import mailer
from . import documents as documents_module
from . import models_db
from . import pipeline as pipeline_module
from . import profile_schema
from .connector import fetch_public_page, html_to_text
from .document_text import extract_text
from . import egp_session
from . import application_spec as application_spec_module
from . import extraction_llm as extraction_llm_module
from . import section_drafting as section_drafting_module
from . import evidence as evidence_module
from . import application_form as application_form_module
from . import throttle as throttle_module
from . import google_auth as google_auth_module
from .extraction_llm import ExtractedFacts, extract_facts_from_text

app = FastAPI(title="OpportunityAI")
_UI_PAGE = (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
db_module.init_db()

# The logo and PWA icons - see web/assets/README.md for where they came from
# and why the icon set is a tight crop of just the mark, not the wordmark.
# Mounted under /assets rather than embedded as data URIs: a favicon and PWA
# icons are referenced by URL by the browser itself (manifest.json, <link
# rel="icon">), not fetched by our own JS, so they need a real path to sit at.
app.mount(
    "/assets",
    StaticFiles(directory=Path(__file__).parent / "web" / "assets"),
    name="assets",
)

# Set once TLS is terminated in front of this (Caddy does, on
# opportunityai.meshcloud.co.zw). Off by default so local development over
# http://localhost still works - a Secure cookie is simply never sent there,
# which would lock a developer out of their own machine with no error to read.
HSTS_ENABLED = os.getenv("HSTS_ENABLED", "").lower() in ("1", "true", "yes")

# Google Identity Services needs only this to verify a credential - no
# secret, because the browser never hands this server anything but a signed
# JWT it can check for itself (google_auth.py). Unset is a supported state:
# /auth/config reports it as absent and the frontend shows the button as not
# configured rather than a broken one, exactly the way SESSION_COOKIE_SECURE
# and CREDENTIALS_SECRET_KEY already degrade rather than crash the app.
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Headers every response carries, whatever produced it.

    Each one closes a specific hole rather than being here for a checklist:

    nosniff       - a document the owner uploaded is served back by its
                    declared type or not at all. Without it a browser may
                    decide an uploaded .txt is really HTML and run it, on our
                    origin, with the session cookie attached.
    frame-ancestors - nobody may put this page in an iframe and collect
                    clicks meant for it. DENY rather than SAMEORIGIN: nothing
                    here frames itself.
    referrer      - opportunity URLs are visited by the user from our pages;
                    without this, the full path they came from travels to
                    whichever site they open next.
    permissions   - this app has no use for a camera, a microphone or a
                    location, and saying so means a compromised script cannot
                    ask for one either.
    HSTS          - only once TLS is actually in front, and only when
                    switched on deliberately: sending it from a deployment
                    that cannot do HTTPS bricks that hostname in every
                    browser that saw it, for the length of the max-age.
    """
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=(), interest-cohort=()"
    )
    if HSTS_ENABLED:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


SESSION_COOKIE = "session"
TEMP_PASSWORD_TTL = timedelta(days=7)
MIN_PASSWORD_LENGTH = 10
# The one canonical public URL - used for SEO's canonical/OG tags and the
# emailed password link alike, so both always point at the same address.
APP_URL = os.getenv("APP_URL", "https://opportunityai.meshcloud.co.zw/ui")
PUBLIC_ORIGIN = APP_URL.rsplit("/ui", 1)[0]

# Injection point so tests never open a real SMTP connection.
_send_mail = mailer.send
DOCUMENTS_BUCKET = "opportunityai-documents"
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024  # 10MB
_SUPPORTED_DOCUMENT_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
}
LIST_PROFILE_FIELDS = ("work_history", "certificates")  # append-not-overwrite on extraction
SCALAR_PROFILE_FIELDS = ("study_level", "field")  # fill-if-empty on extraction


def _minio_client():
    return documents_module.make_client(
        endpoint=os.environ["MINIO_ENDPOINT"],
        access_key=os.environ["MINIO_ACCESS_KEY"],
        secret_key=os.environ["MINIO_SECRET_KEY"],
    )


def _assist_profile_fields(text: str, field_specs) -> dict:
    """Suggest field values from free-typed notes. Injection point for tests."""
    return extraction_llm_module.extract_profile_fields(
        text,
        field_specs=field_specs,
        base_url=os.getenv("OLLAMA_URL", "http://localhost:11434"),
        model=os.getenv("OLLAMA_MODEL", "qwen2.5:0.5b"),
    )


def _extract_facts_from_document(content: bytes, content_type: str) -> ExtractedFacts:
    text = extract_text(content, content_type)
    return extract_facts_from_text(
        text,
        base_url=os.getenv("OLLAMA_URL", "http://localhost:11434"),
        model=os.getenv("OLLAMA_MODEL", "qwen2.5:0.5b"),
    )


class RegisterRequest(BaseModel):
    # No password: the user does not choose one at registration. The system
    # generates it and emails it to them, which is also how it confirms they
    # actually control the address.
    email: str


class LoginRequest(BaseModel):
    email: str
    password: str


class AccountOut(BaseModel):
    id: str
    email: str
    # Lets the frontend send a first-time user straight to "choose a password"
    # instead of leaving them on one that arrived in an email in clear.
    must_change_password: bool = False
    # Shown on the account screen. There is deliberately no password field of
    # any kind here: passwords are bcrypt hashes (auth.py), so the system
    # cannot display one, and a system that could display yours could display
    # everyone's. "When did I last change it" is the honest answer to "let me
    # see my password", and it is the one that actually helps.
    password_set_at: datetime | None = None
    temp_password_expires_at: datetime | None = None
    created_at: datetime | None = None

    model_config = {"from_attributes": True}


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


def get_db_session() -> Session:
    session = db_module.SessionLocal()
    try:
        yield session
    finally:
        session.close()


def get_current_account(
    session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    db: Session = Depends(get_db_session),
) -> models_db.Account:
    """Auth gate for the existing single-tenant endpoints below.

    Deliberately not full multi-tenancy yet (SOLUTION_DEFINITION.md §14): any
    authenticated account reaches the same shared `store`. Registration is
    capped at one account (see /register) specifically so that limitation
    can't become a real data-leak between two different people's accounts
    before the pipeline migration lands.
    """
    if session is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    try:
        account_id = auth_module.decode_session_token(session)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="invalid or expired session")
    account = db.get(models_db.Account, account_id)
    if account is None:
        raise HTTPException(status_code=401, detail="account not found")
    return account


# Served over TLS in production, so the session cookie should refuse to travel
# in clear. Off by default because local development is plain HTTP and a
# `secure` cookie there simply never arrives, which looks exactly like a
# broken login.
COOKIES_SECURE = os.getenv("SESSION_COOKIE_SECURE", "").lower() in ("1", "true", "yes")
# Readable by JavaScript on purpose, and it carries no secret - only the
# deadline. The token itself stays httponly. Without this the page has no way
# to know, after a refresh, how long the session it already holds has left.
SESSION_EXPIRY_COOKIE = "oa_session_expires"


def _set_session_cookie(response: Response, account_id: str) -> str:
    token = auth_module.create_session_token(account_id)
    expires_at = auth_module.session_expires_at()
    max_age = int(auth_module.TOKEN_TTL.total_seconds())
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="lax",
        secure=COOKIES_SECURE, max_age=max_age,
    )
    response.set_cookie(
        SESSION_EXPIRY_COOKIE, expires_at.isoformat(), httponly=False,
        samesite="lax", secure=COOKIES_SECURE, max_age=max_age,
    )
    return expires_at.isoformat()


@app.get("/auth/config")
def auth_config() -> dict[str, str | None]:
    """What the sign-in page needs to know before it renders a single button.

    Public on purpose - it reveals nothing but whether a feature is turned on,
    the same as a login page's own "forgot password?" link being visible to
    someone who isn't signed in yet.
    """
    return {"google_client_id": GOOGLE_CLIENT_ID or None}


class GoogleLoginRequest(BaseModel):
    # Google Identity Services' own field name for the signed JWT it hands
    # back to the page - kept as-is rather than renamed, so the frontend can
    # forward the object it received without reshaping it.
    credential: str


# Swappable in tests, the same pattern as _send_mail: nothing in the suite may
# reach Google's real key set or hold a real Google account.
_verify_google_id_token = google_auth_module.verify_google_id_token


@app.post("/login/google", response_model=AccountOut)
def login_with_google(
    payload: GoogleLoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db_session),
) -> models_db.Account:
    """Sign in - or, on a first visit, sign up - with a Google account.

    There is no separate "register with Google" endpoint: the browser proves
    who someone is the same way whether this is their first visit or their
    hundredth, so the only question worth asking server-side is "have we seen
    this person before", not "did they mean to sign up or log in".
    """
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=503, detail="Google sign-in is not configured")

    address = throttle_module.client_address(request)
    address_key = f"addr:{address}"
    wait = throttle_module.sign_in_by_address.retry_after(address_key)
    if wait:
        raise _too_many(wait)

    try:
        claims = _verify_google_id_token(payload.credential, GOOGLE_CLIENT_ID)
    except google_auth_module.GoogleAuthError as error:
        throttle_module.sign_in_by_address.record_failure(address_key)
        raise HTTPException(status_code=401, detail=str(error))

    # Google verifying the email is exactly the guarantee a "click the link
    # we emailed you" flow gives everywhere else in this system - so it is
    # treated the same way: enough to sign in with, never enough on its own
    # to silently reuse someone else's identity if it were ever false.
    if not claims.get("email_verified"):
        throttle_module.sign_in_by_address.record_failure(address_key)
        raise HTTPException(
            status_code=401,
            detail="Google has not verified this email address",
        )

    subject = str(claims["sub"])
    email = str(claims["email"]).strip().lower()

    account = db.query(models_db.Account).filter_by(
        oauth_provider="google", oauth_subject=subject
    ).first()
    if account is None:
        # Not seen this Google identity before - but the email might already
        # hold a password-based account. Linking is safe here specifically
        # because Google has verified the email, which is the same trust
        # basis a password reset already relies on.
        account = db.query(models_db.Account).filter_by(email=email).first()
    if account is None:
        account = models_db.Account(
            email=email,
            # Never used to sign in - only Google can authenticate this
            # account from here - but every account needs *a* hash, and a
            # random unusable one is the honest way to say so rather than
            # leaving the column empty.
            password_hash=auth_module.hash_password(secrets.token_urlsafe(32)),
            must_change_password=False,
        )
        db.add(account)

    account.oauth_provider = "google"
    account.oauth_subject = subject
    db.commit()

    throttle_module.sign_in_by_address.clear(address_key)
    _set_session_cookie(response, account.id)
    return account


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """A user typing just the domain name should land somewhere real, not a
    bare JSON 404 - found by the owner doing exactly that on the live deploy."""
    return RedirectResponse(url="/ui")


@app.get("/robots.txt", include_in_schema=False, response_class=PlainTextResponse)
def robots() -> str:
    """Let /ui be found and indexed; keep crawlers off the JSON API.

    Everything else in this app is either an authenticated JSON endpoint
    (returns 401 with nothing worth indexing anyway) or a per-account page
    behind login - /ui is the one page meant to be found by someone typing
    "scholarships zimbabwe" into a search engine, and the only one that
    should spend any crawl budget at all.
    """
    return "\n".join([
        "User-agent: *",
        "Allow: /ui",
        "Allow: /assets/",
        "Disallow: /profiles",
        "Disallow: /notifications",
        "Disallow: /me",
        "Disallow: /register",
        "Disallow: /login",
        "",
        f"Sitemap: {PUBLIC_ORIGIN}/sitemap.xml",
    ])


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap() -> Response:
    """One URL. There is exactly one public, indexable page in this app -
    every other route is either an API endpoint or requires an account - so a
    sitemap generator would be solving a problem this app does not have."""
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        "  <url>\n"
        f"    <loc>{PUBLIC_ORIGIN}/ui</loc>\n"
        "    <changefreq>weekly</changefreq>\n"
        "    <priority>1.0</priority>\n"
        "  </url>\n"
        "</urlset>\n"
    )
    return Response(content=body, media_type="application/xml")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/register", status_code=201)
def register(
    payload: RegisterRequest,
    request: Request,
    db: Session = Depends(get_db_session),
) -> dict[str, str]:
    """Create an account and email its first password to the address given.

    The password is generated here, not chosen by the caller, and it is never
    in this response - it goes to the mailbox and nowhere else. That is also
    what makes registration prove control of the address: you cannot get in
    without reading the mail.

    Note this deliberately does NOT log the new user in. Auto-login would make
    the emailed password decorative and let anyone register an address they do
    not own and walk straight in.
    """
    email = payload.email.strip().lower()
    if "@" not in email or "." not in email.split("@")[-1]:
        raise HTTPException(status_code=422, detail="a valid email address is required")

    _check_mail_throttle(request, email)

    if db.query(models_db.Account).filter_by(email=email).first() is not None:
        # This used to answer 409, and said in a comment that leaking whether
        # an address is registered was a deliberate trade for a system with a
        # named, known set of users. That premise is gone: strangers can reach
        # this endpoint now, and a 409 lets anyone test a list of addresses
        # against a system holding tax certificates and procurement logins.
        #
        # So both cases answer identically, and the difference moves into the
        # mailbox - where only the person who controls the address sees it.
        # "I never got the email" stays answerable, because the person who
        # owns that address did get one; it just says they already have an
        # account.
        try:
            _send_mail(
                to=email,
                subject="Your OpportunityAI account",
                body=_already_registered_email(email),
            )
        except mailer.MailError as error:
            raise HTTPException(status_code=503, detail=str(error))
        return {"status": "sent", "email": email}

    temporary = generate_temporary_password()
    account = models_db.Account(
        email=email,
        password_hash=auth_module.hash_password(temporary),
        must_change_password=True,
        temp_password_expires_at=datetime.now(timezone.utc) + TEMP_PASSWORD_TTL,
    )

    # Send *before* committing. An account whose password was never delivered
    # is one nobody can log into, and reporting success for that is worse than
    # refusing outright.
    try:
        _send_mail(
            to=email,
            subject="Your OpportunityAI password",
            body=_welcome_email(email, temporary),
        )
    except mailer.MailError as error:
        db.rollback()
        raise HTTPException(status_code=503, detail=str(error))

    db.add(account)
    db.commit()
    return {"status": "sent", "email": email}


def _already_registered_email(email: str) -> str:
    """Sent when someone tries to register an address that already has an
    account. It has to be useful to the real owner - who may have forgotten
    they signed up - without confirming anything to whoever typed the
    address in."""
    return "\n".join([
        "Someone asked to create an OpportunityAI account with this address.",
        "",
        "You already have one, so we have not created another and nothing has",
        "changed. Sign in as usual:",
        "",
        APP_URL,
        "",
        "If you have forgotten your password, use the 'Forgot your password?'",
        "link on that page and we will email you a new one.",
        "",
        "If this was not you, you can ignore this message - whoever asked was",
        "not told whether this address has an account.",
    ])


def generate_temporary_password() -> str:
    """A readable, unambiguous, high-entropy temporary password.

    Excludes characters that are misread when copied out of an email by hand -
    O/0, l/1/I - because that is exactly how this one gets used.
    """
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(16))


def _welcome_email(email: str, password: str) -> str:
    lines = [
        "Welcome to OpportunityAI.",
        "",
        "Your account is ready. Sign in with:",
        "",
        f"Email: {email}",
        f"Password: {password}",
        "",
        APP_URL,
        "",
        f"This password expires in {TEMP_PASSWORD_TTL.days} days. You will be asked to",
        "replace it when you first sign in - please do. This one travelled by",
        "email, so anyone who can read this message can use it until you do.",
        "",
        "If you did not ask for this account, you can ignore this email.",
    ]
    return "\n".join(lines) + "\n"


class ForgotPasswordRequest(BaseModel):
    email: str


def _reset_email(email: str, password: str) -> str:
    lines = [
        "A password reset was requested for your OpportunityAI account.",
        "",
        "Sign in with:",
        "",
        f"Email: {email}",
        f"Password: {password}",
        "",
        APP_URL,
        "",
        f"This password expires in {TEMP_PASSWORD_TTL.days} days, and you will be asked",
        "to replace it as soon as you sign in.",
        "",
        "If you did not ask for this, someone else typed your address into the",
        "reset form. Your previous password still worked until this email was",
        "sent - if that was not you, sign in and change it now.",
    ]
    return "\n".join(lines) + "\n"


@app.post("/forgot-password")
def forgot_password(
    payload: ForgotPasswordRequest,
    request: Request,
    db: Session = Depends(get_db_session),
) -> dict[str, str]:
    """Email a fresh temporary password to an account that has lost its own.

    Order matters and is the same discipline as registration: the mail is sent
    *before* the new hash is stored. Overwriting the password first and then
    failing to deliver would lock the owner out of their own account using a
    password that exists nowhere - strictly worse than the state they were
    already in.

    Answers the same way whether or not the address is registered. It used to
    answer 404 for an unknown one, on the reasoning that /register leaked the
    same fact anyway so hiding it here would be theatre. /register no longer
    leaks it, so neither does this.
    """
    email = payload.email.strip().lower()

    _check_mail_throttle(request, email)

    account = db.query(models_db.Account).filter_by(email=email).first()
    if account is None:
        # Nothing to send and nothing to say. The wait above is what stops
        # this being a way to time the difference.
        return {"status": "sent", "email": email}

    temporary = generate_temporary_password()
    new_hash = auth_module.hash_password(temporary)

    try:
        _send_mail(
            to=email,
            subject="Your OpportunityAI password reset",
            body=_reset_email(email, temporary),
        )
    except mailer.MailError as error:
        db.rollback()
        raise HTTPException(status_code=503, detail=str(error))

    account.password_hash = new_hash
    account.must_change_password = True
    account.temp_password_expires_at = datetime.now(timezone.utc) + TEMP_PASSWORD_TTL
    db.commit()
    return {"status": "sent", "email": email}


def _too_many(seconds: int) -> HTTPException:
    """One shape for every refusal that is about rate, not about credentials.

    429 with Retry-After, so a browser, a script and a person all get the same
    answer in a form each of them understands.
    """
    minutes = max(1, round(seconds / 60))
    return HTTPException(
        status_code=429,
        detail=(
            f"too many attempts - wait about {minutes} minute"
            f"{'s' if minutes != 1 else ''} and try again"
        ),
        headers={"Retry-After": str(seconds)},
    )


def _check_mail_throttle(request: Request, email: str) -> None:
    """Refuse to keep emailing the same address, or to let one caller keep
    asking us to email strangers. Applies to registration and password reset
    alike - both send mail to an address the caller merely typed in."""
    address = throttle_module.client_address(request)
    pairs = (
        (throttle_module.mail_by_recipient, email),
        (throttle_module.mail_by_sender, address),
    )
    for throttle, key in pairs:
        wait = throttle.retry_after(key)
        if wait:
            raise _too_many(wait)
    # Counted on the way in, not on failure: the thing being limited here is
    # how much mail this endpoint can be made to send, and a message that was
    # sent successfully is exactly what we are rationing.
    for throttle, key in pairs:
        throttle.record_failure(key)


@app.post("/login", response_model=AccountOut)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db_session),
) -> models_db.Account:
    email = payload.email.strip().lower()
    address = throttle_module.client_address(request)
    account_key, address_key = f"account:{email}", f"addr:{address}"

    # Checked before the password is even looked at, so a locked-out key costs
    # an attacker a round trip and costs this server no bcrypt.
    for throttle, key in ((throttle_module.sign_in_by_account, account_key),
                          (throttle_module.sign_in_by_address, address_key)):
        wait = throttle.retry_after(key)
        if wait:
            raise _too_many(wait)

    account = db.query(models_db.Account).filter_by(email=email).first()
    if account is None or not auth_module.verify_password(payload.password, account.password_hash):
        throttle_module.sign_in_by_account.record_failure(account_key)
        throttle_module.sign_in_by_address.record_failure(address_key)
        raise HTTPException(status_code=401, detail="invalid email or password")

    expiry = account.temp_password_expires_at
    if expiry is not None:
        # SQLite hands back naive datetimes; Postgres may too depending on the
        # column type. Compare in UTC either way rather than crashing on a
        # naive/aware subtraction during a login.
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry < datetime.now(timezone.utc):
            raise HTTPException(
                status_code=401,
                detail="this temporary password has expired - ask for a new one",
            )

    # Got in. Someone who mistyped twice and then succeeded is not an
    # attacker and must not meet a lockout a minute later.
    throttle_module.sign_in_by_account.clear(account_key)
    throttle_module.sign_in_by_address.clear(address_key)

    _set_session_cookie(response, account.id)
    return account


@app.post("/change-password", response_model=AccountOut)
def change_password(
    payload: ChangePasswordRequest,
    account: models_db.Account = Depends(get_current_account),
    db: Session = Depends(get_db_session),
) -> models_db.Account:
    """Replace the current password with one the user chose.

    Requires the current password even though the caller is already
    authenticated: without that, a stolen session cookie is enough to take the
    account over permanently.
    """
    if not auth_module.verify_password(payload.current_password, account.password_hash):
        raise HTTPException(status_code=401, detail="current password is incorrect")
    if len(payload.new_password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=422,
            detail=f"the new password must be at least {MIN_PASSWORD_LENGTH} characters",
        )

    account.password_hash = auth_module.hash_password(payload.new_password)
    account.must_change_password = False
    # Only the *emailed* password was temporary. Leaving the clock running
    # would lock the user out a week after they chose a good one.
    account.temp_password_expires_at = None
    account.password_set_at = datetime.now(timezone.utc)
    db.commit()
    return account


@app.post("/logout")
def logout(response: Response) -> dict[str, str]:
    response.delete_cookie(SESSION_COOKIE)
    response.delete_cookie(SESSION_EXPIRY_COOKIE)
    return {"status": "logged out"}


@app.post("/session/extend")
def extend_session(
    response: Response,
    account: models_db.Account = Depends(get_current_account),
) -> dict[str, str]:
    """Push the idle deadline back, because the owner is actually doing something.

    Deliberately its own endpoint rather than a side effect of any authenticated
    request. If simply reading data extended the session, the page's own
    background polling would keep a session alive forever with nobody at the
    keyboard, and the idle timeout would mean nothing. Only activity the client
    judges meaningful calls this.

    It requires a still-valid session - `get_current_account` sees to that - so
    an expired session cannot resurrect itself.
    """
    expires_at = _set_session_cookie(response, account.id)
    return {"expires_at": expires_at}


@app.get("/me", response_model=AccountOut)
def me(account: models_db.Account = Depends(get_current_account)) -> models_db.Account:
    return account


class ProfileCreateRequest(BaseModel):
    profile_type: str
    display_name: str


class ProfileUpdateRequest(BaseModel):
    display_name: str | None = None
    picture_url: str | None = None
    fields: dict | None = None
    hidden_fields: list[str] | None = None


class ProfileOut(BaseModel):
    id: str
    profile_type: str
    display_name: str
    picture_url: str | None
    fields: dict
    hidden_fields: list

    model_config = {"from_attributes": True}


class DocumentOut(BaseModel):
    id: str
    doc_type: str
    original_filename: str
    content_type: str
    size_bytes: int
    extraction_status: str
    uploaded_at: datetime

    model_config = {"from_attributes": True}


def _get_owned_profile(profile_id: str, account: models_db.Account, db: Session) -> models_db.Profile:
    profile = db.get(models_db.Profile, profile_id)
    if profile is None or profile.account_id != account.id:
        raise HTTPException(status_code=404, detail="profile not found")
    return profile


@app.post("/profiles", response_model=ProfileOut, status_code=201)
def create_profile(
    payload: ProfileCreateRequest,
    account: models_db.Account = Depends(get_current_account),
    db: Session = Depends(get_db_session),
) -> models_db.Profile:
    if payload.profile_type not in models_db.PROFILE_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"profile_type must be one of {models_db.PROFILE_TYPES}",
        )
    existing = db.query(models_db.Profile).filter_by(
        account_id=account.id, profile_type=payload.profile_type
    ).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"a {payload.profile_type} profile already exists")
    profile = models_db.Profile(
        account_id=account.id, profile_type=payload.profile_type,
        display_name=payload.display_name, fields={},
    )
    db.add(profile)
    db.commit()
    return profile


@app.get("/profiles", response_model=list[ProfileOut])
def list_profiles(
    account: models_db.Account = Depends(get_current_account), db: Session = Depends(get_db_session)
) -> list[models_db.Profile]:
    # Explicit ordering: without it the switcher's order - and which profile the
    # UI picks as active when nothing is remembered - is whatever the database
    # happens to return, which is not stable.
    return (
        db.query(models_db.Profile)
        .filter_by(account_id=account.id)
        .order_by(models_db.Profile.created_at)
        .all()
    )


@app.get("/profiles/{profile_id}", response_model=ProfileOut)
def get_profile_by_id(
    profile_id: str,
    account: models_db.Account = Depends(get_current_account),
    db: Session = Depends(get_db_session),
) -> models_db.Profile:
    return _get_owned_profile(profile_id, account, db)


@app.put("/profiles/{profile_id}", response_model=ProfileOut)
def update_profile(
    profile_id: str,
    payload: ProfileUpdateRequest,
    account: models_db.Account = Depends(get_current_account),
    db: Session = Depends(get_db_session),
) -> models_db.Profile:
    profile = _get_owned_profile(profile_id, account, db)
    if payload.display_name is not None:
        profile.display_name = payload.display_name
    if payload.picture_url is not None:
        profile.picture_url = payload.picture_url
    if payload.fields is not None:
        profile.fields = {**profile.fields, **payload.fields}
    if payload.hidden_fields is not None:
        profile.hidden_fields = payload.hidden_fields
    db.commit()
    return profile


@app.delete("/profiles/{profile_id}", status_code=204)
def delete_profile(
    profile_id: str,
    account: models_db.Account = Depends(get_current_account),
    db: Session = Depends(get_db_session),
) -> None:
    """Delete a profile and everything that belongs to it.

    Two things the ORM cascade does not cover, both of which would otherwise
    leave real mess behind:

    * **The uploaded files.** Documents live in MinIO, not in Postgres, so
      deleting the rows would leave the owner's CV and tax clearance sitting
      in the bucket after they asked for them to be gone.
    * **Notifications.** They hang off the *account*, not the profile, but
      carry nullable `profile_id`/`opportunity_id` foreign keys. Deleting the
      profile without clearing them violates those constraints.

    An object that has already gone from the bucket is not an error - the row
    is what the owner asked to be rid of, and refusing to delete it because
    its file was already missing would leave them stuck.
    """
    profile = _get_owned_profile(profile_id, account, db)

    documents = db.query(models_db.Document).filter_by(profile_id=profile.id).all()
    if documents:
        client = _minio_client()
        for document in documents:
            try:
                documents_module.delete_document(
                    client, document.object_key, bucket=DOCUMENTS_BUCKET
                )
            except Exception:  # noqa: BLE001 - see docstring
                pass

    opportunity_ids = [
        row[0]
        for row in db.query(models_db.StoredOpportunity.id).filter_by(profile_id=profile.id).all()
    ]
    db.query(models_db.Notification).filter(
        models_db.Notification.profile_id == profile.id
    ).delete(synchronize_session=False)
    if opportunity_ids:
        db.query(models_db.Notification).filter(
            models_db.Notification.opportunity_id.in_(opportunity_ids)
        ).delete(synchronize_session=False)

    db.delete(profile)
    db.commit()


@app.post("/profiles/{profile_id}/documents", response_model=DocumentOut, status_code=201)
async def upload_document(
    profile_id: str,
    file: UploadFile = File(...),
    # What this paper *is*, from profile_schema. Optional on purpose: the owner
    # can always upload something the checklist never asked for, and an
    # unclassified file is still stored and still text-extracted - it just
    # can't satisfy a named requirement, because we don't know what it is.
    doc_type: str = Form("other"),
    account: models_db.Account = Depends(get_current_account),
    db: Session = Depends(get_db_session),
) -> models_db.Document:
    profile = _get_owned_profile(profile_id, account, db)
    content_type = file.content_type or "application/octet-stream"
    if content_type not in _SUPPORTED_DOCUMENT_TYPES:
        raise HTTPException(status_code=415, detail=f"unsupported document type: {content_type}")
    content = await file.read()
    if len(content) > MAX_DOCUMENT_BYTES:
        raise HTTPException(status_code=413, detail="file too large")

    document = models_db.Document(
        profile_id=profile.id, object_key="", doc_type=(doc_type or "other").strip() or "other",
        original_filename=file.filename or "document",
        content_type=content_type, size_bytes=len(content),
        # Document.extraction_status defaults to "skipped" (models_db.py predates
        # extraction existing at all). Now that /extract is real, a fresh upload
        # is awaiting it, not permanently skipped - override explicitly here.
        extraction_status="pending",
    )
    db.add(document)
    db.flush()  # assigns document.id without committing yet

    client = _minio_client()
    documents_module.ensure_bucket(client, DOCUMENTS_BUCKET)
    key = documents_module.upload_document(
        client, account_id=account.id, profile_id=profile.id, document_id=document.id,
        filename=document.original_filename, content=content, content_type=content_type,
        bucket=DOCUMENTS_BUCKET,
    )
    document.object_key = key

    # Read it now and keep what it says. The owner's CV is the only place
    # their actual projects, training and results are written down, and
    # drafting was previously given six one-line profile fields instead -
    # which is exactly why a drafted application read like a stranger wrote
    # it. Extraction failing is not a failed upload: the file is stored and
    # useful either way.
    try:
        document.extracted_text = documents_module.readable_text(content, content_type)
        document.extraction_status = "extracted" if document.extracted_text else "empty"
    except Exception:
        document.extraction_status = "failed"

    db.commit()

    # The agent asked for a document; a document arrived. Check straight away
    # whether that unblocks anything, rather than leaving drafts sitting in
    # "needs_documents" until the next scheduled sweep hours later - the owner
    # is right here, and this is the moment the answer is useful to them.
    if document.doc_type != "other":
        db.refresh(profile)
        try:
            pipeline_module.resume_after_documents(db, profile)
        except Exception:
            # Never fail an upload because the follow-up check failed - the
            # file is safely stored, and the next sweep will retry this.
            db.rollback()
    return document


@app.get("/profiles/{profile_id}/documents", response_model=list[DocumentOut])
def list_documents(
    profile_id: str,
    account: models_db.Account = Depends(get_current_account),
    db: Session = Depends(get_db_session),
) -> list[models_db.Document]:
    profile = _get_owned_profile(profile_id, account, db)
    return db.query(models_db.Document).filter_by(profile_id=profile.id).all()


@app.get("/profiles/{profile_id}/schema")
def get_profile_schema(
    profile_id: str,
    account: models_db.Account = Depends(get_current_account),
    db: Session = Depends(get_db_session),
) -> dict:
    """What this profile type asks for, and what it still owes us.

    The UI renders its setup form and its document slots from this rather than
    hard-coding a person's fields - which is the whole point of picking a
    profile type. A company gets company questions and a compliance pack; a
    student gets a CV and a transcript.
    """
    profile = _get_owned_profile(profile_id, account, db)
    fields = profile.fields or {}
    held = pipeline_module.held_document_keys(profile)
    spec = profile_schema.spec_for(profile.profile_type)

    return {
        "profile_type": profile.profile_type,
        "label": spec.label,
        "subject": profile_schema.resolve_subject(profile.profile_type, fields),
        "subject_is_choosable": spec.subject == profile_schema.EITHER,
        "fields": [
            {"key": f.key, "label": f.label, "kind": f.kind, "hint": f.hint,
             "options": list(f.options)}
            for f in profile_schema.fields_for(profile.profile_type, fields)
        ],
        "documents": [
            {"key": d.key, "label": d.label, "required": d.required, "hint": d.hint,
             "held": d.key in held}
            for d in profile_schema.documents_for(profile.profile_type, fields)
        ],
        "missing_documents": sorted(
            profile_schema.missing_document_keys(profile.profile_type, fields, held)
        ),
    }


class AssistRequest(BaseModel):
    text: str


@app.post("/profiles/{profile_id}/assist")
def assist_profile_fields(
    profile_id: str,
    payload: AssistRequest,
    account: models_db.Account = Depends(get_current_account),
    db: Session = Depends(get_db_session),
) -> dict[str, object]:
    """Turn free-typed notes into suggested values for this profile's fields.

    Deliberately does **not** save anything. It returns suggestions for the
    owner to look at and accept, because the form is the record of what they
    say about themselves and a small local model's reading of their notes is
    not good enough to overwrite that silently. The same stance as document
    extraction, which also never overwrites a value the owner typed.

    Which fields it tries to fill comes from the profile's own schema, so a
    tender profile is asked for a registration number and PRAZ categories
    while a scholarship profile is asked for a study level - the thing that
    makes this useful rather than a generic CV parser.
    """
    profile = _get_owned_profile(profile_id, account, db)
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="there is nothing to read")

    specs = profile_schema.fields_for(profile.profile_type, profile.fields or {})
    try:
        suggested = _assist_profile_fields(text, specs)
    except Exception as error:  # noqa: BLE001 - the model is a remote dependency
        raise HTTPException(
            status_code=503,
            detail=f"could not reach the language model: {type(error).__name__}",
        ) from error

    # Report which suggestions would land on an empty field and which would
    # sit against something already filled in, so the UI can let the owner
    # keep what they wrote without having to compare two screens by eye.
    existing = profile.fields or {}
    return {
        "suggested": suggested,
        "conflicts": sorted(k for k in suggested if existing.get(k) not in (None, "", [], {})),
    }


SUPPORTED_PORTALS = {"egp": "PRAZ eGP"}


class CredentialIn(BaseModel):
    username: str
    password: str


class CredentialOut(BaseModel):
    """What the API is willing to say about a stored credential.

    Note what is absent: the password, and the ciphertext. There is no
    endpoint anywhere that returns either. `has_password` is all the UI needs
    to render "configured" vs "not configured", and anything more would put
    the owner's portal password one careless response away from the page
    source.
    """

    portal: str
    portal_label: str
    username: str
    has_password: bool
    verification_status: str
    verification_detail: str | None
    last_verified_at: datetime | None


def _credential_out(credential: models_db.PortalCredential) -> CredentialOut:
    return CredentialOut(
        portal=credential.portal,
        portal_label=SUPPORTED_PORTALS.get(credential.portal, credential.portal),
        username=credential.username,
        has_password=bool(credential.secret_ciphertext),
        verification_status=credential.verification_status,
        verification_detail=credential.verification_detail,
        last_verified_at=credential.last_verified_at,
    )


@app.get("/profiles/{profile_id}/credentials", response_model=list[CredentialOut])
def list_credentials(
    profile_id: str,
    account: models_db.Account = Depends(get_current_account),
    db: Session = Depends(get_db_session),
) -> list[CredentialOut]:
    profile = _get_owned_profile(profile_id, account, db)
    return [_credential_out(c) for c in profile.credentials]


@app.put("/profiles/{profile_id}/credentials/{portal}", response_model=CredentialOut)
def save_credential(
    profile_id: str,
    portal: str,
    payload: CredentialIn,
    account: models_db.Account = Depends(get_current_account),
    db: Session = Depends(get_db_session),
) -> CredentialOut:
    """Store the owner's portal login, encrypted.

    Refuses outright when no encryption key is configured rather than storing
    the password in clear - see credentials.py. That means a deployment
    without CREDENTIALS_SECRET_KEY simply cannot use this feature, which is
    the correct trade.
    """
    profile = _get_owned_profile(profile_id, account, db)
    if portal not in SUPPORTED_PORTALS:
        raise HTTPException(status_code=422, detail=f"unknown portal: {portal}")
    if not payload.username.strip():
        raise HTTPException(status_code=422, detail="username is required")

    try:
        ciphertext = credentials_module.encrypt_secret(payload.password)
    except credentials_module.CredentialError as error:
        # 503, not 400: the request is fine, the deployment isn't configured.
        raise HTTPException(status_code=503, detail=str(error))

    credential = db.query(models_db.PortalCredential).filter_by(
        profile_id=profile.id, portal=portal
    ).first()
    if credential is None:
        credential = models_db.PortalCredential(profile_id=profile.id, portal=portal)
        db.add(credential)

    credential.username = payload.username.strip()
    credential.secret_ciphertext = ciphertext
    # Changing a password invalidates whatever we knew about the old one.
    credential.verification_status = "untested"
    credential.verification_detail = None
    credential.last_verified_at = None
    db.commit()
    return _credential_out(credential)


@app.post("/profiles/{profile_id}/credentials/{portal}/verify", response_model=CredentialOut)
def verify_credential(
    profile_id: str,
    portal: str,
    account: models_db.Account = Depends(get_current_account),
    db: Session = Depends(get_db_session),
) -> CredentialOut:
    """Try the stored credentials against the portal, once, on demand.

    Deliberately owner-triggered rather than automatic: repeated failed logins
    against a government procurement system can lock the owner's real supplier
    account, so this happens when they ask for it and not on a schedule.
    """
    profile = _get_owned_profile(profile_id, account, db)
    credential = db.query(models_db.PortalCredential).filter_by(
        profile_id=profile.id, portal=portal
    ).first()
    if credential is None:
        raise HTTPException(status_code=404, detail="no credentials stored for this portal")

    try:
        secret = credentials_module.decrypt_secret(credential.secret_ciphertext)
    except credentials_module.CredentialError as error:
        credential.verification_status = "failed"
        credential.verification_detail = str(error)[:400]
        db.commit()
        raise HTTPException(status_code=503, detail=str(error))

    # Three-way, not two: "unreachable" is recorded as its own state so the UI
    # does not tell the owner their password was rejected when the portal was
    # simply not contactable.
    status, message = _verify_egp_credentials(credential.username, secret)
    credential.verification_status = status
    credential.verification_detail = message
    credential.last_verified_at = datetime.now(timezone.utc) if status == "verified" else None
    db.commit()
    return _credential_out(credential)


@app.delete("/profiles/{profile_id}/credentials/{portal}", status_code=204)
def delete_credential(
    profile_id: str,
    portal: str,
    account: models_db.Account = Depends(get_current_account),
    db: Session = Depends(get_db_session),
) -> None:
    profile = _get_owned_profile(profile_id, account, db)
    credential = db.query(models_db.PortalCredential).filter_by(
        profile_id=profile.id, portal=portal
    ).first()
    if credential is None:
        raise HTTPException(status_code=404, detail="no credentials stored for this portal")
    db.delete(credential)
    db.commit()


@app.get("/profile-types")
def list_profile_types() -> list[dict]:
    """Offered on the onboarding screen. Public: it's what the product is."""
    return [
        {"key": s.key, "label": s.label, "subject": s.subject, "blurb": s.blurb}
        for s in profile_schema.all_specs()
    ]


@app.delete("/profiles/{profile_id}/documents/{document_id}", status_code=204)
def delete_document(
    profile_id: str,
    document_id: str,
    account: models_db.Account = Depends(get_current_account),
    db: Session = Depends(get_db_session),
) -> None:
    profile = _get_owned_profile(profile_id, account, db)
    document = db.get(models_db.Document, document_id)
    if document is None or document.profile_id != profile.id:
        raise HTTPException(status_code=404, detail="document not found")
    documents_module.delete_document(_minio_client(), document.object_key, bucket=DOCUMENTS_BUCKET)
    db.delete(document)
    db.commit()


@app.post("/profiles/{profile_id}/documents/{document_id}/suggest")
def suggest_from_document(
    profile_id: str,
    document_id: str,
    account: models_db.Account = Depends(get_current_account),
    db: Session = Depends(get_db_session),
) -> dict[str, object]:
    """Read one uploaded document and suggest values for this profile's fields.

    The older /extract endpoint answers a fixed CV-shaped question - work
    history, certificates, study level, field - which is the wrong question
    for a company. A PRAZ registration certificate has no study level on it;
    it has the supplier category codes that decide which tenders the company
    may bid on at all, and there was no way to get those off it.

    This asks the profile's own schema instead, so the same upload button
    means "read my transcript" for a student and "read my PRAZ certificate"
    for a company. Suggestions are returned for review, never written -
    same stance as the free-text assist.
    """
    profile = _get_owned_profile(profile_id, account, db)
    document = db.get(models_db.Document, document_id)
    if document is None or document.profile_id != profile.id:
        raise HTTPException(status_code=404, detail="document not found")

    client = _minio_client()
    content = documents_module.download_document(
        client, document.object_key, bucket=DOCUMENTS_BUCKET
    )
    try:
        text = extract_text(content, document.content_type)
    except Exception as error:
        raise HTTPException(
            status_code=415, detail=f"could not read that file: {error}"
        ) from error

    if not (text or "").strip():
        # A scanned certificate is an image in a PDF wrapper. Saying so beats
        # returning nothing and letting the owner conclude the feature is broken.
        raise HTTPException(
            status_code=422,
            detail="no text could be read from that file - if it is a scan, "
                   "it would need OCR, which this system does not do",
        )

    specs = profile_schema.fields_for(profile.profile_type, profile.fields or {})
    try:
        suggested = _assist_profile_fields(text, specs)
    except Exception as error:  # noqa: BLE001 - the model is a remote dependency
        raise HTTPException(
            status_code=503,
            detail=f"could not reach the language model: {type(error).__name__}",
        ) from error

    existing = profile.fields or {}
    document.extraction_status = "extracted" if suggested else "skipped"
    db.commit()
    return {
        "document": {"id": document.id, "filename": document.original_filename,
                     "doc_type": document.doc_type},
        "suggested": suggested,
        "conflicts": sorted(k for k in suggested if existing.get(k) not in (None, "", [], {})),
    }


@app.post("/profiles/{profile_id}/documents/{document_id}/extract", response_model=ProfileOut)
def extract_document(
    profile_id: str,
    document_id: str,
    account: models_db.Account = Depends(get_current_account),
    db: Session = Depends(get_db_session),
) -> models_db.Profile:
    """Extract facts from one uploaded document and merge them into the profile.

    Conservative merge, never overwrites what the owner already entered:
    list fields (work_history, certificates) get new unique items appended;
    scalar fields (study_level, field) only fill in if currently empty.
    """
    profile = _get_owned_profile(profile_id, account, db)
    document = db.get(models_db.Document, document_id)
    if document is None or document.profile_id != profile.id:
        raise HTTPException(status_code=404, detail="document not found")

    client = _minio_client()
    content = documents_module.download_document(client, document.object_key, bucket=DOCUMENTS_BUCKET)
    try:
        facts = _extract_facts_from_document(content, document.content_type)
    except Exception as error:
        document.extraction_status = "failed"
        db.commit()
        raise HTTPException(status_code=502, detail=f"extraction failed: {error}") from error

    updated_fields = dict(profile.fields)
    for field_name in LIST_PROFILE_FIELDS:
        existing_items = list(updated_fields.get(field_name) or [])
        for item in getattr(facts, field_name):
            if item not in existing_items:
                existing_items.append(item)
        updated_fields[field_name] = existing_items
    for field_name in SCALAR_PROFILE_FIELDS:
        if not updated_fields.get(field_name):
            new_value = getattr(facts, field_name)
            if new_value:
                updated_fields[field_name] = new_value

    profile.fields = updated_fields
    document.extraction_status = "extracted"
    db.commit()
    return profile


class OpportunityOut(BaseModel):
    id: str
    canonical_url: str
    payload: dict
    match_status: str
    match_score: int
    match_reasons: dict
    stage: str
    escalated: bool
    package: dict | None
    compliance: dict | None
    # Set once this tender's id turns up in the eGP award notices while the
    # owner had not submitted or dismissed it - see pipeline.py's
    # _mark_awarded_elsewhere. None for every non-tender opportunity.
    awarded_to: str | None
    awarded_at: date | None
    deleted_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class OpportunityEventOut(BaseModel):
    kind: str
    detail: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class NotificationOut(BaseModel):
    id: str
    profile_id: str | None
    opportunity_id: str | None
    kind: str
    message: str
    read_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


# Indirection so tests can swap the network-touching halves of the cycle,
# same pattern as `discover` on the older single-tenant path.
# Injection point, same reason as the two below: tests must never make a real
# login attempt against a live government portal.
_verify_egp_credentials = egp_session.verify_credentials

_pipeline_search = pipeline_module.discover
_pipeline_fetch = pipeline_module.fetch_public_page


def _get_owned_opportunity(
    profile: models_db.Profile, opportunity_id: str, db: Session
) -> models_db.StoredOpportunity:
    opportunity = db.get(models_db.StoredOpportunity, opportunity_id)
    if opportunity is None or opportunity.profile_id != profile.id:
        raise HTTPException(status_code=404, detail="opportunity not found")
    return opportunity


@app.post("/profiles/{profile_id}/run")
def run_profile_pipeline(
    profile_id: str,
    account: models_db.Account = Depends(get_current_account),
    db: Session = Depends(get_db_session),
) -> dict[str, object]:
    profile = _get_owned_profile(profile_id, account, db)
    api_key = os.getenv("TAVILY_API_KEY")
    # A tender profile never calls search.py at all - it reads the PRAZ eGP
    # board directly (pipeline.run_profile_cycle's is_tender branch) - so
    # gating it on a web-search backend was refusing a search that was never
    # going to happen. Every other profile type does need one of the two.
    needs_search_backend = profile.profile_type != "tender"
    if needs_search_backend and not (os.getenv("SEARXNG_URL") or api_key):
        raise HTTPException(
            status_code=503,
            detail="no search backend is configured - set SEARXNG_URL or TAVILY_API_KEY",
        )

    run = pipeline_module.run_profile_cycle(
        db, profile, api_key=api_key, search_fn=_pipeline_search, fetch_fn=_pipeline_fetch,
    )
    if run is None:
        raise HTTPException(
            status_code=409,
            detail="this profile needs at least a name before the agent can search for anything",
        )
    return {
        "run_id": run.id, "found": run.found, "added": run.added,
        "drafted": run.drafted, "failures": run.failures,
    }


# Payload keys holding the full text of a fetched page. They are the reason
# drafting works at all - and they are the reason one profile's list response
# measured 66 MB on the live server, 78% of it call text, which is nine
# minutes on a 1 Mbps mobile connection before a single card appears.
#
# The list is a list. The text belongs to the detail page, which asks for one
# opportunity and gets `call_text` capped at 20,000 characters.
_BULK_PAYLOAD_KEYS = ("evidence", "content", "page_text")


def _slim_for_list(opportunity: models_db.StoredOpportunity) -> OpportunityOut:
    """One row of the list: everything the cards draw, none of the bulk.

    Checked against the front end rather than guessed - oppCard() reads title,
    url, source, deadline and the match reasons, and renderPackage() reads
    checklist, cover_note and warnings. Neither ever touches `evidence` or the
    drafted section bodies, so neither is sent.
    """
    row = OpportunityOut.model_validate(opportunity)
    row.payload = {
        key: value for key, value in (opportunity.payload or {}).items()
        if key not in _BULK_PAYLOAD_KEYS
    }
    if row.package:
        # The package keeps its own copy of the fetched page, which is where
        # 96.9% of the remaining bytes were hiding after `payload` was
        # trimmed: 36 KB per row, and one of them a 1.3 MB YouTube page.
        sections = row.package.get("sections") or []
        row.package = {
            key: value for key, value in row.package.items()
            if key not in _BULK_PAYLOAD_KEYS and key != "sections"
        }
        # The count, not the prose: enough for the card to say a draft exists.
        row.package["section_count"] = len(sections)
    return row


@app.get("/profiles/{profile_id}/opportunities", response_model=list[OpportunityOut])
def list_profile_opportunities(
    profile_id: str,
    stage: str | None = None,
    match_status: str | None = None,
    trashed: bool = False,
    account: models_db.Account = Depends(get_current_account),
    db: Session = Depends(get_db_session),
) -> list[models_db.StoredOpportunity]:
    profile = _get_owned_profile(profile_id, account, db)
    query = db.query(models_db.StoredOpportunity).filter_by(profile_id=profile.id)
    # The bin is opt-in. Without this every list the owner opens would be
    # padded out with the things they just cleared, which defeats clearing
    # them - and the whole reason they asked for a bin is that an agent
    # working unattended produces volume.
    if trashed:
        query = query.filter(models_db.StoredOpportunity.deleted_at.isnot(None))
    else:
        query = query.filter(models_db.StoredOpportunity.deleted_at.is_(None))
    if stage:
        query = query.filter_by(stage=stage)
    if match_status:
        query = query.filter_by(match_status=match_status)
    rows = query.order_by(models_db.StoredOpportunity.created_at.desc()).all()
    return [_slim_for_list(row) for row in rows]


@app.get("/profiles/{profile_id}/opportunities/{opportunity_id}", response_model=OpportunityOut)
def get_profile_opportunity(
    profile_id: str,
    opportunity_id: str,
    account: models_db.Account = Depends(get_current_account),
    db: Session = Depends(get_db_session),
) -> models_db.StoredOpportunity:
    profile = _get_owned_profile(profile_id, account, db)
    return _get_owned_opportunity(profile, opportunity_id, db)


def _record_event(
    db: Session, opportunity: models_db.StoredOpportunity, kind: str, detail: str | None = None
) -> None:
    """Append to an opportunity's history. Never raises on its own account -
    losing the audit line is not worth failing the action the owner asked
    for, and a missing history entry is recoverable where a refused approval
    is just confusing."""
    db.add(models_db.OpportunityEvent(
        opportunity_id=opportunity.id, kind=kind, detail=detail,
    ))


def _set_stage(
    profile_id: str, opportunity_id: str, stage: str, account: models_db.Account, db: Session
) -> models_db.StoredOpportunity:
    profile = _get_owned_profile(profile_id, account, db)
    opportunity = _get_owned_opportunity(profile, opportunity_id, db)
    previous = opportunity.stage
    opportunity.stage = stage
    _record_event(db, opportunity, stage, f"from {previous}" if previous != stage else None)
    db.commit()
    return opportunity


@app.post("/profiles/{profile_id}/opportunities/{opportunity_id}/approve", response_model=OpportunityOut)
def approve_opportunity(
    profile_id: str,
    opportunity_id: str,
    account: models_db.Account = Depends(get_current_account),
    db: Session = Depends(get_db_session),
) -> models_db.StoredOpportunity:
    """The hard gate. Approving means the owner has read the draft and is happy
    for it to go out - it deliberately does NOT send anything (see
    SOLUTION_DEFINITION.md §16 on why auto-submission isn't built)."""
    return _set_stage(profile_id, opportunity_id, "approved", account, db)


@app.post("/profiles/{profile_id}/opportunities/{opportunity_id}/submitted", response_model=OpportunityOut)
def mark_opportunity_submitted(
    profile_id: str,
    opportunity_id: str,
    account: models_db.Account = Depends(get_current_account),
    db: Session = Depends(get_db_session),
) -> models_db.StoredOpportunity:
    """Owner confirming they actually sent it, on the portal, themselves."""
    return _set_stage(profile_id, opportunity_id, "submitted", account, db)


@app.post("/profiles/{profile_id}/opportunities/{opportunity_id}/dismiss", response_model=OpportunityOut)
def dismiss_opportunity(
    profile_id: str,
    opportunity_id: str,
    account: models_db.Account = Depends(get_current_account),
    db: Session = Depends(get_db_session),
) -> models_db.StoredOpportunity:
    return _set_stage(profile_id, opportunity_id, "dismissed", account, db)


@app.post("/profiles/{profile_id}/opportunities/{opportunity_id}/escalate", response_model=OpportunityOut)
def escalate_opportunity_endpoint(
    profile_id: str,
    opportunity_id: str,
    account: models_db.Account = Depends(get_current_account),
    db: Session = Depends(get_db_session),
) -> models_db.StoredOpportunity:
    """Owner disagrees with the eligibility verdict - draft it anyway."""
    profile = _get_owned_profile(profile_id, account, db)
    opportunity = _get_owned_opportunity(profile, opportunity_id, db)
    return pipeline_module.escalate_opportunity(db, opportunity)


@app.post("/profiles/{profile_id}/opportunities/{opportunity_id}/trash",
          response_model=OpportunityOut)
def trash_opportunity(
    profile_id: str,
    opportunity_id: str,
    account: models_db.Account = Depends(get_current_account),
    db: Session = Depends(get_db_session),
) -> models_db.StoredOpportunity:
    """Move to the bin. Recoverable - see restore_opportunity."""
    profile = _get_owned_profile(profile_id, account, db)
    opportunity = _get_owned_opportunity(profile, opportunity_id, db)
    if opportunity.deleted_at is None:
        opportunity.deleted_at = datetime.now(timezone.utc)
        _record_event(db, opportunity, "trashed")
        db.commit()
    return opportunity


@app.post("/profiles/{profile_id}/opportunities/{opportunity_id}/restore",
          response_model=OpportunityOut)
def restore_opportunity(
    profile_id: str,
    opportunity_id: str,
    account: models_db.Account = Depends(get_current_account),
    db: Session = Depends(get_db_session),
) -> models_db.StoredOpportunity:
    """Take it back out of the bin, exactly as it was - the stage, the draft
    and the compliance report were never touched by trashing."""
    profile = _get_owned_profile(profile_id, account, db)
    opportunity = _get_owned_opportunity(profile, opportunity_id, db)
    if opportunity.deleted_at is not None:
        opportunity.deleted_at = None
        _record_event(db, opportunity, "restored")
        db.commit()
    return opportunity


@app.delete("/profiles/{profile_id}/opportunities/{opportunity_id}", status_code=204)
def delete_opportunity(
    profile_id: str,
    opportunity_id: str,
    account: models_db.Account = Depends(get_current_account),
    db: Session = Depends(get_db_session),
) -> None:
    """Gone for good.

    Deliberately only permitted from the bin: reaching this by accident from
    a normal list would destroy a drafted application in one click, and the
    two-step - trash, then empty - is what makes the irreversible action a
    considered one. Also clears the notifications and history pointing at it,
    which would otherwise be left as foreign keys to a row that no longer
    exists.
    """
    profile = _get_owned_profile(profile_id, account, db)
    opportunity = _get_owned_opportunity(profile, opportunity_id, db)
    if opportunity.deleted_at is None:
        raise HTTPException(
            status_code=409,
            detail="move it to the bin first - permanent deletion cannot be undone",
        )

    db.query(models_db.Notification).filter(
        models_db.Notification.opportunity_id == opportunity.id
    ).delete(synchronize_session=False)
    db.query(models_db.OpportunityEvent).filter(
        models_db.OpportunityEvent.opportunity_id == opportunity.id
    ).delete(synchronize_session=False)
    db.delete(opportunity)
    db.commit()


@app.get("/profiles/{profile_id}/opportunities/{opportunity_id}/history",
         response_model=list[OpportunityEventOut])
def opportunity_history(
    profile_id: str,
    opportunity_id: str,
    account: models_db.Account = Depends(get_current_account),
    db: Session = Depends(get_db_session),
) -> list[models_db.OpportunityEvent]:
    """Everything that has happened to this opportunity, oldest first."""
    profile = _get_owned_profile(profile_id, account, db)
    opportunity = _get_owned_opportunity(profile, opportunity_id, db)
    return (
        db.query(models_db.OpportunityEvent)
        .filter_by(opportunity_id=opportunity.id)
        .order_by(models_db.OpportunityEvent.created_at.asc())
        .all()
    )


class FullDraftRequest(BaseModel):
    # Lets the owner steer a proposal without editing nine sections by hand
    # afterwards - "focus on data governance" changes what gets written, not
    # what gets corrected.
    steer: str | None = None


def _ollama_settings() -> dict:
    return {
        "base_url": os.getenv("OLLAMA_URL", "http://localhost:11434"),
        "model": os.getenv("OLLAMA_MODEL", "qwen2.5:0.5b"),
    }


def _page_text_for(opportunity: models_db.StoredOpportunity) -> str:
    """The call's own words, as far as we have them.

    Prefers the fetched page content the opportunity was built from; falls
    back to whatever summary was stored. Reading the real page matters here -
    the format rules and section list are usually in the body, not the blurb.

    `evidence` is the important one and was the bug: extraction.py stores the
    fetched page as `evidence=[content]`, and nothing here looked there. So
    this returned a bare title for essentially every opportunity in the
    database, the requirement extractor found no sections in it, and drafting
    fell back to the generic cover letter every single time. Measured on live
    data before the fix: 399 of 400 opportunities had no readable text by the
    keys below, while 307 of them held over 5k characters in `evidence`.
    """
    payload = opportunity.payload or {}
    for key in ("content", "page_text", "summary", "description"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value

    evidence = payload.get("evidence")
    if isinstance(evidence, list):
        joined = "\n\n".join(e for e in evidence if isinstance(e, str) and e.strip())
        if joined.strip():
            # Older rows were stored before connector.py extracted text from
            # HTML, so they still hold raw markup. Cleaning on read means the
            # 400 opportunities already in the database become usable without
            # re-fetching every one of them from its original site.
            if "<" in joined and ">" in joined:
                joined = html_to_text(joined)
            return joined

    return str(payload.get("title") or "")


# Things a form asks for that must never be answered on the applicant's behalf.
# A generated signature is a forgery, and a date the applicant did not choose
# is a claim about when they signed.
_DO_NOT_ANSWER = ("signature", "signed", "date signed", "official use", "stamp",
                  "for office use", "witness")


def _fetch_call_form(opportunity) -> dict | None:
    """Find and read the form the call says to complete, if there is one.

    Every step here is allowed to fail quietly. A call with no form is the
    normal case, a link that 403s is the common case, and neither is a reason
    to lose the rest of the application.
    """
    links = (opportunity.payload or {}).get("document_links") or []
    chosen = application_form_module.likely_form(links)
    if not chosen:
        return None
    try:
        page = fetch_public_page(chosen)
    except Exception:
        return {"url": chosen, "filename": chosen.rsplit("/", 1)[-1],
                "questions": [], "note": "the form could not be downloaded"}

    questions = application_form_module.questions_in(page.content or "")
    return {
        "url": chosen,
        "filename": chosen.rsplit("/", 1)[-1],
        "questions": questions,
        "text": (page.content or "")[:8000],
        "note": "" if questions else "the form was downloaded but no fields could be read",
    }


def _requirements_for(spec, form, opportunity, profile) -> list[dict]:
    """Everything this call asks for, as one ordered list.

    The owner described their own process: "I first list down the
    requirements then I start drafting each requirement step by step". This is
    that list. It deliberately mixes sources - the call's own section
    structure, the questions on its form, the documents it demands - because
    an applicant does not care which part of the call a requirement came
    from, only that it has to be answered.
    """
    requirements: list[dict] = []

    for section in spec.sections:
        requirements.append({
            "kind": "section",
            "prompt": section.title,
            "guidance": section.guidance or "",
            "source": "the call's required structure",
            "answerable": True,
        })

    for question in (form or {}).get("questions") or []:
        lowered = question.lower()
        requirements.append({
            "kind": "form_field",
            "prompt": question,
            "guidance": "",
            "source": f"the application form ({(form or {}).get('filename')})",
            # Signatures and office-use boxes are listed so the applicant sees
            # them, and left blank because filling them would be a forgery.
            "answerable": not any(word in lowered for word in _DO_NOT_ANSWER),
        })

    for item in spec.eligibility:
        requirements.append({
            "kind": "eligibility",
            "prompt": item,
            "guidance": "",
            "source": "the call's eligibility rules",
            "answerable": False,
        })

    held = pipeline_module.held_document_keys(profile)
    for key in (opportunity.payload or {}).get("required_documents") or []:
        requirements.append({
            "kind": "document",
            "prompt": profile_schema.document_label(
                profile.profile_type, profile.fields, str(key)),
            "guidance": "held" if str(key) in held else "not uploaded yet",
            "source": "the documents the call demands",
            "answerable": False,
        })

    return requirements


def _build_full_draft(
    opportunity_id: str, profile_id: str, steer: str | None = None
) -> None:
    """List what the call asks for, then answer each one in turn.

    This is deliberately the applicant's own process rather than a single
    "write me an application" call. The owner described it exactly: list the
    requirements, then work through them one at a time, answering each from
    your own strengths, qualifications and training.

    Four things changed here after the owner reported the output was not
    professional, and all four were about what the model was given rather
    than which model it was:

    * it now sees the applicant's actual CV and transcripts, not six one-line
      profile fields - the old prompt had never seen a word of their evidence;
    * it sees the whole list of requirements and what has already been
      written, so section six does not repeat section two;
    * it downloads the form the call tells the applicant to complete, and
      answers its questions as requirements like any other;
    * it writes a real covering letter instead of assembling one from a
      template.

    Runs in the background because it is slow by nature - nine sections at
    roughly a hundred seconds each - and opens its own session because the
    request that scheduled it is long gone.
    """
    session = db_module.SessionLocal()
    try:
        opportunity = session.get(models_db.StoredOpportunity, opportunity_id)
        profile = session.get(models_db.Profile, profile_id)
        if opportunity is None or profile is None:
            return

        settings = _ollama_settings()
        call_text = _page_text_for(opportunity)

        # --- 1. what does the call ask for? ------------------------------
        try:
            spec = application_spec_module.extract_submission_spec(call_text, **settings)
        except Exception:
            spec = application_spec_module.SubmissionSpec()

        # --- 2. is there a form to complete? -----------------------------
        form = _fetch_call_form(opportunity)

        # --- 3. who is applying, and what can they prove? ----------------
        subject = pipeline_module.profile_to_personal_profile(profile)
        dossier = evidence_module.dossier(subject, list(profile.documents))
        if steer:
            dossier += f"\n\nWhat the applicant wants this application to emphasise: {steer}"

        class _Opp:
            title = (opportunity.payload or {}).get("title") or "this opportunity"
            summary = call_text[:1400]

        opp = _Opp()
        requirements = _requirements_for(spec, form, opportunity, profile)

        # --- 4. answer each requirement, in order, aware of the others ----
        answerable = [r for r in requirements if r["answerable"]]
        as_sections = [
            application_spec_module.SectionSpec(
                title=r["prompt"], guidance=r["guidance"])
            for r in answerable
        ]
        budget = section_drafting_module.words_for_each_section(spec, len(as_sections) or 1)

        sections: list[dict] = []
        for index, requirement in enumerate(answerable):
            section = as_sections[index]
            # A form field wants a line, not an essay: "Full name" answered in
            # 175 words is not an answer, it is a problem.
            words = 40 if requirement["kind"] == "form_field" else budget

            # Each section sees only the evidence it should be writing from.
            # Telling a small model "this section is not a biography" does not
            # work - it was told exactly that and wrote one anyway, because
            # the CV was still the most concrete material in the prompt.
            # Removing the CV from an analytical prompt is what actually
            # stops it: there is then nothing to recite.
            kind = section_drafting_module.section_kind(section.title)
            if requirement["kind"] == "form_field":
                # A form asks for the applicant's own details. This is the one
                # place the whole CV is exactly the right thing to have.
                section_evidence = dossier
            else:
                section_evidence = evidence_module.for_section_kind(
                    kind, subject, list(profile.documents))
                if steer and kind != "title":
                    section_evidence += (
                        "\n\nWhat the applicant wants this application to "
                        "emphasise: " + steer
                    )

            body = section_drafting_module.draft_section(
                section, spec, opp, section_evidence,
                all_sections=as_sections, written=sections, max_words=words,
                **settings,
            )
            sections.append({
                "title": requirement["prompt"],
                "guidance": requirement["guidance"],
                "kind": requirement["kind"],
                "body": body,
            })

        # --- 5. the covering letter --------------------------------------
        cover_letter = section_drafting_module.draft_cover_letter(
            spec, opp, dossier, **settings
        )

        package = dict(opportunity.package or {})
        package["spec"] = spec.model_dump(mode="json")
        package["requirements"] = requirements
        package["sections"] = sections
        package["cover_letter"] = cover_letter
        if form:
            # The form's text is not kept - it can be large, and the list
            # endpoint would carry it to every browser. What is kept is what
            # the owner needs: where it is and what it asked.
            package["form"] = {k: v for k, v in form.items() if k != "text"}
        package["evidence_used"] = evidence_module.has_real_evidence(list(profile.documents))
        package["status"] = "ready" if sections else "no_structure_found"
        opportunity.package = package
        _record_event(
            session, opportunity, "full_draft",
            f"{len(sections)} requirements answered"
            + (f", form: {form['filename']}" if form else "")
            if sections else "no structure found in the call",
        )
        session.commit()
    except Exception:
        session.rollback()
        opportunity = session.get(models_db.StoredOpportunity, opportunity_id)
        if opportunity is not None:
            package = dict(opportunity.package or {})
            package["status"] = "failed"
            opportunity.package = package
            session.commit()
    finally:
        session.close()


# Injection point, same reason as the others: no test may reach a real model.
_full_draft_worker = _build_full_draft


@app.post("/profiles/{profile_id}/opportunities/{opportunity_id}/draft-full", status_code=202)
def draft_full_application(
    profile_id: str,
    opportunity_id: str,
    payload: FullDraftRequest,
    background: BackgroundTasks,
    account: models_db.Account = Depends(get_current_account),
    db: Session = Depends(get_db_session),
) -> dict[str, str]:
    """Write the document this particular call actually asked for.

    Deliberately on demand rather than part of every discovery cycle. Reading
    a call and drafting its sections costs minutes of model time; doing it
    unprompted for all 75 tenders sitting on one profile would spend hours
    writing proposals nobody asked for. The owner picks the one they care
    about.

    Returns 202 immediately - poll the opportunity and watch package.status.
    """
    profile = _get_owned_profile(profile_id, account, db)
    opportunity = _get_owned_opportunity(profile, opportunity_id, db)

    package = dict(opportunity.package or {})
    package["status"] = "drafting"
    opportunity.package = package
    db.commit()

    background.add_task(
        _full_draft_worker, opportunity.id, profile.id, payload.steer
    )
    return {"status": "drafting"}


class PackageEdit(BaseModel):
    sections: list[dict]
    # Optional so an older client that only sends sections does not silently
    # wipe the letter: None means "not edited", "" means "cleared".
    cover_letter: str | None = None


@app.put("/profiles/{profile_id}/opportunities/{opportunity_id}/package",
         response_model=OpportunityOut)
def edit_application_package(
    profile_id: str,
    opportunity_id: str,
    payload: PackageEdit,
    account: models_db.Account = Depends(get_current_account),
    db: Session = Depends(get_db_session),
) -> models_db.StoredOpportunity:
    """Save the owner's edits.

    The whole point of a draft is that it gets changed. Only the section
    bodies and titles are taken from the request - the spec read off the call
    is not the owner's to edit here, because it describes what the call
    demands rather than what they wrote.
    """
    profile = _get_owned_profile(profile_id, account, db)
    opportunity = _get_owned_opportunity(profile, opportunity_id, db)

    package = dict(opportunity.package or {})
    package["sections"] = [
        {
            "title": str(s.get("title") or "").strip(),
            "guidance": str(s.get("guidance") or ""),
            # Kept so the exporter can still tell a proposal section from a
            # form answer after the owner has edited them.
            "kind": str(s.get("kind") or "section"),
            "body": str(s.get("body") or ""),
        }
        for s in payload.sections
    ]
    if payload.cover_letter is not None:
        package["cover_letter"] = payload.cover_letter
    package["status"] = "edited"
    opportunity.package = package
    _record_event(db, opportunity, "edited", f"{len(package['sections'])} sections")
    db.commit()
    return opportunity


@app.get("/profiles/{profile_id}/opportunities/{opportunity_id}/document")
def download_application_document(
    profile_id: str,
    opportunity_id: str,
    account: models_db.Account = Depends(get_current_account),
    db: Session = Depends(get_db_session),
) -> Response:
    """The drafted application as a .docx, formatted the way the call demands.

    Generated rather than described: a call that says Times New Roman 12 at
    1.5 spacing is stating grounds for rejection before anyone reads the
    content, and telling the owner to go and set that themselves is leaving
    the last, most mechanical step undone.
    """
    profile = _get_owned_profile(profile_id, account, db)
    opportunity = _get_owned_opportunity(profile, opportunity_id, db)
    package = opportunity.package or {}
    sections = package.get("sections") or []
    if not sections:
        raise HTTPException(
            status_code=409,
            detail="draft the full application first - there are no sections to export",
        )

    blob = documents_module.build_application_docx(
        title=(opportunity.payload or {}).get("title") or "Application",
        sections=sections,
        format_rules=(package.get("spec") or {}).get("format_rules") or {},
        applicant=(profile.fields or {}).get("name") or "",
        cover_letter=str(package.get("cover_letter") or ""),
    )
    filename = "application.docx"
    return Response(
        content=blob,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class RequiredDocumentOut(BaseModel):
    key: str
    label: str
    held: bool
    # Whether this call asked for it, or whether it is part of the standing
    # compliance pack for this profile type. Different urgency, different
    # wording on screen.
    demanded_by_call: bool = False
    # The uploaded file satisfying this, when there is one, so the owner sees
    # *which* of their documents is being counted rather than a bare tick.
    document_id: str | None = None
    filename: str | None = None


class PrefillFieldOut(BaseModel):
    key: str
    label: str
    value: str


@app.get("/profiles/{profile_id}/opportunities/{opportunity_id}/requirements")
def opportunity_requirements(
    profile_id: str,
    opportunity_id: str,
    account: models_db.Account = Depends(get_current_account),
    db: Session = Depends(get_db_session),
) -> dict[str, object]:
    """Everything the detail page needs, in one request.

    The call in its own words, what it demands, which of those demands the
    owner's already-uploaded documents satisfy, and the values that can be
    filled in on a form on their behalf.

    One endpoint rather than four because it is one screen answering one
    question - "can I apply for this, and what is missing?" - and a page that
    fired four requests would show four loading states for it.
    """
    profile = _get_owned_profile(profile_id, account, db)
    opportunity = _get_owned_opportunity(profile, opportunity_id, db)
    payload = opportunity.payload or {}
    fields = profile.fields or {}

    # --- documents: what is demanded, against what is actually uploaded ---
    # Two sources of demand, deliberately merged. What this specific call
    # asked for, and what this profile type always needs (the compliance pack
    # in profile_schema): a tender that forgets to restate "tax clearance" in
    # its advert still needs one at submission.
    from_call = [
        str(d).strip() for d in (payload.get("required_documents") or []) if str(d).strip()
    ]
    demanded = list(from_call)
    for key in sorted(profile_schema.required_document_keys(profile.profile_type, fields)):
        if key not in demanded:
            demanded.append(key)

    # First upload of each kind wins - enough to show the owner the match is
    # real without listing every duplicate they have ever uploaded.
    uploaded_by_type: dict[str, models_db.Document] = {}
    for document in profile.documents:
        uploaded_by_type.setdefault(document.doc_type or "other", document)

    held = pipeline_module.held_document_keys(profile)
    documents: list[RequiredDocumentOut] = []
    for key in demanded:
        match = uploaded_by_type.get(key)
        documents.append(RequiredDocumentOut(
            key=key,
            label=profile_schema.document_label(profile.profile_type, fields, key),
            held=key in held,
            demanded_by_call=key in from_call,
            document_id=match.id if match else None,
            filename=match.original_filename if match else None,
        ))

    # --- what a form could be filled in with, from the profile -------------
    # Schema-driven, like the onboarding screen: a company gets its
    # registration and BP numbers, a person gets their study level. Only the
    # single-value fields, and only ones the owner actually filled in - the
    # standing rule here is that nothing is invented on their behalf.
    prefill = [PrefillFieldOut(key="email", label="Email address", value=account.email)]
    for spec in profile_schema.fields_for(profile.profile_type, fields):
        if spec.kind not in ("text", "number", "select"):
            continue
        value = fields.get(spec.key)
        if value in (None, "", [], {}):
            continue
        prefill.append(PrefillFieldOut(key=spec.key, label=spec.label, value=str(value)))

    package = opportunity.package or {}
    spec_payload = package.get("spec") or {}

    return {
        "id": opportunity.id,
        "title": payload.get("title") or "Untitled opportunity",
        "url": opportunity.canonical_url,
        "source": payload.get("source"),
        "deadline": payload.get("deadline"),
        "stage": opportunity.stage,
        "match_status": opportunity.match_status,
        "match_score": opportunity.match_score,
        "match_reasons": opportunity.match_reasons or {},
        "compliance": opportunity.compliance,
        # The call in its own words. This was empty for essentially every
        # opportunity until _page_text_for learned to read `evidence`, which
        # is why drafting produced the same generic letter every time.
        "call_text": _page_text_for(opportunity)[:20000],
        "submission": {
            "document_kind": spec_payload.get("document_kind") or "",
            "sections": spec_payload.get("sections") or [],
            "format_rules": spec_payload.get("format_rules") or {},
            "submit_to": spec_payload.get("submit_to"),
            "deadline": spec_payload.get("deadline"),
            "eligibility": spec_payload.get("eligibility") or [],
        },
        "required_documents": [d.model_dump() for d in documents],
        "documents_ready": sum(1 for d in documents if d.held),
        "documents_total": len(documents),
        "prefill": [p.model_dump() for p in prefill],
        "draft": {
            "status": package.get("status"),
            "sections": package.get("sections") or [],
            # The list the owner asked for: everything this call demands, in
            # one place, whichever part of the call it came from.
            "requirements": package.get("requirements") or [],
            "cover_letter": package.get("cover_letter") or "",
            "form": package.get("form"),
            # Whether the draft was written with sight of their documents or
            # only their profile fields. A thin draft written from six fields
            # is doing its best with what it was given, and saying so is more
            # use than letting them conclude the system cannot write.
            "evidence_used": bool(package.get("evidence_used")),
        },
    }


@app.get("/profiles/{profile_id}/summary")
def profile_summary(
    profile_id: str,
    account: models_db.Account = Depends(get_current_account),
    db: Session = Depends(get_db_session),
) -> dict[str, object]:
    profile = _get_owned_profile(profile_id, account, db)
    rows = db.query(models_db.StoredOpportunity).filter_by(profile_id=profile.id).all()
    last_run = (
        db.query(models_db.ProfileDiscoveryRun)
        .filter_by(profile_id=profile.id)
        .order_by(models_db.ProfileDiscoveryRun.completed_at.desc())
        .first()
    )
    # Why is the queue empty? The system already knows - every opportunity it
    # could not clear carries the reason - but until now it kept that to
    # itself and showed a blank dashboard, which reads as "nothing found"
    # when the truth is "75 things found, all waiting on one field you have
    # not filled in".
    stuck: Counter[str] = Counter()
    for row in rows:
        if row.stage != "discovered" or row.match_status == "eligible":
            continue
        reasons = row.match_reasons or {}
        for reason in list(reasons.get("unknown") or []) + list(reasons.get("failed") or []):
            stuck[str(reason)] += 1

    return {
        "total": len(rows),
        "awaiting_review": sum(1 for r in rows if r.stage == "drafted"),
        "approved": sum(1 for r in rows if r.stage == "approved"),
        "submitted": sum(1 for r in rows if r.stage == "submitted"),
        "not_eligible": sum(1 for r in rows if r.match_status != "eligible" and r.stage == "discovered"),
        "last_run": last_run.completed_at.isoformat() if last_run else None,
        "last_run_found": last_run.found if last_run else None,
        "last_run_failures": list(last_run.failures or []) if last_run else [],
        # Most common first: the one to fix is nearly always the one blocking
        # the most opportunities.
        "blockers": [
            {"reason": reason, "count": count} for reason, count in stuck.most_common(5)
        ],
    }


@app.get("/notifications", response_model=list[NotificationOut])
def list_notifications(
    unread: bool = False,
    account: models_db.Account = Depends(get_current_account),
    db: Session = Depends(get_db_session),
) -> list[models_db.Notification]:
    query = db.query(models_db.Notification).filter_by(account_id=account.id)
    if unread:
        query = query.filter(models_db.Notification.read_at.is_(None))
    return query.order_by(models_db.Notification.created_at.desc()).all()


@app.post("/notifications/{notification_id}/read", response_model=NotificationOut)
def mark_notification_read(
    notification_id: str,
    account: models_db.Account = Depends(get_current_account),
    db: Session = Depends(get_db_session),
) -> models_db.Notification:
    notification = db.get(models_db.Notification, notification_id)
    if notification is None or notification.account_id != account.id:
        raise HTTPException(status_code=404, detail="notification not found")
    notification.read_at = datetime.now(timezone.utc)
    db.commit()
    return notification


@app.get("/ui", response_class=HTMLResponse)
def review_ui() -> str:
    """Interactive review page (src/opportunity_agent/web/index.html).

    A single-page app talking to the JSON API below over fetch(): profile
    editing, discovery, filtering, per-opportunity feedback, and the
    application package all happen in place, no full-page reloads or
    server-rendered HTML forms. See AGENTS.md for why this replaced the
    earlier server-rendered /ui.
    """
    return _UI_PAGE
