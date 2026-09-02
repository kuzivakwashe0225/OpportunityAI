from fastapi.testclient import TestClient

from opportunity_agent.api import app, store


client = TestClient(app)


def setup_function():
    store.reset()


def test_review_ui_shows_empty_state():
    response = client.get("/ui")

    assert response.status_code == 200
    assert "Scholarship Scout" in response.text
    assert "No profile yet" in response.text


def test_review_ui_shows_match_and_package_link():
    client.put("/profile", json={
        "name": "Test Applicant",
        "country": "Zimbabwe",
        "study_level": "masters",
        "field": "Computer Science",
        "documents": ["cv"],
    })
    opportunity = client.post("/opportunities", json={
        "source": "Example Foundation",
        "title": "STEM Award",
        "url": "https://example.org/award",
        "evidence": ["official page"],
        "requirements_verified": True,
    }).json()

    response = client.get("/ui")

    assert response.status_code == 200
    assert "STEM Award" in response.text
    assert f"/ui/opportunities/{opportunity['id']}/package" in response.text
    assert "needs_review" in response.text


def test_review_ui_renders_application_package():
    client.put("/profile", json={"name": "Test Applicant"})
    opportunity = client.post("/opportunities", json={
        "source": "Example Foundation",
        "title": "STEM Award",
        "url": "https://example.org/award",
        "evidence": ["official page"],
    }).json()

    response = client.get(f"/ui/opportunities/{opportunity['id']}/package")

    assert response.status_code == 200
    assert "Application package" in response.text
    assert "Dear Selection Committee" in response.text
    assert "https://example.org/award" in response.text


def test_review_ui_can_dismiss_opportunity():
    client.put("/profile", json={"name": "Test Applicant"})
    opportunity = client.post("/opportunities", json={
        "source": "Example Foundation",
        "title": "STEM Award",
        "url": "https://example.org/award",
        "evidence": ["official page"],
    }).json()

    response = client.post(
        f"/ui/opportunities/{opportunity['id']}/feedback",
        data={"decision": "dismissed"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "STEM Award" not in client.get("/ui").text