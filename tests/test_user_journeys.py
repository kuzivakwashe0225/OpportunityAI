"""The whole way through, as a stranger would walk it.

Every other test file checks a part. This one checks that the parts join up,
because that is where this system has actually failed its owner before: each
piece worked, and the journey still delivered a one-paragraph cover letter for
a nine-section research call.

Two personas, because the product is not one product. A student applying for a
scholarship and a company bidding for a tender are asked different questions,
need different papers, and get a different document at the end. A journey test
that only walks the student would have missed the whole company half.

The rule for every assertion here is "would the person notice?" - not
"did the function return". If a step leaves someone stuck, staring at an empty
screen with no idea what to do next, that is a failure even when every
endpoint returned 200.
"""

import pytest
from fastapi.testclient import TestClient

from conftest import sign_up
from opportunity_agent import api, models_db
from opportunity_agent import db as db_module
from opportunity_agent.connector import PublicPage
from opportunity_agent.search import SearchResult


class _FakeStorage:
    def __init__(self):
        self.objects = {}

    def bucket_exists(self, bucket_name):
        return True

    def make_bucket(self, bucket_name, location=None, object_lock=False):
        pass

    def put_object(self, bucket_name, object_name, data, length, **kwargs):
        self.objects[(bucket_name, object_name)] = data.read()

    def get_object(self, bucket_name, object_name, **kwargs):
        blob = self.objects[(bucket_name, object_name)]

        class _Resp:
            def read(self_inner):
                return blob

            def close(self_inner):
                pass

            def release_conn(self_inner):
                pass

        return _Resp()

    def remove_object(self, bucket_name, object_name, version_id=None):
        self.objects.pop((bucket_name, object_name), None)


@pytest.fixture(autouse=True)
def storage(monkeypatch):
    monkeypatch.setattr(api, "_minio_client", lambda: _FakeStorage())


SCHOLARSHIP_CALL = (
    "Commonwealth Masters Scholarship. Open to citizens of Zimbabwe and other "
    "Commonwealth countries. Applicants must be enrolled in or applying to a "
    "master's programme in computer science. Required documents: CV, academic "
    "transcript, national ID. Applications close 3 October 2026."
)


def _page(url, content=SCHOLARSHIP_CALL):
    return PublicPage(url=url, content=content, retrieved_at="2026-01-01T00:00:00Z",
                      sha256="abc", content_type="text/html")


def _stub_search(monkeypatch, results, page_content=SCHOLARSHIP_CALL):
    monkeypatch.setattr(api, "_pipeline_search", lambda p, api_key, **kw: results)
    monkeypatch.setattr(api, "_pipeline_fetch", lambda url, **kw: _page(url, page_content))
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")


# --------------------------------------------------------------------------
# A student, from the sign-up form to a finished application
# --------------------------------------------------------------------------

