import os
from datetime import datetime, timezone
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
from . import documents as documents_module
from . import models_db
from . import pipeline as pipeline_module
from . import profile_schema
from .digest import build_digest
from .discovery import build_search_queries, discover
from .connector import fetch_public_page
from .document_text import extract_text
from .drafting import build_application_package
from . import egp_session
from .extraction import PARSER_VERSION, page_to_opportunity
from .extraction_llm import ExtractedFacts, extract_facts_from_text
from .matching import match_opportunity
from .models import Opportunity, PersonalProfile
from .store import OpportunityStore

app = FastAPI(title="OpportunityAI")
store = OpportunityStore(path=os.getenv("OPPORTUNITY_AGENT_STORE_PATH", ".data/store.json"))
_UI_PAGE = (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
db_module.init_db()

SESSION_COOKIE = "session"
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


def _extract_facts_from_document(content: bytes, content_type: str) -> ExtractedFacts:
    text = extract_text(content, content_type)
    return extract_facts_from_text(
        text,
        base_url=os.getenv("OLLAMA_URL", "http://localhost:11434"),
        model=os.getenv("OLLAMA_MODEL", "qwen2.5:0.5b"),
    )


class Feedback(BaseModel):
    decision: str


class RegisterRequest(BaseModel):
    email: str
    password: str


class LoginRequest(BaseModel):
    email: str
    password: str


class AccountOut(BaseModel):
    id: str
    email: str


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


def _set_session_cookie(response: Response, account_id: str) -> None:
    token = auth_module.create_session_token(account_id)
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="lax",
        max_age=int(auth_module.TOKEN_TTL.total_seconds()),
    )


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """A user typing just the domain name should land somewhere real, not a
    bare JSON 404 - found by the owner doing exactly that on the live deploy."""
    return RedirectResponse(url="/ui")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/register", response_model=AccountOut, status_code=201)
def register(payload: RegisterRequest, response: Response, db: Session = Depends(get_db_session)) -> models_db.Account:
    if db.query(models_db.Account).count() > 0:
        raise HTTPException(
            status_code=403,
            detail="registration is closed - this deployment supports one account until "
            "the pipeline is multi-tenant (SOLUTION_DEFINITION.md §14)",
        )
    if db.query(models_db.Account).filter_by(email=payload.email).first() is not None:
        raise HTTPException(status_code=409, detail="an account with this email already exists")
    account = models_db.Account(email=payload.email, password_hash=auth_module.hash_password(payload.password))
    db.add(account)
    db.commit()
    _set_session_cookie(response, account.id)
    return account


@app.post("/login", response_model=AccountOut)
def login(payload: LoginRequest, response: Response, db: Session = Depends(get_db_session)) -> models_db.Account:
    account = db.query(models_db.Account).filter_by(email=payload.email).first()
    if account is None or not auth_module.verify_password(payload.password, account.password_hash):
        raise HTTPException(status_code=401, detail="invalid email or password")
    _set_session_cookie(response, account.id)
    return account


