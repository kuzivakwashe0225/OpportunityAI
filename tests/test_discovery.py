from opportunity_agent.discovery import build_search_queries, discover
from opportunity_agent.models import PersonalProfile
from opportunity_agent.search import SearchResult


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


def test_discover_uses_search_client_for_profile_queries():
    profile = PersonalProfile(name="Applicant", field="Computer Science", country="Zimbabwe")
    queries: list[str] = []

    def fake_search(query: str, *, api_key: str, max_results: int = 5):
        queries.append(query)
        return [SearchResult(title="Award", url="https://example.org/award", content="Eligibility")]

    results = discover(profile, api_key="test-key", search_fn=fake_search)

    assert queries
    assert all("scholarship" in query.lower() for query in queries)
    assert results == [SearchResult(title="Award", url="https://example.org/award", content="Eligibility")]


def test_discover_deduplicates_results_across_queries():
    profile = PersonalProfile(
        name="Applicant",
        field="Computer Science",
        interests=["Computer Science"],
        country="Zimbabwe",
    )

    def fake_search(query: str, *, api_key: str, max_results: int = 5):
        return [SearchResult(title=query, url="https://example.org/award", content=query)]

    results = discover(profile, api_key="test-key", search_fn=fake_search)

    assert len(results) == 1


def test_queries_include_certificates_and_work_history():
    profile = PersonalProfile(
        name="Applicant",
        certificates=["data science"],
        work_history=["climate research"],
    )

    queries = build_search_queries(profile)

    assert any("data science" in query.lower() for query in queries)
    assert any("climate research" in query.lower() for query in queries)
