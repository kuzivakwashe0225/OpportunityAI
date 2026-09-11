"""The bin: trashing, restoring, deleting for good, and the history trail.

An agent working unattended produces volume, so clearing a cluttered list is
tidying rather than an irreversible decision. That shapes every choice here -
trashing is recoverable, permanent deletion is a deliberate second step, and
what the owner did is recorded rather than inferred from the current stage.
"""

from fastapi.testclient import TestClient

from conftest import sign_up
from opportunity_agent import api, models_db
from opportunity_agent import db as db_module
from opportunity_agent.api import app

client = TestClient(app)


def setup_function():
    client.cookies.clear()
    sign_up(client)


def _profile():
    return client.post(
        "/profiles", json={"profile_type": "job", "display_name": "Jobs"}
    ).json()


def _opportunity(profile_id, url="https://example.org/role", stage="drafted"):
    """Insert directly - the point here is the bin, not the discovery cycle."""
    session = db_module.SessionLocal()
    row = models_db.StoredOpportunity(
        profile_id=profile_id,
        canonical_url=url,
        payload={"title": "A role", "url": url},
        match_status="eligible",
        match_score=10,
        match_reasons={},
        stage=stage,
    )
    session.add(row)
    session.commit()
    row_id = row.id
    session.close()
    return row_id


# --------------------------------------------------------------------------
# Trash and restore
# --------------------------------------------------------------------------

def test_trashing_hides_it_from_the_normal_list():
    profile = _profile()
    opp_id = _opportunity(profile["id"])

    assert len(client.get(f"/profiles/{profile['id']}/opportunities").json()) == 1

    client.post(f"/profiles/{profile['id']}/opportunities/{opp_id}/trash")

    assert client.get(f"/profiles/{profile['id']}/opportunities").json() == []


def test_the_bin_is_listed_on_request():
    profile = _profile()
    opp_id = _opportunity(profile["id"])
    client.post(f"/profiles/{profile['id']}/opportunities/{opp_id}/trash")

    binned = client.get(f"/profiles/{profile['id']}/opportunities?trashed=true").json()

    assert [o["id"] for o in binned] == [opp_id]
    assert binned[0]["deleted_at"] is not None


def test_restoring_puts_it_back_exactly_as_it_was():
    """Trashing never touched the stage or the draft - restoring must not
    either, or a binned-and-recovered application silently loses its work."""
    profile = _profile()
    opp_id = _opportunity(profile["id"], stage="approved")
    client.post(f"/profiles/{profile['id']}/opportunities/{opp_id}/trash")

    restored = client.post(
        f"/profiles/{profile['id']}/opportunities/{opp_id}/restore"
    ).json()

    assert restored["deleted_at"] is None
    assert restored["stage"] == "approved"
    assert len(client.get(f"/profiles/{profile['id']}/opportunities").json()) == 1


def test_trashing_twice_is_harmless():
    profile = _profile()
    opp_id = _opportunity(profile["id"])

    first = client.post(f"/profiles/{profile['id']}/opportunities/{opp_id}/trash").json()
    second = client.post(f"/profiles/{profile['id']}/opportunities/{opp_id}/trash").json()

    # Compared as instants rather than strings: the first response serialises
    # the value still in memory (timezone-aware, "...Z") while the second has
    # been round-tripped through the database, which stores it naive. Same
    # moment, two spellings - and the moment is what "must not reset" means.
    from datetime import datetime

    def moment(iso: str) -> datetime:
        return datetime.fromisoformat(iso.replace("Z", "")).replace(tzinfo=None)

    assert moment(first["deleted_at"]) == moment(second["deleted_at"]), "must not reset the clock"


# --------------------------------------------------------------------------
# Permanent deletion
# --------------------------------------------------------------------------

def test_permanent_deletion_requires_going_through_the_bin_first():
    """One click from a normal list must not be able to destroy a drafted
    application."""
    profile = _profile()
    opp_id = _opportunity(profile["id"])

    response = client.delete(f"/profiles/{profile['id']}/opportunities/{opp_id}")

    assert response.status_code == 409
    assert "bin" in response.json()["detail"]
    assert len(client.get(f"/profiles/{profile['id']}/opportunities").json()) == 1


def test_permanent_deletion_from_the_bin_actually_removes_it():
    profile = _profile()
    opp_id = _opportunity(profile["id"])
    client.post(f"/profiles/{profile['id']}/opportunities/{opp_id}/trash")

    assert client.delete(f"/profiles/{profile['id']}/opportunities/{opp_id}").status_code == 204

    assert client.get(f"/profiles/{profile['id']}/opportunities?trashed=true").json() == []
    session = db_module.SessionLocal()
    assert session.get(models_db.StoredOpportunity, opp_id) is None
    session.close()


def test_permanent_deletion_clears_its_history_and_notifications():
    """Both carry foreign keys to the row - leaving them would point at
    something that no longer exists."""
    profile = _profile()
    opp_id = _opportunity(profile["id"])
    client.post(f"/profiles/{profile['id']}/opportunities/{opp_id}/approve")
    client.post(f"/profiles/{profile['id']}/opportunities/{opp_id}/trash")

    session = db_module.SessionLocal()
    account_id = session.query(models_db.Account).first().id
    session.add(models_db.Notification(
        account_id=account_id, opportunity_id=opp_id,
        kind="ready", message="something",
    ))
    session.commit()
    session.close()

    assert client.delete(f"/profiles/{profile['id']}/opportunities/{opp_id}").status_code == 204

    session = db_module.SessionLocal()
    assert session.query(models_db.OpportunityEvent).filter_by(opportunity_id=opp_id).count() == 0
    assert session.query(models_db.Notification).filter_by(opportunity_id=opp_id).count() == 0
    session.close()


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------

def test_history_records_what_the_owner_actually_did():
    """The stage column only holds the latest state - "why is this approved
    when I remember dismissing it" needs a trail, not an inference."""
    profile = _profile()
    opp_id = _opportunity(profile["id"], stage="drafted")

    client.post(f"/profiles/{profile['id']}/opportunities/{opp_id}/dismiss")
    client.post(f"/profiles/{profile['id']}/opportunities/{opp_id}/approve")
    client.post(f"/profiles/{profile['id']}/opportunities/{opp_id}/trash")
    client.post(f"/profiles/{profile['id']}/opportunities/{opp_id}/restore")

    history = client.get(
        f"/profiles/{profile['id']}/opportunities/{opp_id}/history"
    ).json()

    assert [e["kind"] for e in history] == ["dismissed", "approved", "trashed", "restored"]
    # oldest first, so it reads as a story
    assert history[0]["created_at"] <= history[-1]["created_at"]


def test_history_notes_what_a_stage_change_was_from():
    profile = _profile()
    opp_id = _opportunity(profile["id"], stage="drafted")

    client.post(f"/profiles/{profile['id']}/opportunities/{opp_id}/approve")

    history = client.get(
        f"/profiles/{profile['id']}/opportunities/{opp_id}/history"
    ).json()

    assert history[0]["detail"] == "from drafted"


# --------------------------------------------------------------------------
# Ownership
# --------------------------------------------------------------------------

def test_another_account_cannot_touch_your_bin(outbox):
    profile = _profile()
    opp_id = _opportunity(profile["id"])
    client.cookies.clear()
    sign_up(client, email="someone-else@example.com", outbox=outbox)

    base = f"/profiles/{profile['id']}/opportunities/{opp_id}"
    assert client.post(f"{base}/trash").status_code == 404
    assert client.post(f"{base}/restore").status_code == 404
    assert client.delete(base).status_code == 404
    assert client.get(f"{base}/history").status_code == 404
