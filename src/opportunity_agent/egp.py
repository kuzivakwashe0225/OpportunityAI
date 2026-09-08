"""The PRAZ eGP bulletin board - Zimbabwe's public tender listing.

https://egp.praz.org.zw/Indexes/index publishes every live government tender
openly: no login, ~884 of them across 45 pages at the time of writing, as
server-rendered HTML in a plain table. That makes it a fundamentally better
source than web search - these are structured fields from the procurement
regulator itself, complete with the *supplier category code* each tender
requires, which is the single most reliable eligibility signal in this whole
system. A company registered with PRAZ under GE001 can bid on GE001 tenders;
no fuzzy text matching required.

Two things learned from the real page, both of which would have been got wrong
by writing this against imagined markup:

1. **The `data-label` attributes lie.** Column 2 is headed "Tender Reference
   Number" in `<thead>` but carries `data-label="Control Number"`; column 3 is
   headed "Tender Title" but labelled "Tender Reference Number". The labels are
   shifted by one against the actual headers. So this parser keys off *column
   position*, taken from the header row, and ignores `data-label` entirely.
2. **Pagination is a query string on a different path** - the board is at
   `/Indexes/index` but page 2 is at `/index?url=Indexes%2Findex&page=2&...`.

Politeness: the site publishes no robots.txt (404). Access is unrestricted, so
this fetches with an honest identifying User-Agent, a page ceiling, and a pause
between pages rather than hammering a government server.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from urllib.parse import quote

import httpx

from .connector import USER_AGENT
from .models import Opportunity

BASE_URL = "https://egp.praz.org.zw"
BOARD_PATH = "/Indexes/index"
SOURCE_NAME = "PRAZ eGP"

# The compliance pack essentially every Zimbabwean public tender asks for.
# Keys match profile_schema's DocumentKind keys so the "please upload X"
# notification can name them properly and the UI can offer an upload slot.
STANDARD_ZW_TENDER_DOCUMENTS = (
    "company_profile",
    "certificate_of_incorporation",
    "tax_clearance",
    "praz_registration",
    "cr14",
)

# Column order, read from the board's own <thead>.
_COLUMNS = (
    "tender_id", "reference", "title", "category_code", "category_name",
    "procuring_entity", "scope", "publish_date", "closing_date",
)

_DETAIL_RE = re.compile(r"/Indexes/viewLiveTenderDetails/(\d+)")
_PAGE_COUNT_RE = re.compile(r"Page\s+\d+\s+of\s+(\d+)", re.I)


@dataclass(frozen=True)
class Tender:
    tender_id: str
    reference: str
    title: str
    category_code: str
    category_name: str
    procuring_entity: str
    scope: str
    url: str
    publish_date: date | None = None
    closing_date: date | None = None


@dataclass(frozen=True)
class TenderDetails:
    """What a tender actually costs to enter, and on what terms.

    The board listing says what a tender is; this says what disqualifies you.
    Bid security not lodged, tender fees unpaid, a bid validity period shorter
    than the one demanded, or a bid prepared against superseded requirements
    because seven addenda were issued and nobody re-read them - these are the
    technicalities bids die on, and none of them are visible on the listing.

    Every field is optional. A layout change on their side must degrade to
    "we don't know" rather than take down the nightly run.
    """

    bid_validity_days: int | None = None
    procurement_method: str | None = None
    procurement_class: str | None = None
    procurement_rules: str = ""
    funding_source: str | None = None
    delivery_location: str = ""
    delivery_period: str | None = None
    contact_person: str | None = None
    closing_date: date | None = None
    bid_form_fee: float | None = None
    bid_security_domestic: float | None = None
    bid_security_international: float | None = None
    establishment_domestic: float | None = None
    establishment_international: float | None = None
    spoc_fee: float | None = None
    addendum_count: int = 0
    documents_require_login: bool = False


_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_ADDENDUM_RE = re.compile(
    r"Tender\s+Addendums?\s*:.*?<a[^>]*>\s*(\d+)\s*</a>", re.S | re.I
)


def _label_value(html: str, label: str) -> str | None:
    """Pull the value out of the detail page's `<label>X: </label></br>value</br>`
    pattern. Returns None when the label isn't there at all."""
    match = re.search(
        r"<label>\s*" + re.escape(label) + r"\s*:\s*</label>\s*(?:</?br\s*/?>)*(.*?)(?:</?br\s*/?>)",
        html, re.S | re.I,
    )
    if not match:
        return None
    value = re.sub(r"<[^>]+>", " ", match.group(1))
    value = value.replace("&nbsp;", " ").replace("&amp;", "&")
    value = " ".join(value.split())
    return value or None


