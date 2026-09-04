import httpx
import pytest

from opportunity_agent.connector import fetch_public_page


def test_fetch_public_page_returns_content_and_integrity_metadata():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="Scholarship details", headers={"content-type": "text/html; charset=utf-8"})

    page = fetch_public_page(
        "https://example.org/scholarship",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert page.url == "https://example.org/scholarship"
    assert page.content == "Scholarship details"
    assert page.sha256
    assert page.retrieved_at
    assert page.content_type == "text/html"


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


def test_fetch_public_page_follows_a_safe_redirect():
    """Trailing-slash redirects are extremely common and were costing us real
    opportunities when redirects were refused outright (measured live: 2 of 10
    results lost to nothing but an added '/')."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/scholarships":
            return httpx.Response(301, headers={"location": "https://example.org/scholarships/x"})
        return httpx.Response(200, text="Scholarship details")

    page = fetch_public_page(
        "https://example.org/scholarships",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert page.content == "Scholarship details"
    assert page.url == "https://example.org/scholarships/x"


def test_a_redirect_to_a_private_host_is_still_rejected():
    """The whole reason redirects were refused: a public URL can redirect
    somewhere the pre-flight SSRF checks already cleared the original against.
    Following them is only safe because every hop is re-validated."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://127.0.0.1/secrets"})

    with pytest.raises(ValueError):
        fetch_public_page(
            "https://example.org/evil",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )


def test_a_redirect_to_plain_http_is_still_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://example.org/downgraded"})

    with pytest.raises(ValueError, match="HTTPS"):
        fetch_public_page(
            "https://example.org/downgrade",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )


def test_a_redirect_loop_is_capped():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://example.org/loop?n=" + str(id(request))})

    with pytest.raises(ValueError, match="too many redirects"):
        fetch_public_page(
            "https://example.org/loop",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )


def test_fetch_sends_an_identifying_user_agent():
    """An honest, identifying UA - not a spoofed browser string. Several sites
    403 a bare library default; identifying ourselves is the polite fix. A site
    that still refuses an honest agent is saying no, and that's respected."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["ua"] = request.headers.get("user-agent", "")
        return httpx.Response(200, text="ok")

    fetch_public_page(
        "https://example.org/x",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert "OpportunityAI" in captured["ua"]
    assert "Mozilla" not in captured["ua"]  # not pretending to be a browser


def test_fetch_public_page_enforces_streamed_size_limit():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"too large")

    with pytest.raises(ValueError, match="size limit"):
        fetch_public_page(
            "https://example.org/large",
            max_bytes=3,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )