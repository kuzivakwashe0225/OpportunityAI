"""Parsing the PRAZ eGP award notices page.

The fixture is real markup captured from
https://egp.praz.org.zw/Indexes/getAwardNotices, trimmed to eight rows. The
full live page parses 100 of 100 rows with no missing fields and no unparsed
dates, which is the check that matters - a parser written against invented
markup would not have known the page carries a second, unrelated table above
the data one.
"""

from datetime import date
from pathlib import Path

import httpx
import pytest

from opportunity_agent import egp, egp_awards

FIXTURE = Path(__file__).parent / "fixtures" / "egp_award_notices.html"


@pytest.fixture
def awards_html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def _tender(tender_id: str) -> egp.Tender:
    return egp.Tender(
        tender_id=tender_id, reference="REF-" + tender_id, title="Something",
        category_code="GE001", category_name="General", procuring_entity="A Ministry",
        scope="National", url=f"https://egp.praz.org.zw/Indexes/viewLiveTenderDetails/{tender_id}",
    )


def test_award_rows_are_parsed_into_notices(awards_html):
    notices = egp_awards.parse_award_notices(awards_html)

    assert len(notices) == 8
    first = notices[0]
    assert first.award_number == "9051"
    assert first.tender_id == "102806"
    assert first.awardee == "juvenile electronics pl"
    assert first.award_date == date(2026, 9, 8)


def test_the_notice_url_is_built_from_the_award_number(awards_html):
    notices = egp_awards.parse_award_notices(awards_html)

    assert notices[0].url == "https://egp.praz.org.zw/Indexes/viewAwardNotice/9051"


def test_rows_that_are_not_awards_are_skipped():
    """The live page carries another table above the data one, and CakePHP
    renders a "no records" row when there is nothing. Neither is an award."""
    html = """
    <table><tr><td>' + message + '</td></tr></table>
    <table>
      <tr><th>Award Notice Number</th><th>Tender Id</th><th>Award Title</th>
          <th>Awardee</th><th>Award Date</th></tr>
      <tr><td colspan="5">No records found</td></tr>
      <tr><td>not-a-number</td><td>102806</td><td>T</td><td>Co</td><td>08-Sep-2026</td></tr>
      <tr><td>9051</td><td>102806</td><td>Mobile phones</td><td>Juvenile</td><td>08-Sep-2026</td></tr>
    </table>"""

    notices = egp_awards.parse_award_notices(html)

    assert [n.award_number for n in notices] == ["9051"]


def test_awarded_tender_ids_are_collected(awards_html):
    notices = egp_awards.parse_award_notices(awards_html)

    ids = egp_awards.awarded_tender_ids(notices)

    assert "102806" in ids
    assert len(ids) == 8


def test_still_open_subtracts_the_awarded_tenders(awards_html):
    notices = egp_awards.parse_award_notices(awards_html)
    board = [_tender("102806"), _tender("999999")]

    open_now = egp_awards.still_open(board, notices)

    # 102806 has an award notice; 999999 does not.
    assert [t.tender_id for t in open_now] == ["999999"]


def test_still_open_keeps_everything_when_nothing_has_been_awarded():
    board = [_tender("1"), _tender("2")]

    assert egp_awards.still_open(board, []) == board


def test_fetching_uses_the_award_notices_path(awards_html):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["agent"] = request.headers.get("user-agent", "")
        return httpx.Response(200, text=awards_html)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    notices = egp_awards.fetch_award_notices(client=client)

    assert seen["url"] == "https://egp.praz.org.zw/Indexes/getAwardNotices"
    # identifies itself honestly, like the rest of this codebase
    assert seen["agent"]
    assert len(notices) == 8


def test_awards_group_by_awardee(awards_html):
    notices = egp_awards.parse_award_notices(awards_html)

    grouped = egp_awards.awards_by_awardee(notices)

    assert all(isinstance(v, list) for v in grouped.values())
    assert sum(len(v) for v in grouped.values()) == len(notices)
