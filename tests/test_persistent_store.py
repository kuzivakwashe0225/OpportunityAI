from datetime import date

from opportunity_agent.models import Opportunity, PersonalProfile
from opportunity_agent.store import OpportunityStore


def profile():
    return PersonalProfile(
        name="Test Applicant",
        country="Zimbabwe",
        age=29,
        study_level="masters",
        field="Computer Science",
        documents=["cv"],
        goals=["study climate technology"],
    )


def opportunity():
    return Opportunity(
        source="Example Foundation",
        title="Climate Scholarship",
        url="https://example.org/climate",
        deadline=date(2026, 12, 1),
        evidence=["official page"],
    )


def test_store_restores_profile_and_opportunities_after_restart(tmp_path):
    path = tmp_path / "store.json"
    first = OpportunityStore(path=path)
    first.save_profile(profile())
    stored = first.add_opportunity(opportunity())
    first.set_feedback(stored.id, "shortlisted")

    second = OpportunityStore(path=path)

    assert second.profile == profile()
    assert len(second.opportunities) == 1
    assert second.opportunities[0].id == stored.id
    assert second.opportunities[0].decision == "shortlisted"


def test_store_reset_persists_empty_state(tmp_path):
    path = tmp_path / "store.json"
    store = OpportunityStore(path=path)
    store.save_profile(profile())
    store.reset()

    restored = OpportunityStore(path=path)

    assert restored.profile is None
    assert restored.opportunities == []
