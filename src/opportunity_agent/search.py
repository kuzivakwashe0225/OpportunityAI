"""Tavily search client — the actual fetcher behind profile-driven discovery.

SOLUTION_DEFINITION.md §6: discovery.py's build_search_urls() constructs literal
google.com/search URLs, which nothing should ever fetch (against Google's terms,
and not parseable without a headless browser anyway). This is the real path:
discovery.py's query strings (build_search_queries()) go through here instead.

Requires a Tavily API key (https://tavily.com) — read it from the environment
wherever this is wired up, not baked in here, so tests never need a real key.
"""

from __future__ import annotations

import httpx
from pydantic import BaseModel

_TAVILY_ENDPOINT = "https://api.tavily.com/search"


class SearchResult(BaseModel):
    title: str = ""
    url: str
    content: str = ""
    score: float = 0.0


def search(
    query: str,
    *,
    api_key: str,
    max_results: int = 5,
    client: httpx.Client | None = None,
) -> list[SearchResult]:
    """Run one query against Tavily and return normalized results."""
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
