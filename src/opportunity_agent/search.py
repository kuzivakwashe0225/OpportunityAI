"""Search clients — the actual fetchers behind profile-driven discovery.

SOLUTION_DEFINITION.md §6: discovery.py's build_search_urls() constructs literal
google.com/search URLs, which nothing should ever fetch (against Google's terms,
and not parseable without a headless browser anyway). This is the real path:
discovery.py's query strings (build_search_queries()) go through here instead.

Two backends, same SearchResult contract, chosen once per call by
default_search_fn() rather than hardcoded anywhere that calls it:

* **search()** - Tavily (https://tavily.com). Requires an API key, billed per
  query. Its free "Researcher" plan is a hard 1,000 requests/month cap with no
  pay-as-you-go overage - confirmed live, not assumed: the plan's own /usage
  endpoint reported 1000/1000 used, and every search call returned HTTP 432
  ("This request exceeds your plan's set usage limit").

* **search_via_searxng()** - a self-hosted SearXNG instance (docker-compose.yml,
  no ports published - reached only from inside the compose network). No key,
  no per-query billing, no plan to exhaust. The tradeoff moves rather than
  disappears: SearXNG aggregates other engines by querying them on this
  server's behalf, so heavy use risks *this server's IP* getting rate-limited
  upstream - an operational problem to watch for, not a billing one to be
  surprised by.

default_search_fn() prefers SearXNG when SEARXNG_URL is set, and falls back to
Tavily otherwise - so an existing TAVILY_API_KEY keeps working unchanged for
anyone who has not stood up SearXNG.
"""

from __future__ import annotations

import os

import httpx
from pydantic import BaseModel

_TAVILY_ENDPOINT = "https://api.tavily.com/search"
_DEFAULT_SEARXNG_URL = "http://searxng:8080"


class SearchResult(BaseModel):
    title: str = ""
    url: str
    content: str = ""
    score: float = 0.0


def search(
    query: str,
    *,
    api_key: str | None,
    max_results: int = 5,
    client: httpx.Client | None = None,
) -> list[SearchResult]:
    """Run one query against Tavily and return normalized results.

    api_key is still required in practice - Tavily has none of the "no key
    needed" story SearXNG has - the type only widens for interface parity with
    search_via_searxng, so default_search_fn() can hand back either function
    without the caller needing to know which one it got.
    """
    if not api_key:
        raise ValueError("a Tavily API key is required")
    owns_client = client is None
    http_client = client or httpx.Client(timeout=15.0)
    try:
        response = http_client.post(
            _TAVILY_ENDPOINT,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"query": query, "max_results": max_results},
        )
        response.raise_for_status()
        payload = response.json()
    finally:
        if owns_client:
            http_client.close()

    return [SearchResult(**item) for item in payload.get("results", [])]


class SearxngDegraded(RuntimeError):
    """SearXNG answered (HTTP 200), but at least one of its own engines did not.

    Found live, not anticipated: this server's SearXNG instance returned 200
    with zero results for a real profile's queries while its own JSON body
    reported `unresponsive_engines: [["brave", "too many requests"],
    ["duckduckgo", "timeout"]]`. A per-query httpx status check (429/403)
    never sees this - SearXNG absorbs the individual engine failures and
    still answers 200 - so the caller has no way to tell "genuinely no
    results" from "half the engines were down" without reading this field.

    That distinction matters enormously once caching is involved: caching a
    degraded answer as if it were a real one means an unlucky moment - a
    handful of upstream engines rate-limited during a burst of testing, say -
    gets remembered as "no opportunities exist" for the next
    SEARCH_CACHE_TTL_MINUTES, actively making the outage worse instead of
    self-healing on the next attempt. discover() treats this exception the
    same as a 429/403: stop the rest of this cycle's queries, keep whatever
    was already found, and - critically - never hand the result to cache_set.
    """


def search_via_searxng(
    query: str,
    *,
    api_key: str | None = None,
    max_results: int = 5,
    base_url: str | None = None,
    client: httpx.Client | None = None,
) -> list[SearchResult]:
    """Run one query against a self-hosted SearXNG instance.

    Same return contract as search() (Tavily), so it is a drop-in for
    discovery.build_search_queries()'s caller. `api_key` is accepted and
    ignored - SearXNG needs none - purely so both backends share one call
    signature and discovery.py never has to know which one is active.

    Field names (url, title, content, score) are read straight from SearXNG's
    own JSON response builder (searx/webutils.py's get_json_response, which
    serializes each LegacyResult.as_dict()) rather than assumed - public
    instances with json enabled are rare enough (most disable it, and the two
    tested during development were both behind a bot-detection challenge) that
    guessing the shape and finding out later was not an acceptable risk here.

    Raises SearxngDegraded (see its docstring) if any engine SearXNG queried
    failed on its own instance's side, rather than silently returning
    whatever partial set of results came back from the ones that worked.
    """
    url = (base_url or os.getenv("SEARXNG_URL", _DEFAULT_SEARXNG_URL)).rstrip("/")

    owns_client = client is None
    http_client = client or httpx.Client(timeout=20.0)
    try:
        response = http_client.get(f"{url}/search", params={"q": query, "format": "json"})
        response.raise_for_status()
        payload = response.json()
    finally:
        if owns_client:
            http_client.close()

    unresponsive = payload.get("unresponsive_engines") or []
    if unresponsive:
        names = ", ".join(f"{name} ({reason})" for name, reason in unresponsive)
        raise SearxngDegraded(f"engine(s) unresponsive: {names}")

    results: list[SearchResult] = []
    for item in (payload.get("results") or [])[:max_results]:
        item_url = item.get("url")
        if not item_url:
            continue
        try:
            score = float(item.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        results.append(SearchResult(
            title=item.get("title") or "",
            url=item_url,
            content=item.get("content") or "",
            score=score,
        ))
    return results


def default_search_fn():
    """Which backend to use, resolved fresh on every call rather than fixed at
    import time - so setting SEARXNG_URL in .env takes effect on the next
    discovery cycle, not the next deploy.

    SearXNG wins whenever it is configured: it is what removes the "ran out of
    requests" failure mode this exists to fix. Tavily is the fallback, kept
    working unchanged for anyone who has not set up SearXNG yet.
    """
    if os.getenv("SEARXNG_URL"):
        return search_via_searxng
    return search
