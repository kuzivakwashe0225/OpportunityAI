"""The SearXNG search client, and which backend gets picked by default.

Field names (url, title, content, score) are read straight from SearXNG's own
JSON response builder in its source (searx/webutils.py's get_json_response,
serializing LegacyResult.as_dict()), not assumed - two public instances tried
during development both gated format=json behind a bot-detection challenge
despite returning HTTP 200, which is exactly the "looks fine, silently isn't"
failure this file exists to rule out.
"""

import json

import httpx
import pytest

from opportunity_agent import search as search_module
from opportunity_agent.search import SearchResult, default_search_fn, search_via_searxng


def _client_with(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_searxng_requests_json_format_and_the_query():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"results": []})

    search_via_searxng(
        "ICT tender zimbabwe", base_url="http://searxng:8080", client=_client_with(handler)
    )

    assert captured["url"].startswith("http://searxng:8080/search?")
    assert "q=ICT" in captured["url"] or "q=ICT+tender+zimbabwe" in captured["url"]
    assert "format=json" in captured["url"]


def test_searxng_needs_no_api_key():
    """The whole point: nothing here should require one."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in {h.lower() for h in request.headers.keys()}
        return httpx.Response(200, json={"results": []})

    search_via_searxng(
        "query", api_key=None, base_url="http://searxng:8080", client=_client_with(handler)
    )


def test_searxng_normalizes_a_real_shaped_result():
    """Shape taken verbatim from SearXNG's LegacyResult.as_dict() output."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "query": "ICT tender zimbabwe",
            "results": [{
                "url": "https://egp.praz.org.zw/Indexes/viewLiveTenderDetails/46190",
                "title": "Supply of ICT Equipment - PRAZ",
                "content": "Tender closing 17 September 2026.",
                "engine": "google",
                "score": 4.5,
                "category": "general",
                "template": "default.html",
            }],
            "answers": [], "corrections": [], "infoboxes": [], "suggestions": [],
        })

    results = search_via_searxng("query", base_url="http://searxng:8080",
                                 client=_client_with(handler))

    assert results == [SearchResult(
        title="Supply of ICT Equipment - PRAZ",
        url="https://egp.praz.org.zw/Indexes/viewLiveTenderDetails/46190",
        content="Tender closing 17 September 2026.",
        score=4.5,
    )]


def test_searxng_drops_a_result_with_no_url():
    """A malformed or engine-only entry must not become a fake opportunity."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"title": "no url here"}]})

    results = search_via_searxng("query", base_url="http://searxng:8080",
                                 client=_client_with(handler))

    assert results == []


def test_searxng_respects_max_results():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [
            {"url": f"https://example.org/{i}"} for i in range(10)
        ]})

    results = search_via_searxng("query", max_results=3, base_url="http://searxng:8080",
                                 client=_client_with(handler))

    assert len(results) == 3


def test_searxng_missing_optional_fields_default_sensibly():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"url": "https://example.org/x"}]})

    results = search_via_searxng("query", base_url="http://searxng:8080",
                                 client=_client_with(handler))

    assert results == [SearchResult(title="", url="https://example.org/x", content="", score=0.0)]


def test_searxng_raises_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="Bad Gateway")

    with pytest.raises(httpx.HTTPStatusError):
        search_via_searxng("query", base_url="http://searxng:8080", client=_client_with(handler))


def test_a_score_that_is_not_a_number_does_not_crash_the_whole_search():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "results": [{"url": "https://example.org/x", "score": "not-a-number"}]
        })

    results = search_via_searxng("query", base_url="http://searxng:8080",
                                 client=_client_with(handler))

    assert results[0].score == 0.0


# --------------------------------------------------------------------------
# Which backend gets picked
# --------------------------------------------------------------------------

def test_searxng_is_preferred_when_configured(monkeypatch):
    monkeypatch.setenv("SEARXNG_URL", "http://searxng:8080")

    assert default_search_fn() is search_module.search_via_searxng


def test_tavily_is_the_fallback_when_searxng_is_not_configured(monkeypatch):
    monkeypatch.delenv("SEARXNG_URL", raising=False)

    assert default_search_fn() is search_module.search


def test_search_without_an_api_key_refuses_rather_than_sending_no_auth():
    with pytest.raises(ValueError):
        search_module.search("query", api_key=None)
