"""Probe the configured ASR decoder so /asr/health can tell the truth.

The frontend microphone is gated on HTTP 200 from ``/asr/health``. The local
``depends_on: asr-decoder`` only covered the compose-local container; once the
address can point elsewhere, health itself must ask that machine.

⚠ **探針必須跟著協定走**。原生 decoder 有 `GET /health`、也會用 401 明說
「共享祕密不對」;OpenAI 相容端點**兩個都沒有** —— 它沒有 `/health`,而且
金鑰錯誤是在辨識請求上回 401。用原生探針去打外部端點,結果會是
`decoder_unreachable`(因為 `/health` 回 404),把「金鑰打錯」與「協定選錯」
兩種完全不同的病因都塗成同一個顏色。所以下面兩條路各自實作,三個 reason
在兩種協定下維持相同語意。
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

from anila_core.security.upstream_urls import join_upstream_path

from app.decode_client import (
    OPENAI_TRANSCRIPTION_PATH,
    PROTOCOL_NATIVE,
    PROTOCOL_OPENAI,
    normalise_protocol,
)
from app.wav import silence_wav

logger = logging.getLogger(__name__)

# Stable reason codes surfaced on /asr/health (and in logs). Operators need
# different responses for "voice is off" vs "pointed at something broken".
REASON_OK = "ok"
REASON_DECODER_UNREACHABLE = "decoder_unreachable"
REASON_DECODER_UNAUTHORIZED = "decoder_unauthorized"
REASON_DECODER_NOT_READY = "decoder_not_ready"

# WAN 預設值。呼叫端一律從 Settings 傳進來(ASR_PROBE_*),這兩個常數只是
# 直接呼叫本模組時的退路 —— 別在這裡調參,調 Settings。
DEFAULT_PROBE_TIMEOUT_SECONDS = 8.0
DEFAULT_PROBE_CONNECT_TIMEOUT_SECONDS = 3.0


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


def _redact(text: str, credential: str) -> str:
    token = (credential or "").strip()
    if len(token) >= 4:
        return text.replace(token, "<redacted>")
    return text


def _timeout(total: float | None, connect: float | None) -> httpx.Timeout:
    return httpx.Timeout(
        total if total is not None else DEFAULT_PROBE_TIMEOUT_SECONDS,
        connect=connect if connect is not None else DEFAULT_PROBE_CONNECT_TIMEOUT_SECONDS,
    )


async def probe_decode_target(
    base_url: str,
    credential: str,
    *,
    protocol: str = PROTOCOL_NATIVE,
    openai_model: str = "whisper-1",
    timeout_seconds: float | None = None,
    connect_timeout_seconds: float | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Ask whether speech can actually be transcribed at ``base_url``.

    Returns ``{"ok": bool, "reason": str, "detail": str | None}``.
    Never raises. ``detail`` 永遠不含憑證。
    """
    base = (base_url or "").rstrip("/")
    if not base:
        return {
            "ok": False,
            "reason": REASON_DECODER_UNREACHABLE,
            "detail": "decode URL is empty",
        }

    try:
        kind = normalise_protocol(protocol)
    except ValueError as exc:
        # 不該發生(啟動時已經擋掉),但 health 寧可說實話也不要假設。
        return {
            "ok": False,
            "reason": REASON_DECODER_UNREACHABLE,
            "detail": str(exc),
        }

    limits = _timeout(timeout_seconds, connect_timeout_seconds)
    owns = http_client is None
    client = http_client or httpx.AsyncClient()
    try:
        if kind == PROTOCOL_OPENAI:
            return await _probe_openai(
                client, base, credential, openai_model, limits
            )
        return await _probe_native(client, base, credential, limits)
    finally:
        if owns:
            await client.aclose()


