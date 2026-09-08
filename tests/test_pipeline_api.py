import pytest
from fastapi.testclient import TestClient

from conftest import sign_up
from opportunity_agent import api, models_db
from opportunity_agent import db as db_module
from opportunity_agent.connector import PublicPage
from opportunity_agent.search import SearchResult


client = TestClient(api.app)


def setup_function():
    client.cookies.clear()
    sign_up(client)


def _profile():
    profile = client.post(
        "/profiles", json={"profile_type": "scholarship", "display_name": "Scholarships"}
    ).json()
    client.put(f"/profiles/{profile['id']}", json={"fields": {
        "name": "Tendai Moyo", "country": "Zimbabwe", "study_level": "masters",
        "field": "Computer Science", "documents": ["transcript", "cv"],
    }})
    return profile


def _eligible_page(url):
    return PublicPage(
        url=url,
        content=(
            "Open to citizens of Zimbabwe. Applicants must be enrolled in a master's "
            "programme in computer science. Required documents: CV, transcript."
        ),
        retrieved_at="2026-01-01T00:00:00Z", sha256="abc", content_type="text/html",
    )


def _ineligible_page(url):
    return PublicPage(
        url=url, content="Open to citizens of Kenya only. Agriculture degree required.",
        retrieved_at="2026-01-01T00:00:00Z", sha256="def", content_type="text/html",
    )


def _run_with(monkeypatch, profile_id, results, page_fn):
    monkeypatch.setattr(api, "_pipeline_search", lambda p, api_key, **kw: results)
    monkeypatch.setattr(api, "_pipeline_fetch", page_fn)
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    return client.post(f"/profiles/{profile_id}/run")


def test_run_requires_authentication():
    client.cookies.clear()

    assert client.post("/profiles/anything/run").status_code == 401


def test_run_discovers_and_auto_drafts_eligible_opportunities(monkeypatch):
    profile = _profile()

    response = _run_with(
        monkeypatch, profile["id"],
        [SearchResult(title="Award", url="https://example.org/award", content="x")],
        _eligible_page,
    )

    assert response.status_code == 200
    assert response.json()["drafted"] == 1

    opportunities = client.get(f"/profiles/{profile['id']}/opportunities").json()
    assert len(opportunities) == 1
    assert opportunities[0]["stage"] == "drafted"
    assert opportunities[0]["match_status"] == "eligible"


def test_review_queue_only_returns_drafted_work(monkeypatch):
    profile = _profile()
    _run_with(
        monkeypatch, profile["id"],
        [
            SearchResult(title="Award", url="https://example.org/award", content="x"),
            SearchResult(title="Kenya", url="https://example.org/kenya", content="x"),
        ],
        lambda url: _ineligible_page(url) if "kenya" in url else _eligible_page(url),
    )

    queue = client.get(f"/profiles/{profile['id']}/opportunities?stage=drafted").json()

    assert len(queue) == 1
    assert queue[0]["match_status"] == "eligible"


def test_opportunity_detail_includes_the_drafted_package(monkeypatch):
    profile = _profile()
    _run_with(
        monkeypatch, profile["id"],
        [SearchResult(title="Award", url="https://example.org/award", content="x")],
        _eligible_page,
    )
    opportunity_id = client.get(f"/profiles/{profile['id']}/opportunities").json()[0]["id"]

    detail = client.get(f"/profiles/{profile['id']}/opportunities/{opportunity_id}").json()

    assert "package" in detail
    assert "Dear Selection Committee" in detail["package"]["cover_note"]
    assert detail["package"]["checklist"]


def test_approving_a_draft_advances_it_but_does_not_submit(monkeypatch):
    profile = _profile()
    _run_with(
        monkeypatch, profile["id"],
        [SearchResult(title="Award", url="https://example.org/award", content="x")],
        _eligible_page,
    )
    opportunity_id = client.get(f"/profiles/{profile['id']}/opportunities").json()[0]["id"]

    response = client.post(f"/profiles/{profile['id']}/opportunities/{opportunity_id}/approve")

    assert response.status_code == 200
    assert response.json()["stage"] == "approved"
    # "approved" is explicitly not "submitted" - nothing in this system sends
    # anything anywhere on its own
    assert response.json()["stage"] != "submitted"


def test_marking_submitted_is_a_separate_deliberate_step(monkeypatch):
    profile = _profile()
    _run_with(
        monkeypatch, profile["id"],
        [SearchResult(title="Award", url="https://example.org/award", content="x")],
        _eligible_page,
    )
    opportunity_id = client.get(f"/profiles/{profile['id']}/opportunities").json()[0]["id"]
    client.post(f"/profiles/{profile['id']}/opportunities/{opportunity_id}/approve")

    response = client.post(f"/profiles/{profile['id']}/opportunities/{opportunity_id}/submitted")

    assert response.json()["stage"] == "submitted"