@app.post("/logout")
def logout(response: Response) -> dict[str, str]:
    response.delete_cookie(SESSION_COOKIE)
    return {"status": "logged out"}


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

    ok, message = _verify_egp_credentials(credential.username, secret)
    credential.verification_status = "verified" if ok else "failed"
    credential.verification_detail = message
    credential.last_verified_at = datetime.now(timezone.utc) if ok else None
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
    return {
        "total": len(rows),
        "awaiting_review": sum(1 for r in rows if r.stage == "drafted"),
        "approved": sum(1 for r in rows if r.stage == "approved"),
        "submitted": sum(1 for r in rows if r.stage == "submitted"),
        "not_eligible": sum(1 for r in rows if r.match_status != "eligible" and r.stage == "discovered"),
        "last_run": last_run.completed_at.isoformat() if last_run else None,
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


@app.get("/profile", response_model=PersonalProfile | None)
def get_profile(account: models_db.Account = Depends(get_current_account)) -> PersonalProfile | None:
    return store.profile


@app.put("/profile", response_model=PersonalProfile)
def save_profile(
    profile: PersonalProfile, account: models_db.Account = Depends(get_current_account)
) -> PersonalProfile:
    return store.save_profile(profile)


@app.post("/opportunities", status_code=201)
def add_opportunity(
    opportunity: Opportunity, account: models_db.Account = Depends(get_current_account)
) -> dict[str, object]:
    stored = store.add_opportunity(opportunity)
    return {"id": stored.id, "opportunity": stored.opportunity}


@app.post("/discover")
def run_discovery(account: models_db.Account = Depends(get_current_account)) -> dict[str, object]:
    if store.profile is None:
        raise HTTPException(status_code=409, detail="profile is required")
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        raise HTTPException(status_code=503, detail="TAVILY_API_KEY is not configured")
    started_at = datetime.now(timezone.utc).isoformat()
    queries = build_search_queries(store.profile)
    try:
        results = discover(store.profile, api_key=api_key)
    except Exception as error:
        run = store.record_run(
            queries=queries,
            found=0,
            added=0,
            failures=[f"search: {error}"],
            started_at=started_at,
            sources=[],
        )
        raise HTTPException(status_code=502, detail=f"discovery failed; run {run.id}") from error
    added = []
    failures = []
    sources = []
    for result in results:
        try:
            page = fetch_public_page(result.url)
            title = result.title or "Untitled scholarship opportunity"
            opportunity = page_to_opportunity(page, title=title)
        except Exception as error:
            failures.append(f"{result.url}: {error}")
            sources.append({"url": result.url, "status": "failed", "error": str(error)})
            continue
        try:
            added.append(store.add_opportunity(opportunity))
        except Exception as error:
            failures.append(f"{result.url}: {error}")
            sources.append({"url": result.url, "status": "failed", "error": str(error)})
            continue
        sources.append({
            "url": page.url,
            "status": "parsed",
            "content_type": page.content_type,
            "parser_version": PARSER_VERSION,
        })
    run = store.record_run(
        queries=queries,
        found=len(results),
        added=len(added),
        failures=failures,
        started_at=started_at,
        sources=sources,
    )
    return {
        "run_id": run.id,
        "found": len(results),
        "added": len(added),
        "opportunities": [stored.opportunity for stored in added],
    }


@app.get("/runs")
def get_runs(account: models_db.Account = Depends(get_current_account)) -> list[dict[str, object]]:
    return [run.__dict__ for run in reversed(store.runs)]


@app.get("/opportunities/{opportunity_id}/package")
def get_application_package(
    opportunity_id: str, account: models_db.Account = Depends(get_current_account)
):
    if store.profile is None:
        raise HTTPException(status_code=409, detail="profile is required")
    try:
        stored = store.get_opportunity(opportunity_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="opportunity not found") from error
    match = match_opportunity(stored.opportunity, store.profile)
    return build_application_package(store.profile, stored.opportunity, match)


@app.get("/matches")
def get_matches(account: models_db.Account = Depends(get_current_account)) -> list[dict[str, object]]:
    try:
        matches = store.matches()
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return [
        {
            "id": stored.id,
            "opportunity": stored.opportunity,
            "match": result,
            "decision": stored.decision,
            "usefulness": stored.usefulness,
        }
        for stored, result in matches
    ]


@app.post("/opportunities/{opportunity_id}/feedback")
def record_feedback(
    opportunity_id: str, feedback: Feedback, account: models_db.Account = Depends(get_current_account)
) -> dict[str, str]:
    if feedback.decision not in {"shortlisted", "dismissed", "useful", "not_useful"}:
        raise HTTPException(status_code=422, detail="unsupported feedback decision")
    try:
        stored = store.set_feedback(opportunity_id, feedback.decision)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="opportunity not found") from error
    return {
        "id": stored.id,
        "decision": stored.decision or "",
        "usefulness": stored.usefulness or "",
    }


@app.get("/digest")
def get_digest(account: models_db.Account = Depends(get_current_account)) -> dict[str, str]:
    try:
        matches = store.matches()
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    content = build_digest([
        (stored.opportunity, result)
        for stored, result in matches
        if stored.decision != "dismissed"
    ])
    return {"content": content}
