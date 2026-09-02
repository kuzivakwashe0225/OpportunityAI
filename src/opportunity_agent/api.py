import os
from datetime import datetime, timezone

from dotenv import load_dotenv
import httpx
from fastapi import FastAPI
from fastapi import HTTPException
from pydantic import BaseModel

load_dotenv()

from .digest import build_digest
from .discovery import build_search_queries, discover
from .connector import fetch_public_page
from .drafting import build_application_package
from .extraction import PARSER_VERSION, page_to_opportunity
from .matching import match_opportunity
from .models import Opportunity, PersonalProfile
from .store import OpportunityStore

app = FastAPI(title="Opportunity Agent")
store = OpportunityStore(path=os.getenv("OPPORTUNITY_AGENT_STORE_PATH", ".data/store.json"))


class Feedback(BaseModel):
    decision: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.put("/profile", response_model=PersonalProfile)
def save_profile(profile: PersonalProfile) -> PersonalProfile:
    return store.save_profile(profile)


@app.post("/opportunities", status_code=201)
def add_opportunity(opportunity: Opportunity) -> dict[str, object]:
    stored = store.add_opportunity(opportunity)
    return {"id": stored.id, "opportunity": stored.opportunity}


@app.post("/discover")
def run_discovery() -> dict[str, object]:
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
def get_runs() -> list[dict[str, object]]:
    return [run.__dict__ for run in reversed(store.runs)]


@app.get("/opportunities/{opportunity_id}/package")
def get_application_package(opportunity_id: str):
    if store.profile is None:
        raise HTTPException(status_code=409, detail="profile is required")
    try:
        stored = store.get_opportunity(opportunity_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="opportunity not found") from error
    match = match_opportunity(stored.opportunity, store.profile)
    return build_application_package(store.profile, stored.opportunity, match)


@app.get("/matches")
def get_matches() -> list[dict[str, object]]:
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
def record_feedback(opportunity_id: str, feedback: Feedback) -> dict[str, str]:
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
def get_digest() -> dict[str, str]:
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