def test_dismissing_a_draft_removes_it_from_the_queue(monkeypatch):
    profile = _profile()
    _run_with(
        monkeypatch, profile["id"],
        [SearchResult(title="Award", url="https://example.org/award", content="x")],
        _eligible_page,
    )
    opportunity_id = client.get(f"/profiles/{profile['id']}/opportunities").json()[0]["id"]

    client.post(f"/profiles/{profile['id']}/opportunities/{opportunity_id}/dismiss")

    assert client.get(f"/profiles/{profile['id']}/opportunities?stage=drafted").json() == []


def test_escalating_an_ineligible_opportunity_drafts_it(monkeypatch):
    profile = _profile()
    _run_with(
        monkeypatch, profile["id"],
        [SearchResult(title="Kenya", url="https://example.org/kenya", content="x")],
        _ineligible_page,
    )
    opportunity_id = client.get(f"/profiles/{profile['id']}/opportunities").json()[0]["id"]

    response = client.post(f"/profiles/{profile['id']}/opportunities/{opportunity_id}/escalate")

    assert response.status_code == 200
    assert response.json()["stage"] == "drafted"
    assert response.json()["escalated"] is True
    assert response.json()["match_status"] == "ineligible"  # verdict preserved


def test_notifications_are_listed_and_can_be_marked_read(monkeypatch):
    profile = _profile()
    _run_with(
        monkeypatch, profile["id"],
        [SearchResult(title="Award", url="https://example.org/award", content="x")],
        _eligible_page,
    )

    notifications = client.get("/notifications").json()
    assert len(notifications) == 1
    assert notifications[0]["read_at"] is None

    client.post(f"/notifications/{notifications[0]['id']}/read")

    assert client.get("/notifications?unread=true").json() == []


def test_profile_summary_powers_the_dashboard(monkeypatch):
    profile = _profile()
    _run_with(
        monkeypatch, profile["id"],
        [
            SearchResult(title="Award", url="https://example.org/award", content="x"),
            SearchResult(title="Kenya", url="https://example.org/kenya", content="x"),
        ],
        lambda url: _ineligible_page(url) if "kenya" in url else _eligible_page(url),
    )

    summary = client.get(f"/profiles/{profile['id']}/summary").json()

    assert summary["total"] == 2
    assert summary["awaiting_review"] == 1
    assert summary["not_eligible"] == 1
    assert summary["last_run"] is not None


def test_opportunities_of_another_account_are_not_reachable(monkeypatch):
    profile = _profile()
    _run_with(
        monkeypatch, profile["id"],
        [SearchResult(title="Award", url="https://example.org/award", content="x")],
        _eligible_page,
    )

    from opportunity_agent import auth as auth_module

    with db_module.SessionLocal() as session:
        intruder = models_db.Account(email="intruder@example.com", password_hash="x")
        session.add(intruder)
        session.commit()
        token = auth_module.create_session_token(intruder.id)

    client.cookies.set("session", token)

    assert client.get(f"/profiles/{profile['id']}/opportunities").status_code == 404
    assert client.get("/notifications").json() == []


# ---------------------------------------------------------------------------
# The setup form and document slots are driven by the profile type
# ---------------------------------------------------------------------------

def test_a_scholarship_profile_is_asked_person_questions():
    profile = _profile()
    schema = client.get(f"/profiles/{profile['id']}/schema").json()

    keys = {f["key"] for f in schema["fields"]}
    assert schema["subject"] == "individual"
    assert "study_level" in keys
    assert "praz_categories" not in keys
    assert "cv" in {d["key"] for d in schema["documents"]}


def test_a_tender_profile_is_asked_company_questions_instead():
    """The core of the request: choosing tenders must not hand a company the
    scholarship form."""
    profile = client.post(
        "/profiles", json={"profile_type": "tender", "display_name": "Tenders"}
    ).json()
    schema = client.get(f"/profiles/{profile['id']}/schema").json()

    keys = {f["key"] for f in schema["fields"]}
    documents = {d["key"] for d in schema["documents"]}

    assert schema["subject"] == "organisation"
    assert "praz_categories" in keys
    assert "study_level" not in keys
    assert "certificate_of_incorporation" in documents
    assert "tax_clearance" in documents
    assert "cv" not in documents


def test_the_schema_says_which_papers_are_still_outstanding():
    profile = client.post(
        "/profiles", json={"profile_type": "tender", "display_name": "Tenders"}
    ).json()
    schema = client.get(f"/profiles/{profile['id']}/schema").json()

    assert "tax_clearance" in schema["missing_documents"]
    assert all(d["held"] is False for d in schema["documents"])


