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






def test_onboarding_final_step_is_profile_type_aware():
    """A company finishing setup must not be told to "upload a CV first".

    Steps 1 and 2 both branch on the profile's subject, but step 3 was static
    HTML that always pointed at a CV - so a tender profile ended its own
    onboarding aimed at the wrong paperwork. It now reads /schema like the
    other steps, which is also what makes it correct for profile types that
    do not exist yet.
    """
    response = client.get("/ui")

    assert "async function renderOnboardStep3" in response.text, "step 3 must fetch the schema"
    assert "Upload company documents" in response.text, "company path missing"
    assert "Upload a CV first" in response.text, "person path missing"