def _label_number(html: str, label: str) -> float | None:
    raw = _label_value(html, label)
    if raw is None:
        return None
    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", raw)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def parse_tender_details(html: str) -> TenderDetails:
    # Strip HTML comments first. The real page keeps the contact person and
    # creator inside a comment - the publisher is choosing not to display
    # them, and a regex over raw markup would happily lift them anyway.
    clean = _COMMENT_RE.sub("", html)

    addendum = _ADDENDUM_RE.search(clean)
    validity = _label_value(clean, "Bid Validity Period (in Days)")

    return TenderDetails(
        bid_validity_days=int(validity) if validity and validity.isdigit() else None,
        procurement_method=_label_value(clean, "Procurement Method"),
        procurement_class=_label_value(clean, "Class of Procurement"),
        procurement_rules=_label_value(clean, "Applicable Procurement Rules") or "",
        funding_source=_label_value(clean, "Funding Source"),
        delivery_location=_label_value(clean, "Delivery/Project Location") or "",
        delivery_period=_label_value(clean, "Delivery Period"),
        contact_person=_label_value(clean, "Contact Person"),
        closing_date=_parse_date(_label_value(clean, "Closing Date") or ""),
        bid_form_fee=_label_number(clean, "Bid Form Fee"),
        bid_security_domestic=_label_number(clean, "Bid Security Amount(Domestic)"),
        bid_security_international=_label_number(clean, "Bid Security Amount(International)"),
        establishment_domestic=_label_number(clean, "Establishment Amount(Domestic)"),
        establishment_international=_label_number(clean, "Establishment Amount(International)"),
        spoc_fee=_label_number(clean, "SPOC Fee"),
        addendum_count=int(addendum.group(1)) if addendum else 0,
        # The bid pack itself is not public: it hangs off a JS `href_path` to
        # /Tenders/tender_doc_view/, which needs a logged-in supplier account.
        # Say so, rather than reporting a document set we never saw.
        documents_require_login="tender_doc_view" in clean,
    )


