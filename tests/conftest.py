import pytest

from opportunity_agent.api import store as api_store


@pytest.fixture(autouse=True)
def isolate_opportunity_store(tmp_path, monkeypatch):
    """Redirect the module-level store singleton to a throwaway file per test.

    Without this, api.py's `store = OpportunityStore(path=os.getenv(...,
    ".data/store.json"))` defaults to the real dev data file whenever
    OPPORTUNITY_AGENT_STORE_PATH isn't set - which it isn't in .env. Several
    test files call store.reset() in setup_function, which persists an empty
    state back to whatever store.path currently is. Without this fixture,
    running the test suite silently wipes and rewrites real accumulated
    discovery data in .data/store.json on every run.
    """
    monkeypatch.setattr(api_store, "path", tmp_path / "store.json")
    api_store.reset()
    yield
    api_store.reset()
