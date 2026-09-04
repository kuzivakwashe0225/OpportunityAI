from fastapi.testclient import TestClient

from opportunity_agent.api import app, store


client = TestClient(app)


def setup_function():
    store.reset()
    client.cookies.clear()
    client.post("/register", json={"email": "owner@example.com", "password": "correct horse battery staple"})


def test_create_profile_requires_authentication():
    client.cookies.clear()

    response = client.post("/profiles", json={"profile_type": "scholarship", "display_name": "My scholarships"})

    assert response.status_code == 401


def test_create_profile_succeeds_for_a_valid_type():
    response = client.post("/profiles", json={"profile_type": "scholarship", "display_name": "My scholarships"})

    assert response.status_code == 201
    body = response.json()
    assert body["profile_type"] == "scholarship"
    assert body["display_name"] == "My scholarships"
    assert body["fields"] == {}


def test_create_profile_rejects_an_invalid_type():
    response = client.post("/profiles", json={"profile_type": "hobby", "display_name": "Nope"})

    assert response.status_code == 422


def test_cannot_create_two_profiles_of_the_same_type():
    client.post("/profiles", json={"profile_type": "job", "display_name": "Job search"})

    response = client.post("/profiles", json={"profile_type": "job", "display_name": "Second job profile"})

    assert response.status_code == 409


def test_can_create_all_three_profile_types():
    for profile_type in ["scholarship", "job", "grant"]:
        response = client.post("/profiles", json={"profile_type": profile_type, "display_name": profile_type})
        assert response.status_code == 201, profile_type


def test_list_profiles_returns_only_the_current_accounts_profiles():
    client.post("/profiles", json={"profile_type": "scholarship", "display_name": "Scholarships"})
    client.post("/profiles", json={"profile_type": "job", "display_name": "Jobs"})

    response = client.get("/profiles")

    assert response.status_code == 200
    types = {p["profile_type"] for p in response.json()}
    assert types == {"scholarship", "job"}


def test_get_one_profile_by_id():
    created = client.post("/profiles", json={"profile_type": "grant", "display_name": "Grants"}).json()

    response = client.get(f"/profiles/{created['id']}")

    assert response.status_code == 200
    assert response.json()["display_name"] == "Grants"


def test_get_unknown_profile_id_is_404():
    response = client.get("/profiles/does-not-exist")

    assert response.status_code == 404


def test_update_profile_fields_and_display_name():
    created = client.post("/profiles", json={"profile_type": "job", "display_name": "Jobs"}).json()

    response = client.put(
        f"/profiles/{created['id']}",
        json={"display_name": "My job search", "fields": {"name": "Tendai Moyo", "goals": ["climate tech"]}},
    )

    assert response.status_code == 200
    assert response.json()["display_name"] == "My job search"
    assert response.json()["fields"]["goals"] == ["climate tech"]


def test_a_second_registered_account_cannot_see_the_first_accounts_profiles():
    client.post("/profiles", json={"profile_type": "scholarship", "display_name": "Owner's scholarships"})

    # registration is capped at one account (see api.py), so simulate a second
    # account the way the app itself would prevent in production - directly,
    # to prove the isolation query itself is correct regardless of that cap
    from opportunity_agent import db as db_module
    from opportunity_agent import models_db

    with db_module.SessionLocal() as session:
        session.add(models_db.Account(email="intruder@example.com", password_hash="x"))
        session.commit()
        intruder = session.query(models_db.Account).filter_by(email="intruder@example.com").first()
        intruder_id = intruder.id

    from opportunity_agent import auth as auth_module

    token = auth_module.create_session_token(intruder_id)
    client.cookies.set("session", token)

    response = client.get("/profiles")

    assert response.status_code == 200
    assert response.json() == []