def test_a_student_gets_from_signing_up_to_a_finished_application(monkeypatch):
    """The journey the product exists for, walked end to end without a
    shortcut. Every step is something the person does on screen."""
    student = TestClient(api.app)

    # 1. Sign up. The password arrives by email - they never chose one.
    password = sign_up(student, email="tendai@example.com")
    assert student.get("/me").status_code == 200

    # 2. They are told to replace the emailed password, and not nagged
    #    afterwards.
    assert student.get("/me").json()["must_change_password"] is True
    student.post("/change-password", json={"current_password": password,
                                           "new_password": "a-password-i-chose"})
    assert student.get("/me").json()["must_change_password"] is False

    # 3. Pick what they are looking for. The choices come from the server.
    types = {t["key"] for t in student.get("/profile-types").json()}
    assert "scholarship" in types
    profile = student.post("/profiles", json={
        "profile_type": "scholarship", "display_name": "My scholarships"
    }).json()

    # 4. The questions asked are the ones a person gets, not a company's.
    schema = student.get(f"/profiles/{profile['id']}/schema").json()
    field_keys = {f["key"] for f in schema["fields"]}
    assert "study_level" in field_keys
    assert "vat_number" not in field_keys
    document_keys = {d["key"] for d in schema["documents"]}
    assert "transcript" in document_keys
    assert "certificate_of_incorporation" not in document_keys

    # 5. Answer them.
    student.put(f"/profiles/{profile['id']}", json={"fields": {
        "name": "Tendai Moyo", "country": "Zimbabwe", "study_level": "masters",
        "field": "Computer Science",
    }})

    # 6. Upload the papers the schema asked for.
    for doc_type, filename in (("cv", "cv.txt"), ("transcript", "transcript.txt"),
                               ("id", "id.txt")):
        created = student.post(
            f"/profiles/{profile['id']}/documents",
            files={"file": (filename, b"Tendai Moyo, BSc Computer Science", "text/plain")},
            data={"doc_type": doc_type},
        )
        assert created.status_code == 201, doc_type
    assert len(student.get(f"/profiles/{profile['id']}/documents").json()) == 3

    # 7. Let the agent search.
    _stub_search(monkeypatch, [SearchResult(
        title="Commonwealth Masters Scholarship",
        url="https://example.org/commonwealth", snippet="Masters funding",
        source="example.org")])
    run = student.post(f"/profiles/{profile['id']}/run").json()
    assert run["found"] >= 1 and run["added"] >= 1

    # 8. It is in the list, and it says why it matched.
    opportunities = student.get(f"/profiles/{profile['id']}/opportunities").json()
    assert len(opportunities) == 1
    found = opportunities[0]
    assert found["match_status"] == "eligible"
    assert found["match_reasons"].get("matched")

    # 9. Click it. This is the screen that did not exist until recently.
    detail = student.get(
        f"/profiles/{profile['id']}/opportunities/{found['id']}/requirements"
    ).json()
    assert "3 October 2026" in detail["call_text"]
    assert detail["documents_total"] > 0
    assert detail["documents_ready"] == detail["documents_total"], (
        "they uploaded everything the schema asked for and are still shown as short"
    )
    assert {f["key"] for f in detail["prefill"]} >= {"name", "email", "study_level"}

    # 10. Edit the draft and keep the edit.
    student.put(
        f"/profiles/{profile['id']}/opportunities/{found['id']}/package",
        json={"sections": [{"title": "Personal statement",
                            "body": "I am applying because..."}]},
    )
    after = student.get(
        f"/profiles/{profile['id']}/opportunities/{found['id']}/requirements"
    ).json()
    assert after["draft"]["sections"][0]["body"] == "I am applying because..."

    # 11. Download it as a real file.
    export = student.get(f"/profiles/{profile['id']}/opportunities/{found['id']}/document")
    assert export.status_code == 200
    assert export.content[:2] == b"PK"  # a .docx is a zip
    assert "attachment" in export.headers["content-disposition"]

    # 12. Approve, then separately say it was sent. Two steps on purpose:
    #     nothing here submits on anyone's behalf.
    student.post(f"/profiles/{profile['id']}/opportunities/{found['id']}/approve")
    assert student.get(
        f"/profiles/{profile['id']}/opportunities/{found['id']}"
    ).json()["stage"] == "approved"
    student.post(f"/profiles/{profile['id']}/opportunities/{found['id']}/submitted")
    assert student.get(
        f"/profiles/{profile['id']}/opportunities/{found['id']}"
    ).json()["stage"] == "submitted"

    # 13. The history reads as an account of what happened.
    kinds = [e["kind"] for e in student.get(
        f"/profiles/{profile['id']}/opportunities/{found['id']}/history").json()]
    assert "approved" in kinds and "submitted" in kinds


# --------------------------------------------------------------------------
# A company, which is a different product wearing the same login
# --------------------------------------------------------------------------

def test_a_company_is_asked_for_company_things_not_student_things():
    company = TestClient(api.app)
    sign_up(company, email="meshcloud@example.com")
    profile = company.post("/profiles", json={
        "profile_type": "tender", "display_name": "Tenders"}).json()

    schema = company.get(f"/profiles/{profile['id']}/schema").json()
    field_keys = {f["key"] for f in schema["fields"]}
    document_keys = {d["key"] for d in schema["documents"]}

    assert {"registration_number", "tax_number", "praz_categories"} <= field_keys
    assert "study_level" not in field_keys
    assert {"tax_clearance", "cr14", "certificate_of_incorporation"} <= document_keys
    assert "transcript" not in document_keys


