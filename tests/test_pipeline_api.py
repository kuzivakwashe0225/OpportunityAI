import pytest
from fastapi.testclient import TestClient

from opportunity_agent import api, models_db
from opportunity_agent import db as db_module
from opportunity_agent.connector import PublicPage
from opportunity_agent.search import SearchResult


client = TestClient(api.app)


def setup_function():
    client.cookies.clear()
    client.post("/register", json={"email": "owner@example.com", "password": "correct horse battery staple"})


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
