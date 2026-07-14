"""Downstream identity + credential headers for CSP outbound calls.

Split verbatim out of ``app/services/proxy_service.py`` (Doc-10 Slice 1,
behavior-preserving refactor). SECURITY-CRITICAL: ``downstream_identity`` /
``build_agent_headers`` / ``build_model_gateway_headers`` carry the
employee-id (員編) downstream semantics — moved unchanged.
"""
import logging
import json
import re
import time
from threading import Lock
from collections.abc import Mapping, Sequence
from typing import Optional
from urllib.parse import quote

from app.config import settings
from app.database import SessionLocal
from app.services import agent_credential_service

logger = logging.getLogger("app.services.proxy_service")

# ---------------------------------------------------------------------------
# Per-agent service-token cache (Sprint 8 X / Phase A — decision #2)
#
# DB-decrypt-per-outgoing-call is wasteful: under chat streaming load
# we'd hit ``decrypt_credential`` once per request just to write a
# header value that hasn't changed. A 5-minute TTL cache keyed by
# agent id covers ≥99% of traffic; rotation makes the cached value
# stale, but a cached stale token only hurts during the rotation
# grace window — and ``service_token_previous_*`` is precisely the
# mechanism that keeps the stale value valid for the next 24h.
#
# Concurrency: ``Lock`` rather than asyncio.Lock so synchronous
# callers (e.g. test fixtures, ad-hoc scripts) can use the cache too.
# Lock contention is irrelevant — get/set is microsecond-level work.
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


