from opportunity_agent.connector import PublicPage
from opportunity_agent.models import PersonalProfile
from opportunity_agent.search import SearchResult
from opportunity_agent.store import OpportunityStore
from opportunity_agent import worker


def make_store(tmp_path, profile=None):
    store = OpportunityStore(path=tmp_path / "store.json")
    if profile is not None:
        store.save_profile(profile)
    return store


def test_run_discovery_cycle_skips_when_no_profile_is_set(tmp_path):
    store = make_store(tmp_path)

    run = worker.run_discovery_cycle(store, api_key="test-key")

    assert run is None


def test_run_discovery_cycle_records_a_run_and_adds_opportunities(tmp_path, monkeypatch):
    store = make_store(tmp_path, PersonalProfile(name="Test", country="Zimbabwe"))

    def fake_discover(profile, *, api_key):
        assert api_key == "test-key"
        return [SearchResult(title="Award", url="https://example.org/award", content="snippet")]

    def fake_fetch(url):
        return PublicPage(
            url=url, content="Full page text.", retrieved_at="2026-01-01T00:00:00Z",
            sha256="abc123", content_type="text/html",
        )

    monkeypatch.setattr(worker, "discover", fake_discover)
    monkeypatch.setattr(worker, "fetch_public_page", fake_fetch)

    run = worker.run_discovery_cycle(store, api_key="test-key")

    assert run is not None
    assert run.found == 1
    assert run.added == 1
    assert len(store.opportunities) == 1
    assert store.opportunities[0].opportunity.title == "Award"


def test_run_discovery_cycle_records_search_failure_without_raising(tmp_path, monkeypatch):
    store = make_store(tmp_path, PersonalProfile(name="Test"))

    def failing_discover(profile, *, api_key):
        raise RuntimeError("search API unreachable")

    monkeypatch.setattr(worker, "discover", failing_discover)

    run = worker.run_discovery_cycle(store, api_key="test-key")

    assert run is not None
    assert run.found == 0
    assert run.added == 0
    assert "search API unreachable" in run.failures[0]


def test_run_discovery_cycle_records_per_result_fetch_failures(tmp_path, monkeypatch):
    store = make_store(tmp_path, PersonalProfile(name="Test"))

    def fake_discover(profile, *, api_key):
        return [SearchResult(title="Broken", url="https://example.org/broken", content="")]

    def failing_fetch(url):
        raise ValueError("blocked by robots.txt")

    monkeypatch.setattr(worker, "discover", fake_discover)
    monkeypatch.setattr(worker, "fetch_public_page", failing_fetch)

    run = worker.run_discovery_cycle(store, api_key="test-key")

    assert run.found == 1
    assert run.added == 0
    assert len(run.failures) == 1
    assert "blocked by robots.txt" in run.failures[0]
