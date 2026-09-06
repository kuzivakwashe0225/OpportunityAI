from fastapi.testclient import TestClient

from conftest import sign_up
from opportunity_agent.api import app


client = TestClient(app)


def setup_function():
    client.cookies.clear()
    sign_up(client)


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




