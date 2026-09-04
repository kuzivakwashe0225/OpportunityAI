from fastapi.testclient import TestClient

from opportunity_agent.api import app


client = TestClient(app)


def test_health_endpoint():
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_root_redirects_to_ui():
    # A user visiting just the domain name should land somewhere real, not a
    # bare JSON 404 - found live on the deployed domain.
    response = client.get("/", follow_redirects=False)

    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/ui"