def test_a_company_is_told_exactly_which_papers_a_tender_still_needs():
    """The compliance answer, from the company's side: not "incomplete" but
    which specific certificate, named the way PRAZ names it."""
    company = TestClient(api.app)
    sign_up(company, email="meshcloud@example.com")
    profile = company.post("/profiles", json={
        "profile_type": "tender", "display_name": "Tenders"}).json()
    company.put(f"/profiles/{profile['id']}", json={"fields": {"name": "Meshcloud"}})
    company.post(
        f"/profiles/{profile['id']}/documents",
        files={"file": ("tax.txt", b"ITF263", "text/plain")},
        data={"doc_type": "tax_clearance"},
    )

    session = db_module.SessionLocal()
    row = models_db.StoredOpportunity(
        profile_id=profile["id"], canonical_url="https://egp.praz.org.zw/t/1",
        payload={"title": "Supply of ICT equipment",
                 "required_documents": ["tax_clearance", "cr14"],
                 "evidence": ["Bidders must submit a valid tax clearance and CR14."]},
        match_status="eligible", match_score=10, match_reasons={},
    )
    session.add(row)
    session.commit()
    opportunity_id = row.id
    session.close()

    detail = company.get(
        f"/profiles/{profile['id']}/opportunities/{opportunity_id}/requirements"
    ).json()
    by_key = {d["key"]: d for d in detail["required_documents"]}

    assert by_key["tax_clearance"]["held"] is True
    assert by_key["tax_clearance"]["filename"] == "tax.txt"
    assert by_key["cr14"]["held"] is False
    assert by_key["cr14"]["label"] == "CR14 / return of directors"
    assert detail["documents_ready"] < detail["documents_total"]


# --------------------------------------------------------------------------
# Several people using it at once
# --------------------------------------------------------------------------

def test_two_strangers_run_the_agent_at_the_same_time_and_see_only_their_own(monkeypatch):
    """What "usable by different people" has to mean before this goes public."""
    first = TestClient(api.app)
    second = TestClient(api.app)
    sign_up(first, email="first@example.com")
    sign_up(second, email="second@example.com")

    def profile_for(caller, name):
        profile = caller.post("/profiles", json={
            "profile_type": "scholarship", "display_name": name}).json()
        caller.put(f"/profiles/{profile['id']}", json={"fields": {
            "name": name, "country": "Zimbabwe", "study_level": "masters",
            "field": "Computer Science"}})
        return profile

    first_profile = profile_for(first, "Tendai")
    second_profile = profile_for(second, "Rudo")

    _stub_search(monkeypatch, [SearchResult(
        title="Commonwealth Masters Scholarship", url="https://example.org/commonwealth",
        snippet="", source="example.org")])
    first.post(f"/profiles/{first_profile['id']}/run")
    second.post(f"/profiles/{second_profile['id']}/run")

    first_list = first.get(f"/profiles/{first_profile['id']}/opportunities").json()
    second_list = second.get(f"/profiles/{second_profile['id']}/opportunities").json()

    # Same call, found for both - but stored per profile, with its own id,
    # its own stage and its own draft. One approving it must not move the
    # other's copy.
    assert len(first_list) == len(second_list) == 1
    assert first_list[0]["id"] != second_list[0]["id"]

    first.post(f"/profiles/{first_profile['id']}/opportunities/{first_list[0]['id']}/approve")

    assert second.get(
        f"/profiles/{second_profile['id']}/opportunities/{second_list[0]['id']}"
    ).json()["stage"] != "approved"


# --------------------------------------------------------------------------
# The parts of the journey that go wrong
# --------------------------------------------------------------------------

def test_a_brand_new_account_is_not_shown_a_broken_dashboard():
    """The first screen a stranger sees, with nothing in the system at all.
    It has to answer rather than 500."""
    newcomer = TestClient(api.app)
    sign_up(newcomer, email="new@example.com")

    assert newcomer.get("/profiles").json() == []
    assert newcomer.get("/notifications").json() == []

    profile = newcomer.post("/profiles", json={
        "profile_type": "grant", "display_name": "Grants"}).json()
    summary = newcomer.get(f"/profiles/{profile['id']}/summary").json()

    assert summary["total"] == 0
    assert summary["last_run"] is None
    assert newcomer.get(f"/profiles/{profile['id']}/opportunities").json() == []


