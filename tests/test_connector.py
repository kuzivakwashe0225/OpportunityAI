import httpx
import pytest

from opportunity_agent.connector import fetch_public_page


def test_fetch_public_page_returns_content_and_integrity_metadata():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="Scholarship details")

    page = fetch_public_page(
        "https://example.org/scholarship",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert page.url == "https://example.org/scholarship"
    assert page.content == "Scholarship details"
    assert page.sha256
    assert page.retrieved_at


def test_fetch_public_page_rejects_non_https_urls():
    with pytest.raises(ValueError, match="HTTPS"):
        fetch_public_page("http://example.org/scholarship")


@pytest.mark.parametrize("url", [
    "https://127.0.0.1/scholarship",
    "https://localhost/scholarship",
    "https://user:pass@example.org/scholarship",
    "https://example.org:8443/scholarship",
])
def test_fetch_public_page_rejects_unsafe_targets(url):
    with pytest.raises(ValueError):
        fetch_public_page(url)


def test_fetch_public_page_rejects_http_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with pytest.raises(httpx.HTTPStatusError):
        fetch_public_page(
            "https://example.org/missing",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )


def test_fetch_public_page_enforces_streamed_size_limit():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"too large")

    with pytest.raises(ValueError, match="size limit"):
        fetch_public_page(
            "https://example.org/large",
            max_bytes=3,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )