"""Reading an uploaded document into suggestions for the profile's own fields.

The point is that the question follows the profile. A PRAZ registration
certificate has no study level on it; it has the supplier category codes that
decide which tenders a company may bid on at all, and the fixed CV-shaped
extraction had no way to ask for them.
"""

import httpx
from fastapi.testclient import TestClient

from conftest import sign_up
from opportunity_agent import api
from opportunity_agent.api import app
from test_documents_api import FakeMinioClient

client = TestClient(app)


def setup_function():
    client.cookies.clear()


def _tender_profile():
    return client.post(
        "/profiles", json={"profile_type": "tender", "display_name": "Tenders"}
    ).json()


def _upload(profile_id, body=b"PRAZ certificate: categories GE001, SV002", name="praz.txt"):
    return client.post(
        f"/profiles/{profile_id}/documents",
        files={"file": (name, body, "text/plain")},
    ).json()


def test_a_document_is_read_against_the_profiles_own_fields(outbox, monkeypatch):
    sign_up(client, outbox=outbox)
    store = FakeMinioClient()
    monkeypatch.setattr(api, "_minio_client", lambda: store)
    profile = _tender_profile()
    doc = _upload(profile["id"])
    seen = {}

    def capture(text, specs):
        seen["keys"] = {s.key for s in specs}
        seen["text"] = text
        return {"praz_categories": ["GE001", "SV002"]}

    monkeypatch.setattr(api, "_assist_profile_fields", capture)

    body = client.post(f"/profiles/{profile['id']}/documents/{doc['id']}/suggest").json()

    # a company is asked company questions
    assert "praz_categories" in seen["keys"]
    assert "study_level" not in seen["keys"]
    assert "GE001" in seen["text"]
    assert body["suggested"] == {"praz_categories": ["GE001", "SV002"]}


def test_reading_a_document_does_not_write_to_the_profile(outbox, monkeypatch):
    """Suggestions are offered, never applied. The profile is the owner's own
    account of themselves."""
    sign_up(client, outbox=outbox)
    store = FakeMinioClient()
    monkeypatch.setattr(api, "_minio_client", lambda: store)
    profile = _tender_profile()
    doc = _upload(profile["id"])
    monkeypatch.setattr(api, "_assist_profile_fields",
                        lambda text, specs: {"praz_categories": ["GE001"]})

    client.post(f"/profiles/{profile['id']}/documents/{doc['id']}/suggest")

    saved = client.get(f"/profiles/{profile['id']}").json()["fields"]
    assert saved.get("praz_categories") in (None, [], "")


def test_a_suggestion_over_an_existing_value_is_flagged(outbox, monkeypatch):
    sign_up(client, outbox=outbox)
    store = FakeMinioClient()
    monkeypatch.setattr(api, "_minio_client", lambda: store)
    profile = _tender_profile()
    client.put(f"/profiles/{profile['id']}", json={"fields": {"praz_categories": ["GT002"]}})
    doc = _upload(profile["id"])
    monkeypatch.setattr(api, "_assist_profile_fields",
                        lambda text, specs: {"praz_categories": ["GE001"], "country": "Zimbabwe"})

    body = client.post(f"/profiles/{profile['id']}/documents/{doc['id']}/suggest").json()

    assert body["conflicts"] == ["praz_categories"]


def test_a_file_with_no_readable_text_says_so(outbox, monkeypatch):
    """A scanned certificate is an image in a PDF wrapper. Silence would look
    like a broken feature."""
    sign_up(client, outbox=outbox)
    store = FakeMinioClient()
    monkeypatch.setattr(api, "_minio_client", lambda: store)
    profile = _tender_profile()
    doc = _upload(profile["id"], body=b"   ", name="scan.txt")

    response = client.post(f"/profiles/{profile['id']}/documents/{doc['id']}/suggest")

    assert response.status_code == 422
    assert "OCR" in response.json()["detail"]


def test_an_unreachable_model_is_a_503_not_a_silent_nothing(outbox, monkeypatch):
    sign_up(client, outbox=outbox)
    store = FakeMinioClient()
    monkeypatch.setattr(api, "_minio_client", lambda: store)
    profile = _tender_profile()
    doc = _upload(profile["id"])

    def blow_up(text, specs):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(api, "_assist_profile_fields", blow_up)

    response = client.post(f"/profiles/{profile['id']}/documents/{doc['id']}/suggest")

    assert response.status_code == 503


def test_another_account_cannot_read_your_document(outbox, monkeypatch):
    sign_up(client, outbox=outbox)
    store = FakeMinioClient()
    monkeypatch.setattr(api, "_minio_client", lambda: store)
    profile = _tender_profile()
    doc = _upload(profile["id"])
    client.cookies.clear()
    sign_up(client, email="other@example.com", outbox=outbox)

    response = client.post(f"/profiles/{profile['id']}/documents/{doc['id']}/suggest")

    assert response.status_code == 404
