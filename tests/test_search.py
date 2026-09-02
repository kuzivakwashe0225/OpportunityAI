import json

import httpx
import pytest

from opportunity_agent.search import SearchResult, search


def _client_with(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_search_sends_bearer_token_and_query():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"results": []})

    search("masters scholarship Zimbabwe", api_key="tvly-test", client=_client_with(handler))

    assert captured["url"] == "https://api.tavily.com/search"
    assert captured["auth"] == "Bearer tvly-test"
    assert captured["body"]["query"] == "masters scholarship Zimbabwe"


def test_search_respects_max_results():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"results": []})

    search("query", api_key="k", max_results=3, client=_client_with(handler))

    assert captured["body"]["max_results"] == 3


def test_search_normalizes_results():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Global Scholars Award",
                        "url": "https://example.org/award",
                        "content": "Deadline 1 October 2026",
                        "score": 0.9,
                    }
                ]
            },
        )

    results = search("query", api_key="k", client=_client_with(handler))

    assert results == [
        SearchResult(
            title="Global Scholars Award",
            url="https://example.org/award",
            content="Deadline 1 October 2026",
            score=0.9,
        )
    ]


def test_search_missing_optional_fields_default_sensibly():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"url": "https://example.org/x"}]})

    results = search("query", api_key="k", client=_client_with(handler))

    assert results == [SearchResult(title="", url="https://example.org/x", content="", score=0.0)]


def test_search_raises_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "invalid API key"})

    with pytest.raises(httpx.HTTPStatusError):
        search("query", api_key="bad-key", client=_client_with(handler))
