"""The scheduled sweep - what runs while nobody is watching.

Replaces the old single-tenant `run_discovery_cycle` tests, which went with
`store.py`. Worth noting that `run_all_profile_cycles` had *no* test coverage
at all while the retired path had four tests; this is the function that
actually runs in production.
"""

import pytest
from sqlalchemy.orm import Session

from opportunity_agent import db as db_module
from opportunity_agent import models_db, worker


@pytest.fixture
def accounts():
    """Two accounts, three profiles between them - so the sweep is genuinely
    crossing account boundaries rather than looping over one person's work."""
    with db_module.SessionLocal() as session:
        first = models_db.Account(email="a@example.com", password_hash="x")
        second = models_db.Account(email="b@example.com", password_hash="x")
        session.add_all([first, second])
        session.commit()

        session.add_all([
            models_db.Profile(
                account_id=first.id, profile_type="scholarship",
                display_name="Scholarships",
                fields={"name": "Tendai", "country": "Zimbabwe"},
            ),
            models_db.Profile(
                account_id=first.id, profile_type="tender", display_name="Tenders",
                fields={"name": "Meshcloud", "country": "Zimbabwe"},
            ),
            models_db.Profile(
                account_id=second.id, profile_type="job", display_name="Jobs",
                fields={"name": "Rudo", "country": "Zimbabwe"},
            ),
        ])
        session.commit()
        yield


def test_the_sweep_covers_every_profile_on_every_account(accounts, monkeypatch):
    seen = []

    def fake_cycle(session, profile, *, api_key, **kwargs):
        seen.append(profile.display_name)
        return models_db.ProfileDiscoveryRun(profile_id=profile.id, found=1, added=1, drafted=0)

    from opportunity_agent import pipeline
    monkeypatch.setattr(pipeline, "run_profile_cycle", fake_cycle)

    cycles = worker.run_all_profile_cycles("test-key")

    assert cycles == 3
    assert sorted(seen) == ["Jobs", "Scholarships", "Tenders"]


def test_one_failing_profile_does_not_stop_the_others(accounts, monkeypatch):
    """The property the whole schedule depends on: an unreachable source or a
    malformed profile must cost that profile's run, not everyone else's."""
    from opportunity_agent import pipeline

    def flaky(session, profile, *, api_key, **kwargs):
        if profile.display_name == "Tenders":
            raise RuntimeError("eGP unreachable")
        return models_db.ProfileDiscoveryRun(profile_id=profile.id, found=1, added=0, drafted=0)

    monkeypatch.setattr(pipeline, "run_profile_cycle", flaky)

    assert worker.run_all_profile_cycles("test-key") == 2


def test_a_profile_that_is_not_set_up_is_skipped_quietly(accounts, monkeypatch):
    from opportunity_agent import pipeline
    monkeypatch.setattr(pipeline, "run_profile_cycle",
                        lambda session, profile, *, api_key, **kwargs: None)

    assert worker.run_all_profile_cycles("test-key") == 0


def test_the_sweep_works_with_no_accounts_at_all():
    assert worker.run_all_profile_cycles("test-key") == 0


# --------------------------------------------------------------------------
# main()'s startup check
# --------------------------------------------------------------------------
# main() itself is an infinite loop and is not unit-tested as a whole; these
# pin the one thing that changed - it used to hard-crash the entire worker
# (including tender profiles, which need no search backend at all) whenever
# TAVILY_API_KEY specifically was unset, even with SearXNG configured.

def test_main_no_longer_requires_tavily_specifically(monkeypatch, capsys):
    """SearXNG needs no key. A worker with only SEARXNG_URL configured must
    not refuse to start."""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("SEARXNG_URL", "http://searxng:8080")
    monkeypatch.setattr(worker, "run_all_profile_cycles", lambda api_key: 0)
    monkeypatch.setattr(worker.time, "sleep", lambda *_: (_ for _ in ()).throw(StopIteration))

    with pytest.raises(StopIteration):
        worker.main()

    assert "starting" in capsys.readouterr().out


def test_main_warns_but_does_not_crash_with_no_backend_at_all(monkeypatch, capsys):
    """Tender profiles read the eGP board directly and need neither backend,
    so the worker still starts - it just says other profiles will not run."""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("SEARXNG_URL", raising=False)
    monkeypatch.setattr(worker, "run_all_profile_cycles", lambda api_key: 0)
    monkeypatch.setattr(worker.time, "sleep", lambda *_: (_ for _ in ()).throw(StopIteration))

    with pytest.raises(StopIteration):
        worker.main()

    out = capsys.readouterr().out
    assert "no web search backend configured" in out
    assert "tender profiles still run" in out