class _BoardParser(HTMLParser):
    """Pulls rows out of the bulletin board table.

    Deliberately stdlib: this codebase has stayed dependency-light, and the
    markup is a plain table, not something that needs a full DOM.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.row_links: list[str | None] = []
        self._in_row = False
        self._in_cell = False
        self._in_header = False
        self._cells: list[str] = []
        self._buffer: list[str] = []
        self._link: str | None = None

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        if tag == "tr":
            self._in_row, self._cells, self._link = True, [], None
        elif tag in ("td", "th") and self._in_row:
            self._in_cell = True
            self._in_header = tag == "th"
            self._buffer = []
        elif tag == "a" and self._in_cell and self._link is None:
            match = _DETAIL_RE.search(attrs_d.get("href", ""))
            if match:
                self._link = f"{BASE_URL}/Indexes/viewLiveTenderDetails/{match.group(1)}"

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._in_cell:
            self._cells.append(" ".join("".join(self._buffer).split()))
            self._in_cell = False
        elif tag == "tr" and self._in_row:
            if self._cells and not self._in_header:
                self.rows.append(self._cells)
                self.row_links.append(self._link)
            self._in_row, self._in_header = False, False

    def handle_data(self, data):
        if self._in_cell:
            self._buffer.append(data)


def _parse_date(raw: str) -> date | None:
    """The board formats dates as '17-Sep-2026 10:00 AM'.

    Returns None rather than raising: one procuring entity typing a date badly
    must not cost us the other 883 tenders on the board.
    """
    cleaned = raw.strip()
    if not cleaned:
        return None
    for fmt in ("%d-%b-%Y %I:%M %p", "%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(cleaned[:len(datetime.now().strftime(fmt)) + 4].strip(), fmt).date()
        except ValueError:
            continue
    # last resort: just the leading date token
    try:
        return datetime.strptime(cleaned.split()[0], "%d-%b-%Y").date()
    except (ValueError, IndexError):
        return None


def parse_table_rows(html: str) -> list[list[str]]:
    """Every non-header row of the page's tables, as lists of cell text.

    Public because egp_awards.py needs exactly this: the award notices page is
    the same shape of plain server-rendered table, and writing a second parser
    for it would be duplicating a thing already proven against real markup.
    """
    parser = _BoardParser()
    parser.feed(html)
    return parser.rows


def parse_board_date(raw: str) -> date | None:
    """The board's date format, shared with egp_awards.py.

    Both pages render dates the same way, so they parse them the same way.
    """
    return _parse_date(raw)


def parse_bulletin_board(html: str) -> list[Tender]:
    parser = _BoardParser()
    parser.feed(html)

    tenders: list[Tender] = []
    for cells, link in zip(parser.rows, parser.row_links):
        # A row with no detail link is not a tender - it's a "no records found"
        # or layout row. Same for a row too short to carry the columns we need.
        if not link or len(cells) < len(_COLUMNS):
            continue
        values = dict(zip(_COLUMNS, cells))
        tenders.append(Tender(
            tender_id=values["tender_id"],
            reference=values["reference"],
            title=values["title"],
            category_code=values["category_code"],
            category_name=values["category_name"],
            procuring_entity=values["procuring_entity"],
            scope=values["scope"],
            url=link,
            publish_date=_parse_date(values["publish_date"]),
            closing_date=_parse_date(values["closing_date"]),
        ))
    return tenders


def split_category_codes(raw: str) -> list[str]:
    """A tender can accept several supplier categories.

    Found only by running the parser over a real full page: the board renders
    these as one comma-separated cell with ragged spacing - "SH001 ,SP001
    ,SV001". Treating that string as a single code would make every
    multi-category tender unmatchable for every company.
    """
    return [code.strip().upper() for code in re.split(r"[,;/]", raw or "") if code.strip()]


def parse_page_count(html: str) -> int:
    match = _PAGE_COUNT_RE.search(html)
    return int(match.group(1)) if match else 1


def page_url(page: int) -> str:
    """Page 1 lives at the board path; later pages at the site's own paginator
    URL, which is a query string on `/index` rather than on the board path."""
    if page <= 1:
        return f"{BASE_URL}{BOARD_PATH}"
    return (
        f"{BASE_URL}/index?url={quote(BOARD_PATH.lstrip('/'), safe='')}"
        f"&page={page}&direction=BulletinBoardLive.id"
    )


def tender_to_opportunity(tender: Tender) -> Opportunity:
    """A board row as an Opportunity the matching engine can act on.

    `requirements_verified=True` here, unlike a search result. That flag means
    "these requirements came from the authoritative source, not from guessing
    at prose", and a structured row on the procurement regulator's own bulletin
    board is exactly that. The category code in particular is a hard, checkable
    eligibility rule rather than an inference.
    """
    evidence = [
        f"PRAZ eGP bulletin board listing, tender {tender.tender_id}",
        f"Reference: {tender.reference}",
        f"Procuring entity: {tender.procuring_entity}",
        f"Required supplier category: {tender.category_code} - {tender.category_name}"[:480],
    ]
    if tender.closing_date:
        evidence.append(f"Closing date: {tender.closing_date.isoformat()}")

    return Opportunity(
        source=SOURCE_NAME,
        title=tender.title or f"Tender {tender.reference}",
        url=tender.url,
        deadline=tender.closing_date,
        eligible_countries=["Zimbabwe"],
        required_categories=split_category_codes(tender.category_code),
        required_documents=list(STANDARD_ZW_TENDER_DOCUMENTS),
        interests=[part.strip() for part in tender.category_name.split(",") if part.strip()][:6],
        evidence=evidence,
        sources=[tender.url],
        retrieved_at=datetime.now(timezone.utc).isoformat(),
        requirements_verified=True,
        content_type="text/html",
        parser_version="egp-1",
    )


def fetch_live_tenders(
    *,
    client: httpx.Client | None = None,
    max_pages: int = 5,
    pause_seconds: float = 0.0,
) -> list[Tender]:
    """Walk the live board. `max_pages` is a ceiling, not a target - if the
    board reports fewer pages than that, we stop there.

    A page that fails is logged past, not fatal: losing page 2 to a timeout
    should not throw away pages 1 and 3.
    """
    owns_client = client is None
    http_client = client or httpx.Client(timeout=30.0, follow_redirects=True)
    tenders: list[Tender] = []
    total_pages = max_pages

    try:
        page = 1
        while page <= min(max_pages, total_pages):
            try:
                response = http_client.get(page_url(page), headers={"User-Agent": USER_AGENT})
                response.raise_for_status()
            except Exception:
                page += 1
                continue
            if page == 1:
                total_pages = parse_page_count(response.text)
            tenders.extend(parse_bulletin_board(response.text))
            page += 1
            if pause_seconds and page <= min(max_pages, total_pages):
                time.sleep(pause_seconds)
    finally:
        if owns_client:
            http_client.close()
    return tenders
