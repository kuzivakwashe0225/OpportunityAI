from datetime import date

from fastapi.testclient import TestClient

from opportunity_agent.api import app, store
from opportunity_agent.search import SearchResult
from opportunity_agent.connector import PublicPage


client = TestClient(app)


def setup_function():
    store.reset()


def profile_payload():
    return {
        "name": "Test Applicant",
        "country": "Zimbabwe",
        "age": 29,
        "study_level": "masters",
        "field": "Computer Science",
        "documents": ["transcript", "cv"],
        "interests": ["technology"],
    }


def opportunity_payload(title="STEM Award"):
    return {
        "source": "Example Foundation",
        "title": title,
        "url": f"https://example.org/{title.lower().replace(' ', '-')}",
        "deadline": "2026-12-01",
        "eligible_countries": ["Zimbabwe"],
        "required_levels": ["masters"],
        "required_fields": ["computer science"],
        "required_documents": ["transcript", "cv"],
        "evidence": ["official eligibility page"],
        "requirements_verified": True,
        "interests": ["technology"],
    }


def test_get_profile_returns_null_before_one_is_saved():
    response = client.get("/profile")

    assert response.status_code == 200
    assert response.json() is None


def test_get_profile_returns_the_saved_profile():
    client.put("/profile", json=profile_payload())

    response = client.get("/profile")

    assert response.status_code == 200
    assert response.json()["name"] == "Test Applicant"


def test_profile_opportunity_match_and_feedback_flow():
    profile_response = client.put("/profile", json=profile_payload())
    assert profile_response.status_code == 200

    opportunity_response = client.post("/opportunities", json=opportunity_payload())
    assert opportunity_response.status_code == 201
    opportunity_id = opportunity_response.json()["id"]

    matches_response = client.get("/matches")
    assert matches_response.status_code == 200
    assert matches_response.json()[0]["opportunity"]["title"] == "STEM Award"
    assert matches_response.json()[0]["match"]["status"] == "eligible"

    feedback_response = client.post(
        f"/opportunities/{opportunity_id}/feedback",
        json={"decision": "shortlisted"},
    )
    assert feedback_response.status_code == 200
    assert feedback_response.json()["decision"] == "shortlisted"


def test_digest_excludes_ineligible_items():
    client.put("/profile", json=profile_payload())
    client.post("/opportunities", json=opportunity_payload())
    client.post(
        "/opportunities",
        json={**opportunity_payload("Kenya Award"), "eligible_countries": ["Kenya"]},
    )

    response = client.get("/digest")

    assert response.status_code == 200
    assert "STEM Award" in response.json()["content"]
    assert "Kenya Award" not in response.json()["content"]


def test_missing_profile_prevents_matching():
    response = client.get("/matches")

    assert response.status_code == 409


def test_discover_stores_search_result_drafts(monkeypatch):
    client.put("/profile", json=profile_payload())

    def fake_discover(profile, *, api_key):
        return [SearchResult(
            title="Discovered Award",
            url="https://scholarships.example.org/award",
            content="Official eligibility notice",
        )]

    monkeypatch.setattr("opportunity_agent.api.discover", fake_discover)
    monkeypatch.setattr(
        "opportunity_agent.api.fetch_public_page",
        lambda url: PublicPage(url=url, content="Applications close 1 December 2026.", retrieved_at="now", sha256="hash"),
    )
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    response = client.post("/discover")

    assert response.status_code == 200
    assert response.json()["added"] == 1
    assert response.json()["opportunities"][0]["title"] == "Discovered Award"
    assert response.json()["opportunities"][0]["retrieved_at"] == "now"


def test_repeated_discovery_does_not_duplicate_urls(monkeypatch):
    client.put("/profile", json=profile_payload())

    def fake_discover(profile, *, api_key):
        return [SearchResult(
            title="Same Award",
            url="https://scholarships.example.org/award#details",
            content="Official eligibility notice",
        )]

    monkeypatch.setattr("opportunity_agent.api.discover", fake_discover)
    monkeypatch.setattr(
        "opportunity_agent.api.fetch_public_page",
        lambda url: PublicPage(url=url, content="Official eligibility notice", retrieved_at="now", sha256="hash"),
    )
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    client.post("/discover")
    client.post("/discover")

    assert len(client.get("/matches").json()) == 1


