"""Probe the configured ASR decoder so /asr/health can tell the truth.

The frontend microphone is gated on HTTP 200 from ``/asr/health``. The local
``depends_on: asr-decoder`` only covered the compose-local container; once the
address can point elsewhere, health itself must ask that machine.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

logger = logging.getLogger(__name__)

# Stable reason codes surfaced on /asr/health (and in logs). Operators need
# different responses for "voice is off" vs "pointed at something broken".
REASON_OK = "ok"
REASON_DECODER_UNREACHABLE = "decoder_unreachable"
REASON_DECODER_UNAUTHORIZED = "decoder_unauthorized"
REASON_DECODER_NOT_READY = "decoder_not_ready"

_PROBE_TIMEOUT = httpx.Timeout(2.0, connect=1.0)


def strip_url_userinfo(url: str) -> str:
    """Keep host/path; drop userinfo so a pasted secret is not echoed."""
    if not url:
        return url
    parsed = urlparse(url)
    if parsed.username is None and parsed.password is None:
        return url
    hostname = parsed.hostname or ""
    if ":" in hostname:
        host = f"[{hostname}]"
    else:
        host = hostname
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=host))


async def probe_decode_target(
    base_url: str,
    token: str,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Ask whether speech can actually be transcribed at ``base_url``.

    Returns ``{"ok": bool, "reason": str, "detail": str | None}``.
    Never raises.
    """
    base = (base_url or "").rstrip("/")
    if not base:
        return {
            "ok": False,
            "reason": REASON_DECODER_UNREACHABLE,
            "detail": "decode URL is empty",
        }

    owns = http_client is None
    client = http_client or httpx.AsyncClient()
    try:
        try:
            health = await client.get(f"{base}/health", timeout=_PROBE_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 — probe must never raise
            detail = f"{type(exc).__name__}: {exc}"
            logger.info("decoder probe connect failed: %s", detail)
            return {
                "ok": False,
                "reason": REASON_DECODER_UNREACHABLE,
                "detail": detail,
            }

        if health.status_code == 503:
            return {
                "ok": False,
                "reason": REASON_DECODER_NOT_READY,
                "detail": "decoder /health returned 503 (model loading or not ready)",
            }
        if health.status_code != 200:
            return {
                "ok": False,
                "reason": REASON_DECODER_UNREACHABLE,
                "detail": f"decoder /health returned HTTP {health.status_code}",
            }

        # /health is unauthenticated. A wrong shared secret still yields a
        # green health body and 401 on every utterance — catch that here.
        try:
            auth = await client.post(
                f"{base}/transcribe",
                params={"kind": "final", "beam": 1, "prompt": "", "language": "zh"},
                content=b"\x00\x00",
                headers={
                    "Content-Type": "application/octet-stream",
                    "X-Token": token or "",
                },
                timeout=_PROBE_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001
            detail = f"{type(exc).__name__}: {exc}"
            return {
                "ok": False,
                "reason": REASON_DECODER_UNREACHABLE,
                "detail": f"decoder /transcribe probe failed: {detail}",
            }

        if auth.status_code == 401:
            return {
                "ok": False,
                "reason": REASON_DECODER_UNAUTHORIZED,
                "detail": (
                    "decoder rejected ASR_DECODER_TOKEN (HTTP 401); "
                    "the new machine must be deployed with the same shared token"
                ),
            }
        # 200 = decoded; 400 = bad/short audio after auth; 503 = loading.
        # Any of those means the token was accepted (401 is the auth miss).
        if auth.status_code == 503:
            return {
                "ok": False,
                "reason": REASON_DECODER_NOT_READY,
                "detail": "decoder /transcribe returned 503 (model not ready)",
            }
        if auth.status_code in (200, 400, 422):
            return {"ok": True, "reason": REASON_OK, "detail": None}
        return {
            "ok": False,
            "reason": REASON_DECODER_UNREACHABLE,
            "detail": f"decoder /transcribe probe returned HTTP {auth.status_code}",
        }
    finally:
        if owns:
            await client.aclose()
