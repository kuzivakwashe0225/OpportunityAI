"""Scheduled discovery runner - the `worker` service in docker-compose.yml.

Runs independently of the `api` service's request-handling process so a real
~30s /discover call never blocks incoming HTTP requests. Operates on the same
file-backed OpportunityStore the API uses (a shared Docker volume) - this is
still the single-tenant MVP pipeline (SOLUTION_DEFINITION.md §14's "what this
explicitly does not do yet": pipeline migration to the Postgres/multi-tenant
model is a separate, later step).

Known duplication, not fixed here: run_discovery_cycle() below duplicates the
loop in api.py's POST /discover handler. The right fix is extracting one
shared function both call - not done in this pass since api.py has been under
continuous active concurrent development all session; flagged in AGENTS.md
rather than risking a collision to chase DRY-cleanliness in someone else's
actively-changing file.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from .connector import fetch_public_page
from .discovery import build_search_queries, discover
from .extraction import PARSER_VERSION, page_to_opportunity
from .store import DiscoveryRun, OpportunityStore


def run_discovery_cycle(store: OpportunityStore, api_key: str) -> DiscoveryRun | None:
    if store.profile is None:
        return None

    started_at = datetime.now(timezone.utc).isoformat()
    queries = build_search_queries(store.profile)
    try:
        results = discover(store.profile, api_key=api_key)
    except Exception as error:
        return store.record_run(
            queries=queries, found=0, added=0,
            failures=[f"search: {error}"], started_at=started_at, sources=[],
        )

    added = []
    failures: list[str] = []
    sources: list[dict[str, str]] = []
    for result in results:
        try:
            page = fetch_public_page(result.url)
            title = result.title or "Untitled scholarship opportunity"
            opportunity = page_to_opportunity(page, title=title)
        except Exception as error:
            failures.append(f"{result.url}: {error}")
            sources.append({"url": result.url, "status": "failed", "error": str(error)})
            continue
        try:
            added.append(store.add_opportunity(opportunity))
        except Exception as error:
            failures.append(f"{result.url}: {error}")
            sources.append({"url": result.url, "status": "failed", "error": str(error)})
            continue
        sources.append({
            "url": page.url, "status": "parsed",
            "content_type": page.content_type, "parser_version": PARSER_VERSION,
        })

    return store.record_run(
        queries=queries, found=len(results), added=len(added),
        failures=failures, started_at=started_at, sources=sources,
    )


def main() -> None:
    interval = int(os.getenv("DISCOVERY_INTERVAL_SECONDS", "3600"))
    store_path = os.getenv("OPPORTUNITY_AGENT_STORE_PATH", ".data/store.json")
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        raise RuntimeError("TAVILY_API_KEY is not configured")

    print(f"worker: starting, interval={interval}s, store={store_path}", flush=True)
    while True:
        store = OpportunityStore(path=store_path)  # reload - api may have written since last cycle
        run = run_discovery_cycle(store, api_key)
        if run is None:
            print("worker: no profile set yet, skipping this cycle", flush=True)
        else:
            print(f"worker: found {run.found}, added {run.added}, failures {len(run.failures)}", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    main()