async def _probe_native(
    client: httpx.AsyncClient,
    base: str,
    credential: str,
    limits: httpx.Timeout,
) -> dict[str, Any]:
    try:
        health = await client.get(f"{base}/health", timeout=limits)
    except Exception as exc:  # noqa: BLE001 — probe must never raise
        detail = _redact(f"{type(exc).__name__}: {exc}", credential)
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
            "detail": (
                f"decoder /health returned HTTP {health.status_code}"
                + (
                    " —— 原生解碼端一律有 /health;若這個位址是算力中心的"
                    " OpenAI 相容端點,要設 ASR_DECODE_PROTOCOL=openai"
                    if health.status_code == 404
                    else ""
                )
            ),
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
                "X-Token": credential or "",
            },
            timeout=limits,
        )
    except Exception as exc:  # noqa: BLE001
        detail = _redact(f"{type(exc).__name__}: {exc}", credential)
        return {
            "ok": False,
            "reason": REASON_DECODER_UNREACHABLE,
            "detail": f"decoder /transcribe probe failed: {detail}",
        }

    if auth.status_code in (401, 403):
        return {
            "ok": False,
            "reason": REASON_DECODER_UNAUTHORIZED,
            "detail": (
                f"decoder rejected ASR_DECODER_TOKEN (HTTP {auth.status_code}); "
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


async def _probe_openai(
    client: httpx.AsyncClient,
    base: str,
    credential: str,
    model: str,
    limits: httpx.Timeout,
) -> dict[str, Any]:
    """OpenAI 相容端點:沒有 /health,只能真的打一次辨識。

    送 100 ms 靜音 WAV —— 足夠讓對方走完認證與路由,又小到不算工作量
    (見 app/wav.py:silence_wav 對「為什麼不是更短」的說明)。
    """
    url = join_upstream_path(base, OPENAI_TRANSCRIPTION_PATH)
    headers = {"Authorization": f"Bearer {credential}"} if credential else {}
    try:
        resp = await client.post(
            url,
            files={"file": ("probe.wav", silence_wav(100), "audio/wav")},
            data={"model": model, "response_format": "json"},
            headers=headers,
            timeout=limits,
        )
    except Exception as exc:  # noqa: BLE001 — probe must never raise
        detail = _redact(f"{type(exc).__name__}: {exc}", credential)
        logger.info("openai transcription probe connect failed: %s", detail)
        return {
            "ok": False,
            "reason": REASON_DECODER_UNREACHABLE,
            "detail": detail,
        }

    status = resp.status_code
    if status in (401, 403):
        # ⚠ 這一條是遠端部署最容易踩、也最貴的:金鑰錯 → 每句話都 401,
        # 而 unreachable 會讓人去查網路。必須是 decoder_unauthorized。
        return {
            "ok": False,
            "reason": REASON_DECODER_UNAUTHORIZED,
            "detail": (
                f"辨識端點拒絕金鑰(HTTP {status});"
                "請確認治理中心該模型的 api_key,或 ASR_DECODE_API_KEY"
            ),
        }
    if status == 404:
        return {
            "ok": False,
            "reason": REASON_DECODER_UNREACHABLE,
            "detail": (
                f"HTTP 404 at {strip_url_userinfo(url)} —— 這個位址沒有 "
                "OpenAI 相容的辨識路徑;若它其實是本地 asr-decoder,"
                "ASR_DECODE_PROTOCOL 要設回 native"
            ),
        }
    if status in (429, 500, 502, 503, 504):
        return {
            "ok": False,
            "reason": REASON_DECODER_NOT_READY,
            "detail": f"辨識端點暫時無法服務(HTTP {status})",
        }
    if status in (200, 400, 415, 422):
        # 400/415/422 = 認證過了,只是嫌這段 100 ms 靜音 —— 金鑰與路由都對。
        return {"ok": True, "reason": REASON_OK, "detail": None}
    return {
        "ok": False,
        "reason": REASON_DECODER_UNREACHABLE,
        "detail": f"辨識端點回了非預期的 HTTP {status}",
    }