def _resolve_outgoing_service_token(target_agent_id: Optional[int]) -> Optional[str]:
    """Decide which service token to put on the outgoing CSP→agent header.

    Resolution order:

      1. Per-agent token from ``agent_credentials`` (cached 5 min) when
         ``target_agent_id`` identifies a registered agent.
      2. Legacy fleet-shared ``CSP_SERVICE_TOKEN`` env var only for
         legacy calls with no registered target id.
      3. ``None`` — no applicable service credential.

    Step 1 needs ``target_agent_id`` because we don't know which row
    is "this agent's" without it. When the proxy is forwarding to a
    raw legacy endpoint with no registered agent id,
    ``target_agent_id`` is ``None`` and we may use the legacy token.
    """
    if target_agent_id is not None:
        cached = _get_cached_agent_token(target_agent_id)
        if cached:
            return cached
        db = SessionLocal()
        try:
            plaintext = agent_credential_service.get_active_plaintext_for_agent(
                db, agent_id=target_agent_id
            )
        finally:
            db.close()
        if plaintext:
            _set_cached_agent_token(target_agent_id, plaintext)
            return plaintext
        # Registered agents must not silently receive the fleet-shared
        # legacy token. Missing per-agent credential is safer as "no
        # service token" than broadening one leaked token across agents.
        return None

    if settings.CSP_SERVICE_TOKEN:
        return settings.CSP_SERVICE_TOKEN

    return None


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
    """
    username = getattr(user, "username", None)
    if username and _EMPLOYEE_ID_RE.match(username):
        return username
    return None


def build_agent_headers(
    user_identity: Optional[str],
    user_email: Optional[str] = None,
    user_groups: Optional[str] = None,
    target_agent_id: Optional[int] = None,
    task_id: Optional[str] = None,
    trace_id: Optional[str] = None,
) -> dict:
    """Build service-credential + identity headers for downstream AGENTS.

    Carries the full identity set (員編 + email + groups) plus the agent
    service token. ``X-ANILA-User-Id`` is the **employee ID (員編)**, a
    string forwarded verbatim; it is OMITTED when ``user_identity`` is
    falsy so non-card accounts (e.g. admin) send NO identity rather than a
    forged one. The call still proceeds — the agent simply has no user
    attribution (it still receives the service token).

    Sprint 8 X: ``target_agent_id`` is the registered ``agents.id``. When
    set, we use only that agent's token from ``agent_credentials``; missing
    credential does not fall back to the fleet-shared legacy env var.

    Slice 2b-C (doc 05 §4 dispatch contract): ``task_id`` / ``trace_id``
    ride as ``X-ANILA-Task-Id`` / ``X-ANILA-Trace-Id`` when the call
    belongs to a Task; both come from the task row and are omitted for
    legacy (task-less) traffic. AGENT dispatch only — doc 04 §3/AC5
    forbids task/trace headers toward the model gateway.

    AGENTS ONLY. Never use this for the model gateway — it must not
    receive ``X-CSP-Service-Token`` (use ``build_model_gateway_headers``).
    """
    headers: dict = {"Content-Type": "application/json"}
    token = _resolve_outgoing_service_token(target_agent_id)
    if token:
        headers["X-CSP-Service-Token"] = token
    if user_identity:
        headers["X-ANILA-User-Id"] = user_identity
    if user_email:
        headers["X-ANILA-User-Email"] = user_email
    if user_groups:
        headers["X-ANILA-User-Groups"] = user_groups
    if task_id:
        headers["X-ANILA-Task-Id"] = str(task_id)
    if trace_id:
        headers["X-ANILA-Trace-Id"] = str(trace_id)
    return headers


def build_model_gateway_headers(
    user_identity: Optional[str],
    *,
    router_caller_user_id: int | None = None,
) -> dict:
    """Build headers for an LLM / embedding model gateway (.12) call.

    Carries ONLY the employee ID (員編) in ``X-ANILA-User-Id`` for
    traceability. Deliberately omits BOTH ``X-CSP-Service-Token`` (the
    model gateway must never receive CSP's service credential) and
    ``X-ANILA-User-Email`` / ``-Groups`` (no end-user PII into model
    prompt logs). The gateway bearer key is layered on separately by
    ``_apply_gateway_auth`` at the call site.

    ``X-ANILA-User-Id`` is omitted when ``user_identity`` is falsy
    (non-card accounts send no identity rather than a forged one; the
    model call still proceeds).  ``router_caller_user_id`` is a separate,
    integer DB-PK context used only when the destination is the internal
    ``anila-router`` model.  Callers must leave it ``None`` for ordinary
    LLM/embedding destinations; it is never an employee-id alias.
    """
    headers: dict = {"Content-Type": "application/json"}
    if user_identity:
        headers["X-ANILA-User-Id"] = user_identity
    if router_caller_user_id is not None:
        if int(router_caller_user_id) <= 0:
            raise ValueError("router caller user id must be a positive integer")
        headers["X-ANILA-Caller-User-Id"] = str(int(router_caller_user_id))
    return headers


_ROUTER_CONTEXT_HEADERS = {
    "task_id": "X-ANILA-Task-Id",
    "run_id": "X-ANILA-Run-Id",
    "source_snapshot_id": "X-ANILA-Source-Snapshot-Id",
    "trace_id": "X-ANILA-Trace-Id",
    "task_type": "X-ANILA-Task-Type",
    "classification_level": "X-ANILA-Classification-Level",
    "scopes": "X-ANILA-Scopes",
    "required_capabilities": "X-ANILA-Required-Capabilities",
    "auth_assurance": "X-ANILA-Auth-Assurance",
    "auth_session_id": "X-ANILA-Auth-Session-Id",
    "auth_methods": "X-ANILA-Auth-Methods",
    "auth_level": "X-ANILA-Auth-Level",
    "auth_time": "X-ANILA-Auth-Time",
    "break_glass": "X-ANILA-Break-Glass",
    "owner_id": "X-ANILA-Owner-Id",
    "invocation_id": "X-ANILA-Invocation-Id",
}

_ROUTER_REQUIRED_CONTEXT = frozenset(
    {
        "task_id",
        "run_id",
        "source_snapshot_id",
        "trace_id",
        "task_type",
        "classification_level",
        "scopes",
    }
)
_POSITIVE_DECIMAL_RE = re.compile(r"\A[1-9][0-9]*\Z")
_HEADER_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _router_wire_text(value: object, *, name: str) -> str:
    """Return a safe single-line Router header value."""

    text = str(value).strip()
    if not text or _HEADER_CONTROL_RE.search(text):
        raise ValueError(f"{name} 必須是非空單行字串")
    return text


def _router_wire_positive(value: object, *, name: str) -> str:
    if isinstance(value, bool):
        raise ValueError(f"{name} 必須是 positive numeric id")
    text = str(value).strip()
    if not _POSITIVE_DECIMAL_RE.fullmatch(text):
        raise ValueError(f"{name} 必須是 positive numeric id")
    return text


def _router_wire_tokens(value: object, *, name: str) -> str:
    if isinstance(value, str):
        values = [part.strip() for part in value.split(",")]
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        values = [str(part).strip() for part in value]
    else:
        raise ValueError(f"{name} 必須是 token sequence")
    values = [part for part in values if part]
    if not values or any(_HEADER_CONTROL_RE.search(part) for part in values):
        raise ValueError(f"{name} 必須是非空 token sequence")
    return ",".join(values)


def build_router_model_gateway_headers(
    user_identity: Optional[str],
    *,
    router_caller_user_id: int,
    router_context: Mapping[str, object],
) -> dict:
    """Build the *Router-only* model gateway header contract.

    ``build_model_gateway_headers`` intentionally carries only employee
    identity plus the dedicated caller PK.  The internal ``anila-router``
    target is different: it needs the complete CSP-derived task context for
    R3 admission.  This helper is therefore the sole place that can add
    formal context headers, and it rejects an incomplete/unknown projection
    instead of silently inventing governance values.  Callers must pass a
    server-derived mapping; ordinary model/embedding destinations never call
    this helper.
    """

    if not isinstance(router_context, Mapping):
        raise TypeError("router_context 必須是 mapping")
    unknown = set(router_context) - set(_ROUTER_CONTEXT_HEADERS)
    if unknown:
        raise ValueError("router_context 含有未允許欄位")
    missing = _ROUTER_REQUIRED_CONTEXT - set(router_context)
    if missing:
        raise ValueError(
            "Router formal context 缺少必要欄位: " + ", ".join(sorted(missing))
        )

    headers = build_model_gateway_headers(
        user_identity,
        router_caller_user_id=router_caller_user_id,
    )
    positive = {"task_id", "run_id", "source_snapshot_id", "owner_id"}
    token_fields = {
        "scopes",
        "required_capabilities",
        "auth_methods",
    }
    for name, value in router_context.items():
        if value is None:
            continue
        header_name = _ROUTER_CONTEXT_HEADERS[name]
        if name in positive:
            headers[header_name] = _router_wire_positive(value, name=name)
        elif name in token_fields:
            headers[header_name] = _router_wire_tokens(value, name=name)
        elif name == "auth_assurance":
            if isinstance(value, Mapping):
                value = json.dumps(dict(value), ensure_ascii=False, separators=(",", ":"))
            headers[header_name] = quote(
                _router_wire_text(value, name=name), safe=""
            )
        elif name == "break_glass":
            if not isinstance(value, bool):
                raise ValueError("break_glass 必須是 bool")
            headers[header_name] = "true" if value else "false"
        else:
            # Starlette/httpx header values are latin-1 constrained in many
            # deployments; Router's formal parser unquotes these values.
            headers[header_name] = quote(
                _router_wire_text(value, name=name), safe=""
            )
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
