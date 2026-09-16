"""What happens at the size this is already running at, not at the size of a
fixture.

The live database holds 400 opportunities across nine profiles, and one
profile alone holds 265. Every number below is measured against that shape,
because an endpoint that is fine with three rows and quadratic with three
hundred looks identical in every other test in this repository.

Two things are being watched, and they fail differently:

  queries  - a count that grows with the number of rows is an N+1, and it is
             the failure that does not show up locally against SQLite and then
             takes the box down against Postgres over a network.
  bytes    - the page loads a profile's whole opportunity list in one request
             and holds it in memory. On a Zimbabwean mobile connection the
             size of that response is the feature.

The budgets are deliberately loose - a few times the current measurement, not
the current measurement. They are here to catch a regression of kind, not to
break when a machine is busy.
"""

import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from conftest import sign_up
from opportunity_agent import api, models_db
from opportunity_agent import db as db_module

client = TestClient(api.app)

# What one live profile actually holds.
REALISTIC_OPPORTUNITY_COUNT = 265

CALL_TEXT = (
    "Invitation to tender. Bidders must submit a valid tax clearance certificate, "
    "CR14, and PRAZ supplier registration covering category GE001. Proposals must "
    "not exceed 3.5 pages. Closes 3 October 2026. " * 40
)


@pytest.fixture
def counted_queries():
    """Count SQL statements issued while serving one request."""
    counter = {"n": 0}

    def before(conn, cursor, statement, parameters, context, executemany):
        counter["n"] += 1

    engine = db_module.SessionLocal.kw["bind"]
    event.listen(engine, "before_cursor_execute", before)
    yield counter
    event.remove(engine, "before_cursor_execute", before)


_BUILT = {"n": 0}


def _profile_with(count, *, with_packages=False):
    client.cookies.clear()
    _BUILT["n"] += 1
    sign_up(client, email=f"scale{_BUILT['n']}@example.com")
    profile = client.post(
        "/profiles", json={"profile_type": "tender", "display_name": "At scale"}
    ).json()
    client.put(f"/profiles/{profile['id']}", json={"fields": {"name": "Meshcloud"}})

    session = db_module.SessionLocal()
    for index in range(count):
        session.add(models_db.StoredOpportunity(
            profile_id=profile["id"],
            canonical_url=f"https://egp.praz.org.zw/tender/{index}",
            payload={
                "title": f"Supply and delivery of equipment, lot {index}",
                "source": "PRAZ eGP", "deadline": "3 October 2026",
                "required_documents": ["tax_clearance", "cr14"],
                "evidence": [CALL_TEXT],
            },
            match_status="eligible" if index % 3 else "needs_review",
            match_score=index % 20,
            match_reasons={"matched": ["category GE001"]},
            # Shaped like the packages actually in the live database: they
            # keep their own copy of the fetched page alongside the draft.
            package=({"status": "ready", "cover_note": "Dear Sir or Madam...",
                      "checklist": [{"requirement": "tax_clearance", "status": "met"}],
                      "evidence": [CALL_TEXT],
                      "sections": [
                          {"title": "Technical Proposal", "body": "We will supply..." * 50}
                      ]} if with_packages else None),
        ))
    session.commit()
    session.close()
    return profile


# --------------------------------------------------------------------------
# Query counts
# --------------------------------------------------------------------------

def test_listing_a_profiles_opportunities_does_not_query_per_row(counted_queries):
    """The N+1 test.

    If this ever starts scaling with the row count, the page that every user
    lands on issues hundreds of round trips to Postgres to draw one list.
    """
    profile = _profile_with(REALISTIC_OPPORTUNITY_COUNT)

    counted_queries["n"] = 0
    response = client.get(f"/profiles/{profile['id']}/opportunities")

    assert response.status_code == 200
    assert len(response.json()) == REALISTIC_OPPORTUNITY_COUNT
    assert counted_queries["n"] < 15, (
        f"{counted_queries['n']} queries to list "
        f"{REALISTIC_OPPORTUNITY_COUNT} rows - that is one per row"
    )


def test_the_dashboard_summary_is_a_handful_of_queries_whatever_the_size(counted_queries):
    """The most-loaded screen in the product, and the one refreshed most."""
    profile = _profile_with(REALISTIC_OPPORTUNITY_COUNT)

    counted_queries["n"] = 0
    response = client.get(f"/profiles/{profile['id']}/summary")

    assert response.status_code == 200
    assert counted_queries["n"] < 15, f"{counted_queries['n']} queries for one summary"


