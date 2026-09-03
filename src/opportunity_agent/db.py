"""Database engine and session management for Phase 2 (SOLUTION_DEFINITION.md §14).

DATABASE_URL controls the backend: defaults to a local SQLite file so nothing
external is required for development or the test suite. docker-compose sets
it to the `db` (Postgres) service's URL - see docker-compose.yml.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def _database_url() -> str:
    return os.getenv("DATABASE_URL", "sqlite:///.data/app.db")


def make_engine(url: str | None = None) -> Engine:
    resolved = url or _database_url()
    connect_args = {"check_same_thread": False} if resolved.startswith("sqlite") else {}
    return create_engine(resolved, connect_args=connect_args)


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db(bind_engine: Engine | None = None) -> None:
    from . import models_db  # noqa: F401  (import registers the models on Base)

    Base.metadata.create_all(bind=bind_engine or engine)


def get_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
