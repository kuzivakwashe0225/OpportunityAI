"""Apply the ingestion gates to opportunities that were stored before them.

The gates in relevance.py only stop new junk arriving. 468 rows were already
in the live database when they went in, and more than half of them were
consent screens, Wikipedia articles, vendor support pages and - from two days
in early September - a run of Lorem Ipsum template sites.

Three rules, all of them about being reversible:

**Nothing is deleted.** Rows are moved to the bin, exactly as the owner's own
"Remove" button does. They stay listed under the bin, they can be restored one
by one, and nothing about this is a one-way door. I permanently deleted a real
opportunity from this database once while testing the bin; that is not
happening twice.

**Nothing the owner has touched is moved.** An approved or submitted bid is
theirs, whatever this filter thinks of the URL it came from.

**It says what it will do before it does it.** Run without --apply to see the
counts and a sample, which is how you check the filter is not eating something
real before it eats it.

Piped into the container rather than installed in it, because this is an
operator's tool run by hand a few times, not part of the application:

    cd /home/isaiah/apps/OpportunityAI
    docker compose exec -T api python3 - < ops/tidy_existing.py           # dry run
    docker compose exec -T api python3 - --apply < ops/tidy_existing.py
"""

from __future__ import annotations

import sys
from collections import Counter
from datetime import datetime, timezone

from opportunity_agent import db as db_module
from opportunity_agent import models_db, relevance
from opportunity_agent.api import _page_text_for

# An approved or submitted opportunity belongs to the person who acted on it.
PROTECTED_STAGES = {"approved", "submitted"}


def assess(row) -> str | None:
    """Why this stored row is not an opportunity, or None if it is."""
    if row.stage in PROTECTED_STAGES:
        return None
    reason = relevance.junk_url_reason(row.canonical_url or "")
    if reason:
        return reason
    title = (row.payload or {}).get("title") or ""
    if not relevance.reads_like_an_opportunity(_page_text_for(row), title):
        return "the page never mentions applying for anything"
    return None


def main(apply: bool) -> int:
    session = db_module.SessionLocal()
    try:
        rows = (
            session.query(models_db.StoredOpportunity)
            .filter(models_db.StoredOpportunity.deleted_at.is_(None))
            .all()
        )
        junk = [(row, reason) for row in rows if (reason := assess(row))]
        reasons = Counter(reason for _, reason in junk)

        print(f"live opportunities : {len(rows)}")
        print(f"not opportunities  : {len(junk)}")
        print(f"staying            : {len(rows) - len(junk)}")
        print()
        for reason, count in reasons.most_common():
            print(f"   {count:>4}  {reason}")
        print()
        print("A SAMPLE OF WHAT MOVES TO THE BIN:")
        for row, _ in junk[:10]:
            title = str((row.payload or {}).get("title") or "")[:48]
            print(f"   {title:<48} {(row.canonical_url or '')[:54]}")

        if not apply:
            print()
            print("Dry run. Nothing was changed. Re-run with --apply to move these to the bin,")
            print("where they stay listed and can be restored one by one.")
            return 0

        now = datetime.now(timezone.utc)
        for row, reason in junk:
            row.deleted_at = now
            session.add(models_db.OpportunityEvent(
                opportunity_id=row.id,
                kind="trashed",
                detail=f"tidied automatically: {reason}",
            ))
        session.commit()
        print()
        print(f"Moved {len(junk)} to the bin. Nothing was deleted; the bin lists every one.")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main("--apply" in sys.argv))
