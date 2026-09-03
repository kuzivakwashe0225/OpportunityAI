from fastapi.testclient import TestClient

from opportunity_agent.api import app, store


client = TestClient(app)


def setup_function():
    store.reset()
    client.cookies.clear()


def register(email="owner@example.com", password="correct horse battery staple"):
    return client.post("/register", json={"email": email, "password": password})


def test_register_creates_the_first_account_and_logs_in():
    response = register()

    assert response.status_code == 201
    assert response.json()["email"] == "owner@example.com"
    assert "session" in response.cookies


def test_register_password_is_never_returned():
    response = register()

    assert "password" not in response.json()
    assert "password_hash" not in response.json()


def test_registration_is_closed_after_the_first_account_exists():
    register(email="first@example.com")

    response = register(email="second@example.com")

    assert response.status_code == 403


def test_registering_the_same_email_twice_is_rejected():
    # only reachable in practice if the single-account gate above didn't
    # already block it, but this is the right error if it ever does apply
    register(email="owner@example.com")
    client.cookies.clear()

    response = client.post(
        "/register", json={"email": "owner@example.com", "password": "a different password"}
    )

    assert response.status_code in (403, 409)


def test_login_with_correct_credentials_succeeds():
    register()
    client.cookies.clear()

    response = client.post(
        "/login", json={"email": "owner@example.com", "password": "correct horse battery staple"}
    )

    assert response.status_code == 200
    assert "session" in response.cookies


def test_login_with_wrong_password_is_rejected():
    register()
    client.cookies.clear()

    response = client.post("/login", json={"email": "owner@example.com", "password": "wrong password"})

    assert response.status_code == 401


def test_login_with_unknown_email_is_rejected():
    response = client.post("/login", json={"email": "nobody@example.com", "password": "whatever"})

    assert response.status_code == 401


def test_me_requires_authentication():
    response = client.get("/me")

    assert response.status_code == 401


def test_me_returns_the_logged_in_account():
    register()

    response = client.get("/me")

    assert response.status_code == 200
    assert response.json()["email"] == "owner@example.com"


def test_logout_clears_the_session():
    register()

    client.post("/logout")
    response = client.get("/me")

    assert response.status_code == 401


def test_health_and_ui_stay_unauthenticated():
    assert client.get("/health").status_code == 200
    assert client.get("/ui").status_code == 200


def test_profile_endpoints_require_authentication():
    assert client.get("/profile").status_code == 401
    assert client.put("/profile", json={"name": "Test"}).status_code == 401


def test_discover_endpoint_requires_authentication():
    assert client.post("/discover").status_code == 401


def test_matches_and_digest_endpoints_require_authentication():
    assert client.get("/matches").status_code == 401
    assert client.get("/digest").status_code == 401


def test_runs_endpoint_requires_authentication():
    assert client.get("/runs").status_code == 401


def test_opportunities_endpoints_require_authentication():
    assert client.post("/opportunities", json={
        "source": "x", "title": "x", "url": "https://example.org", "evidence": ["x"],
    }).status_code == 401
    assert client.get("/opportunities/whatever/package").status_code == 401
    assert client.post("/opportunities/whatever/feedback", json={"decision": "shortlisted"}).status_code == 401


def test_authenticated_session_reaches_the_existing_endpoints():
    register()

    response = client.put("/profile", json={"name": "Test Applicant"})

    assert response.status_code == 200
    assert client.get("/matches").status_code == 200
    assert client.get("/digest").status_code == 200
    assert client.get("/runs").status_code == 200


def test_invalid_session_cookie_is_rejected_not_crashed():
    client.cookies.set("session", "not-a-real-jwt")

    response = client.get("/me")

    assert response.status_code == 401
