from fastapi.testclient import TestClient

from opportunity_agent.api import app, store


client = TestClient(app)


def setup_function():
    store.reset()


def test_interactive_ui_is_reachable_and_contains_controls():
    response = client.get("/ui")

    assert response.status_code == 200
    assert "Scholarship Scout" in response.text
    assert "profile-form" in response.text
    assert "discover-btn" in response.text
    assert "filter-tabs" in response.text
    assert "package-toggle" in response.text


def test_profile_read_endpoint_supports_frontend_bootstrap():
    assert client.get("/profile").json() is None
    client.put("/profile", json={"name": "Test Applicant", "goals": ["study climate technology"]})

    response = client.get("/profile")

    assert response.status_code == 200
    assert response.json()["goals"] == ["study climate technology"]


def test_interactive_ui_loads_opportunities_through_api():
    client.put("/profile", json={"name": "Test Applicant", "country": "Zimbabwe"})
    client.post("/opportunities", json={
        "source": "Example Foundation",
        "title": "STEM Award",
        "url": "https://example.org/award",
        "evidence": ["official page"],
    })

    response = client.get("/matches")

    assert response.status_code == 200
    assert response.json()[0]["opportunity"]["title"] == "STEM Award"


def test_feedback_actions_update_api_state():
    client.put("/profile", json={"name": "Test Applicant"})
    opportunity = client.post("/opportunities", json={
        "source": "Example Foundation",
        "title": "STEM Award",
        "url": "https://example.org/award",
        "evidence": ["official page"],
    }).json()

    response = client.post(
        f"/opportunities/{opportunity['id']}/feedback",
        json={"decision": "shortlisted"},
    )

    assert response.status_code == 200
    assert response.json()["decision"] == "shortlisted"
