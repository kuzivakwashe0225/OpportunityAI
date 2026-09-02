import os
from html import escape
from datetime import datetime, timezone
from urllib.parse import parse_qs

from dotenv import load_dotenv
import httpx
from fastapi import FastAPI, Request
from fastapi import HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
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


@app.get("/ui", response_class=HTMLResponse)
def review_ui() -> str:
    if store.profile is None:
        body = "<p class='empty'>No profile yet. Add one through the profile API.</p>"
    else:
        entries = []
        for stored, result in store.matches():
            if stored.decision == "dismissed":
                continue
            opportunity = stored.opportunity
            actions = (
                f"<form method='post' action='/ui/opportunities/{stored.id}/feedback'>"
                "<button name='decision' value='shortlisted'>Shortlist</button>"
                "<button name='decision' value='dismissed'>Dismiss</button>"
                "<button name='decision' value='useful'>Useful</button>"
                "<button name='decision' value='not_useful'>Not useful</button></form>"
            )
            blockers = result.failed_requirements + result.unknown_requirements
            blocker_html = (
                f"<p class='blockers'><strong>Review:</strong> {escape('; '.join(blockers))}</p>"
                if blockers else ""
            )
            entries.append(
                "<article class='opportunity'>"
                f"<h2>{escape(opportunity.title)}</h2>"
                f"<p class='meta'>{escape(opportunity.source)} · {result.status} · score {result.score}</p>"
                f"<p class='meta'>Deadline: {escape(str(opportunity.deadline or 'Not specified'))}</p>"
                f"{blocker_html}"
                f"<p>{escape(opportunity.evidence[0]) if opportunity.evidence else 'Evidence pending review.'}</p>"
                f"<a href='/ui/opportunities/{stored.id}/package'>Open application package</a>{actions}"
                "</article>"
            )
        body = "".join(entries) or "<p class='empty'>No opportunities yet.</p>"
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Scholarship Scout</title><style>"
        ":root{font-family:Georgia,serif;color:#18211b;background:#f4f0e8}"
        "body{margin:0}main{max-width:900px;margin:auto;padding:48px 24px}"
        "h1{font-size:clamp(2rem,5vw,4rem);margin:0 0 8px}"
        ".intro{color:#536157;margin-bottom:32px}.opportunity{background:#fffdf8;"
        "border:1px solid #d8d2c5;border-left:5px solid #bc5b35;padding:20px;margin:16px 0}"
        ".meta{color:#536157;font-family:ui-sans-serif,system-ui,sans-serif;font-size:.9rem}"
        "a{color:#934326;font-weight:bold}form{display:flex;gap:8px;margin-top:18px}"
        "button{border:1px solid #934326;background:#934326;color:white;padding:9px 13px;cursor:pointer}"
        "button[value=dismissed]{background:transparent;color:#934326}.empty{padding:24px 0}"
        "</style></head><body><main><h1>Scholarship Scout</h1>"
        "<p class='intro'>A quiet inbox for opportunities worth your attention.</p>"
        f"{body}</main></body></html>"
    )


@app.post("/ui/opportunities/{opportunity_id}/feedback")
async def review_feedback(opportunity_id: str, request: Request) -> RedirectResponse:
    values = parse_qs((await request.body()).decode("utf-8"))
    decision = values.get("decision", [""])[0]
    if decision not in {"shortlisted", "dismissed", "useful", "not_useful"}:
        raise HTTPException(status_code=422, detail="unsupported feedback decision")
    try:
        store.set_feedback(opportunity_id, decision)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="opportunity not found") from error
    return RedirectResponse(url="/ui", status_code=303)


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


@app.get("/ui/opportunities/{opportunity_id}/package", response_class=HTMLResponse)
def review_package_ui(opportunity_id: str) -> str:
    if store.profile is None:
        raise HTTPException(status_code=409, detail="profile is required")
    try:
        stored = store.get_opportunity(opportunity_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="opportunity not found") from error
    package = build_application_package(
        store.profile,
        stored.opportunity,
        match_opportunity(stored.opportunity, store.profile),
    )
    checklist = "".join(
        f"<li>{escape(item.status)}: {escape(item.requirement)}</li>"
        for item in package.checklist
    )
    warnings = "".join(f"<li>{escape(warning)}</li>" for warning in package.warnings)
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<title>Application package: {escape(stored.opportunity.title)}</title></head><body>"
        f"<main><p><a href='/ui'>Back to Scholarship Scout</a></p>"
        f"<h1>Application package</h1><h2>{escape(stored.opportunity.title)}</h2>"
        f"<p><a href='{escape(str(stored.opportunity.url))}'>Open official opportunity</a></p>"
        f"<h3>Checklist</h3><ul>{checklist}</ul>"
        f"<h3>Draft cover note</h3><pre>{escape(package.cover_note)}</pre>"
        f"<h3>Warnings</h3><ul>{warnings or '<li>None</li>'}</ul>"
        "</main></body></html>"
    )


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
