import os

from fastapi import FastAPI
from fastapi import HTTPException
from pydantic import BaseModel

from .digest import build_digest
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
