"""Award notices from the PRAZ eGP board - who actually won what.

https://egp.praz.org.zw/Indexes/getAwardNotices publishes every award openly,
with no login: award notice number, tender id, title, awardee, award date.
(The site also serves it as /egp-SW5kZXhlcy9nZXRBd2FyZE5vdGljZXM=, which is
just the same path base64'd into the URL.)

This is a different thing from the live board in egp.py, and worth keeping
separate. The board says what is being asked for; this says how it ended.
Together they answer two questions the board alone cannot:

1. **Which listed tenders are still winnable.** `Tender Id` here is the same
   identifier `egp.py` already parses off the board, so the two join directly
   and awarded ones can be subtracted. See `still_open`.
2. **What outcomes look like.** This is the only public record of who wins,
   which is the input any "learn from results" feature has to start from.

Two limits to state plainly, because both bound what can honestly be built on
top of this:

* **Winners only.** There is no record of who else bid and lost. So this can
  support "what do winning bids look like in this category, for this
  procuring entity" and cannot support "why did ours lose" - that needs the
  owner's own submission history, which is ours to record, not PRAZ's.
* **A rolling window, not an archive.** The page carries ~100 rows and no
  pagination controls at all (checked against the live page, not assumed).
  History therefore has to be *accumulated* by polling and storing, and a
  caller that treats one fetch as "every award ever" will be wrong about
  everything older than the window.

Politeness matches egp.py: honest identifying User-Agent, one request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

import httpx

from .connector import USER_AGENT
from .egp import BASE_URL, Tender, parse_board_date, parse_table_rows

AWARDS_PATH = "/Indexes/getAwardNotices"

# Column order, read off the live page's own <thead>:
# Award Notice Number | Tender Id | Award Title | Awardee | Award Date
_AWARD_COLUMNS = ("award_number", "tender_id", "title", "awardee", "award_date")

_NOTICE_LINK_RE = re.compile(r"/Indexes/viewAwardNotice/(\d+)")


@dataclass(frozen=True)
class AwardNotice:
    award_number: str
    tender_id: str
    title: str
    awardee: str
    award_date: date | None = None

    @property
    def url(self) -> str:
        return f"{BASE_URL}/Indexes/viewAwardNotice/{self.award_number}"


def parse_award_notices(html: str) -> list[AwardNotice]:
    """Rows off the award notices table.

    Skips anything that is not a real award row rather than raising: the page
    carries a second, unrelated table, and one malformed row must not cost us
    the other ninety-nine.
    """
    notices: list[AwardNotice] = []
    for cells in parse_table_rows(html):
        if len(cells) < len(_AWARD_COLUMNS):
            continue
        values = dict(zip(_AWARD_COLUMNS, cells))
        award_number = values["award_number"].strip()
        tender_id = values["tender_id"].strip()
        # Both identifiers are numeric on the real page. Requiring that is what
        # keeps layout and "no records found" rows out of the results.
        if not award_number.isdigit() or not tender_id.isdigit():
            continue
        notices.append(AwardNotice(
            award_number=award_number,
            tender_id=tender_id,
            title=values["title"].strip(),
            awardee=values["awardee"].strip(),
            award_date=parse_board_date(values["award_date"]),
        ))
    return notices


def fetch_award_notices(*, client: httpx.Client | None = None) -> list[AwardNotice]:
    """One request - the page is not paginated.

    Client is injectable for the same reason as everywhere else here: no test
    should reach a live government server.
    """
    owns_client = client is None
    http = client or httpx.Client(timeout=30.0, follow_redirects=True)
    try:
        response = http.get(f"{BASE_URL}{AWARDS_PATH}", headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
        return parse_award_notices(response.text)
    finally:
        if owns_client:
            http.close()


def awarded_tender_ids(notices: list[AwardNotice]) -> set[str]:
    return {n.tender_id for n in notices if n.tender_id}


def still_open(tenders: list[Tender], notices: list[AwardNotice]) -> list[Tender]:
    """The board's tenders minus the ones already awarded.

    Deliberately conservative in one direction: because the notices page is a
    rolling window, a tender awarded long ago may no longer appear on it and
    would be reported here as still open. Being told a closed tender is open
    costs the owner some reading; being told an open one is closed would cost
    them the bid, so the error is pointed the cheaper way.
    """
    awarded = awarded_tender_ids(notices)
    return [t for t in tenders if t.tender_id not in awarded]


def awards_by_awardee(notices: list[AwardNotice]) -> dict[str, list[AwardNotice]]:
    """Grouped by winner, casefolded - the same company is spelled several
    ways across the board ("(Private) Limited", "(Pvt) Ltd", trailing t/a
    names), so this groups on what is written and leaves normalising to a
    caller that has decided what "the same company" means."""
    grouped: dict[str, list[AwardNotice]] = {}
    for notice in notices:
        grouped.setdefault(notice.awardee.casefold(), []).append(notice)
    return grouped
