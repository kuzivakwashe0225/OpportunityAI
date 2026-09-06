from fastapi.testclient import TestClient

from conftest import sign_up
from opportunity_agent import api
from opportunity_agent.api import app


client = TestClient(app)


class FakeMinioClient:
    def __init__(self):
        self.buckets = {api.DOCUMENTS_BUCKET}
        self.objects = {}

    def bucket_exists(self, bucket_name):
        return bucket_name in self.buckets

    def make_bucket(self, bucket_name, location=None, object_lock=False):
        self.buckets.add(bucket_name)

    def put_object(self, bucket_name, object_name, data, length, content_type="application/octet-stream", **kw):
        self.objects[(bucket_name, object_name)] = data.read()

    def get_object(self, bucket_name, object_name, **kw):
        data = self.objects[(bucket_name, object_name)]

        class _Resp:
            def read(self_inner):
                return data

            def close(self_inner):
                pass

            def release_conn(self_inner):
                pass

        return _Resp()

    def presigned_get_object(self, bucket_name, object_name, expires=None, **kw):
        return f"https://minio.local/{bucket_name}/{object_name}?presigned=1"

    def remove_object(self, bucket_name, object_name, version_id=None):
        self.objects.pop((bucket_name, object_name), None)


def setup_function(monkeypatch=None):
    client.cookies.clear()
    sign_up(client)


def _make_profile():
    return client.post("/profiles", json={"profile_type": "job", "display_name": "Jobs"}).json()


def test_upload_document_requires_authentication(monkeypatch):
    fake_client = FakeMinioClient()
    monkeypatch.setattr(api, "_minio_client", lambda: fake_client)
    client.cookies.clear()

    response = client.post(
        "/profiles/whatever/documents",
        files={"file": ("cv.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 401


def test_upload_document_to_own_profile_succeeds(monkeypatch):
    fake_client = FakeMinioClient()
    monkeypatch.setattr(api, "_minio_client", lambda: fake_client)
    profile = _make_profile()

    response = client.post(
        f"/profiles/{profile['id']}/documents",
        files={"file": ("cv.txt", b"Software Engineer, Acme Corp", "text/plain")},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["original_filename"] == "cv.txt"
    assert body["extraction_status"] == "pending"


def test_upload_document_to_someone_elses_profile_is_404(monkeypatch):
    fake_client = FakeMinioClient()
    monkeypatch.setattr(api, "_minio_client", lambda: fake_client)

    response = client.post(
        "/profiles/does-not-exist/documents",
        files={"file": ("cv.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 404


def test_list_documents_for_a_profile(monkeypatch):
    fake_client = FakeMinioClient()
    monkeypatch.setattr(api, "_minio_client", lambda: fake_client)
    profile = _make_profile()
    client.post(f"/profiles/{profile['id']}/documents", files={"file": ("cv.txt", b"data", "text/plain")})

    response = client.get(f"/profiles/{profile['id']}/documents")

    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["original_filename"] == "cv.txt"


def test_delete_document_removes_it_from_the_list(monkeypatch):
    fake_client = FakeMinioClient()
    monkeypatch.setattr(api, "_minio_client", lambda: fake_client)
    profile = _make_profile()
    doc = client.post(
        f"/profiles/{profile['id']}/documents", files={"file": ("cv.txt", b"data", "text/plain")}
    ).json()

    delete_response = client.delete(f"/profiles/{profile['id']}/documents/{doc['id']}")

    assert delete_response.status_code == 204
    assert client.get(f"/profiles/{profile['id']}/documents").json() == []


def test_extract_endpoint_merges_facts_into_profile_without_overwriting_existing_values(monkeypatch):
    from opportunity_agent.extraction_llm import ExtractedFacts

    fake_client = FakeMinioClient()
    monkeypatch.setattr(api, "_minio_client", lambda: fake_client)
    monkeypatch.setattr(
        api,
        "_extract_facts_from_document",
        lambda content, content_type: ExtractedFacts(
            work_history=["Software Engineer, Acme Corp (2021-2024)"],
            certificates=["AWS Certified Solutions Architect (2023)"],
            study_level="masters",
            field="Computer Science",
        ),
    )

    profile = _make_profile()
    client.put(f"/profiles/{profile['id']}", json={"fields": {"study_level": "doctoral"}})
    doc = client.post(
        f"/profiles/{profile['id']}/documents", files={"file": ("cv.txt", b"cv text", "text/plain")}
    ).json()

    response = client.post(f"/profiles/{profile['id']}/documents/{doc['id']}/extract")

    assert response.status_code == 200
    updated_profile = client.get(f"/profiles/{profile['id']}").json()
    assert updated_profile["fields"]["work_history"] == ["Software Engineer, Acme Corp (2021-2024)"]
    assert updated_profile["fields"]["certificates"] == ["AWS Certified Solutions Architect (2023)"]
    # study_level was already set by the owner - extraction must not clobber it
    assert updated_profile["fields"]["study_level"] == "doctoral"
    # field was empty - extraction is allowed to fill it in
    assert updated_profile["fields"]["field"] == "Computer Science"

    document_after = client.get(f"/profiles/{profile['id']}/documents").json()[0]
    assert document_after["extraction_status"] == "extracted"


def test_extract_endpoint_appends_new_work_history_without_duplicating(monkeypatch):
    from opportunity_agent.extraction_llm import ExtractedFacts

    fake_client = FakeMinioClient()
    monkeypatch.setattr(api, "_minio_client", lambda: fake_client)
    monkeypatch.setattr(
        api,
        "_extract_facts_from_document",
        lambda content, content_type: ExtractedFacts(work_history=["Role A", "Role B"]),
    )

    profile = _make_profile()
    client.put(f"/profiles/{profile['id']}", json={"fields": {"work_history": ["Role A"]}})
    doc = client.post(
        f"/profiles/{profile['id']}/documents", files={"file": ("cv.txt", b"cv text", "text/plain")}
    ).json()

    client.post(f"/profiles/{profile['id']}/documents/{doc['id']}/extract")

    updated = client.get(f"/profiles/{profile['id']}").json()
    assert updated["fields"]["work_history"] == ["Role A", "Role B"]
