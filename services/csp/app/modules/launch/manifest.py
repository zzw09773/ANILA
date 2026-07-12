# -*- coding: utf-8 -*-
"""Service manifest fetch — ``GET {origin}/.well-known/anila-service.json``.

doc 07 §4. Fetched THROUGH the central outbound SSRF guard
(``anila_security.validate_outbound_url``) — the same call-time guard the
CSP proxy uses — so registering a service can't be turned into an SSRF probe
of the internal network.
"""

from __future__ import annotations

from urllib.parse import urlparse, urlunparse

import httpx
from anila_security import UnsafeEndpointError, validate_outbound_url

_WELL_KNOWN_PATH = "/.well-known/anila-service.json"
_TIMEOUT_SECONDS = 5.0
_MAX_BYTES = 64 * 1024


class ManifestFetchError(RuntimeError):
    """Manifest could not be fetched or parsed (SSRF-rejected, network, or
    malformed JSON). Carries a user-safe message."""


def manifest_url_for(entry_url: str) -> str:
    """Derive the well-known manifest URL from a service entry/base URL."""
    parts = urlparse(entry_url)
    if not parts.scheme or not parts.netloc:
        raise ManifestFetchError(f"無效的服務 URL:{entry_url!r}")
    return urlunparse((parts.scheme, parts.netloc, _WELL_KNOWN_PATH, "", "", ""))


def fetch_service_manifest(entry_url: str) -> dict:
    """Fetch + parse the service manifest, guarding the outbound URL first."""
    url = manifest_url_for(entry_url)
    try:
        validate_outbound_url(url)
    except UnsafeEndpointError as exc:
        raise ManifestFetchError(f"manifest URL 未通過出向安全驗證:{exc}") from exc
    try:
        with httpx.Client(timeout=_TIMEOUT_SECONDS, follow_redirects=False) as client:
            resp = client.get(url)
    except httpx.HTTPError as exc:
        raise ManifestFetchError(f"無法連線服務 manifest:{exc}") from exc
    if resp.status_code != 200:
        raise ManifestFetchError(
            f"服務 manifest 回應 {resp.status_code}(預期 200)"
        )
    if len(resp.content) > _MAX_BYTES:
        raise ManifestFetchError("服務 manifest 過大")
    try:
        return resp.json()
    except ValueError as exc:
        raise ManifestFetchError(f"服務 manifest 非合法 JSON:{exc}") from exc
