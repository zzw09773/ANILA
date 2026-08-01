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
import logging
import re
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


def build_agent_headers(
    *,
    user_id: int,
    department: int | None,
    agent_id: int,
    task_id: Optional[str] = None,
    trace_id: Optional[str] = None,
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


def resolve_model_gateway_key(model) -> Optional[str]:
    """Resolve the outbound gateway bearer key for a model call (Slice 6a).

    doc 04 §3 New rule: per-model ``api_key_secret_ref`` takes precedence;
    the global ``MODEL_GATEWAY_API_KEY`` env stays as the MVP fallback. The
    secret ref is an ``enc::v1::`` AES-GCM envelope (the exact same crypto as
    csk- / ingestion credentials); decode failures fall back to the env key
    (fail-soft on the *key source*, never fail-open on auth — a wrong key
    just means the gateway rejects the call) and are logged.

    Returns ``None`` when neither source yields a key (bare same-host vLLM
    with no gateway — behaviour unchanged: no Authorization header injected).
    """
    ref = getattr(model, "api_key_secret_ref", None)
    if ref:
        try:
            from app.services.service_token_envelope import (
                decode_service_token_envelope,
            )

            key = decode_service_token_envelope(ref)
            if key:
                return key
        except Exception:
            logger.warning(
                "per-model gateway key 解密失敗 model_id=%s,退回全域 "
                "MODEL_GATEWAY_API_KEY",
                getattr(model, "id", None),
                exc_info=True,
            )
    return (settings.MODEL_GATEWAY_API_KEY or "").strip() or None


def _apply_gateway_auth(headers: dict, api_key: Optional[str] = None) -> dict:
    """注入出向模型 gateway 的 API key (in-place,並回傳同一 dict)。

    ``api_key`` 為 None(既有呼叫端 / 測試)時退回全域
    ``MODEL_GATEWAY_API_KEY``;Slice 6a 起呼叫端傳入
    ``resolve_model_gateway_key(model)`` 讓 per-model secret ref 優先。
    非空才動作 — 內網拓撲下模型在 10.53.100.12 的 My-OpenAI-Frontend gateway
    後面,/v1 全路由要 ``Authorization: Bearer``。呼叫端負責 scope:只用在
    model 呼叫,agent dispatch 不帶 (key 不該外流給第三方 agent)。
    不覆蓋既有 Authorization。
    """
    resolved = api_key if api_key is not None else settings.MODEL_GATEWAY_API_KEY
    key = (resolved or "").strip()
    if key and "Authorization" not in headers:
        headers["Authorization"] = f"Bearer {key}"
    return headers
