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

    target = bind_engine or engine
    Base.metadata.create_all(bind=target)
    _add_missing_columns(target)


def _add_missing_columns(bind_engine: Engine) -> None:
    """Add columns the models declare and the live tables do not have.

    `create_all` creates missing *tables* and silently ignores missing
    *columns*, which has already cost this deployment one outage: a
    `notifications.profile_id` that existed in the model and not in Postgres
    made every notification query 500 until it was added by hand.

    Deliberately additive and nothing else. It never drops a column, never
    changes a type, and never touches data - so the worst it can do on a
    database it does not understand is nothing. Anything beyond adding a
    nullable column is a real migration and should be written as one.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(bind_engine)
    with bind_engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            present = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present or not column.nullable:
                    continue
                kind = column.type.compile(dialect=bind_engine.dialect)
                connection.execute(
                    text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {kind}')
                )


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