def test_the_detail_page_costs_the_same_whether_the_profile_holds_5_or_265(counted_queries):
    """One opportunity's page must not pay for its neighbours."""
    small = _profile_with(5)
    small_id = client.get(f"/profiles/{small['id']}/opportunities").json()[0]["id"]
    counted_queries["n"] = 0
    client.get(f"/profiles/{small['id']}/opportunities/{small_id}/requirements")
    cost_of_small = counted_queries["n"]

    large = _profile_with(REALISTIC_OPPORTUNITY_COUNT)
    large_id = client.get(f"/profiles/{large['id']}/opportunities").json()[0]["id"]
    counted_queries["n"] = 0
    client.get(f"/profiles/{large['id']}/opportunities/{large_id}/requirements")
    cost_of_large = counted_queries["n"]

    assert cost_of_large <= cost_of_small + 2, (
        f"{cost_of_small} queries on a small profile, {cost_of_large} on a large one"
    )


# --------------------------------------------------------------------------
# Response size
# --------------------------------------------------------------------------

def test_the_opportunity_list_does_not_ship_the_whole_call_text_to_the_browser():
    """Each opportunity now carries the full readable page in `evidence`.

    That is the fix that made drafting work - and it is exactly the thing that
    must not be multiplied by 265 and sent to a phone. The list is a list; the
    text belongs on the detail page, which asks for one.
    """
    profile = _profile_with(REALISTIC_OPPORTUNITY_COUNT, with_packages=True)

    body = client.get(f"/profiles/{profile['id']}/opportunities").content

    per_row = len(body) / REALISTIC_OPPORTUNITY_COUNT
    assert per_row < 2000, (
        f"{per_row:.0f} bytes per row - the list is carrying the call text "
        f"({len(body)/1024:.0f} KB total)"
    )


def test_one_calls_text_is_capped_before_it_reaches_the_page():
    """A single scraped page was 257,822 characters. Rendering that into a
    <pre> is a locked-up browser tab."""
    profile = _profile_with(1)
    opportunity_id = client.get(f"/profiles/{profile['id']}/opportunities").json()[0]["id"]

    session = db_module.SessionLocal()
    row = session.get(models_db.StoredOpportunity, opportunity_id)
    row.payload = dict(row.payload, evidence=["x" * 300_000])
    session.commit()
    session.close()

    detail = client.get(
        f"/profiles/{profile['id']}/opportunities/{opportunity_id}/requirements"
    ).json()

    assert len(detail["call_text"]) <= 20_000


def test_the_whole_front_end_is_one_page_a_phone_can_load():
    """No build step and no bundle - so the single file is the budget."""
    page = client.get("/ui")

    assert page.status_code == 200
    assert len(page.content) < 400_000, f"{len(page.content)/1024:.0f} KB of HTML"


# --------------------------------------------------------------------------
# Wall clock
# --------------------------------------------------------------------------

def _elapsed_ms(call):
    started = time.perf_counter()
    response = call()
    return (time.perf_counter() - started) * 1000, response


def test_the_screens_people_wait_on_answer_quickly_at_full_size():
    """Budgets, not benchmarks. Anything an order of magnitude over these is a
    regression of kind rather than a slow morning on a laptop."""
    profile = _profile_with(REALISTIC_OPPORTUNITY_COUNT, with_packages=True)
    opportunity_id = client.get(f"/profiles/{profile['id']}/opportunities").json()[0]["id"]
    base = f"/profiles/{profile['id']}"

    budgets = {
        f"{base}/summary": 1500,
        f"{base}/opportunities": 3000,
        f"{base}/opportunities/{opportunity_id}/requirements": 1500,
        f"{base}/documents": 1000,
        "/notifications": 1000,
        "/ui": 1000,
    }

    slow = {}
    for path, budget in budgets.items():
        elapsed, response = _elapsed_ms(lambda p=path: client.get(p))
        assert response.status_code == 200, path
        if elapsed > budget:
            slow[path] = f"{elapsed:.0f}ms (budget {budget}ms)"

    assert not slow, json.dumps(slow, indent=2)


def test_signing_in_is_slow_on_purpose_and_stays_that_way():
    """bcrypt's cost is the feature - it is what makes a stolen database
    expensive to crack. A login that suddenly got fast has had its work
    factor dropped."""
    sign_up(client, email="costly@example.com")
    client.cookies.clear()

    elapsed, response = _elapsed_ms(
        lambda: client.post("/login", json={"email": "costly@example.com",
                                            "password": "wrong-on-purpose"})
    )

    assert response.status_code == 401
    assert elapsed > 20, f"a failed login took {elapsed:.0f}ms - is the hash still bcrypt?"
