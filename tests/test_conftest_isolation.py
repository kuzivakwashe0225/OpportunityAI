from pathlib import Path

from opportunity_agent.api import store as api_store


def test_module_store_is_redirected_away_from_real_data_dir():
    """Regression guard for the autouse fixture in conftest.py.

    Found by running the full suite with real dev data in .data/store.json:
    without isolation, store.reset() (called in several test files' setup_function)
    persists an empty state back to that real file on every test run.
    """
    assert Path(api_store.path).resolve() != Path(".data/store.json").resolve()


def test_isolated_store_starts_empty():
    assert api_store.profile is None
    assert api_store.opportunities == []
    assert api_store.runs == []
