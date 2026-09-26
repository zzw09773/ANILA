"""從治理中心的外部服務讀語音解碼位址。

沒有啟用、或還沒讀到，就不指向任何機器。不再退回 ``ASR_DECODE_URL``。
CSP 暫時連不上時，沿用上一筆成功讀到的位址，不悄悄改去別台。

憑證只活在記憶體，不進 log、不進 ``/asr/health``、不進錯誤訊息。
採用前再過一次 SSRF guard。
"""

from __future__ import annotations

import logging
import os
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
from app.decode_client import align_decode_client, normalise_protocol
from app.decode_probe import strip_url_userinfo

logger = logging.getLogger(__name__)

# source values operators will see on /asr/health:
#   unconfigured        — 治理中心沒啟用，或還沒成功讀過
#   csp_registry        — GET /api/internal/external-services/speech
#   csp_registry_stale  — CSP 這次讀不到，仍用上一筆成功的位址
_SPEECH_PATH = "/api/internal/external-services/speech"
_state: dict[str, Any] = {
    "url": None,
    # ⚠ 祕密。只在記憶體、只往 decode client 走;不進 log 也不進 health。
    "api_key": None,
    "protocol": "native",
    "openai_model": "whisper-1",
    "source": "unconfigured",
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
                "protocol": "native",
                "openai_model": "whisper-1",
                "source": "unconfigured",
                "at": 0.0,
                "last_refresh_error": None,
                "last_refresh_at": None,
            }
        )


def current_decode_url(settings: Settings) -> str:
    """目前要打的解碼位址。沒有治理中心位址就是空字串。"""
    del settings
    with _lock:
        return (_state["url"] or "").rstrip("/")


def current_decode_credential(settings: Settings) -> str:
    """治理中心這筆的憑證。沒有就是空字串，不退回環境變數。

    ⚠ 回傳值是祕密:呼叫端只能拿去發請求,不可以印出來。
    """
    del settings
    with _lock:
        return str(_state["api_key"] or "")


def current_decode_protocol() -> str:
    with _lock:
        return str(_state["protocol"] or "native")


def current_openai_model() -> str:
    with _lock:
        return str(_state["openai_model"] or "whisper-1")


