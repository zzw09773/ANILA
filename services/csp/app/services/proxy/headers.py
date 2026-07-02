"""Downstream identity + credential headers for CSP outbound calls.

Split verbatim out of ``app/services/proxy_service.py`` (Doc-10 Slice 1,
behavior-preserving refactor). SECURITY-CRITICAL: ``downstream_identity`` /
``build_agent_headers`` / ``build_model_gateway_headers`` carry the
employee-id (員編) downstream semantics — moved unchanged.
"""
import re
import time
from threading import Lock
from typing import Optional

from app.config import settings
from app.database import SessionLocal
from app.services import agent_credential_service

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

      1. Per-agent token from ``agent_credentials`` (cached 5 min).
      2. Legacy fleet-shared ``CSP_SERVICE_TOKEN`` env var.
      3. ``None`` — caller chose to skip identity injection.

    Step 1 needs ``target_agent_id`` because we don't know which row
    is "this agent's" without it. When the proxy is forwarding to a
    raw model endpoint (e.g. vLLM, not a registered agent),
    ``target_agent_id`` is ``None`` and we go straight to legacy.
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
        # Fall through to legacy when an agent has no DB credential
        # yet — happens during the migration window before admin runs
        # the cutover for that specific agent.

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
    set, we prefer the per-agent token from ``agent_credentials``; falls
    back to the legacy env-var token when no DB credential exists yet.

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


def _apply_gateway_auth(headers: dict) -> dict:
    """注入出向模型 gateway 的 API key (in-place,並回傳同一 dict)。

    ``MODEL_GATEWAY_API_KEY`` 非空才動作 — 內網拓撲下模型在
    10.53.100.12 的 My-OpenAI-Frontend gateway 後面,/v1 全路由要
    ``Authorization: Bearer``。呼叫端負責 scope:只用在 model 呼叫,
    agent dispatch 不帶 (key 不該外流給第三方 agent)。
    不覆蓋既有 Authorization。
    """
    key = (settings.MODEL_GATEWAY_API_KEY or "").strip()
    if key and "Authorization" not in headers:
        headers["Authorization"] = f"Bearer {key}"
    return headers
