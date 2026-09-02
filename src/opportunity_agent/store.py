from dataclasses import dataclass, field
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

    def reset(self) -> None:
        self.profile = None
        self.opportunities.clear()

    def save_profile(self, profile: PersonalProfile) -> PersonalProfile:
        self.profile = profile
        return profile

    def add_opportunity(self, opportunity: Opportunity) -> StoredOpportunity:
        stored = StoredOpportunity(id=str(uuid4()), opportunity=opportunity)
        self.opportunities.append(stored)
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
                return stored
        raise KeyError(opportunity_id)
