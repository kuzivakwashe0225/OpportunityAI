from fastapi.testclient import TestClient

from opportunity_agent.api import app, store


client = TestClient(app)


def setup_function():
    store.reset()
    client.cookies.clear()
    client.post("/register", json={"email": "owner@example.com", "password": "correct horse battery staple"})


def test_interactive_ui_is_reachable_and_contains_the_app_shell():
    response = client.get("/ui")

    assert response.status_code == 200
    assert "OpportunityAI" in response.text
    # app shell: auth gate, profile switcher, notification bell, routed views
    assert "auth-view" in response.text
    assert "app-shell" in response.text
    assert "switcher-menu" in response.text
    assert "view-root" in response.text


def test_ui_exposes_the_agentic_workflow_routes():
    """The views the workflow actually needs - onboarding first, then a
    dashboard, a review queue, and a place to browse what was ruled out."""
    response = client.get("/ui")

    for route in ["#/onboarding", "#/dashboard", "#/review", "#/opportunities", "#/profile"]:
        assert route in response.text, f"{route} missing from the UI"


def test_ui_talks_to_the_per_profile_pipeline_endpoints():
    response = client.get("/ui")

    for endpoint in ["/profiles", "/summary", "/opportunities", "/notifications", "/run"]:
        assert endpoint in response.text, f"{endpoint} not referenced by the UI"


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
