from opportunity_agent.discovery import build_search_queries
from opportunity_agent.models import PersonalProfile


def test_search_queries_use_profile_goals_and_constraints():
    profile = PersonalProfile(
        name="Test Applicant",
        country="Zimbabwe",
        study_level="masters",
        field="Computer Science",
        goals=["climate technology"],
        preferred_countries=["Zimbabwe", "South Africa"],
        interests=["renewable energy"],
    )

    queries = build_search_queries(profile)

    assert queries
    assert any("masters" in query.lower() for query in queries)
    assert any("climate technology" in query.lower() for query in queries)
    assert any("scholarship" in query.lower() for query in queries)
