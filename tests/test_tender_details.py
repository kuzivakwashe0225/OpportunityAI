"""Reading an eGP tender detail page for the things that sink a bid.

Fixture is real markup from
https://egp.praz.org.zw/Indexes/viewLiveTenderDetails/46190.

The board listing says what a tender *is*. The detail page says what it
actually *costs to enter* - bid security, tender fees, validity period - and
whether the requirements have been amended since publication. Those are the
things a bidder misses and gets disqualified for, so they are what the agent
has to surface.
"""

from datetime import date
from pathlib import Path

import pytest

from opportunity_agent import egp

FIXTURE = Path(__file__).parent / "fixtures" / "egp_tender_details.html"


@pytest.fixture
def details_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_reads_the_money_a_bidder_has_to_put_up(details_html):
    details = egp.parse_tender_details(details_html)

    assert details.bid_security_domestic == 25000
    assert details.bid_security_international == 25000
    assert details.spoc_fee == 400
    assert details.bid_form_fee == 0


def test_reads_the_procurement_terms_that_decide_how_to_bid(details_html):
    details = egp.parse_tender_details(details_html)

    assert details.procurement_method == "Competitive Bidding Method"
    assert details.procurement_class == "Goods"
    assert details.bid_validity_days == 90
    assert "PPDPA" in details.procurement_rules


def test_reads_delivery_terms(details_html):
    details = egp.parse_tender_details(details_html)

    assert "FILABUSI" in details.delivery_location
    assert details.delivery_period == "1 Year(s)"


def test_does_not_lift_data_the_page_deliberately_hides(details_html):
    """The contact person and creator are present in the HTML but wrapped in
    an HTML comment - the site is choosing not to show them. A regex over raw
    markup happily reads them anyway; that is scraping something the publisher
    withheld, so comments get stripped before parsing."""
    assert egp.parse_tender_details(details_html).contact_person is None
    assert "Kudzai Makomo" in details_html  # it really is in the markup


def test_counts_addendums_because_they_change_the_requirements(details_html):
    """Seven addenda on this tender. A bid prepared against the original
    published requirements and never re-checked is how bidders get thrown
    out on a technicality."""
    details = egp.parse_tender_details(details_html)

    assert details.addendum_count == 7


def test_notices_that_the_tender_documents_are_behind_the_login(details_html):
    """The board is public; the actual bid documents are not. Pretending
    otherwise would have the agent report a document set it never saw."""
    details = egp.parse_tender_details(details_html)

    assert details.documents_require_login is True


def test_closing_date_is_read_from_the_detail_page_too(details_html):
    assert egp.parse_tender_details(details_html).closing_date == date(2026, 9, 17)


def test_a_page_missing_everything_yields_empty_details_not_an_exception():
    """A layout change on their side must degrade to "we don't know", not
    take down the nightly run for every tender profile."""
    details = egp.parse_tender_details("<html><body>Nothing here</body></html>")

    assert details.bid_security_domestic is None
    assert details.procurement_method is None
    assert details.addendum_count == 0
    assert details.documents_require_login is False