def decode_url_source() -> str:
    """``unconfigured`` / ``csp_registry`` / ``csp_registry_stale``."""
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

    ⚠ **不要為了讓 ASR 過關而放寬 guard。** 單標籤主機名要靠
    ``ANILA_TRUSTED_HOSTS`` 點名，不是把檢查拿掉。
    """
    validate_outbound_url(url, ENDPOINT_KIND_MODEL)


def _service_token(settings: Settings) -> str:
    """有憑證檔就只讀檔。沒設路徑時才用這次傳進來的 Settings（測試）。

    重新啟用後走專屬憑證檔，不再注入共用 CSP_SERVICE_TOKEN。
    """
    if os.environ.get("ANILA_SERVICE_TOKEN_FILE", "").strip():
        from app.service_token import token

        return token()
    return (settings.CSP_SERVICE_TOKEN or "").strip()


async def refresh_decode_endpoint(
    settings: Settings,
    *,
    decode_client: Any | None = None,
    force: bool = False,
    http_client: httpx.AsyncClient | None = None,
) -> Any:
    """重讀治理中心的語音服務。不擲出。

    沒啟用就清空位址，不退回環境變數。CSP 這次讀失敗時，留著上一筆成功的位址。
    """
    token = _service_token(settings)
    if not token:
        with _lock:
            _state["url"] = None
            _state["api_key"] = None
            _state["source"] = "unconfigured"
            _state["last_refresh_error"] = "CSP service token unset"
            _state["last_refresh_at"] = datetime.now(timezone.utc).isoformat()
            _state["at"] = time.monotonic()
        return _apply(decode_client)

    ttl = float(settings.ASR_DECODE_URL_TTL)
    now = time.monotonic()
    with _lock:
        if not force and _state["at"] and now - _state["at"] < ttl:
            return decode_client
        _state["at"] = now

    csp_url: str | None = None
    csp_key: str | None = None
    csp_protocol = "native"
    csp_model = "whisper-1"
    error: str | None = None
    # True 採用；False 明確沒設定；None 這次讀失敗，留著上一筆。
    designated: bool | None = None
    try:
        owns_client = http_client is None
        client = http_client or httpx.AsyncClient()
        try:
            response = await client.get(
                f"{settings.CSP_BASE_URL.rstrip('/')}{_SPEECH_PATH}",
                headers={"X-CSP-Service-Token": token},
                timeout=5.0,
            )
        finally:
            if owns_client:
                await client.aclose()

        if response.status_code == 200:
            # ⚠ payload 可能含 credential。整包不要進 log。
            payload = response.json() or {}
            raw = str(payload.get("base_url") or payload.get("endpoint_url") or "").strip()
            if "configured" in payload:
                configured = bool(payload.get("configured"))
            else:
                configured = bool(raw)
            if not configured:
                designated = False
            else:
                hinted = str(payload.get("protocol") or "").strip()
                if hinted:
                    protocol_source = hinted
                else:
                    protocol_source = str(
                        getattr(decode_client, "protocol", None) or "native"
                    )
                try:
                    csp_protocol = normalise_protocol(protocol_source)
                except ValueError as exc:
                    error = f"語音協定無法辨識: {exc}"
                    logger.error(error)
                    designated = None
                    raw = ""
                if designated is None and error:
                    pass
                elif not _looks_like_http_url(raw):
                    error = (
                        "CSP speech returned unusable base_url="
                        f"{strip_url_userinfo(raw)!r}"
                    )
                    logger.error(error)
                    designated = None
                else:
                    try:
                        guard_decode_url(raw)
                    except UnsafeEndpointError as exc:
                        error = (
                            "CSP speech base_url 未通過出向檢查 "
                            f"({exc.reason}): {exc}"
                        )
                        logger.error(error)
                        designated = None
                    else:
                        csp_url = raw.rstrip("/")
                        csp_key = str(
                            payload.get("credential") or payload.get("api_key") or ""
                        ).strip() or None
                        csp_model = (
                            str(payload.get("openai_model") or "whisper-1").strip()
                            or "whisper-1"
                        )
                        designated = True
        elif response.status_code in (404, 409):
            designated = False
            logger.info("speech service unavailable (HTTP %s)", response.status_code)
        else:
            designated = None
            error = f"speech lookup failed: HTTP {response.status_code}"
            logger.warning("%s; keeping previous decode URL", error)
    except Exception as exc:  # noqa: BLE001 — 讀設定失敗不能弄斷進行中的連線
        designated = None
        error = f"speech lookup errored ({type(exc).__name__}: {exc})"
        logger.warning("%s; keeping previous decode URL", error)

    with _lock:
        _state["last_refresh_at"] = datetime.now(timezone.utc).isoformat()
        _state["last_refresh_error"] = error
        if designated is True:
            _state["url"] = csp_url
            _state["api_key"] = csp_key
            _state["protocol"] = csp_protocol
            _state["openai_model"] = csp_model
            _state["source"] = "csp_registry"
        elif designated is False:
            _state["url"] = None
            _state["api_key"] = None
            _state["protocol"] = "native"
            _state["source"] = "unconfigured"
        elif _state["url"]:
            _state["source"] = "csp_registry_stale"
        else:
            _state["source"] = "unconfigured"

    return _apply(decode_client)


def _apply(decode_client: Any | None) -> Any:
    """位址、憑證、協定一起套用。"""
    with _lock:
        url = (_state["url"] or "").rstrip("/")
        credential = str(_state["api_key"] or "")
        protocol = str(_state["protocol"] or "native")
        model = str(_state["openai_model"] or "whisper-1")
    try:
        return align_decode_client(
            decode_client,
            base_url=url,
            credential=credential,
            protocol=protocol,
            openai_model=model,
        )
    except Exception:
        # 測試用的假 client 沒有 protocol。直接改位址與憑證。
        if decode_client is None:
            return None
        setter = getattr(decode_client, "set_base_url", None)
        if callable(setter):
            setter(url)
        elif hasattr(decode_client, "_base_url"):
            decode_client._base_url = url
        set_credential = getattr(decode_client, "set_credential", None)
        if callable(set_credential):
            set_credential(credential)
        return decode_client


def host_of(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).hostname or ""