def test_a_grant_profile_can_be_switched_to_a_company_and_its_form_changes():
    profile = client.post(
        "/profiles", json={"profile_type": "grant", "display_name": "Grants"}
    ).json()

    as_person = client.get(f"/profiles/{profile['id']}/schema").json()
    assert as_person["subject"] == "individual"
    assert as_person["subject_is_choosable"] is True

    client.put(f"/profiles/{profile['id']}", json={
        "fields": {"name": "Meshcloud", "subject": "organisation"}
    })
    as_company = client.get(f"/profiles/{profile['id']}/schema").json()

    assert as_company["subject"] == "organisation"
    assert "certificate_of_incorporation" in {d["key"] for d in as_company["documents"]}


def test_profile_types_are_listed_for_onboarding():
    types = {t["key"]: t for t in client.get("/profile-types").json()}

    assert set(types) == {"scholarship", "job", "grant", "tender"}
    assert types["tender"]["subject"] == "organisation"
    assert types["grant"]["subject"] == "either"


def test_another_account_cannot_read_a_profiles_schema():
    profile = _profile()
    client.cookies.clear()

    assert client.get(f"/profiles/{profile['id']}/schema").status_code == 401


# ---------------------------------------------------------------------------
# eGP credentials: the owner's own portal login, stored encrypted
# ---------------------------------------------------------------------------

import pytest

from opportunity_agent import credentials as credentials_module


@pytest.fixture
def key(monkeypatch):
    k = credentials_module.generate_key()
    monkeypatch.setenv(credentials_module.KEY_ENV_VAR, k)
    return k


def _tender_profile():
    return client.post(
        "/profiles", json={"profile_type": "tender", "display_name": "Tenders"}
    ).json()


def test_credentials_can_be_stored_and_reported_without_the_password(key):
    profile = _tender_profile()

    saved = client.put(f"/profiles/{profile['id']}/credentials/egp",
                       json={"username": "meshcloud", "password": "sup3rs3cret"})

    assert saved.status_code == 200
    body = saved.json()
    assert body["username"] == "meshcloud"
    assert body["has_password"] is True
    assert body["verification_status"] == "untested"
    # The whole point: no endpoint anywhere hands the password back.
    assert "sup3rs3cret" not in saved.text
    assert "password" not in body
    assert "secret_ciphertext" not in body


def test_the_password_is_encrypted_in_the_database_not_stored_in_clear(key):
    profile = _tender_profile()
    client.put(f"/profiles/{profile['id']}/credentials/egp",
               json={"username": "meshcloud", "password": "sup3rs3cret"})

    db = db_module.SessionLocal()
    try:
        row = db.query(models_db.PortalCredential).filter_by(profile_id=profile["id"]).one()
        assert "sup3rs3cret" not in row.secret_ciphertext
        assert credentials_module.decrypt_secret(row.secret_ciphertext) == "sup3rs3cret"
    finally:
        db.close()


def test_the_password_never_appears_in_any_read_endpoint(key):
    profile = _tender_profile()
    client.put(f"/profiles/{profile['id']}/credentials/egp",
               json={"username": "meshcloud", "password": "sup3rs3cret"})

    for path in (f"/profiles/{profile['id']}",
                 f"/profiles/{profile['id']}/credentials",
                 f"/profiles/{profile['id']}/schema",
                 "/profiles"):
        assert "sup3rs3cret" not in client.get(path).text, path


def test_without_an_encryption_key_storing_is_refused_not_downgraded(monkeypatch):
    """The failure that turns a database backup into a credential dump."""
    monkeypatch.delenv(credentials_module.KEY_ENV_VAR, raising=False)
    profile = _tender_profile()

    response = client.put(f"/profiles/{profile['id']}/credentials/egp",
                          json={"username": "meshcloud", "password": "sup3rs3cret"})

    assert response.status_code == 503
    assert credentials_module.KEY_ENV_VAR in response.json()["detail"]

    db = db_module.SessionLocal()
    try:
        assert db.query(models_db.PortalCredential).count() == 0
    finally:
        db.close()


def test_verification_is_owner_triggered_and_records_the_outcome(key, monkeypatch):
    profile = _tender_profile()
    client.put(f"/profiles/{profile['id']}/credentials/egp",
               json={"username": "meshcloud", "password": "sup3rs3cret"})

    seen = {}

    def fake_verify(username, password):
        seen["username"] = username
        seen["password"] = password
        return "verified", "eGP accepted these credentials"

    monkeypatch.setattr(api, "_verify_egp_credentials", fake_verify)
    body = client.post(f"/profiles/{profile['id']}/credentials/egp/verify").json()

    # the real password is decrypted and handed to the portal, not a placeholder
    assert seen == {"username": "meshcloud", "password": "sup3rs3cret"}
    assert body["verification_status"] == "verified"
    assert body["last_verified_at"] is not None


