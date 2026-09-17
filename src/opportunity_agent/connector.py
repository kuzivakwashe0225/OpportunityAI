from __future__ import annotations

import re
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from html.parser import HTMLParser
from ipaddress import ip_address
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx


@dataclass(frozen=True)
class PublicPage:
    url: str
    content: str
    retrieved_at: str
    sha256: str
    content_type: str = "text/html"
    # Links on the page that point at a document rather than another page.
    #
    # Extracting a page to readable text throws its markup away, and with it
    # every href - which meant the application form a call links to was
    # invisible to this system even when the call said "complete the attached
    # form". These are kept so that form can be found, downloaded and filled.
    document_links: tuple[str, ...] = ()


MAX_REDIRECTS = 4

# An honest, identifying agent string - deliberately NOT a spoofed browser one.
# Several sites 403 a bare library default; identifying ourselves is the polite
# fix for that. A site that still refuses an honest agent is saying no, and
# SOLUTION_DEFINITION.md §8 means we take no for an answer rather than
# disguising the client to get around it.
USER_AGENT = "OpportunityAI/0.1 (+https://opportunityai.meshcloud.co.zw)"

# What we ask for, alongside who we are. These are content negotiation, not
# disguise: they say which formats and language this reader wants, exactly as
# a browser would, while the User-Agent above still says plainly what we are.
#
# The distinction matters and is the project's, not mine. SOLUTION_DEFINITION
# §8 rules out dressing the client up as a browser to get past a refusal, and
# a site that says no to an honest agent is saying no. Sending an Accept
# header is not saying no to that; claiming to be Chrome would be.
#
# Measured against seven sites currently refusing us, these headers recover
# one. The other six serve a JavaScript challenge rather than a page, and no
# header fixes that - it needs a real browser engine, which this project has
# deliberately not built.
#
# Accept-Encoding omits br: httpx only decodes brotli when its extra is
# installed, and advertising what we cannot read turns a working fetch into an
# unreadable one.
DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "application/pdf;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
}

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


# Tags whose *contents* are not page text at all. Stripping only the tags -
# which is what a bare re.sub(r"<[^>]+>") does - leaves the JavaScript source
# and the stylesheet sitting in the middle of the "text", which is worse than
# useless once it reaches a language model with a limited context window.
_NON_TEXT_TAGS = {"script", "style", "noscript", "svg", "canvas", "template", "iframe"}

# Tags that should end up as a line break, so headings and list items do not
# run into the sentence after them.
_BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "section", "article", "header", "footer",
    "h1", "h2", "h3", "h4", "h5", "h6", "table", "ul", "ol", "blockquote",
}


# Site furniture. Not the call, on any page, ever - and expensive to keep: a
# university scholarship page led with 90 lines of "Academics | Admissions |
# Campus Life | Alumni | Athletics", which is what a model reading the call
# would have spent its context window on.
_CHROME_TAGS = {"nav", "aside"}

# What a page declares to be its own content. When one of these is present it
# beats any heuristic we could write, because the page's author said so.
_MAIN_TAGS = {"main", "article"}

# Below this, a <main> is a wrapper rather than the content - a shell that a
# script fills in later, say - and the whole body is the better answer.
_MIN_MAIN_CHARS = 400


# Extensions worth following from a call page. Deliberately not every file
# type: a .zip or a .jpg on a tender page is a logo or a drawing pack, not
# something to read for requirements.
_DOCUMENT_EXTENSIONS = (".pdf", ".doc", ".docx", ".rtf", ".odt", ".xls", ".xlsx")


