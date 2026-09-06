from fastapi.testclient import TestClient

from conftest import sign_up

from opportunity_agent.api import app


client = TestClient(app)


def setup_function():
    client.cookies.clear()


def register(email="owner@example.com"):
    return client.post("/register", json={"email": email})


def test_register_creates_an_account_without_logging_it_in():
    """Changed deliberately: registration used to log the caller straight in.
    It now emails a password instead, which is also how it proves the person
    controls the address - so no session comes back."""
    response = register()

    assert response.status_code == 201
    assert response.json()["email"] == "owner@example.com"
    assert "session" not in response.cookies


def test_register_password_is_never_returned():
    response = register()

    assert "password" not in response.json()
    assert "password_hash" not in response.json()


def test_registration_is_open_to_more_than_one_account():
    """The single-account cap is gone. It existed because the legacy endpoints
    shared one global store; that store has been deleted."""
    assert register(email="first@example.com").status_code == 201
    assert register(email="second@example.com").status_code == 201


def test_registering_the_same_email_twice_is_rejected():
    # only reachable in practice if the single-account gate above didn't
    # already block it, but this is the right error if it ever does apply
    register(email="owner@example.com")
    client.cookies.clear()

    response = client.post("/register", json={"email": "owner@example.com"})

    assert response.status_code == 409


def test_login_with_correct_credentials_succeeds(outbox):
    register()
    client.cookies.clear()
    password = outbox[-1]["body"].split("Password:")[1].splitlines()[0].strip()

    response = client.post("/login", json={"email": "owner@example.com", "password": password})

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
    sign_up(client)

    response = client.get("/me")

    assert response.status_code == 200
    assert response.json()["email"] == "owner@example.com"


def test_logout_clears_the_session():
    sign_up(client)

    client.post("/logout")
    response = client.get("/me")

    assert response.status_code == 401


def test_health_and_ui_stay_unauthenticated():
    assert client.get("/health").status_code == 200
    assert client.get("/ui").status_code == 200








def test_invalid_session_cookie_is_rejected_not_crashed():
    client.cookies.set("session", "not-a-real-jwt")

    response = client.get("/me")

    assert response.status_code == 401
