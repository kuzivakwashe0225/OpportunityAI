"""Feedback, automatic issue capture, and the admin monitoring dashboard.

Built for the point the owner said they had reached: real people, testing
real use cases, and a need to see what is happening - and fix it - while the
test is still running rather than after. Three things had to be true:

  1. A tester can tell us something without leaving the app.
  2. A fault the system hits on its own - a client exception, a failed
     request, an unhandled server error - is captured without anyone having
     to notice, describe, and report it.
  3. Only the developer can see any of this. It is deliberately cross-account
     - the one screen in this system that is - so the boundary that keeps it
     that way (require_admin) gets tested as hard as the isolation the rest
     of the app depends on.
"""

from fastapi.testclient import TestClient

from conftest import sign_up
from opportunity_agent import api, models_db, pipeline as pipeline_module
from opportunity_agent import db as db_module

client = TestClient(api.app)


def setup_function():
    client.cookies.clear()


def _profile(caller, email):
    sign_up(caller, email=email)
    return caller.post(
        "/profiles", json={"profile_type": "grant", "display_name": "G"}
    ).json()


def _make_admin(monkeypatch, email):
    monkeypatch.setattr(api, "ADMIN_EMAILS", {email.lower()})


# --------------------------------------------------------------------------
# Feedback
# --------------------------------------------------------------------------

def test_a_tester_can_submit_feedback():
    profile = _profile(client, "tester@example.com")

    response = client.post("/feedback", json={
        "category": "confusing", "message": "I couldn't find where to upload my CV",
        "route": "#/onboarding",
    })

    assert response.status_code == 201
    body = response.json()
    assert body["category"] == "confusing"
    assert body["status"] == "new"
    assert body["route"] == "#/onboarding"


def test_feedback_is_attached_to_the_one_unambiguous_profile():
    profile = _profile(client, "tester@example.com")

    body = client.post("/feedback", json={"message": "nice"}).json()

    assert body["profile_id"] == profile["id"]


def test_feedback_is_left_unattached_when_there_are_several_profiles():
    """Guessing wrong would attach it to the wrong one, which is worse than
    leaving it blank."""
    sign_up(client, email="tester@example.com")
    client.post("/profiles", json={"profile_type": "grant", "display_name": "A"})
    client.post("/profiles", json={"profile_type": "job", "display_name": "B"})

    body = client.post("/feedback", json={"message": "nice"}).json()

    assert body["profile_id"] is None


def test_an_empty_message_is_refused():
    _profile(client, "tester@example.com")

    response = client.post("/feedback", json={"message": "   "})

    assert response.status_code == 422


def test_an_unrecognised_category_falls_back_to_other_rather_than_failing():
    _profile(client, "tester@example.com")

    body = client.post("/feedback", json={
        "category": "something-i-made-up", "message": "hello"}).json()

    assert body["category"] == "other"


def test_submitting_feedback_requires_being_signed_in():
    response = client.post("/feedback", json={"message": "hello"})

    assert response.status_code == 401


def test_feedback_is_invisible_without_admin_access():
    """The one property that has to hold: an ordinary account, however many
    profiles it has, cannot read anyone's feedback - including its own,
    through this endpoint. That view exists only for the developer."""
    _profile(client, "tester@example.com")
    client.post("/feedback", json={"message": "hello"})

    assert client.get("/admin/feedback").status_code == 404


# --------------------------------------------------------------------------
# Client-side issue reports
# --------------------------------------------------------------------------

def test_a_client_error_can_be_reported_while_signed_out():
    """The point of this endpoint: the sign-in screen itself is not exempt
    from bugs, and an error there is exactly the kind nobody would think to
    report by hand."""
    response = client.post("/issues/client", json={
        "message": "TypeError: cannot read properties of undefined",
        "detail": "at renderDashboard (index.html:1234)",
        "route": "#/dashboard",
    })

    assert response.status_code == 202


def test_a_signed_out_report_is_recorded_with_no_account():
    client.post("/issues/client", json={"message": "boom"})

    session = db_module.SessionLocal()
    row = session.query(models_db.IssueReport).filter_by(source="client").first()
    session.close()

    assert row is not None
    assert row.account_id is None
    assert row.message == "boom"


def test_a_signed_in_reports_error_is_attributed_to_the_account():
    sign_up(client, email="tester@example.com")

    client.post("/issues/client", json={"message": "boom while signed in"})

    session = db_module.SessionLocal()
    account = session.query(models_db.Account).filter_by(email="tester@example.com").one()
    row = session.query(models_db.IssueReport).filter_by(
        message="boom while signed in").first()
    assert row.account_id == account.id
    session.close()


def test_a_long_message_and_detail_are_capped_not_rejected():
    response = client.post("/issues/client", json={
        "message": "x" * 5000, "detail": "y" * 10000})

    assert response.status_code == 202
    session = db_module.SessionLocal()
    row = session.query(models_db.IssueReport).order_by(
        models_db.IssueReport.created_at.desc()).first()
    assert len(row.message) <= 500
    assert len(row.detail) <= 4000
    session.close()