class _TextExtractor(HTMLParser):
    """Readable text out of a page, using the stdlib parser egp.py already uses.

    Deliberately not BeautifulSoup: this project has stayed dependency-light
    and this is a few dozen lines of the standard library doing a job that
    does not need a full DOM.

    Collects two versions at once - everything, and just what sat inside
    <main>/<article> - and `text()` picks. Doing it in one pass keeps this a
    parser rather than a document model.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._main_parts: list[str] = []
        self._suppress_depth = 0
        self._main_depth = 0
        # Ordered, de-duplicated: the order a call lists its attachments is
        # usually meaningful, and the same form is often linked twice.
        self._links: list[str] = []

    def _emit(self, chunk: str) -> None:
        self._parts.append(chunk)
        if self._main_depth:
            self._main_parts.append(chunk)

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for name, value in attrs:
                if name != "href" or not value:
                    continue
                cleaned = value.split("#", 1)[0].strip()
                if cleaned.lower().split("?", 1)[0].endswith(_DOCUMENT_EXTENSIONS):
                    if cleaned not in self._links:
                        self._links.append(cleaned)
        if tag in _MAIN_TAGS:
            self._main_depth += 1
        if tag in _NON_TEXT_TAGS or tag in _CHROME_TAGS:
            self._suppress_depth += 1
        elif tag in _BLOCK_TAGS:
            self._emit("\n")

    def handle_endtag(self, tag):
        if tag in _NON_TEXT_TAGS or tag in _CHROME_TAGS:
            if self._suppress_depth:
                self._suppress_depth -= 1
        elif tag in _BLOCK_TAGS:
            self._emit("\n")
        if tag in _MAIN_TAGS and self._main_depth:
            self._main_depth -= 1

    def handle_data(self, data):
        if self._suppress_depth:
            return
        self._emit(data)

    @staticmethod
    def _clean(parts: list[str]) -> str:
        joined = "".join(parts)
        # Collapse runs of spaces/tabs, then runs of blank lines, so the result
        # reads like prose rather than a column of whitespace.
        joined = re.sub(r"[ \t\r\f\v]+", " ", joined)
        lines = [line.strip() for line in joined.split("\n")]
        return "\n".join(line for line in lines if line)

    def links(self) -> list[str]:
        return list(self._links)

    def text(self) -> str:
        main = self._clean(self._main_parts)
        if len(main) >= _MIN_MAIN_CHARS:
            return main
        return self._clean(self._parts)


def document_links(html: str, base_url: str = "") -> tuple[str, ...]:
    """Absolute URLs of documents linked from this page.

    Used to find the application form a call tells the applicant to complete.
    Relative hrefs are resolved against the page they were found on, because
    a tender board writes `/downloads/bid-form.docx` far more often than it
    writes the full address.
    """
    try:
        parser = _TextExtractor()
        parser.feed(html)
        parser.close()
        found = parser.links()
    except Exception:
        return ()
    if not base_url:
        return tuple(found)
    return tuple(urljoin(base_url, href) for href in found)


def html_to_text(html: str) -> str:
    """Readable text from an HTML page, or the input unchanged if it will not parse."""
    try:
        parser = _TextExtractor()
        parser.feed(html)
        parser.close()
        extracted = parser.text()
    except Exception:
        return html
    # A page that is mostly markup can still legitimately extract to very
    # little (a JS-rendered app shell, say). Returning that is correct -
    # returning the raw markup instead would be pretending we read something.
    return extracted


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

    decoded = body.decode(encoding, errors="replace")

    # HTML was the remaining hole, and by volume the biggest one. Storing the
    # raw markup meant a single scholarship page was kept as 257,822
    # characters of doctype, IE conditional comments, inline scripts and
    # navigation - and everything downstream that reads a call (the
    # eligibility matcher, the requirement extractor, the section drafter)
    # was handed that instead of the prose. Measured on live data: 307 of 400
    # stored opportunities held over 5k characters each, essentially none of
    # it readable.
    if content_type in ("text/html", "application/xhtml+xml"):
        return html_to_text(decoded)
    return decoded


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
            with http_client.stream("GET", target, headers=DEFAULT_HEADERS) as response:
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
                # Links are read from the markup before it is flattened to
                # text, because flattening is what loses them - and the
                # application form a call tells you to complete is only ever
                # reachable through one.
                links: tuple[str, ...] = ()
                if content_type in ("text/html", "application/xhtml+xml"):
                    links = document_links(
                        body.decode(encoding, errors="replace"), target
                    )
                return PublicPage(
                    url=target,
                    content=_decode_body(body, content_type, encoding),
                    retrieved_at=datetime.now(timezone.utc).isoformat(),
                    sha256=sha256(body).hexdigest(),
                    content_type=content_type,
                    document_links=links,
                )
        raise ValueError("public source returned too many redirects")
    finally:
        if owns_client:
            http_client.close()