"""Downstream identity + credential headers for CSP outbound calls.

Split verbatim out of ``app/services/proxy_service.py`` (Doc-10 Slice 1,
behavior-preserving refactor). SECURITY-CRITICAL: ``downstream_identity`` /
``build_agent_headers`` / ``build_model_gateway_headers`` carry the
employee-id (員編) downstream semantics — moved unchanged.

P2.1 (2026-08-01): agent dispatch no longer sends ``X-CSP-Service-Token``
or plaintext ``X-ANILA-User-*`` headers. Identity rides in a 5-minute
RS256 Bearer JWT (see ``dispatch_token``). The per-agent csk- cache below
remains for credential-rotation invalidation hooks (W2/W3); dispatch
itself no longer reads it.
"""
import asyncio
import logging
import re
import threading
import time
from threading import Lock
from typing import Optional

from app.config import settings
from app.services.proxy.dispatch_token import issue_dispatch_token

logger = logging.getLogger("app.services.proxy_service")

# ---------------------------------------------------------------------------
# Per-agent service-token cache (Sprint 8 X / Phase A — decision #2)
#
# Retained for ``invalidate_agent_token_cache`` hooks called from credential
# rotation / revocation admin paths. Dispatch no longer resolves or sends
# csk- tokens (P2.1); cache population via ``_set_cached_agent_token`` is
# test-only until W3 retires the credential surface.
#
# Concurrency: ``Lock`` rather than asyncio.Lock so synchronous
# callers (e.g. test fixtures, ad-hoc scripts) can use the cache too.
# ---------------------------------------------------------------------------

_PER_AGENT_TOKEN_TTL_SECONDS = 300

_per_agent_token_cache: dict[int, tuple[str, float]] = {}
_per_agent_token_lock = Lock()


def _get_cached_agent_token(agent_id: int) -> Optional[str]:
    with _per_agent_token_lock:
        entry = _per_agent_token_cache.get(agent_id)
        if entry is None:
            return None
        token, expires_at = entry
        if expires_at < time.monotonic():
            _per_agent_token_cache.pop(agent_id, None)
            return None
        return token


def _set_cached_agent_token(agent_id: int, token: str) -> None:
    with _per_agent_token_lock:
        _per_agent_token_cache[agent_id] = (
            token,
            time.monotonic() + _PER_AGENT_TOKEN_TTL_SECONDS,
        )


def invalidate_agent_token_cache(agent_id: Optional[int] = None) -> None:
    """Hook for rotation / revocation paths.

    Called by ``agents.py`` admin endpoints after writing a new
    credential / rotating an existing one. Pass ``None`` to flush
    everything (rare — used by tests).
    """
    with _per_agent_token_lock:
        if agent_id is None:
            _per_agent_token_cache.clear()
        else:
            _per_agent_token_cache.pop(agent_id, None)


# 員編 shape (X.509 subject.serialNumber): 6–9 digits. Mirrors
# card_auth._EMPLOYEE_ID_RE — kept local to avoid importing a private name.
_EMPLOYEE_ID_RE = re.compile(r"\A\d{6,9}\Z")


def downstream_identity(user) -> Optional[str]:
    """Wire identity (員編) forwarded downstream as ``X-ANILA-User-Id``.

    On the card-login branch ``user.username`` IS the employee ID. We
    forward it only when it matches the 員編 shape; non-card accounts
    (e.g. admin) return ``None``. Callers then OMIT the identity header —
    we never forge a bogus identity (e.g. ``"admin"``) downstream. This is
    fail-SAFE, not request-blocking: the chat/embedding call still proceeds
    (admin can use chat), just with no downstream user attribution.

    Note: agent dispatch (P2.1) no longer uses this for wire identity —
    that rides inside the signed dispatch JWT. Model-gateway headers still
    use the 員編 string via ``build_model_gateway_headers``.
    """
    username = getattr(user, "username", None)
    if username and _EMPLOYEE_ID_RE.match(username):
        return username
    return None


