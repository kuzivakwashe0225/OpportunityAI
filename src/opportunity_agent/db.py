"""Database engine and session management for Phase 2 (SOLUTION_DEFINITION.md §14).

DATABASE_URL controls the backend: defaults to a local SQLite file so nothing
external is required for development or the test suite. docker-compose sets
it to the `db` (Postgres) service's URL - see docker-compose.yml.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def _database_url() -> str:
    return os.getenv("DATABASE_URL", "sqlite:///.data/app.db")


def _ensure_sqlite_dir_exists(resolved: str) -> None:
    """SQLite will create the *file*, never a missing parent directory.

    .data/ is gitignored on purpose - it is a local dev artifact, not
    something the repo ships. That made the default URL work everywhere this
    project had actually been developed, where .data/ already existed from
    earlier runs, and fail with "unable to open database file" on any
    genuinely fresh checkout - a new contributor, a new machine, or GitHub
    Actions - which never had a reason to create it. Confirmed live: the
    first CI run against this workflow failed here, at collection, before a
    single test executed.
    """
    parsed = make_url(resolved)
    database = parsed.database
    if not database or database == ":memory:":
        return
    parent = Path(database).parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)


def make_engine(url: str | None = None) -> Engine:
    resolved = url or _database_url()
    if resolved.startswith("sqlite"):
        _ensure_sqlite_dir_exists(resolved)
    connect_args = {"check_same_thread": False} if resolved.startswith("sqlite") else {}
    return create_engine(resolved, connect_args=connect_args)


def make_sessionmaker(bind_engine: Engine) -> sessionmaker:
    return sessionmaker(bind=bind_engine, expire_on_commit=False)


engine = make_engine()
SessionLocal = make_sessionmaker(engine)


def init_db(bind_engine: Engine | None = None) -> None:
    from . import models_db  # noqa: F401  (import registers the models on Base)

    Base.metadata.create_all(bind=bind_engine or engine)


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