def test_repeated_client_error_reports_from_one_address_are_throttled():
    codes = [client.post("/issues/client", json={"message": f"error {n}"}).status_code
             for n in range(40)]

    assert 429 in codes


# --------------------------------------------------------------------------
# Server-side issue capture
# --------------------------------------------------------------------------

def test_an_unhandled_exception_is_recorded_and_the_caller_gets_a_clean_500(monkeypatch):
    """Nothing about what actually failed reaches whoever triggered it - only
    the admin issue feed sees the real message and traceback.

    Uses a client with raise_server_exceptions=False: Starlette's TestClient
    re-raises an unhandled exception into the *test* by default, even once a
    registered handler has already turned it into a real response - useful
    for a debugger, useless for asserting on the response the handler
    actually produced, which is what this test is checking.
    """
    no_raise = TestClient(api.app, raise_server_exceptions=False)
    profile = _profile(no_raise, "tester@example.com")

    def boom(profile_arg):
        raise RuntimeError("a deliberately broken dependency")

    monkeypatch.setattr(pipeline_module, "held_document_keys", boom)

    response = no_raise.get(f"/profiles/{profile['id']}/schema")

    assert response.status_code == 500
    assert response.json() == {"detail": "internal server error"}
    assert "RuntimeError" not in response.text
    assert "deliberately broken" not in response.text

    session = db_module.SessionLocal()
    row = session.query(models_db.IssueReport).filter_by(source="server").order_by(
        models_db.IssueReport.created_at.desc()).first()
    session.close()

    assert row is not None
    assert "RuntimeError" in row.message
    assert "deliberately broken dependency" in row.detail
    assert row.status_code == 500
    assert f"/profiles/{profile['id']}/schema" in row.route


def test_the_server_error_is_attributed_to_whoever_triggered_it(monkeypatch):
    no_raise = TestClient(api.app, raise_server_exceptions=False)
    profile = _profile(no_raise, "tester@example.com")

    monkeypatch.setattr(pipeline_module, "held_document_keys",
                        lambda p: (_ for _ in ()).throw(RuntimeError("x")))
    no_raise.get(f"/profiles/{profile['id']}/schema")

    session = db_module.SessionLocal()
    account = session.query(models_db.Account).filter_by(email="tester@example.com").one()
    row = session.query(models_db.IssueReport).filter_by(source="server").order_by(
        models_db.IssueReport.created_at.desc()).first()
    session.close()

    assert row.account_id == account.id


# --------------------------------------------------------------------------
# The admin gate
# --------------------------------------------------------------------------

def test_an_ordinary_account_is_refused_every_admin_endpoint():
    """404, not 403 - the same discipline as every ownership check in this
    system: a 403 would confirm the dashboard exists."""
    _profile(client, "tester@example.com")

    for method, path in [
        ("GET", "/admin/overview"), ("GET", "/admin/users"),
        ("GET", "/admin/feedback"), ("GET", "/admin/issues"),
        ("PUT", "/admin/feedback/nonexistent"),
        ("POST", "/admin/issues/nonexistent/resolve"),
    ]:
        response = client.request(method, path, json={"status": "seen"})
        assert response.status_code == 404, f"{method} {path}"


def test_admin_status_is_granted_from_configuration_on_the_next_request(monkeypatch):
    """Self-healing from ADMIN_EMAILS, the same pattern as GOOGLE_CLIENT_ID -
    the owner controls this by editing .env, not by a one-off database write."""
    sign_up(client, email="owner@example.com")
    assert client.get("/admin/overview").status_code == 404

    _make_admin(monkeypatch, "owner@example.com")

    assert client.get("/admin/overview").status_code == 200


def test_admin_access_is_email_specific():
    sign_up(client, email="not-the-owner@example.com")

    assert client.get("/admin/overview").status_code == 404


# --------------------------------------------------------------------------
# The admin views actually see across every account - deliberately
# --------------------------------------------------------------------------

def test_the_overview_counts_every_account_not_just_the_admins_own(monkeypatch):
    _profile(client, "person-one@example.com")
    client.cookies.clear()
    _profile(client, "person-two@example.com")
    client.cookies.clear()
    sign_up(client, email="dev@example.com")
    _make_admin(monkeypatch, "dev@example.com")

    overview = client.get("/admin/overview").json()

    assert overview["total_accounts"] >= 3
    assert overview["total_profiles"] >= 2


def test_the_user_list_shows_every_real_account(monkeypatch):
    _profile(client, "person-one@example.com")
    client.cookies.clear()
    sign_up(client, email="dev@example.com")
    _make_admin(monkeypatch, "dev@example.com")

    users = client.get("/admin/users").json()
    emails = {u["email"] for u in users}

    assert "person-one@example.com" in emails
    assert "dev@example.com" in emails


