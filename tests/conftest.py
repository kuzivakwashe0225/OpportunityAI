import pytest

from opportunity_agent import db as db_module


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


@pytest.fixture(autouse=True)
def outbox(monkeypatch):
    """Capture outgoing mail instead of opening a real SMTP connection.

    Autouse because registration now *depends* on delivery succeeding - an
    unstubbed test would either 503 or, far worse, try to reach a real mail
    server from the test suite.
    """
    from opportunity_agent import api as api_module

    sent: list[dict] = []
    monkeypatch.setattr(api_module, "_send_mail", lambda **kwargs: sent.append(kwargs))
    return sent


def sign_up(client, email="owner@example.com", outbox=None):
    """Register an account and log in with the password that was emailed.

    Registration no longer accepts a password or returns a session, so every
    test that just needs "an authenticated client" goes through here rather
    than reproducing the two-step dance.
    """
    from opportunity_agent import api as api_module

    captured: list[dict] = [] if outbox is None else outbox
    if outbox is None:
        original = api_module._send_mail
        api_module._send_mail = lambda **kwargs: captured.append(kwargs)
        try:
            client.post("/register", json={"email": email})
        finally:
            api_module._send_mail = original
    else:
        client.post("/register", json={"email": email})

    password = _password_from(captured[-1]["body"])
    client.post("/login", json={"email": email, "password": password})
    return password


def _password_from(body: str) -> str:
    for line in body.splitlines():
        if line.startswith("Password:"):
            return line.split("Password:", 1)[1].strip()
    raise AssertionError("no password line in the welcome email")
