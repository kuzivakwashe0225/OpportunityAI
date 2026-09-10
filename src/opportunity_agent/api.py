import os
import secrets
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import jwt
from dotenv import load_dotenv
from fastapi import Cookie, Depends, FastAPI, File, Form, Response, UploadFile
from fastapi import HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
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
from .connector import fetch_public_page
from .document_text import extract_text
from . import egp_session
from . import extraction_llm as extraction_llm_module
from .extraction_llm import ExtractedFacts, extract_facts_from_text

app = FastAPI(title="OpportunityAI")
_UI_PAGE = (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
db_module.init_db()

SESSION_COOKIE = "session"
TEMP_PASSWORD_TTL = timedelta(days=7)
MIN_PASSWORD_LENGTH = 10
APP_URL = os.getenv("APP_URL", "https://opportunityai.meshcloud.co.zw/ui")

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


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """A user typing just the domain name should land somewhere real, not a
    bare JSON 404 - found by the owner doing exactly that on the live deploy."""
    return RedirectResponse(url="/ui")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/register", response_model=AccountOut, status_code=201)
def register(payload: RegisterRequest, db: Session = Depends(get_db_session)) -> models_db.Account:
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
    if db.query(models_db.Account).filter_by(email=email).first() is not None:
        # Note this does leak whether an address is registered. That is a
        # deliberate trade for a system with a named, known set of users: the
        # alternative - reporting success and sending nothing - makes "I never
        # got the email" unanswerable.
        raise HTTPException(status_code=409, detail="an account with this email already exists")

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
    return account


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
    payload: ForgotPasswordRequest, db: Session = Depends(get_db_session)
) -> dict[str, str]:
    """Email a fresh temporary password to an account that has lost its own.

    Order matters and is the same discipline as registration: the mail is sent
    *before* the new hash is stored. Overwriting the password first and then
    failing to deliver would lock the owner out of their own account using a
    password that exists nowhere - strictly worse than the state they were
    already in.

    This reports whether the address is registered, which is a leak. It is the
    same trade `register` already documents and makes deliberately: /register
    answers the same question to anyone who asks, so refusing to answer it
    here would be theatre, while "I never got the email" would become
    unanswerable for a real user.
    """
    email = payload.email.strip().lower()
    account = db.query(models_db.Account).filter_by(email=email).first()
    if account is None:
        raise HTTPException(status_code=404, detail="no account with this email address")

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


@app.post("/login", response_model=AccountOut)
def login(payload: LoginRequest, response: Response, db: Session = Depends(get_db_session)) -> models_db.Account:
    account = db.query(models_db.Account).filter_by(email=payload.email.strip().lower()).first()
    if account is None or not auth_module.verify_password(payload.password, account.password_hash):
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
    if not api_key:
        raise HTTPException(status_code=503, detail="TAVILY_API_KEY is not configured")

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


@app.get("/profiles/{profile_id}/opportunities", response_model=list[OpportunityOut])
def list_profile_opportunities(
    profile_id: str,
    stage: str | None = None,
    match_status: str | None = None,
    account: models_db.Account = Depends(get_current_account),
    db: Session = Depends(get_db_session),
) -> list[models_db.StoredOpportunity]:
    profile = _get_owned_profile(profile_id, account, db)
    query = db.query(models_db.StoredOpportunity).filter_by(profile_id=profile.id)
    if stage:
        query = query.filter_by(stage=stage)
    if match_status:
        query = query.filter_by(match_status=match_status)
    return query.order_by(models_db.StoredOpportunity.created_at.desc()).all()


@app.get("/profiles/{profile_id}/opportunities/{opportunity_id}", response_model=OpportunityOut)
def get_profile_opportunity(
    profile_id: str,
    opportunity_id: str,
    account: models_db.Account = Depends(get_current_account),
    db: Session = Depends(get_db_session),
) -> models_db.StoredOpportunity:
    profile = _get_owned_profile(profile_id, account, db)
    return _get_owned_opportunity(profile, opportunity_id, db)


def _set_stage(
    profile_id: str, opportunity_id: str, stage: str, account: models_db.Account, db: Session
) -> models_db.StoredOpportunity:
    profile = _get_owned_profile(profile_id, account, db)
    opportunity = _get_owned_opportunity(profile, opportunity_id, db)
    opportunity.stage = stage
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