def test_running_the_agent_before_answering_anything_says_what_is_missing():
    """Rather than searching for nothing and reporting zero results, which
    would read as "there are no opportunities for you"."""
    newcomer = TestClient(api.app)
    sign_up(newcomer, email="empty@example.com")
    profile = newcomer.post("/profiles", json={
        "profile_type": "scholarship", "display_name": "S"}).json()

    response = newcomer.post(f"/profiles/{profile['id']}/run")

    assert response.status_code == 409
    assert "name" in response.json()["detail"]


def test_exporting_before_there_is_anything_to_export_explains_itself():
    newcomer = TestClient(api.app)
    sign_up(newcomer, email="early@example.com")
    profile = newcomer.post("/profiles", json={
        "profile_type": "grant", "display_name": "G"}).json()
    session = db_module.SessionLocal()
    row = models_db.StoredOpportunity(
        profile_id=profile["id"], canonical_url="https://example.org/x",
        payload={"title": "A call"}, match_status="eligible",
        match_score=1, match_reasons={},
    )
    session.add(row)
    session.commit()
    opportunity_id = row.id
    session.close()

    response = newcomer.get(
        f"/profiles/{profile['id']}/opportunities/{opportunity_id}/document")

    assert response.status_code == 409
    assert "draft" in response.json()["detail"].lower()


def test_a_removed_opportunity_can_be_got_back():
    """Because the agent fills this list unattended, and tidying it is not
    meant to be a decision anyone has to be careful about."""
    owner = TestClient(api.app)
    sign_up(owner, email="tidy@example.com")
    profile = owner.post("/profiles", json={
        "profile_type": "grant", "display_name": "G"}).json()
    session = db_module.SessionLocal()
    row = models_db.StoredOpportunity(
        profile_id=profile["id"], canonical_url="https://example.org/x",
        payload={"title": "A call worth keeping"}, match_status="eligible",
        match_score=1, match_reasons={},
    )
    session.add(row)
    session.commit()
    opportunity_id = row.id
    session.close()

    owner.post(f"/profiles/{profile['id']}/opportunities/{opportunity_id}/trash")
    assert owner.get(f"/profiles/{profile['id']}/opportunities").json() == []

    owner.post(f"/profiles/{profile['id']}/opportunities/{opportunity_id}/restore")
    assert len(owner.get(f"/profiles/{profile['id']}/opportunities").json()) == 1


def test_someone_locked_out_can_get_back_in_and_the_old_password_dies():
    locked = TestClient(api.app)
    original = sign_up(locked, email="locked@example.com")
    locked.post("/logout")

    from opportunity_agent import api as api_module
    sent = []
    api_module._send_mail, saved = (lambda **kw: sent.append(kw)), api_module._send_mail
    try:
        assert locked.post("/forgot-password",
                           json={"email": "locked@example.com"}).status_code == 200
    finally:
        api_module._send_mail = saved

    replacement = [line.split("Password:", 1)[1].strip()
                   for line in sent[-1]["body"].splitlines()
                   if line.startswith("Password:")][0]

    assert locked.post("/login", json={"email": "locked@example.com",
                                       "password": original}).status_code == 401
    assert locked.post("/login", json={"email": "locked@example.com",
                                       "password": replacement}).status_code == 200


def test_deleting_a_profile_takes_its_documents_and_opportunities_with_it():
    """Someone leaving has to be able to actually leave."""
    leaver = TestClient(api.app)
    sign_up(leaver, email="leaver@example.com")
    profile = leaver.post("/profiles", json={
        "profile_type": "scholarship", "display_name": "S"}).json()
    leaver.post(
        f"/profiles/{profile['id']}/documents",
        files={"file": ("cv.txt", b"my cv", "text/plain")},
        data={"doc_type": "cv"},
    )
    session = db_module.SessionLocal()
    session.add(models_db.StoredOpportunity(
        profile_id=profile["id"], canonical_url="https://example.org/x",
        payload={"title": "A call"}, match_status="eligible",
        match_score=1, match_reasons={}))
    session.commit()
    session.close()

    assert leaver.delete(f"/profiles/{profile['id']}").status_code == 204

    session = db_module.SessionLocal()
    remaining_documents = session.query(models_db.Document).filter_by(
        profile_id=profile["id"]).count()
    remaining_opportunities = session.query(models_db.StoredOpportunity).filter_by(
        profile_id=profile["id"]).count()
    session.close()

    assert remaining_documents == 0
    assert remaining_opportunities == 0
    assert leaver.get(f"/profiles/{profile['id']}").status_code == 404