def test_a_rejected_login_is_recorded_as_failed_not_verified(key, monkeypatch):
    profile = _tender_profile()
    client.put(f"/profiles/{profile['id']}/credentials/egp",
               json={"username": "meshcloud", "password": "wrong"})
    monkeypatch.setattr(api, "_verify_egp_credentials",
                        lambda u, p: ("failed", "eGP rejected the credentials"))

    body = client.post(f"/profiles/{profile['id']}/credentials/egp/verify").json()

    assert body["verification_status"] == "failed"
    assert body["last_verified_at"] is None
    assert "rejected" in body["verification_detail"]


def test_changing_the_password_resets_a_previous_verification(key, monkeypatch):
    profile = _tender_profile()
    client.put(f"/profiles/{profile['id']}/credentials/egp",
               json={"username": "meshcloud", "password": "old"})
    monkeypatch.setattr(api, "_verify_egp_credentials", lambda u, p: ("verified", "ok"))
    client.post(f"/profiles/{profile['id']}/credentials/egp/verify")

    body = client.put(f"/profiles/{profile['id']}/credentials/egp",
                      json={"username": "meshcloud", "password": "new"}).json()

    assert body["verification_status"] == "untested"
    assert body["last_verified_at"] is None


def test_credentials_can_be_removed(key):
    profile = _tender_profile()
    client.put(f"/profiles/{profile['id']}/credentials/egp",
               json={"username": "meshcloud", "password": "sup3rs3cret"})

    assert client.delete(f"/profiles/{profile['id']}/credentials/egp").status_code == 204
    assert client.get(f"/profiles/{profile['id']}/credentials").json() == []


def test_an_unknown_portal_is_refused(key):
    profile = _tender_profile()
    response = client.put(f"/profiles/{profile['id']}/credentials/not-a-portal",
                          json={"username": "x", "password": "y"})
    assert response.status_code == 422


def test_another_account_cannot_touch_stored_credentials(key):
    profile = _tender_profile()
    client.put(f"/profiles/{profile['id']}/credentials/egp",
               json={"username": "meshcloud", "password": "sup3rs3cret"})
    client.cookies.clear()

    assert client.get(f"/profiles/{profile['id']}/credentials").status_code == 401
    assert client.delete(f"/profiles/{profile['id']}/credentials/egp").status_code == 401


# ---------------------------------------------------------------------------
# Award notices reach the owner through the same opportunity the agent
# already showed them - see pipeline.py's _mark_awarded_elsewhere.
# ---------------------------------------------------------------------------

def test_an_opportunity_awarded_elsewhere_reports_it_over_the_api():
    from datetime import date

    profile = _tender_profile()
    with db_module.SessionLocal() as session:
        stored = models_db.StoredOpportunity(
            profile_id=profile["id"],
            canonical_url="https://egp.praz.org.zw/Indexes/viewLiveTenderDetails/1",
            payload={"source": "PRAZ eGP", "title": "Supply of transformers",
                     "url": "https://egp.praz.org.zw/Indexes/viewLiveTenderDetails/1"},
            match_status="eligible", match_score=90, stage="drafted",
            awarded_to="Acme Rivals Ltd", awarded_at=date(2026, 9, 1),
        )
        session.add(stored)
        session.commit()
        opp_id = stored.id

    body = client.get(f"/profiles/{profile['id']}/opportunities/{opp_id}").json()

    assert body["awarded_to"] == "Acme Rivals Ltd"
    assert body["awarded_at"] == "2026-09-01"


def test_an_opportunity_never_awarded_reports_null():
    profile = _tender_profile()
    with db_module.SessionLocal() as session:
        stored = models_db.StoredOpportunity(
            profile_id=profile["id"],
            canonical_url="https://egp.praz.org.zw/Indexes/viewLiveTenderDetails/2",
            payload={"source": "PRAZ eGP", "title": "Supply of desks",
                     "url": "https://egp.praz.org.zw/Indexes/viewLiveTenderDetails/2"},
            match_status="eligible", match_score=90, stage="drafted",
        )
        session.add(stored)
        session.commit()
        opp_id = stored.id

    body = client.get(f"/profiles/{profile['id']}/opportunities/{opp_id}").json()

    assert body["awarded_to"] is None
    assert body["awarded_at"] is None
