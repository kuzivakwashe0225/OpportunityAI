from datetime import date

from fastapi.testclient import TestClient

from opportunity_agent.api import app, store


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
        "interests": ["technology"],
    }


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
