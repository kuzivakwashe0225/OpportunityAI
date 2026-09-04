from datetime import timedelta

import pytest

from opportunity_agent.documents import (
    delete_document,
    download_document,
    ensure_bucket,
    object_key,
    presigned_url,
    upload_document,
)


class _FakeHTTPResponse:
    def __init__(self, data: bytes):
        self._data = data
        self.closed = False
        self.released = False

    def read(self):
        return self._data

    def close(self):
        self.closed = True

    def release_conn(self):
        self.released = True


class FakeMinioClient:
    """Stands in for minio.Minio - same call shapes, in-memory, no network."""

    def __init__(self, existing_buckets=None):
        self.buckets = set(existing_buckets or [])
        self.objects = {}  # (bucket, key) -> (data, content_type)
        self.presign_calls = []
        self.last_response = None

    def bucket_exists(self, bucket_name):
        return bucket_name in self.buckets

    def make_bucket(self, bucket_name, location=None, object_lock=False):
        self.buckets.add(bucket_name)

    def put_object(self, bucket_name, object_name, data, length, content_type="application/octet-stream", **kwargs):
        self.objects[(bucket_name, object_name)] = (data.read(), content_type)

    def get_object(self, bucket_name, object_name, **kwargs):
        data, _content_type = self.objects[(bucket_name, object_name)]
        self.last_response = _FakeHTTPResponse(data)
        return self.last_response

    def presigned_get_object(self, bucket_name, object_name, expires=timedelta(days=7), **kwargs):
        self.presign_calls.append((bucket_name, object_name, expires))
        return f"https://minio.local/{bucket_name}/{object_name}?presigned=1"

    def remove_object(self, bucket_name, object_name, version_id=None):
        self.objects.pop((bucket_name, object_name), None)


def test_object_key_namespaces_by_account_profile_and_document():
    key = object_key("acct-1", "profile-2", "doc-3", "cv.pdf")

    assert key == "acct-1/profile-2/doc-3/cv.pdf"


def test_object_key_sanitizes_slashes_in_the_filename():
    key = object_key("acct-1", "profile-2", "doc-3", "not/a/path.pdf")

    assert "/" not in key.rsplit("/", 1)[-1]
    assert key == "acct-1/profile-2/doc-3/not_a_path.pdf"


def test_ensure_bucket_creates_it_if_missing():
    client = FakeMinioClient()

    ensure_bucket(client, "test-bucket")

    assert client.bucket_exists("test-bucket")


def test_ensure_bucket_is_a_no_op_if_it_already_exists():
    client = FakeMinioClient(existing_buckets={"test-bucket"})

    ensure_bucket(client, "test-bucket")  # must not raise

    assert client.bucket_exists("test-bucket")


def test_upload_document_stores_the_content_under_the_right_key():
    client = FakeMinioClient(existing_buckets={"docs"})

    key = upload_document(
        client,
        account_id="acct-1", profile_id="profile-1", document_id="doc-1",
        filename="transcript.pdf", content=b"pdf-bytes-here",
        content_type="application/pdf", bucket="docs",
    )

    assert key == "acct-1/profile-1/doc-1/transcript.pdf"
    stored_data, stored_type = client.objects[("docs", key)]
    assert stored_data == b"pdf-bytes-here"
    assert stored_type == "application/pdf"


def test_presigned_url_delegates_with_the_given_bucket_and_expiry():
    client = FakeMinioClient()

    url = presigned_url(client, "acct-1/profile-1/doc-1/cv.pdf", bucket="docs", expires=timedelta(minutes=15))

    assert url == "https://minio.local/docs/acct-1/profile-1/doc-1/cv.pdf?presigned=1"
    assert client.presign_calls == [("docs", "acct-1/profile-1/doc-1/cv.pdf", timedelta(minutes=15))]


def test_download_document_returns_the_stored_bytes():
    client = FakeMinioClient(existing_buckets={"docs"})
    key = upload_document(
        client, account_id="a", profile_id="p", document_id="d",
        filename="cv.pdf", content=b"the actual document bytes", content_type="application/pdf", bucket="docs",
    )

    content = download_document(client, key, bucket="docs")

    assert content == b"the actual document bytes"


def test_download_document_closes_and_releases_the_response():
    client = FakeMinioClient(existing_buckets={"docs"})
    key = upload_document(
        client, account_id="a", profile_id="p", document_id="d",
        filename="cv.pdf", content=b"data", content_type="application/pdf", bucket="docs",
    )

    download_document(client, key, bucket="docs")

    assert client.last_response.closed
    assert client.last_response.released


def test_delete_document_removes_the_object():
    client = FakeMinioClient(existing_buckets={"docs"})
    key = upload_document(
        client, account_id="a", profile_id="p", document_id="d",
        filename="x.pdf", content=b"data", content_type="application/pdf", bucket="docs",
    )
    assert ("docs", key) in client.objects

    delete_document(client, key, bucket="docs")

    assert ("docs", key) not in client.objects
