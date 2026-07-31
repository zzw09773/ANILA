"""Resolve the ASR decoder URL from CSP (governance) with env fallback.

Mirrors the router-primary pattern in anila-core's router_server: read at
boot and on a TTL refresh, fall back to ``ASR_DECODE_URL`` when CSP has no
designation (404/409), and keep the last known CSP address when CSP is
unreachable — never silently switch to a different machine than the one
the operator selected. Source of truth is always visible on ``/asr/health``.

The shared decoder secret (``ASR_DECODER_TOKEN``) is intentionally NOT
fetched from CSP: primary-model endpoints never return credentials, and
the encrypted ``api_key_secret_ref`` store is for CSP-proxied gateway keys,
not for a peer shared secret between gateway and decoder.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)

# source values operators will see on /asr/health:
#   env                 — using ASR_DECODE_URL (CSP has none, or never reached)
#   csp_registry        — using the address from GET /api/models/asr-primary
#   csp_registry_stale  — CSP unreachable; still using last successful CSP URL
_state: dict[str, Any] = {
    "url": None,
    "source": "env",
    "at": 0.0,
    "last_refresh_error": None,
    "last_refresh_at": None,
}
_lock = threading.Lock()


def reset_decode_endpoint_cache() -> None:
    """Forget the resolved decoder URL. Test-only / ops escape hatch."""
    with _lock:
        _state.update(
            {
                "url": None,
                "source": "env",
                "at": 0.0,
                "last_refresh_error": None,
                "last_refresh_at": None,
            }
        )


def current_decode_url(settings: Settings) -> str:
    """URL the gateway will hit for /transcribe right now."""
    with _lock:
        return (_state["url"] or settings.ASR_DECODE_URL).rstrip("/")


def decode_url_source() -> str:
    """``env`` / ``csp_registry`` / ``csp_registry_stale``."""
    with _lock:
        return str(_state["source"])


def decode_url_refresh_meta() -> dict[str, Any]:
    with _lock:
        return {
            "last_refresh_error": _state["last_refresh_error"],
            "last_refresh_at": _state["last_refresh_at"],
        }


def _looks_like_http_url(url: str) -> bool:
    return bool(url) and url.startswith(("http://", "https://"))


async def refresh_decode_endpoint(
    settings: Settings,
    *,
    decode_client: Any | None = None,
    force: bool = False,
    http_client: httpx.AsyncClient | None = None,
) -> None:
    """Re-read CSP's asr-primary designation when the TTL expires.

    Never raises. A CSP hiccup must not take voice down — it leaves the
    previously resolved (or env) URL in place and records the error so
    ``/asr/health`` shows the degraded source. The TTL clock advances on
    failure too, so an unreachable CSP is not hammered every request.

    Invariant: a successful CSP 200 that names an address becomes the only
    address in force. We do NOT fall back to the env URL while that
    designation stands — using a different machine silently is worse than
    voice being off.
    """
    token = (settings.CSP_SERVICE_TOKEN or "").strip()
    if not token:
        # No service credential → the service-to-service endpoint is not
        # callable. Env var stays authoritative; surface that clearly.
        with _lock:
            _state["url"] = None
            _state["source"] = "env"
            _state["last_refresh_error"] = "CSP_SERVICE_TOKEN unset; using ASR_DECODE_URL"
            _state["last_refresh_at"] = datetime.now(timezone.utc).isoformat()
            _state["at"] = time.monotonic()
        _apply_url(decode_client, current_decode_url(settings))
        return

    ttl = float(settings.ASR_DECODE_URL_TTL)
    now = time.monotonic()
    with _lock:
        if (
            not force
            and _state["at"]
            and now - _state["at"] < ttl
        ):
            return
        _state["at"] = now

    csp_url: str | None = None
    error: str | None = None
    # Three outcomes:
    #   designated=True  → CSP named an address; adopt it (or reject if invalid)
    #   designated=False → CSP said none (404/409); use env
    #   designated=None  → transport/HTTP failure; keep last known
    designated: bool | None = None
    try:
        owns_client = http_client is None
        client = http_client or httpx.AsyncClient()
        try:
            response = await client.get(
                f"{settings.CSP_BASE_URL.rstrip('/')}/api/models/asr-primary",
                headers={"X-CSP-Service-Token": token},
                timeout=5.0,
            )
        finally:
            if owns_client:
                await client.aclose()

        if response.status_code == 200:
            raw = (response.json() or {}).get("endpoint_url") or ""
            raw = str(raw).strip()
            if not _looks_like_http_url(raw):
                error = f"CSP asr-primary returned unusable endpoint_url={raw!r}"
                logger.error(error)
                # Do not adopt; do not fall back if we already have a CSP URL —
                # leave previous state and surface the error.
                designated = None
            else:
                csp_url = raw.rstrip("/")
                designated = True
        elif response.status_code in (404, 409):
            designated = False
            logger.info(
                "ASR primary unavailable (HTTP %s); using ASR_DECODE_URL",
                response.status_code,
            )
        else:
            designated = None
            error = f"ASR primary lookup failed: HTTP {response.status_code}"
            logger.warning("%s; keeping previous decode URL", error)
    except Exception as exc:  # noqa: BLE001 — never break voice over this
        designated = None
        error = f"ASR primary lookup errored ({type(exc).__name__}: {exc})"
        logger.warning("%s; keeping previous decode URL", error)

    with _lock:
        _state["last_refresh_at"] = datetime.now(timezone.utc).isoformat()
        _state["last_refresh_error"] = error
        if designated is True:
            _state["url"] = csp_url
            _state["source"] = "csp_registry"
        elif designated is False:
            _state["url"] = None
            _state["source"] = "env"
        else:
            # Unreachable / bad payload: keep last known. If we never had a
            # CSP URL, source stays env; if we did, mark it stale so the
            # operator can see the refresh failed.
            if _state["url"]:
                _state["source"] = "csp_registry_stale"
            else:
                _state["source"] = "env"

    _apply_url(decode_client, current_decode_url(settings))


def _apply_url(decode_client: Any | None, url: str) -> None:
    if decode_client is None:
        return
    setter = getattr(decode_client, "set_base_url", None)
    if callable(setter):
        setter(url)
        return
    # Fake clients in tests may only expose a bare attribute.
    if hasattr(decode_client, "_base_url"):
        decode_client._base_url = url.rstrip("/")


def host_of(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).hostname or ""
