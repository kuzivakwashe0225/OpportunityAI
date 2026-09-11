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
    # Every query uses the scholarship *vocabulary* - not necessarily the
    # single word "scholarship". Searching only that word missed funded
    # positions advertised as fellowships or bursaries.
    vocabulary = ("scholarship", "fellowship", "bursary", "funded")
    assert all(any(term in query.lower() for term in vocabulary) for query in queries)
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


def test_a_tender_profile_does_not_search_for_scholarships():
    """The bug this parameter exists for: build_search_queries appended the
    literal word "scholarship" to every query for every profile type, so a
    company looking for contracts was searching the scholarship web."""
    from opportunity_agent.discovery import build_search_queries
    from opportunity_agent.models import OrganisationProfile

    company = OrganisationProfile(
        name="Meshcloud", country="Zimbabwe", sectors=["ICT hardware"]
    )
    queries = build_search_queries(company, "tender")

    assert queries
    assert not any("scholarship" in query.lower() for query in queries)
    assert any("tender" in query.lower() for query in queries)
    assert any("ICT hardware" in query for query in queries)


def test_a_company_is_never_asked_for_a_study_level_in_a_query():
    from opportunity_agent.discovery import build_search_queries
    from opportunity_agent.models import OrganisationProfile

    company = OrganisationProfile(name="Meshcloud", country="Zimbabwe", sectors=["catering"])
    for query in build_search_queries(company, "tender"):
        assert "masters" not in query.lower()
        assert "graduate" not in query.lower()


def test_a_job_profile_searches_for_jobs():
    from opportunity_agent.discovery import build_search_queries

    profile = PersonalProfile(name="A", field="Computer Science", country="Zimbabwe")
    queries = build_search_queries(profile, "job")

    assert queries
    assert all("scholarship" not in query.lower() for query in queries)
    assert any("job" in query.lower() for query in queries)


# --------------------------------------------------------------------------
# Being a considerate caller of a self-hosted, rate-limitable backend
# --------------------------------------------------------------------------
# discover() fans queries out to a search backend that, when self-hosted via
# SearXNG, in turn fans each one out to several upstream engines on the
# server's own behalf. These three behaviours exist so that heavy, repeated
# polling of an unchanged profile does not read as an attack pattern to those
# upstream engines.

import httpx
import pytest


def test_a_cached_query_never_reaches_the_backend():
    profile = PersonalProfile(name="A", field="Computer Science", country="Zimbabwe")
    calls = []

    def fake_search(query, *, api_key, max_results=5):
        calls.append(query)
        return [SearchResult(title="Fresh", url="https://example.org/fresh", content="x")]

    cached = {}

    def cache_get(query):
        return cached.get(query)

    def cache_set(query, results):
        cached[query] = results

    first = discover(profile, search_fn=fake_search, cache_get=cache_get, cache_set=cache_set)
    calls_after_first = len(calls)
    second = discover(profile, search_fn=fake_search, cache_get=cache_get, cache_set=cache_set)

    assert calls_after_first > 0
    assert len(calls) == calls_after_first, "second call must be served entirely from cache"
    assert first == second


def test_a_cache_miss_falls_through_to_the_backend_and_is_stored():
    profile = PersonalProfile(name="A", field="Computer Science", country="Zimbabwe")
    stored = {}

    def fake_search(query, *, api_key, max_results=5):
        return [SearchResult(title="X", url="https://example.org/x", content="")]

    discover(profile, search_fn=fake_search,
            cache_get=lambda q: None, cache_set=lambda q, r: stored.__setitem__(q, r))

    assert stored, "a miss must populate the cache for next time"


def test_without_cache_hooks_every_query_reaches_the_backend_as_before():
    """No regression for the many existing callers that pass neither hook."""
    profile = PersonalProfile(name="A", field="Computer Science", country="Zimbabwe")
    calls = []

    def fake_search(query, *, api_key, max_results=5):
        calls.append(query)
        return []

    discover(profile, search_fn=fake_search)
    discover(profile, search_fn=fake_search)

    assert len(calls) > 0
    assert len(calls) % 2 == 0, "identical, uncached calls must both reach the backend"


