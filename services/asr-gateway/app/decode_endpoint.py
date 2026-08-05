"""Resolve the ASR decoder URL from CSP (governance) with env fallback.

Mirrors the router-primary pattern in anila-core's router_server: read at
boot and on a TTL refresh, fall back to ``ASR_DECODE_URL`` when CSP has no
designation (404/409), and keep the last known CSP address when CSP is
unreachable — never silently switch to a different machine than the one
the operator selected. Source of truth is always visible on ``/asr/health``.

解碼端憑證(2026-08-05 起):CSP 的 asr-primary 在**服務權杖**通道上會一併
回傳該筆模型自帶的 `api_key`(來源是加密的 ``api_key_secret_ref``,見
``services/csp/app/api/models.py`` 的 ``get_asr_primary``)。原本這裡刻意不拿
金鑰,理由是「主模型端點從不回傳憑證」—— 那個自我設限在解碼端還在同一台
機器時成立,一旦端點在算力中心後面就等於「沒有地方可以放金鑰」,結果是每
一句話都 401。CSP 沒給金鑰時退回環境變數
(``ASR_DECODER_TOKEN`` / ``ASR_DECODE_API_KEY``,依協定)。

⚠ **金鑰不進 log、不進 /asr/health、不進錯誤訊息。** 這個 repo 是 PUBLIC,
而音訊路徑本來就守著「不記逐字稿」那條線(``services/asr-decoder/app/main.py``)
—— 金鑰同一條線。

SSRF:CSP 端在登記時已經驗過 ``endpoint_kind="model"``,但採用點自己也要驗
(縱深防禦;而且治理中心的資料庫值可能是登記之後才被改的)。環境變數那條
以前**沒有任何一層在驗**。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from anila_core.security.url_guard import (
    ENDPOINT_KIND_MODEL,
    UnsafeEndpointError,
    validate_outbound_url,
)

from app.config import Settings
from app.decode_client import env_credential
from app.decode_probe import strip_url_userinfo

logger = logging.getLogger(__name__)

# source values operators will see on /asr/health:
#   env                 — using ASR_DECODE_URL (CSP has none, or never reached)
#   csp_registry        — using the address from GET /api/models/asr-primary
#   csp_registry_stale  — CSP unreachable; still using last successful CSP URL
_state: dict[str, Any] = {
    "url": None,
    # ⚠ 祕密。只在記憶體、只往 decode client 走;不進 log 也不進 health。
    "api_key": None,
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
                "api_key": None,
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


def current_decode_credential(settings: Settings) -> str:
    """治理中心指派的金鑰優先,否則用環境變數那把(依協定選欄位)。

    ⚠ 回傳值是祕密:呼叫端只能拿去發請求,不可以印出來。
    """
    with _lock:
        designated = _state["api_key"]
    if designated:
        return str(designated)
    return env_credential(settings)


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


def guard_decode_url(url: str) -> None:
    """SSRF guard,採用任何解碼位址之前都要過。

    ``endpoint_kind='model'`` —— 解碼端就是一個模型端點,http 由
    ``ANILA_ALLOW_HTTP_ENDPOINT`` 決定、私網 IP 由
    ``ANILA_ALLOW_PRIVATE_ENDPOINT`` 決定、compose 服務名(單標籤)由
    ``ANILA_TRUSTED_HOSTS`` 點名。這三個旗標本來就是「這個出向目的地是被
    認可的」的表達方式;ASR 沒有理由是唯一跳過它們的模型呼叫。

    ⚠ **不要為了讓 ASR 過關而放寬 guard。** 本地 `http://asr-decoder:9000`
    是單標籤 docker 服務名,靠的是 platform.yml 把 `asr-decoder` 併進這個
    服務自己的 ``ANILA_TRUSTED_HOSTS`` —— 那正是 guard 文件裡寫明的
    operator 專用機制,不是把檢查拿掉。
    """
    validate_outbound_url(url, ENDPOINT_KIND_MODEL)


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
            _state["api_key"] = None
            _state["source"] = "env"
            _state["last_refresh_error"] = "CSP_SERVICE_TOKEN unset; using ASR_DECODE_URL"
            _state["last_refresh_at"] = datetime.now(timezone.utc).isoformat()
            _state["at"] = time.monotonic()
        _apply(decode_client, current_decode_url(settings),
               current_decode_credential(settings))
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
    csp_key: str | None = None
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
            # ⚠ 這個 payload 可能含 api_key。整包**永遠不要**進 log。
            payload = response.json() or {}
            raw = str(payload.get("endpoint_url") or "").strip()
            if not _looks_like_http_url(raw):
                error = (
                    "CSP asr-primary returned unusable endpoint_url="
                    f"{strip_url_userinfo(raw)!r}"
                )
                logger.error(error)
                # Do not adopt; do not fall back if we already have a CSP URL —
                # leave previous state and surface the error.
                designated = None
            else:
                try:
                    guard_decode_url(raw)
                except UnsafeEndpointError as exc:
                    # 治理中心登記時就驗過一次;這裡再驗一次是縱深防禦 ——
                    # 登記之後被改動的資料庫值不該把 gateway 指到內網深處。
                    error = (
                        "CSP asr-primary endpoint_url 未通過出向檢查 "
                        f"({exc.reason}): {exc}"
                    )
                    logger.error(error)
                    designated = None
                else:
                    csp_url = raw.rstrip("/")
                    csp_key = str(payload.get("api_key") or "").strip() or None
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
            _state["api_key"] = csp_key
            _state["source"] = "csp_registry"
        elif designated is False:
            _state["url"] = None
            _state["api_key"] = None
            _state["source"] = "env"
        else:
            # Unreachable / bad payload: keep last known. If we never had a
            # CSP URL, source stays env; if we did, mark it stale so the
            # operator can see the refresh failed.
            if _state["url"]:
                _state["source"] = "csp_registry_stale"
            else:
                _state["source"] = "env"

    _apply(decode_client, current_decode_url(settings),
           current_decode_credential(settings))


def _apply(decode_client: Any | None, url: str, credential: str) -> None:
    """位址與憑證一起套用 —— 兩者是同一個決定的兩半。

    分開套會出現「新機器 + 舊金鑰」的中間狀態,而那個狀態的症狀
    (每句話 401)跟「金鑰設錯」長得一模一樣。
    """
    if decode_client is None:
        return
    setter = getattr(decode_client, "set_base_url", None)
    if callable(setter):
        setter(url)
    elif hasattr(decode_client, "_base_url"):
        # Fake clients in tests may only expose a bare attribute.
        decode_client._base_url = url.rstrip("/")

    set_credential = getattr(decode_client, "set_credential", None)
    if callable(set_credential):
        set_credential(credential)


def host_of(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).hostname or ""