def test_the_most_recently_active_account_is_listed_first(monkeypatch):
    sign_up(client, email="early@example.com")
    client.cookies.clear()
    sign_up(client, email="dev@example.com")
    _make_admin(monkeypatch, "dev@example.com")
    client.get("/me")  # touches dev's own last_seen_at, making it the latest

    users = client.get("/admin/users").json()

    assert users[0]["email"] == "dev@example.com"


def test_an_account_never_active_sorts_after_ones_that_have_been(monkeypatch):
    """A None last_seen_at must not crash the sort or be mistaken for the
    most recent."""
    session = db_module.SessionLocal()
    session.add(models_db.Account(
        email="ghost@example.com", password_hash="x", last_seen_at=None))
    session.commit()
    session.close()

    sign_up(client, email="dev@example.com")
    _make_admin(monkeypatch, "dev@example.com")

    users = client.get("/admin/users").json()

    assert users[0]["email"] != "ghost@example.com"
    assert "ghost@example.com" in [u["email"] for u in users]


def test_admin_can_see_and_resolve_feedback_from_any_account(monkeypatch):
    _profile(client, "tester@example.com")
    client.post("/feedback", json={"category": "bug", "message": "the button does nothing"})
    client.cookies.clear()
    sign_up(client, email="dev@example.com")
    _make_admin(monkeypatch, "dev@example.com")

    listed = client.get("/admin/feedback").json()
    assert len(listed) == 1
    assert listed[0]["submitter_email"] == "tester@example.com"
    assert listed[0]["message"] == "the button does nothing"

    updated = client.put(f"/admin/feedback/{listed[0]['id']}",
                         json={"status": "resolved", "admin_note": "fixed in the next deploy"})
    assert updated.status_code == 200
    assert updated.json()["status"] == "resolved"
    assert updated.json()["resolved_at"] is not None
    assert updated.json()["admin_note"] == "fixed in the next deploy"


def test_feedback_can_be_filtered_by_status(monkeypatch):
    _profile(client, "tester@example.com")
    open_id = client.post("/feedback", json={"message": "still open"}).json()["id"]
    client.post("/feedback", json={"message": "will be resolved"})
    client.cookies.clear()
    sign_up(client, email="dev@example.com")
    _make_admin(monkeypatch, "dev@example.com")

    resolved_id = [f for f in client.get("/admin/feedback").json()
                   if f["message"] == "will be resolved"][0]["id"]
    client.put(f"/admin/feedback/{resolved_id}", json={"status": "resolved"})

    still_new = client.get("/admin/feedback", params={"status": "new"}).json()
    assert [f["id"] for f in still_new] == [open_id]


def test_an_invalid_status_is_refused(monkeypatch):
    _profile(client, "tester@example.com")
    fid = client.post("/feedback", json={"message": "hi"}).json()["id"]
    client.cookies.clear()
    sign_up(client, email="dev@example.com")
    _make_admin(monkeypatch, "dev@example.com")

    response = client.put(f"/admin/feedback/{fid}", json={"status": "urgent!!"})

    assert response.status_code == 422


def test_admin_can_see_and_resolve_issues_from_any_source(monkeypatch):
    client.post("/issues/client", json={"message": "a real client bug", "route": "#/profile"})
    client.cookies.clear()
    sign_up(client, email="dev@example.com")
    _make_admin(monkeypatch, "dev@example.com")

    open_issues = client.get("/admin/issues").json()
    assert any(i["message"] == "a real client bug" for i in open_issues)
    issue_id = [i for i in open_issues if i["message"] == "a real client bug"][0]["id"]

    resolved = client.post(f"/admin/issues/{issue_id}/resolve")
    assert resolved.status_code == 200
    assert resolved.json()["resolved"] is True

    still_open = client.get("/admin/issues").json()
    assert not any(i["id"] == issue_id for i in still_open)
    now_resolved = client.get("/admin/issues", params={"resolved": True}).json()
    assert any(i["id"] == issue_id for i in now_resolved)


def test_resolving_someone_elses_reported_issue_still_works_for_admin(monkeypatch):
    """Unlike every ownership-scoped endpoint elsewhere, this one is
    deliberately not scoped to one account - an issue belongs to the system,
    not to whoever happened to trigger it."""
    sign_up(client, email="whoever@example.com")
    client.post("/issues/client", json={"message": "signed-in report"})
    client.cookies.clear()
    sign_up(client, email="dev@example.com")
    _make_admin(monkeypatch, "dev@example.com")

    issues = client.get("/admin/issues").json()
    target = [i for i in issues if i["message"] == "signed-in report"][0]
    assert target["reporter_email"] == "whoever@example.com"

    assert client.post(f"/admin/issues/{target['id']}/resolve").status_code == 200
