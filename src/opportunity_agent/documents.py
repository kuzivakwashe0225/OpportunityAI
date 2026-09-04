"""MinIO-backed document storage for account profiles.

See SOLUTION_DEFINITION.md §14.2. The client is injectable (same pattern as
search.py's Tavily client): every function here takes a client object rather
than constructing one internally, so tests exercise the real key-construction
and call logic against a lightweight in-memory fake, never a real MinIO
server. make_client() constructs the real one for actual use.
"""

from __future__ import annotations

from datetime import timedelta
from io import BytesIO
from typing import Protocol

DEFAULT_BUCKET = "opportunity-agent-documents"


class ObjectStoreClient(Protocol):
    """The subset of minio.Minio's interface these functions rely on."""

    def bucket_exists(self, bucket_name: str) -> bool: ...
    def make_bucket(self, bucket_name: str) -> None: ...
    def put_object(self, bucket_name: str, object_name: str, data, length: int, content_type: str): ...
    def get_object(self, bucket_name: str, object_name: str): ...
    def presigned_get_object(self, bucket_name: str, object_name: str, expires: timedelta): ...
    def remove_object(self, bucket_name: str, object_name: str) -> None: ...


def make_client(*, endpoint: str, access_key: str, secret_key: str, secure: bool = False):
    from minio import Minio

    return Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=secure)


def ensure_bucket(client: ObjectStoreClient, bucket: str = DEFAULT_BUCKET) -> None:
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)


def object_key(account_id: str, profile_id: str, document_id: str, filename: str) -> str:
    safe_name = filename.replace("/", "_").replace("\\", "_")
    return f"{account_id}/{profile_id}/{document_id}/{safe_name}"


def upload_document(
    client: ObjectStoreClient,
    *,
    account_id: str,
    profile_id: str,
    document_id: str,
    filename: str,
    content: bytes,
    content_type: str,
    bucket: str = DEFAULT_BUCKET,
) -> str:
    key = object_key(account_id, profile_id, document_id, filename)
    client.put_object(bucket, key, BytesIO(content), length=len(content), content_type=content_type)
    return key


def download_document(client: ObjectStoreClient, key: str, *, bucket: str = DEFAULT_BUCKET) -> bytes:
    response = client.get_object(bucket, key)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


def presigned_url(
    client: ObjectStoreClient,
    key: str,
    *,
    bucket: str = DEFAULT_BUCKET,
    expires: timedelta = timedelta(minutes=15),
) -> str:
    return client.presigned_get_object(bucket, key, expires=expires)


def delete_document(client: ObjectStoreClient, key: str, *, bucket: str = DEFAULT_BUCKET) -> None:
    client.remove_object(bucket, key)