def _claim_int(value: object) -> int | None:
    """正整數才寫進派工 JWT。像 task-1 這種關聯字串只留在標頭。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
    else:
        return None
    if parsed <= 0:
        return None
    return parsed


def build_agent_headers(
    *,
    user_id: int,
    department: int | None,
    agent_id: int,
    task_id: Optional[str | int] = None,
    trace_id: Optional[str] = None,
    conversation_id: Optional[str | int] = None,
    api_key_id: Optional[int] = None,
) -> dict:
    """Build signed-identity headers for downstream AGENTS (P2.1).

    Mints a 5-minute RS256 dispatch JWT (``user_id`` / ``department`` /
    ``agent_id``) and sends it as ``Authorization: Bearer``. Correlation
    headers ``X-ANILA-Task-Id`` / ``X-ANILA-Trace-Id`` are kept when the
    call belongs to a Task. Does NOT send ``X-CSP-Service-Token`` or
    plaintext ``X-ANILA-User-Id`` / ``-Email`` / ``-Groups``.

    AGENTS ONLY. Never use this for the model gateway — use
    ``build_model_gateway_headers``.
    """
    headers: dict = {"Content-Type": "application/json"}
    token = issue_dispatch_token(
        user_id=user_id,
        department=department,
        agent_id=agent_id,
        task_id=_claim_int(task_id),
        conversation_id=_claim_int(conversation_id),
        api_key_id=api_key_id,
    )
    headers["Authorization"] = f"Bearer {token}"
    if task_id:
        headers["X-ANILA-Task-Id"] = str(task_id)
    if trace_id:
        headers["X-ANILA-Trace-Id"] = str(trace_id)
    return headers


def build_model_gateway_headers(user_identity: Optional[str]) -> dict:
    """Build headers for an LLM / embedding model gateway (.12) call.

    Carries ONLY the employee ID (員編) in ``X-ANILA-User-Id`` for
    traceability. Deliberately omits BOTH ``X-CSP-Service-Token`` (the
    model gateway must never receive CSP's service credential) and
    ``X-ANILA-User-Email`` / ``-Groups`` (no end-user PII into model
    prompt logs). The gateway bearer key is layered on separately by
    ``_apply_gateway_auth`` at the call site.

    ``X-ANILA-User-Id`` is omitted when ``user_identity`` is falsy
    (non-card accounts send no identity rather than a forged one; the
    model call still proceeds).
    """
    headers: dict = {"Content-Type": "application/json"}
    if user_identity:
        headers["X-ANILA-User-Id"] = user_identity
    return headers


MODEL_CREDENTIAL_UNREADABLE = "模型暫時無法使用：憑證無法讀取，請通知管理員"


def credential_alert_title(display_name: str) -> str:
    return f"模型 {display_name} 憑證無法讀取"


def credential_alert_message(display_name: str, name: str) -> str:
    return f"模型「{display_name}」（{name}）的專屬金鑰無法解密，請到模型頁重新設定金鑰"


# 請求路徑上不做 DB 與寄信：同一模型 60 秒內只送一次，事件迴圈上交給背景執行緒，
# 同時最多 4 條；滿了或寫入失敗就不記這次，下一個失敗的請求再送。
# 指紋與健康迴圈共用，健康迴圈探到恢復時結案。
_CREDENTIAL_ALERT_INTERVAL_S = 60.0
_credential_alert_sent: dict[str, float] = {}
_credential_alert_lock = threading.Lock()
_credential_alert_slots = threading.BoundedSemaphore(4)


def reset_credential_alert_throttle() -> None:
    with _credential_alert_lock:
        _credential_alert_sent.clear()


def _forget_credential_alert(fingerprint: str) -> None:
    with _credential_alert_lock:
        _credential_alert_sent.pop(fingerprint, None)


def _write_credential_alert(kwargs: dict) -> None:
    try:
        from app.services.alert_detectors import _emit_alert_autocommit

        _emit_alert_autocommit(**kwargs)
    except Exception:
        _forget_credential_alert(kwargs["fingerprint"])
        logger.exception("無法寫入模型憑證讀取失敗的警報")


def _write_credential_alert_in_background(kwargs: dict) -> None:
    try:
        _write_credential_alert(kwargs)
    finally:
        _credential_alert_slots.release()


def _alert_unreadable_model_credential(model) -> None:
    model_id = getattr(model, "id", None)
    name = getattr(model, "name", None) or (
        str(model_id) if model_id is not None else "unknown"
    )
    display_name = getattr(model, "display_name", None) or name
    fingerprint = f"health:model:{model_id if model_id is not None else name}"
    now = time.monotonic()
    with _credential_alert_lock:
        last = _credential_alert_sent.get(fingerprint)
        if last is not None and now - last < _CREDENTIAL_ALERT_INTERVAL_S:
            return
        _credential_alert_sent[fingerprint] = now
    kwargs = {
        "fingerprint": fingerprint,
        "category": "health",
        "severity": "high",
        "title": credential_alert_title(display_name),
        "message": credential_alert_message(display_name, name),
        "source_type": "model",
        "source_id": model_id,
        "metadata": {"model_name": name, "display_name": display_name},
    }
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        _write_credential_alert(kwargs)
        return
    if not _credential_alert_slots.acquire(blocking=False):
        _forget_credential_alert(fingerprint)
        return
    try:
        threading.Thread(
            target=_write_credential_alert_in_background,
            args=(kwargs,),
            name="model-credential-alert",
            daemon=True,
        ).start()
    except Exception:
        _credential_alert_slots.release()
        _forget_credential_alert(fingerprint)
        logger.exception("無法啟動模型憑證警報的背景寫入")


def _fail_unreadable_model_credential(model) -> None:
    from fastapi import HTTPException

    _alert_unreadable_model_credential(model)
    raise HTTPException(status_code=503, detail=MODEL_CREDENTIAL_UNREADABLE)


def resolve_model_gateway_key(model) -> Optional[str]:
    """Resolve the outbound gateway bearer key for a model call (Slice 6a).

    The platform entry ``anila-router`` never inherits MODEL_GATEWAY_API_KEY.
    Callers must forward the verified JWT / CSP sk- instead.

    沒有專屬金鑰的模型才用全域 ``MODEL_GATEWAY_API_KEY``。有專屬金鑰但
    解不開（或解開是空的）就失敗，不改用全域金鑰。

    Returns ``None`` when a model has no per-model secret and the global
    key is also empty (bare same-host vLLM with no gateway).
    """
    if getattr(model, "name", None) == "anila-router":
        return ""
    ref = getattr(model, "api_key_secret_ref", None)
    if ref:
        try:
            from app.services.service_token_envelope import (
                decode_service_token_envelope,
            )

            key = decode_service_token_envelope(ref)
        except Exception:
            logger.error(
                "模型 %s 的專屬金鑰無法解密，不改用全域 MODEL_GATEWAY_API_KEY",
                getattr(model, "name", None) or getattr(model, "id", None),
                exc_info=True,
            )
            _fail_unreadable_model_credential(model)
        if not key:
            logger.error(
                "模型 %s 的專屬金鑰解開後是空的，不改用全域 MODEL_GATEWAY_API_KEY",
                getattr(model, "name", None) or getattr(model, "id", None),
            )
            _fail_unreadable_model_credential(model)
        return key
    return (settings.MODEL_GATEWAY_API_KEY or "").strip() or None


def _apply_gateway_auth(headers: dict, api_key: Optional[str] = None) -> dict:
    """注入出向模型 gateway 的 API key (in-place,並回傳同一 dict)。

    ``api_key`` 為 None(既有呼叫端 / 測試)時退回全域
    ``MODEL_GATEWAY_API_KEY``;Slice 6a 起呼叫端傳入
    ``resolve_model_gateway_key(model)`` 讓 per-model secret ref 優先。
    非空才動作 — 模型 gateway 的 /v1 要 ``Authorization: Bearer``。
    呼叫端負責 scope:只用在
    model 呼叫,agent dispatch 不帶 (key 不該外流給第三方 agent)。
    不覆蓋既有 Authorization。
    """
    resolved = api_key if api_key is not None else settings.MODEL_GATEWAY_API_KEY
    key = (resolved or "").strip()
    if key and "Authorization" not in headers:
        headers["Authorization"] = f"Bearer {key}"
    return headers
