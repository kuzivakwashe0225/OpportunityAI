"""make_engine()'s handling of a SQLite URL whose directory doesn't exist yet.

The default DATABASE_URL is sqlite:///.data/app.db, and .data/ is gitignored
on purpose - a local dev artifact, not something the repo ships. That made
the default work everywhere this project had actually been run, where
.data/ already existed from earlier sessions, and fail on any genuinely
fresh checkout with "unable to open database file": SQLite creates the file
itself but never a missing parent directory. Confirmed live - the first run
of the deploy workflow's test job failed at collection with exactly that
error, before a single test executed, because GitHub Actions' checkout never
had a reason to create .data/.
"""

from opportunity_agent.db import make_engine


def test_a_missing_parent_directory_is_created(tmp_path):
    target = tmp_path / "does" / "not" / "exist" / "yet" / "app.db"
    assert not target.parent.exists()

    engine = make_engine(f"sqlite:///{target}")

    assert target.parent.exists()
    # not just the directory - a real, usable connection
    with engine.connect() as conn:
        conn.exec_driver_sql("SELECT 1")
    engine.dispose()


def test_an_existing_parent_directory_is_left_alone(tmp_path):
    target = tmp_path / "app.db"
    assert target.parent.exists()  # tmp_path itself already exists

    engine = make_engine(f"sqlite:///{target}")

    with engine.connect() as conn:
        conn.exec_driver_sql("SELECT 1")
    engine.dispose()


def test_an_in_memory_database_needs_no_directory():
    """':memory:' has no filesystem path at all - must not be treated as one."""
    engine = make_engine("sqlite:///:memory:")

    with engine.connect() as conn:
        conn.exec_driver_sql("SELECT 1")
    engine.dispose()


def test_a_non_sqlite_url_is_left_entirely_alone():
    """Postgres has no local file to create a directory for - this must be a
    no-op for it, not an attempt to interpret its connection string as a path."""
    # Never actually connects - just proves no filesystem side effect happens
    # for a URL make_engine should not be touching the disk for at all.
    engine = make_engine("postgresql+psycopg://user:pass@localhost:5432/db")

    assert engine.url.drivername.startswith("postgresql")
    engine.dispose()
