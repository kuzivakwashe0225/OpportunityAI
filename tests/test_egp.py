"""Parsing the PRAZ eGP bulletin board.

The fixture is real markup captured from https://egp.praz.org.zw/Indexes/index,
not markup invented to match the parser. That matters: the live page's
`data-label` attributes are misaligned against its own `<thead>` (column 2 is
headed "Tender Reference Number" but labelled "Control Number"), so a parser
written against imagined markup would have keyed off the labels and quietly
mis-assigned every field.
"""

from datetime import date
from pathlib import Path

import httpx
import pytest

from opportunity_agent import egp

FIXTURE = Path(__file__).parent / "fixtures" / "egp_bulletin_board.html"


@pytest.fixture
def board_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_parses_every_tender_row(board_html):
    tenders = egp.parse_bulletin_board(board_html)
    assert len(tenders) == 2


def test_parses_the_fields_a_bid_decision_actually_turns_on(board_html):
    tender = egp.parse_bulletin_board(board_html)[0]

    assert tender.tender_id == "46190"
    assert tender.reference == "ZETDC/INTER/28/2025"
    assert tender.title.startswith("Supply, Delivery, Installation and Commissioning")
    assert tender.category_code == "GE001"
    assert "Transformers" in tender.category_name
    assert tender.procuring_entity == "ZIMBABWE ELECTRICITY TRANSMISSION AND DISTRIBUTION COMPANY"
    assert tender.scope == "Open"
    assert tender.closing_date == date(2026, 9, 17)
    assert tender.url == "https://egp.praz.org.zw/Indexes/viewLiveTenderDetails/46190"


def test_dates_come_back_as_dates_not_strings(board_html):
    tender = egp.parse_bulletin_board(board_html)[0]
    assert tender.publish_date == date(2025, 12, 13)


def test_an_unparseable_date_is_none_rather_than_an_exception():
    """One malformed date must not lose the other 883 tenders on the board."""
    html = """<table><thead><tr><th>Tender Id</th><th>Tender Reference Number</th>
    <th>Tender Title</th><th>Required Supplier Category Code</th>
    <th>Required Supplier Category Name</th><th>Procuring Entity</th><th>Scope</th>
    <th>Publish Date</th><th>Closing Date</th></tr></thead>
    <tr><td><a href="/Indexes/viewLiveTenderDetails/1">1</a></td><td>REF/1</td><td>A thing</td>
    <td>GE001</td><td>Stuff</td><td>An entity</td><td>Open</td>
    <td>not a date</td><td>whenever</td></tr></table>"""

    tender = egp.parse_bulletin_board(html)[0]
    assert tender.closing_date is None
    assert tender.reference == "REF/1"


def test_rows_without_a_tender_link_are_skipped():
    """Layout/spacer rows exist on the real page; they are not tenders."""
    html = """<table><thead><tr><th>Tender Id</th><th>Tender Reference Number</th>
    <th>Tender Title</th><th>Required Supplier Category Code</th>
    <th>Required Supplier Category Name</th><th>Procuring Entity</th><th>Scope</th>
    <th>Publish Date</th><th>Closing Date</th></tr></thead>
    <tr><td colspan="9">No records found</td></tr></table>"""

    assert egp.parse_bulletin_board(html) == []


def test_reads_the_total_page_count_so_paging_is_bounded_by_the_site(board_html):
    assert egp.parse_page_count(board_html) == 45


def test_page_count_defaults_to_one_when_there_is_no_paginator():
    assert egp.parse_page_count("<table></table>") == 1


def test_page_url_matches_the_sites_own_pagination_links():
    assert egp.page_url(1) == "https://egp.praz.org.zw/Indexes/index"
    assert egp.page_url(3) == (
        "https://egp.praz.org.zw/index?url=Indexes%2Findex&page=3"
        "&direction=BulletinBoardLive.id"
    )


# ---------------------------------------------------------------------------
# Converting a board row into the Opportunity the rest of the system matches on
# ---------------------------------------------------------------------------

def test_a_tender_becomes_a_verified_opportunity(board_html):
    """The board *is* the authoritative source - these are structured fields
    off the procurement regulator's own listing, not guesses scraped out of
    prose - so unlike a search result this is `requirements_verified=True`."""
    tender = egp.parse_bulletin_board(board_html)[0]
    opportunity = egp.tender_to_opportunity(tender)

    assert opportunity.source == "PRAZ eGP"
    assert opportunity.requirements_verified is True
    assert opportunity.eligible_countries == ["Zimbabwe"]
    assert opportunity.deadline == date(2026, 9, 17)
    assert opportunity.required_categories == ["GE001"]
    assert opportunity.evidence, "the row itself is the evidence"


def test_a_tender_accepting_several_categories_yields_all_of_them():
    """Found by running the parser over a real full page, not from the fixture:
    the board renders multi-category tenders as one ragged comma-separated
    cell. Read as a single code, every one of them is unmatchable."""
    assert egp.split_category_codes("SH001 ,SP001 ,SV001") == ["SH001", "SP001", "SV001"]
    assert egp.split_category_codes("GE001") == ["GE001"]
    assert egp.split_category_codes("") == []


def test_the_opportunity_carries_the_compliance_pack_a_zimbabwe_bid_needs(board_html):
    tender = egp.parse_bulletin_board(board_html)[0]
    opportunity = egp.tender_to_opportunity(tender)

    assert "tax_clearance" in opportunity.required_documents
    assert "certificate_of_incorporation" in opportunity.required_documents
    assert "praz_registration" in opportunity.required_documents


def test_evidence_is_the_structured_row_not_raw_html(board_html):
    """Raw page HTML as evidence is a known bug elsewhere in this codebase
    (it produced a 3MB review page). Don't repeat it here."""
    opportunity = egp.tender_to_opportunity(egp.parse_bulletin_board(board_html)[0])
    for line in opportunity.evidence:
        assert "<" not in line
        assert len(line) < 500


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def test_fetch_walks_pages_until_the_limit(board_html):
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, text=board_html)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    tenders = egp.fetch_live_tenders(client=client, max_pages=3)

    assert len(requested) == 3
    assert len(tenders) == 6


def test_fetch_stops_at_the_sites_real_page_count(board_html):
    """max_pages is a ceiling, not a target - asking for 99 pages of a 45-page
    board must not produce 54 pointless requests."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        # a board reporting only 2 pages
        return httpx.Response(200, text=board_html.replace("Page 1 of 45", "Page 1 of 2"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    egp.fetch_live_tenders(client=client, max_pages=99)

    assert len(calls) == 2


def test_a_failing_page_does_not_lose_the_pages_that_worked(board_html):
    def handler(request: httpx.Request) -> httpx.Response:
        if "page=2" in str(request.url):
            return httpx.Response(500)
        return httpx.Response(200, text=board_html)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    tenders = egp.fetch_live_tenders(client=client, max_pages=3)

    assert len(tenders) == 4  # page 1 and page 3 survived


def test_fetch_identifies_itself_honestly(board_html):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers.get("user-agent", "")
        return httpx.Response(200, text=board_html)

    egp.fetch_live_tenders(
        client=httpx.Client(transport=httpx.MockTransport(handler)), max_pages=1
    )

    assert "OpportunityAI" in seen["ua"]
    assert "Mozilla" not in seen["ua"]