def test_pause_seconds_is_not_applied_before_the_first_query_or_on_a_cache_hit(monkeypatch):
    """Pacing only matters between real network calls. Sleeping before the
    first one, or on a cache hit that costs nothing, would just be slow for
    no reason."""
    import opportunity_agent.discovery as discovery_module
    sleeps = []
    monkeypatch.setattr(discovery_module.time, "sleep", lambda s: sleeps.append(s))

    profile = PersonalProfile(name="A", field="Computer Science", country="Zimbabwe")

    def fake_search(query, *, api_key, max_results=5):
        return []

    # All cached: no network calls at all, so no pause should fire either.
    discover(profile, search_fn=fake_search, pause_seconds=5.0,
            cache_get=lambda q: [], cache_set=lambda q, r: None)

    assert sleeps == []


def test_pause_seconds_separates_real_backend_calls(monkeypatch):
    import opportunity_agent.discovery as discovery_module
    sleeps = []
    monkeypatch.setattr(discovery_module.time, "sleep", lambda s: sleeps.append(s))

    profile = PersonalProfile(
        name="A", field="Computer Science", country="Zimbabwe",
        interests=["a", "b", "c"],
    )

    def fake_search(query, *, api_key, max_results=5):
        return []

    discover(profile, search_fn=fake_search, pause_seconds=2.0)

    # One fewer pause than queries: none before the first.
    assert sleeps
    assert all(s == 2.0 for s in sleeps)


def test_a_429_stops_the_cycle_but_keeps_what_was_already_found():
    """Retrying into a rate limit is how a temporary block becomes a longer
    one - and losing results already found to one bad query later in the
    list would be strictly worse than just stopping there."""
    profile = PersonalProfile(
        name="A", field="Computer Science", country="Zimbabwe",
        interests=["renewables", "agritech"],
    )
    calls = []

    def flaky_search(query, *, api_key, max_results=5):
        calls.append(query)
        if len(calls) == 1:
            return [SearchResult(title="Found first", url="https://example.org/1", content="")]
        request = httpx.Request("GET", "https://searxng.local/search")
        response = httpx.Response(429, request=request)
        raise httpx.HTTPStatusError("rate limited", request=request, response=response)

    results = discover(profile, search_fn=flaky_search)

    assert [r.url for r in results] == ["https://example.org/1"]
    assert len(calls) == 2, "must stop at the failing query, not keep going through the rest"


def test_a_degraded_searxng_response_stops_the_cycle_and_is_never_cached():
    """The actual live bug this fixes: SearXNG answers 200 with zero results
    while some of its own engines failed, and the old code cached that empty
    answer as if it were a real "no opportunities" - turning a five-minute
    upstream hiccup into six hours of false negatives.
    """
    from opportunity_agent.search import SearxngDegraded

    profile = PersonalProfile(
        name="A", field="Computer Science", country="Zimbabwe",
        interests=["renewables", "agritech"],
    )
    calls = []
    cached = {}

    def degraded_search(query, *, api_key=None, max_results=5):
        calls.append(query)
        raise SearxngDegraded("engine(s) unresponsive: brave (too many requests)")

    results = discover(
        profile, search_fn=degraded_search,
        cache_get=lambda q: cached.get(q),
        cache_set=lambda q, r: cached.__setitem__(q, r),
    )

    assert results == []
    assert len(calls) == 1, "must stop at the first degraded response, not try every query"
    assert cached == {}, "a degraded response must never be written to the cache"


def test_a_403_is_treated_as_a_rate_limit_too():
    """Some engines phrase a block as 403 rather than 429."""
    profile = PersonalProfile(name="A", field="Computer Science", country="Zimbabwe")

    def blocked_search(query, *, api_key, max_results=5):
        request = httpx.Request("GET", "https://searxng.local/search")
        response = httpx.Response(403, request=request)
        raise httpx.HTTPStatusError("forbidden", request=request, response=response)

    assert discover(profile, search_fn=blocked_search) == []


def test_an_ordinary_error_on_one_query_does_not_stop_the_others():
    """Not every failure is a rate limit - a single malformed query timing
    out must not cost every other query in the same cycle."""
    profile = PersonalProfile(
        name="A", field="Computer Science", country="Zimbabwe",
        interests=["renewables", "agritech"],
    )
    calls = []

    def flaky_search(query, *, api_key, max_results=5):
        calls.append(query)
        if len(calls) == 1:
            raise httpx.ConnectError("timeout")
        return [SearchResult(title="Second", url="https://example.org/2", content="")]

    results = discover(profile, search_fn=flaky_search)

    assert len(calls) > 1, "an ordinary error must not stop the remaining queries"
    assert any(r.url == "https://example.org/2" for r in results)
