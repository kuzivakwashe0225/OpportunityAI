from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from ipaddress import ip_address
import socket
from urllib.parse import urlsplit

import httpx

from .discovery import canonicalize_url


@dataclass(frozen=True)
class PublicPage:
    url: str
    content: str
    retrieved_at: str
    sha256: str
    content_type: str = "text/html"


def fetch_public_page(
    url: str,
    *,
    client: httpx.Client | None = None,
    max_bytes: int = 2_000_000,
) -> PublicPage:
    canonical_url = canonicalize_url(url)
    parsed = urlsplit(canonical_url)
    if parsed.scheme != "https":
        raise ValueError("public source URL must use HTTPS")
    if parsed.username or parsed.password or parsed.port not in {None, 443}:
        raise ValueError("public source URL contains an unsafe authority")
    if parsed.hostname in {"localhost", "localhost.localdomain"}:
        raise ValueError("public source URL targets a private host")
    try:
        if parsed.hostname and ip_address(parsed.hostname).is_private:
            raise ValueError("public source URL targets a private host")
    except ValueError as error:
        if "private host" in str(error):
            raise
    if parsed.hostname:
        try:
            addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        except socket.gaierror as error:
            raise ValueError("public source hostname could not be resolved") from error
        if any(ip_address(address[4][0]).is_private for address in addresses):
            raise ValueError("public source URL resolves to a private host")
    owns_client = client is None
    http_client = client or httpx.Client(timeout=20.0, follow_redirects=False)
    if http_client.follow_redirects:
        raise ValueError("public source client must not follow redirects")
    try:
        with http_client.stream("GET", canonical_url) as response:
            response.raise_for_status()
            if response.is_redirect:
                raise ValueError("public source redirects are not followed")
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("public source response exceeds size limit")
                chunks.append(chunk)
            body = b"".join(chunks)
            encoding = response.encoding or "utf-8"
            content_type = response.headers.get("content-type", "text/html").split(";", 1)[0].strip().lower()
    finally:
        if owns_client:
            http_client.close()
    return PublicPage(
        url=canonical_url,
        content=body.decode(encoding, errors="replace"),
        retrieved_at=datetime.now(timezone.utc).isoformat(),
        sha256=sha256(body).hexdigest(),
        content_type=content_type,
    )