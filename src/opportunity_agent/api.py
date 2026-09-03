import os
from datetime import datetime, timezone
from pathlib import Path

import jwt
from dotenv import load_dotenv
from fastapi import Cookie, Depends, FastAPI, Response
from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

load_dotenv()

from . import auth as auth_module
from . import db as db_module
from . import models_db
from .digest import build_digest
from .discovery import build_search_queries, discover
from .connector import fetch_public_page
from .drafting import build_application_package
from .extraction import PARSER_VERSION, page_to_opportunity
from .matching import match_opportunity
from .models import Opportunity, PersonalProfile
from .store import OpportunityStore

app = FastAPI(title="OpportunityAI")
store = OpportunityStore(path=os.getenv("OPPORTUNITY_AGENT_STORE_PATH", ".data/store.json"))
_UI_PAGE = (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
db_module.init_db()

SESSION_COOKIE = "session"


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
