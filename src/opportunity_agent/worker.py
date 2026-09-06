"""Scheduled discovery runner - the `worker` service in docker-compose.yml.

Runs independently of the `api` service's request-handling process so a real
~30s discovery cycle never blocks incoming HTTP requests.

Sweeps every profile on every account (SOLUTION_DEFINITION.md §16). The older
single-tenant file-backed path this used to run alongside is gone - see the
commit that removed `store.py`.
"""

from __future__ import annotations

import os
import time

from dotenv import load_dotenv

load_dotenv()


def main() -> None:
    interval = int(os.getenv("DISCOVERY_INTERVAL_SECONDS", "3600"))
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        raise RuntimeError("TAVILY_API_KEY is not configured")

    print(f"worker: starting, interval={interval}s", flush=True)
    while True:
        run_all_profile_cycles(api_key)
        time.sleep(interval)


def run_all_profile_cycles(api_key: str) -> int:
    """Run one unattended agent cycle for every profile that's set up.

    This is what makes the system work while the owner isn't there: every
    profile on every account gets its own discovery, matching, auto-shortlist
    and auto-draft pass, with notifications queued for whatever came out.
    """
    from . import models_db, pipeline
    from .db import SessionLocal

    cycles = 0
    with SessionLocal() as session:
        profiles = session.query(models_db.Profile).all()
        for profile in profiles:
            try:
                run = pipeline.run_profile_cycle(session, profile, api_key=api_key)
            except Exception as error:  # one bad profile must not stop the rest
                print(f"worker: profile {profile.id} failed: {error}", flush=True)
                session.rollback()
                continue
            if run is None:
                continue
            cycles += 1
            print(
                f"worker: profile '{profile.display_name}' ({profile.profile_type}) - "
                f"found {run.found}, added {run.added}, drafted {run.drafted}",
                flush=True,
            )
    return cycles


if __name__ == "__main__":
    main()
