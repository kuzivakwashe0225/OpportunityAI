import pytest

from opportunity_agent import db as db_module
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


@pytest.fixture(autouse=True)
def isolate_database(tmp_path, monkeypatch):
    """Redirect db.SessionLocal to a throwaway SQLite file per test.

    Without this, api.py's account/profile endpoints would hit whatever
    DATABASE_URL resolves to locally (the real dev SQLite file, or a real
    Postgres if DATABASE_URL is set) - same class of problem the store
    fixture above prevents, for the newer SQLAlchemy-backed data.
    """
    engine = db_module.make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    db_module.init_db(engine)
    monkeypatch.setattr(db_module, "SessionLocal", db_module.make_sessionmaker(engine))


@pytest.fixture(autouse=True)
def ensure_session_secret_key(monkeypatch):
    """auth.py refuses to run without SESSION_SECRET_KEY set - give tests a
    fixed one rather than depending on a real .env file being present."""
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-only-secret-do-not-use-in-production")