def test_discovery_skips_malformed_and_untitled_results(monkeypatch):
    client.put("/profile", json=profile_payload())

    def fake_discover(profile, *, api_key):
        return [
            SearchResult(title="", url="https://example.org/untitled", content="details"),
            SearchResult(title="Bad", url="not-a-url", content="details"),
        ]

    monkeypatch.setattr("opportunity_agent.api.discover", fake_discover)
    monkeypatch.setattr(
        "opportunity_agent.api.fetch_public_page",
        lambda url: PublicPage(url=url, content="details", retrieved_at="now", sha256="hash"),
    )
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")

    response = client.post("/discover")

    assert response.status_code == 200
    assert response.json()["added"] == 1
    assert response.json()["opportunities"][0]["title"] == "Untitled scholarship opportunity"


def test_discovery_exposes_run_audit_record(monkeypatch):
    client.put("/profile", json=profile_payload())
    monkeypatch.setattr("opportunity_agent.api.discover", lambda profile, *, api_key: [])
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")

    response = client.post("/discover")
    runs = client.get("/runs")

    assert response.status_code == 200
    assert response.json()["run_id"]
    assert runs.json()[0]["id"] == response.json()["run_id"]
    assert runs.json()[0]["found"] == 0
    assert runs.json()[0]["sources"] == []


def test_discovery_run_records_source_provenance(monkeypatch):
    client.put("/profile", json=profile_payload())
    monkeypatch.setattr(
        "opportunity_agent.api.discover",
        lambda profile, *, api_key: [SearchResult(
            title="Award", url="https://example.org/award", content="snippet"
        )],
    )
    monkeypatch.setattr(
        "opportunity_agent.api.fetch_public_page",
        lambda url: PublicPage(
            url=url,
            content="Open to Zimbabwe applicants.",
            retrieved_at="now",
            sha256="hash",
            content_type="text/html",
        ),
    )
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")

    response = client.post("/discover")

    assert response.status_code == 200
    assert client.get("/runs").json()[0]["sources"] == [{
        "url": "https://example.org/award",
        "status": "parsed",
        "content_type": "text/html",
        "parser_version": "scholarship-regex-v1",
    }]


def test_failed_discovery_still_records_run(monkeypatch):
    client.put("/profile", json=profile_payload())

    def failing_discover(profile, *, api_key):
        raise RuntimeError("search unavailable")

    monkeypatch.setattr("opportunity_agent.api.discover", failing_discover)
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")

    response = client.post("/discover")
    runs = client.get("/runs")

    assert response.status_code == 502
    assert runs.json()[0]["found"] == 0
    assert "search unavailable" in runs.json()[0]["failures"][0]


def test_source_failure_preserves_error_detail(monkeypatch):
    client.put("/profile", json=profile_payload())
    monkeypatch.setattr(
        "opportunity_agent.api.discover",
        lambda profile, *, api_key: [SearchResult(title="Award", url="https://example.org/award")],
    )
    monkeypatch.setattr(
        "opportunity_agent.api.fetch_public_page",
        lambda url: (_ for _ in ()).throw(ValueError("response exceeds size limit")),
    )
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")

    client.post("/discover")
    run = client.get("/runs").json()[0]

    assert "response exceeds size limit" in run["failures"][0]
    assert "response exceeds size limit" in run["sources"][0]["error"]


def test_dismissed_opportunity_is_removed_from_digest():
    client.put("/profile", json=profile_payload())
    opportunity_response = client.post("/opportunities", json=opportunity_payload())
    opportunity_id = opportunity_response.json()["id"]

    client.post(
        f"/opportunities/{opportunity_id}/feedback",
        json={"decision": "dismissed"},
    )

    response = client.get("/digest")

    assert "STEM Award" not in response.json()["content"]


def test_usefulness_feedback_does_not_replace_shortlist_decision():
    client.put("/profile", json=profile_payload())
    opportunity_response = client.post("/opportunities", json=opportunity_payload())
    opportunity_id = opportunity_response.json()["id"]

    client.post(
        f"/opportunities/{opportunity_id}/feedback",
        json={"decision": "shortlisted"},
    )
    response = client.post(
        f"/opportunities/{opportunity_id}/feedback",
        json={"decision": "useful"},
    )

    assert response.json()["decision"] == "shortlisted"
    assert response.json()["usefulness"] == "useful"


def test_package_endpoint_builds_reviewable_application_package():
    client.put("/profile", json=profile_payload())
    opportunity_response = client.post("/opportunities", json=opportunity_payload())
    opportunity_id = opportunity_response.json()["id"]

    response = client.get(f"/opportunities/{opportunity_id}/package")

    assert response.status_code == 200
    assert response.json()["opportunity_url"] == opportunity_payload()["url"]
    assert response.json()["checklist"]
    assert response.json()["evidence"] == ["official eligibility page"]


def test_package_endpoint_requires_profile_and_known_opportunity():
    response = client.get("/opportunities/missing/package")

    assert response.status_code == 409
