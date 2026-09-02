from dataclasses import dataclass, field
import json
from pathlib import Path
from uuid import uuid4

from .matching import match_opportunity
from .models import MatchResult, Opportunity, PersonalProfile


@dataclass
class StoredOpportunity:
    id: str
    opportunity: Opportunity
    decision: str | None = None
    usefulness: str | None = None


@dataclass
class OpportunityStore:
    profile: PersonalProfile | None = None
    opportunities: list[StoredOpportunity] = field(default_factory=list)
    path: Path | str | None = None

    def __post_init__(self) -> None:
        if self.path is not None:
            self._load()

    def reset(self) -> None:
        self.profile = None
        self.opportunities.clear()
        self._persist()

    def save_profile(self, profile: PersonalProfile) -> PersonalProfile:
        self.profile = profile
        self._persist()
        return profile

    def add_opportunity(self, opportunity: Opportunity) -> StoredOpportunity:
        stored = StoredOpportunity(id=str(uuid4()), opportunity=opportunity)
        self.opportunities.append(stored)
        self._persist()
        return stored

    def matches(self) -> list[tuple[StoredOpportunity, MatchResult]]:
        if self.profile is None:
            raise ValueError("profile is required")
        return [
            (stored, match_opportunity(stored.opportunity, self.profile))
            for stored in self.opportunities
        ]

    def set_feedback(self, opportunity_id: str, feedback: str) -> StoredOpportunity:
        for stored in self.opportunities:
            if stored.id == opportunity_id:
                if feedback in {"shortlisted", "dismissed"}:
                    stored.decision = feedback
                else:
                    stored.usefulness = feedback
                self._persist()
                return stored
        raise KeyError(opportunity_id)

    def _persist(self) -> None:
        if self.path is None:
            return
        path = Path(self.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "profile": self.profile.model_dump(mode="json") if self.profile else None,
            "opportunities": [
                {
                    "id": stored.id,
                    "opportunity": stored.opportunity.model_dump(mode="json"),
                    "decision": stored.decision,
                    "usefulness": stored.usefulness,
                }
                for stored in self.opportunities
            ],
        }
        temporary = path.with_suffix(f"{path.suffix}.tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(path)

    def _load(self) -> None:
        path = Path(self.path)  # type: ignore[arg-type]
        if not path.exists():
            return
        payload = json.loads(path.read_text(encoding="utf-8"))
        profile_data = payload.get("profile")
        self.profile = PersonalProfile.model_validate(profile_data) if profile_data else None
        self.opportunities = [
            StoredOpportunity(
                id=item["id"],
                opportunity=Opportunity.model_validate(item["opportunity"]),
                decision=item.get("decision"),
                usefulness=item.get("usefulness"),
            )
            for item in payload.get("opportunities", [])
        ]
