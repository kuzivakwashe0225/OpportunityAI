from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from ipaddress import ip_address
import socket
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx


@dataclass(frozen=True)
class PublicPage:
    url: str
    content: str
    retrieved_at: str
    sha256: str
    content_type: str = "text/html"


MAX_REDIRECTS = 4

# An honest, identifying agent string - deliberately NOT a spoofed browser one.
# Several sites 403 a bare library default; identifying ourselves is the polite
# fix for that. A site that still refuses an honest agent is saying no, and
# SOLUTION_DEFINITION.md §8 means we take no for an answer rather than
# disguising the client to get around it.
USER_AGENT = "OpportunityAI/0.1 (+https://opportunityai.meshcloud.co.zw)"

_DOCX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def _validate_public_url(url: str) -> str:
    """SSRF-check one URL and return the exact URL to request.

    Every redirect hop goes through this, not just the original URL - that is
    the whole reason following redirects is safe here at all.

    Deliberately does NOT return `canonicalize_url()`'s output. That function
    strips trailing slashes because it defines *dedup identity* ("/x" and "/x/"
    are the same opportunity). Requesting the stripped form makes servers that
    canonicalise the other way 301 straight back to the slashed URL, which we'd
    strip again - an infinite redirect loop. Measured live: this alone was
    losing real scholarship pages (beittrust.org.uk, canoncollins.org).
    Identity and fetch target are two different jobs.
    """
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        raise ValueError("public source URL must use HTTPS")
    if parsed.username or parsed.password or parsed.port not in {None, 443}:
        raise ValueError("public source URL contains an unsafe authority")
    if parsed.hostname in {"localhost", "localhost.localdomain"}:
        raise ValueError("public source URL targets a private host")
    try:
        if parsed.hostname and ip_address(parsed.hostname).is_private:
            raise ValueError("public source URL targets a private host")
    except ValueError as error:
        if "private host" in str(error):
            raise
    if parsed.hostname:
        try:
            addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        except socket.gaierror as error:
            raise ValueError("public source hostname could not be resolved") from error
        if any(ip_address(address[4][0]).is_private for address in addresses):
            raise ValueError("public source URL resolves to a private host")
    # normalise only scheme/host casing and drop the fragment; path is untouched
    return urlunsplit((
        parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, "",
    ))


def _decode_body(body: bytes, content_type: str, encoding: str) -> str:
    """Text out of a response, whatever kind of document it is.

    Decoding everything as text was silently losing the most important pages
    in the system. Funding calls are very often published as a PDF - the real
    POTRAZ research call this was found on is a link straight to
    POTRAZ-...-CALL-FOR-RESEARCH-PROPOSALS.pdf - and running those bytes
    through .decode() produces binary noise. The opportunity was stored with
    43 characters of usable text (its title), so everything downstream that
    reads the call - the eligibility matcher, the requirement extractor, the
    section drafter - was working from nothing and concluding nothing was
    there.

    pypdf and python-docx are already dependencies for reading the owner's
    own uploads, so the same extraction is used here rather than a second
    implementation. A file that cannot be parsed falls back to the plain
    decode: worse text is still better than an exception that costs the whole
    opportunity.
    """
    from .document_text import extract_text

    if content_type in ("application/pdf", _DOCX_CONTENT_TYPE):
        try:
            return extract_text(body, content_type)
        except Exception:
            pass
    return body.decode(encoding, errors="replace")


def fetch_public_page(
    url: str,
    *,
    client: httpx.Client | None = None,
    max_bytes: int = 2_000_000,
    max_redirects: int = MAX_REDIRECTS,
) -> PublicPage:
    target = _validate_public_url(url)

    owns_client = client is None
    http_client = client or httpx.Client(timeout=20.0, follow_redirects=False)
    # httpx must still not follow redirects on its own: we follow them by hand
    # precisely so each hop gets re-validated above before it's requested.
    if http_client.follow_redirects:
        raise ValueError("public source client must not follow redirects")
    try:
        for _hop in range(max_redirects + 1):
            with http_client.stream("GET", target, headers={"User-Agent": USER_AGENT}) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("public source redirect had no location")
                    target = _validate_public_url(urljoin(target, location))
                    continue
                response.raise_for_status()
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("public source response exceeds size limit")
                    chunks.append(chunk)
                body = b"".join(chunks)
                encoding = response.encoding or "utf-8"
                content_type = (
                    response.headers.get("content-type", "text/html").split(";", 1)[0].strip().lower()
                )
                return PublicPage(
                    url=target,
                    content=_decode_body(body, content_type, encoding),
                    retrieved_at=datetime.now(timezone.utc).isoformat(),
                    sha256=sha256(body).hexdigest(),
                    content_type=content_type,
                )
        raise ValueError("public source returned too many redirects")
    finally:
        if owns_client:
            http_client.close()