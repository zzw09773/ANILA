"""CSP-owned final Agent dispatch boundary (Gate 5 R3/R4).

The Router is an untrusted transport hop at this boundary.  A signed
ExecutionGrant is verified first, then every mutable authority input is read
again from the CSP database before the per-agent credential is used.  The
service intentionally does not reuse the public ``/v1/chat/completions``
route: that route accepts user/API-key callers and therefore has a different
trust contract.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, unquote

import httpx
from fastapi import HTTPException
from sqlalchemy.orm import Session

from anila_contracts import Classification, ExecutionGrant, StepEvent
from anila_contracts.events import STEP_EVENT_SSE_NAME, StepKind, StepStatus

from app.config import settings
from app.models.agent import Agent
from app.models.auth_session import AuthRefreshToken, AuthSession
from app.models.policy_decision import PolicyDecision
from app.models.service_client import ServiceClient
from app.models.source_snapshot import SourceSnapshot
from app.models.task import Task, TaskRun
from app.models.user import User
from app.services.agent_credential_service import CallerIdentity, get_active_plaintext_for_agent
from app.services.agent_readiness import evaluate_agent_readiness, manifest_sha256
from app.services.agent_registry import build_registry_snapshot
from app.services.auth_service import _assurance_from_session
from app.services.execution_grant_service import (
    ExecutionGrantVerificationError,
    verify_execution_grant_token,
)
from app.services.proxy.guard import _guard_outbound
from app.services.proxy.session_event_store import SqlAlchemySessionEventStore
from app.services.proxy.sse import _aggregate_sse_to_chat_completion, _parse_sse_block
from app.services.proxy.stream_bridge import (
    BridgeContext,
    EventConflictError,
    EventOrderError,
    MAX_EVENT_BYTES,
    StreamBridge,
    TerminalConflictError,
)
from anila_security import ENDPOINT_KIND_AGENT


logger = logging.getLogger(__name__)

_POSITIVE = re.compile(r"\A[1-9][0-9]*\Z")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(slots=True)
class _InvocationLockEntry:
    lock: asyncio.Lock
    users: int = 0


_INVOCATION_LOCKS: dict[str, _InvocationLockEntry] = {}
_INVOCATION_LOCKS_GUARD = asyncio.Lock()
# The durable Agent FileTaskStore remains the final idempotency authority. CSP
# also remembers the accepted resume key for the lifetime of this process so a
# terminal replay with a different key cannot be mistaken for the same retry.
# A future CSP SessionEventRun schema should persist this latch across CSP
# restarts; until then cache misses fail closed only when authority is absent.
_RESUME_IDEMPOTENCY_KEYS: dict[str, str] = {}


def _positive(value: object, *, field: str) -> int:
    if isinstance(value, bool) or _POSITIVE.fullmatch(str(value or "")) is None:
        raise HTTPException(status_code=400, detail=f"{field} 必須是正十進位整數")
    return int(str(value))


def _text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or _CONTROL.search(value):
        raise HTTPException(status_code=400, detail=f"{field} 格式無效")
    return value.strip()


def _header(request_headers: Mapping[str, str], name: str) -> str:
    value = request_headers.get(name.lower()) or request_headers.get(name)
    return _text(value, field=name)


@dataclass(frozen=True, slots=True)
class DispatchBinding:
    caller_user_id: int
    owner_id: int
    task_id: int
    run_id: int
    source_snapshot_id: int
    trace_id: str
    invocation_id: str
    session_id: str
    agent_id: str
    registry_snapshot_id: str
    registry_snapshot_revision: str
    registry_snapshot_hash: str
    manifest_revision: str
    manifest_sha256: str
    grant_id: str
    route_decision_id: str
    policy_decision_id: str
    classification: Classification


@dataclass(frozen=True, slots=True)
class DispatchAuthority:
    caller: CallerIdentity
    user: User
    agent: Agent
    grant: ExecutionGrant
    binding: DispatchBinding
    endpoint_url: str
    # Set by ``authorize_dispatch`` from the current durable TaskRun.  It is
    # intentionally optional for older injected/unit callers; the real CSP
    # sink always supplies it before an outbound claim.
    run_status: str | None = None


def _require_named_router_client(db: Session, caller: CallerIdentity | None) -> ServiceClient:
    if (
        caller is None
        or caller.kind != "service_client"
        or caller.is_legacy
        or caller.service_client_id is None
        or caller.service_client_id <= 0
    ):
        raise HTTPException(status_code=403, detail="Agent dispatch 僅限具名 Router service client")
    client = (
        db.query(ServiceClient)
        .filter(ServiceClient.id == caller.service_client_id)
        .populate_existing()
        .one_or_none()
    )
    if (
        client is None
        or not bool(client.is_active)
        or bool(client.is_legacy)
        or client.revoked_at is not None
        or client.client_type != "router"
        or not isinstance(client.client_name, str)
        or not client.client_name.strip()
    ):
        raise HTTPException(status_code=403, detail="Router service client 未啟用")
    return client


def _durable_auth_session(db: Session, *, grant: ExecutionGrant, caller_user_id: int, now: datetime) -> None:
    sid = grant.auth_assurance.sid
    session = (
        db.query(AuthSession)
        .filter(AuthSession.sid == sid)
        .populate_existing()
        .one_or_none()
    )
    if session is None or session.user_id != caller_user_id or session.revoked_at is not None:
        raise HTTPException(status_code=403, detail="ExecutionGrant AuthSession 無效")
    refresh_rows = (
        db.query(AuthRefreshToken)
        .filter(
            AuthRefreshToken.sid == sid,
            AuthRefreshToken.consumed_at.is_(None),
            AuthRefreshToken.revoked_at.is_(None),
        )
        .all()
    )
    if not any(
        isinstance(row.expires_at, datetime)
        and (row.expires_at.replace(tzinfo=timezone.utc) if row.expires_at.tzinfo is None else row.expires_at) > now
        for row in refresh_rows
    ):
        raise HTTPException(status_code=403, detail="ExecutionGrant refresh family 已過期")
    try:
        methods, acr, auth_time_epoch, break_glass, _ticket, break_glass_expires = _assurance_from_session(session)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=403, detail="ExecutionGrant AuthSession assurance 無效") from exc
    if break_glass_expires is not None:
        expiry = break_glass_expires.replace(tzinfo=timezone.utc) if break_glass_expires.tzinfo is None else break_glass_expires
        if expiry <= now:
            raise HTTPException(status_code=403, detail="ExecutionGrant assurance 已過期")
    if (
        tuple(methods) != tuple(grant.auth_assurance.amr)
        or acr != grant.auth_assurance.acr
        or bool(break_glass) != bool(grant.auth_assurance.break_glass)
        or datetime.fromtimestamp(auth_time_epoch, tz=timezone.utc) != grant.auth_assurance.auth_time
    ):
        raise HTTPException(status_code=403, detail="ExecutionGrant AuthSession binding 不一致")


def _binding_from_request(
    *,
    headers: Mapping[str, str],
    binding_payload: Mapping[str, Any],
    grant: ExecutionGrant,
) -> DispatchBinding:
    def payload_text(name: str) -> str:
        return _text(binding_payload.get(name), field=f"anila_binding.{name}")

    def payload_positive(name: str) -> int:
        return _positive(binding_payload.get(name), field=f"anila_binding.{name}")

    # Headers are CSP-authored correlation facts.  Every one is required and
    # compared to the JSON binding and the signed inner grant below.
    caller_user_id = _positive(_header(headers, "x-anila-caller-user-id"), field="X-ANILA-Caller-User-Id")
    owner_id = _positive(_header(headers, "x-anila-owner-id"), field="X-ANILA-Owner-Id")
    task_id = _positive(_header(headers, "x-anila-task-id"), field="X-ANILA-Task-Id")
    run_id = _positive(_header(headers, "x-anila-run-id"), field="X-ANILA-Run-Id")
    source_id = _positive(_header(headers, "x-anila-source-snapshot-id"), field="X-ANILA-Source-Snapshot-Id")
    text_fields = {
        "trace_id": "X-ANILA-Trace-Id",
        "invocation_id": "X-ANILA-Invocation-Id",
        "session_id": "X-ANILA-Session-Id",
        "agent_id": "X-ANILA-Agent-Id",
        "registry_snapshot_id": "X-ANILA-Registry-Snapshot-Id",
        "registry_snapshot_revision": "X-ANILA-Registry-Snapshot-Revision",
        "registry_snapshot_hash": "X-ANILA-Registry-Snapshot-Hash",
        "manifest_revision": "X-ANILA-Agent-Manifest-Revision",
        "manifest_sha256": "X-ANILA-Agent-Manifest-SHA256",
        "grant_id": "X-ANILA-Execution-Grant-Id",
        "route_decision_id": "X-ANILA-Route-Decision-Id",
        "policy_decision_id": "X-ANILA-Policy-Decision-Id",
    }
    values = {name: _header(headers, header) for name, header in text_fields.items()}
    classification_raw = unquote(_header(headers, "x-anila-classification-level"))
    try:
        classification = Classification.from_storage(classification_raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=403, detail="Agent dispatch classification 無效") from exc

    payload_values: dict[str, object] = {
        "task_id": payload_positive("task_id"),
        "run_id": payload_positive("run_id"),
        "source_snapshot_id": payload_positive("source_snapshot_id"),
        "trace_id": payload_text("trace_id"),
        "invocation_id": payload_text("invocation_id"),
        "session_id": payload_text("session_id"),
        "agent_id": payload_text("agent_id"),
        "registry_snapshot_id": payload_text("registry_snapshot_id"),
        "registry_snapshot_revision": payload_text("registry_snapshot_revision"),
        "registry_snapshot_hash": payload_text("registry_snapshot_hash"),
        "manifest_revision": payload_text("manifest_revision"),
        "manifest_sha256": payload_text("manifest_sha256"),
        "grant_id": payload_text("grant_id"),
        "route_decision_id": payload_text("route_decision_id"),
        "policy_decision_id": payload_text("policy_decision_id"),
    }
    if binding_payload.get("owner_id") is not None:
        payload_values["owner_id"] = payload_positive("owner_id")
    else:
        raise HTTPException(status_code=400, detail="anila_binding.owner_id 缺少")
    for name, expected in (
        ("task_id", task_id),
        ("run_id", run_id),
        ("source_snapshot_id", source_id),
        ("trace_id", values["trace_id"]),
        ("invocation_id", values["invocation_id"]),
        ("session_id", values["session_id"]),
        ("agent_id", values["agent_id"]),
        ("registry_snapshot_id", values["registry_snapshot_id"]),
        ("registry_snapshot_revision", values["registry_snapshot_revision"]),
        ("registry_snapshot_hash", values["registry_snapshot_hash"]),
        ("manifest_revision", values["manifest_revision"]),
        ("manifest_sha256", values["manifest_sha256"]),
        ("grant_id", values["grant_id"]),
        ("route_decision_id", values["route_decision_id"]),
        ("policy_decision_id", values["policy_decision_id"]),
        ("owner_id", owner_id),
    ):
        if payload_values.get(name) != expected:
            raise HTTPException(status_code=403, detail=f"Agent dispatch binding {name} 不一致")

    expected_grant = {
        "task_id": grant.task_id,
        "run_id": grant.run_id,
        "source_snapshot_id": grant.source_snapshot_id,
        "trace_id": grant.trace_id,
        "invocation_id": grant.invocation_id,
        "session_id": grant.session_id,
        "agent_id": grant.target.id,
        "registry_snapshot_id": grant.registry_snapshot_id,
        "manifest_revision": grant.manifest_revision,
        "grant_id": grant.grant_id,
        "route_decision_id": grant.route_decision_id,
        "policy_decision_id": grant.policy_decision_id,
    }
    for name, expected in expected_grant.items():
        if payload_values.get(name) != expected or values.get(name) != expected:
            raise HTTPException(status_code=403, detail=f"ExecutionGrant binding {name} 不一致")
    if grant.classification is not classification:
        raise HTTPException(status_code=403, detail="ExecutionGrant classification binding 不一致")
    if grant.session_id != values["session_id"]:
        raise HTTPException(status_code=403, detail="ExecutionGrant session binding 不一致")
    if values["manifest_revision"] != f"sha256:{values['manifest_sha256']}":
        raise HTTPException(status_code=403, detail="manifest revision/hash binding 不一致")
    if values["registry_snapshot_id"] != values["registry_snapshot_revision"] or values["registry_snapshot_id"] != values["registry_snapshot_hash"]:
        raise HTTPException(status_code=403, detail="registry snapshot generation binding 不一致")
    return DispatchBinding(
        caller_user_id=caller_user_id,
        owner_id=owner_id,
        task_id=task_id,
        run_id=run_id,
        source_snapshot_id=source_id,
        trace_id=values["trace_id"],
        invocation_id=values["invocation_id"],
        session_id=values["session_id"],
        agent_id=values["agent_id"],
        registry_snapshot_id=values["registry_snapshot_id"],
        registry_snapshot_revision=values["registry_snapshot_revision"],
        registry_snapshot_hash=values["registry_snapshot_hash"],
        manifest_revision=values["manifest_revision"],
        manifest_sha256=values["manifest_sha256"],
        grant_id=values["grant_id"],
        route_decision_id=values["route_decision_id"],
        policy_decision_id=values["policy_decision_id"],
        classification=classification,
    )


def authorize_dispatch(
    db: Session,
    *,
    caller: CallerIdentity | None,
    headers: Mapping[str, str],
    binding_payload: Mapping[str, Any],
    grant_token: str,
) -> DispatchAuthority:
    """Verify signed token and re-read all current CSP authority inputs."""

    _require_named_router_client(db, caller)
    if headers.get("authorization") or headers.get("Authorization"):
        raise HTTPException(status_code=403, detail="Agent dispatch 禁止 inbound bearer")
    try:
        grant = verify_execution_grant_token(grant_token)
    except ExecutionGrantVerificationError as exc:
        raise HTTPException(status_code=403, detail="ExecutionGrant token 無效") from exc
    binding = _binding_from_request(headers=headers, binding_payload=binding_payload, grant=grant)
    if binding.caller_user_id != binding.owner_id:
        raise HTTPException(status_code=403, detail="caller/owner binding 不一致")
    user = (
        db.query(User)
        .filter(User.id == binding.caller_user_id, User.is_active.is_(True), User.is_approved.is_(True))
        .one_or_none()
    )
    if user is None:
        raise HTTPException(status_code=403, detail="caller user 未核准或已停用")
    _durable_auth_session(db, grant=grant, caller_user_id=user.id, now=datetime.now(timezone.utc))

    task = (
        db.query(Task).filter(Task.id == binding.task_id).populate_existing().with_for_update().one_or_none()
    )
    run = (
        db.query(TaskRun).filter(TaskRun.id == binding.run_id, TaskRun.task_id == binding.task_id).populate_existing().with_for_update().one_or_none()
    )
    snapshot = (
        db.query(SourceSnapshot)
        .filter(SourceSnapshot.id == binding.source_snapshot_id, SourceSnapshot.task_id == binding.task_id)
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if task is None or run is None or snapshot is None:
        raise HTTPException(status_code=409, detail="Task/TaskRun/SourceSnapshot 不存在")
    if task.requester_user_id != user.id or task.status not in {"running", "completed"}:
        raise HTTPException(status_code=403, detail="Task owner/status 不允許 dispatch")
    if task.trace_id != binding.trace_id or task.source_snapshot_id != binding.source_snapshot_id:
        raise HTTPException(status_code=403, detail="Task trace/source binding 不一致")
    if run.status not in {"running", "completed"} or run.dispatch_target != "agent":
        raise HTTPException(status_code=409, detail="TaskRun 非 Agent dispatch")
    try:
        task_level = Classification.from_storage(task.classification_level)
        run_level = Classification.from_storage(run.classification_level)
        source_level = Classification.from_storage(snapshot.classification_level)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=403, detail="Task classification 狀態無效") from exc
    if grant.classification is not task_level or grant.classification is not run_level or source_level > task_level:
        raise HTTPException(status_code=403, detail="Task/Run/Source classification binding 不一致")

    # Re-read the append-only policy decision attached to the Task.  A denied
    # or replaced policy row invalidates an otherwise valid signed envelope.
    if task.policy_decision_id is None:
        raise HTTPException(status_code=403, detail="Task 缺少 durable policy decision")
    policy = db.get(PolicyDecision, task.policy_decision_id)
    if policy is None or policy.decision != "allow" or policy.task_id != task.id:
        raise HTTPException(status_code=403, detail="current policy decision 不允許 dispatch")
    if policy.action not in {"task.run", "agent.invoke", "model.invoke"}:
        raise HTTPException(status_code=403, detail="current policy action 不允許 dispatch")

    try:
        current = build_registry_snapshot(db, user_id=user.id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=403, detail="current registry snapshot 無效") from exc
    if current.expires_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=403, detail="registry snapshot 已過期")
    if (
        current.snapshot_id != binding.registry_snapshot_id
        or current.snapshot_revision != binding.registry_snapshot_revision
        or current.snapshot_hash != binding.registry_snapshot_hash
    ):
        raise HTTPException(status_code=409, detail="registry snapshot 已變更")
    entries = [entry for entry in current.agents if entry.agent_id == binding.agent_id]
    if len(entries) != 1:
        raise HTTPException(status_code=403, detail="Agent 不在 current registry")
    entry = entries[0]
    agent = db.query(Agent).filter(Agent.id == entry.registry_id, Agent.name == binding.agent_id).populate_existing().with_for_update().one_or_none()
    if agent is None:
        raise HTTPException(status_code=403, detail="Agent registry binding 無效")
    if (
        not entry.ready_for_dispatch
        or not entry.manifest_valid
        or not entry.endpoint_via_csp
        or entry.model_gateway != "csp"
        or entry.manifest is None
        or entry.manifest_revision != binding.manifest_revision
        or entry.manifest_sha256 != binding.manifest_sha256
        or grant.target.id != entry.agent_id
        or grant.target.model_binding != entry.manifest.model_binding
    ):
        raise HTTPException(status_code=403, detail="Agent readiness/manifest binding 不一致")
    try:
        if manifest_sha256(entry.manifest) != binding.manifest_sha256:
            raise HTTPException(status_code=403, detail="Agent manifest hash drift")
        readiness = evaluate_agent_readiness(agent, db=db)
    except (TypeError, ValueError):
        raise HTTPException(status_code=403, detail="Agent readiness recheck 失敗") from None
    if not readiness.ready_for_dispatch:
        raise HTTPException(status_code=403, detail="Agent readiness 已失效")
    try:
        _guard_outbound(agent.endpoint_url.rstrip("/") + "/v1/chat/completions", endpoint_kind=ENDPOINT_KIND_AGENT)
    except Exception as exc:
        raise HTTPException(status_code=403, detail="Agent endpoint 不在 CSP allow-list") from exc
    return DispatchAuthority(
        caller=caller,  # type: ignore[arg-type]
        user=user,
        agent=agent,
        grant=grant,
        binding=binding,
        endpoint_url=agent.endpoint_url.rstrip("/") + "/v1/chat/completions",
        run_status=run.status,
    )


def build_agent_outbound_headers(
    db: Session,
    authority: DispatchAuthority,
    *,
    grant_token: str,
    idempotency_key: str | None = None,
) -> dict[str, str]:
    """Build CSP-authored, per-agent-only outbound headers."""

    token = get_active_plaintext_for_agent(db, agent_id=authority.agent.id)
    if not token:
        raise HTTPException(status_code=403, detail="Agent 缺少 active per-agent credential")
    b = authority.binding
    return {
        "Content-Type": "application/json",
        "X-CSP-Service-Token": token,
        "X-ANILA-Agent-Id": authority.agent.name,
        "X-ANILA-Caller-User-Id": str(b.caller_user_id),
        "X-ANILA-Owner-Id": str(b.owner_id),
        "X-ANILA-Task-Id": str(b.task_id),
        "X-ANILA-Run-Id": str(b.run_id),
        "X-ANILA-Source-Snapshot-Id": str(b.source_snapshot_id),
        "X-ANILA-Trace-Id": b.trace_id,
        "X-ANILA-Invocation-Id": b.invocation_id,
        "X-ANILA-Session-Id": b.session_id,
        "X-ANILA-Registry-Snapshot-Id": b.registry_snapshot_id,
        "X-ANILA-Registry-Snapshot-Revision": b.registry_snapshot_revision,
        "X-ANILA-Registry-Snapshot-Hash": b.registry_snapshot_hash,
        "X-ANILA-Agent-Manifest-Revision": b.manifest_revision,
        "X-ANILA-Agent-Manifest-SHA256": b.manifest_sha256,
        "X-ANILA-Execution-Grant-Id": b.grant_id,
        "X-ANILA-Execution-Grant": grant_token,
        "X-ANILA-Route-Decision-Id": b.route_decision_id,
        "X-ANILA-Policy-Decision-Id": b.policy_decision_id,
        "X-ANILA-Classification-Level": quote(b.classification.to_storage(), safe=""),
        # Initial dispatches use the invocation id as their exactly-once key;
        # an approve/resume call supplies its own CSP-issued retry key.
        "X-ANILA-Idempotency-Key": idempotency_key or b.invocation_id,
    }


def _bridge(authority: DispatchAuthority, db: Session) -> StreamBridge:
    b = authority.binding
    return StreamBridge(
        BridgeContext(
            task_id=str(b.task_id),
            trace_id=b.trace_id,
            agent_id=b.agent_id,
            session_id=b.session_id,
            run_id=str(b.run_id),
            classification=b.classification,
            invocation_id=b.invocation_id,
            registry_snapshot_id=b.registry_snapshot_id,
            manifest_revision=b.manifest_revision,
            manifest_sha256=b.manifest_sha256,
            grant_id=b.grant_id,
            source_snapshot_id=b.source_snapshot_id,
            route_decision_id=b.route_decision_id,
            policy_decision_id=b.policy_decision_id,
        ),
        store=SqlAlchemySessionEventStore(db),
    )


def _terminal_payload(bridge: StreamBridge) -> dict[str, Any] | None:
    event = bridge.terminal_event()
    if event is None:
        return None
    return {
        "choices": [{"index": 0, "message": {"role": "assistant", "content": event.safe_output_summary or ""}, "finish_reason": "stop"}],
        "anila_meta": {"terminal_event_id": event.event_id, "status": event.status.value},
    }


def _reject_completed_without_terminal(
    authority: DispatchAuthority, bridge: StreamBridge
) -> None:
    """Do not turn a completed TaskRun into a fresh Agent invocation.

    ``authorize_dispatch`` permits ``completed`` only so a caller can replay
    its durable terminal result.  A completed run with no terminal is an
    inconsistent/partial state and must fail closed before ``claim_dispatch``
    or any Agent network I/O.
    """

    if authority.run_status == "completed" and bridge.terminal_event() is None:
        raise HTTPException(
            status_code=409,
            detail="completed TaskRun 缺少 durable terminal replay",
        )


def _extract_agent_content(payload: object) -> str:
    """Extract only the bounded assistant content from an Agent response."""

    if not isinstance(payload, Mapping):
        raise ValueError("Agent response 必須是 JSON object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
        raise ValueError("Agent response choices 無效")
    message = choices[0].get("message")
    if not isinstance(message, Mapping) or not isinstance(message.get("content"), str):
        raise ValueError("Agent response content 無效")
    return message["content"]


def _append_agent_event_history(
    bridge: StreamBridge,
    payload: object,
) -> tuple[StepEvent, ...]:
    """Rebind the Agent's durable event projection into CSP's event ledger.

    The Agent response is never trusted as an authority object.  Its
    ``anila_events`` list is only a replay hint; :class:`StreamBridge` validates
    every StepEvent, overwrites governance identity/cursor fields from the
    already-authorized binding and applies the durable idempotency/terminal
    latches.  A malformed projection therefore fails closed before a response
    is returned to Router.
    """

    if not isinstance(payload, Mapping):
        return ()
    raw_events = payload.get("anila_events")
    if raw_events is None:
        return ()
    if not isinstance(raw_events, list):
        raise ValueError("Agent anila_events 必須是 list")
    for raw_event in raw_events:
        if not isinstance(raw_event, Mapping):
            raise ValueError("Agent anila_events item 必須是 object")
        bridge.append(
            STEP_EVENT_SSE_NAME,
            json.dumps(dict(raw_event), ensure_ascii=False, separators=(",", ":")),
        )
    return bridge.replay_events()


def _paused_payload(authority: DispatchAuthority) -> dict[str, Any]:
    """Return a non-terminal HITL response without fabricating completion."""

    b = authority.binding
    return {
        "object": "anila.task",
        "task_id": str(b.task_id),
        "run_id": str(b.run_id),
        "session_id": b.session_id,
        "invocation_id": b.invocation_id,
        "status": "paused",
        "anila_meta": {
            "task_id": str(b.task_id),
            "run_id": str(b.run_id),
            "session_id": b.session_id,
            "invocation_id": b.invocation_id,
            "status": "paused",
        },
    }


def _bridge_is_paused(bridge: StreamBridge) -> bool:
    """Return true when the newest durable event is BLOCKED and non-terminal."""

    events = bridge.replay_events()
    return bool(events and events[-1].status is StepStatus.BLOCKED and bridge.terminal_event() is None)


def _canonical_nonstream_response(
    *,
    content: str,
    authority: DispatchAuthority,
    terminal_event: StepEvent,
) -> dict[str, Any]:
    """Rebuild a minimal CSP-authored OpenAI response; never echo Agent JSON."""

    return {
        "id": f"chatcmpl-{authority.binding.invocation_id}",
        "object": "chat.completion",
        "model": authority.agent.name,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "anila_meta": {
            "terminal_event_id": terminal_event.event_id,
            "status": terminal_event.status.value,
        },
    }


def _step_frame(event: StepEvent) -> str:
    """Render one canonical timeline event after CSP validation."""

    return f"event: {STEP_EVENT_SSE_NAME}\ndata: {event.model_dump_json()}\n\n"


def _openai_content_frame(event: StepEvent, *, model: str) -> str | None:
    """Render a sanitized OpenAI chunk from a trusted StepEvent only.

    Agent bytes are never copied into this frame.  The only content admitted
    is the CSP-rebound safe output summary that has already passed the Gate 4
    validator and durable store append.
    """

    if event.status in {
        StepStatus.COMPLETED,
        StepStatus.FAILED,
        StepStatus.CANCELLED,
        StepStatus.BLOCKED,
    }:
        return None
    content = event.safe_output_summary
    if not isinstance(content, str) or not content:
        return None
    payload = {
        "id": f"chatcmpl-{event.event_id}",
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"content": content},
                "finish_reason": None,
            }
        ],
    }
    return "data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n\n"


def _frame_event(frame: str) -> StepEvent | None:
    """Parse only a frame authored by ``StreamBridge``."""

    _event_name, data = _parse_sse_block(frame)
    if _event_name != STEP_EVENT_SSE_NAME or not data:
        return None
    try:
        parsed = StepEvent.model_validate(json.loads(data))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return parsed


def _append_agent_step(
    bridge: StreamBridge,
    *,
    data: str,
    model: str,
    source_sequence: int,
) -> tuple[list[str], int]:
    """Validate/store one named ``anila.step`` and expose sanitized content."""

    if len(data.encode("utf-8")) > MAX_EVENT_BYTES:
        # Route through the validator so oversize input is audited/dropped;
        # do not log or return the untrusted payload.
        bridge.append(STEP_EVENT_SSE_NAME, data)
        return [], source_sequence
    rendered = bridge.append(STEP_EVENT_SSE_NAME, data)
    if not isinstance(rendered, str):
        return [], source_sequence
    event = _frame_event(rendered)
    if event is None:
        return [], source_sequence
    next_sequence = max(source_sequence, int(event.sequence) + 1)
    frames = [rendered]
    content_frame = _openai_content_frame(event, model=model)
    if content_frame is not None:
        frames.append(content_frame)
    return frames, next_sequence


def _append_openai_content(
    bridge: StreamBridge,
    *,
    content: str,
    model: str,
    source_sequence: int,
) -> tuple[list[str], int]:
    """Convert an OpenAI delta into a trusted StepEvent before forwarding."""

    if not content:
        return [], source_sequence
    event = StepEvent(
        event_id=f"{bridge.context.invocation_id or bridge.context.run_id}:delta:{source_sequence}",
        sequence=source_sequence,
        cursor=f"agent:{source_sequence}",
        trace_id=bridge.context.trace_id,
        task_id=bridge.context.task_id,
        session_id=bridge.context.session_id,
        invocation_id=bridge.context.invocation_id or bridge.context.run_id,
        run_id=bridge.context.run_id,
        step_id=f"agent:{bridge.context.agent_id}",
        kind=StepKind.AGENT,
        status=StepStatus.RUNNING,
        safe_output_summary=content,
        agent_id=bridge.context.agent_id,
        classification=bridge.context.classification,
    )
    rendered = bridge.append(STEP_EVENT_SSE_NAME, event.model_dump_json())
    if not isinstance(rendered, str):
        return [], source_sequence + 1
    canonical = _frame_event(rendered)
    if canonical is None:
        return [], source_sequence + 1
    frames = [rendered]
    content_frame = _openai_content_frame(canonical, model=model)
    if content_frame is not None:
        frames.append(content_frame)
    return frames, source_sequence + 1


def _normalize_agent_block(
    bridge: StreamBridge,
    *,
    block: str,
    model: str,
    source_sequence: int,
) -> tuple[list[str], int]:
    """Normalize every Agent SSE block through the CSP StreamBridge.

    Unknown/malformed/meta frames are deliberately sent to the validator's
    drop/audit path.  No Agent-authored block is ever yielded verbatim.
    """

    event_name, data = _parse_sse_block(block)
    if len(block.encode("utf-8")) > MAX_EVENT_BYTES:
        bridge.append(event_name or "message", data or "")
        return [], source_sequence
    if event_name == STEP_EVENT_SSE_NAME:
        if data is None:
            bridge.append(event_name, None)
            return [], source_sequence
        return _append_agent_step(
            bridge,
            data=data,
            model=model,
            source_sequence=source_sequence,
        )
    if data is None or data == "[DONE]":
        if event_name not in (None, "message"):
            bridge.append(event_name, data)
        return [], source_sequence
    # OpenAI-compatible ``message``/unnamed data is parsed, then only its
    # delta content is converted to a trusted StepEvent.  All other fields
    # (including an Agent-authored meta object) are discarded.
    if event_name in (None, "message"):
        try:
            payload = json.loads(data)
        except (json.JSONDecodeError, TypeError, ValueError):
            bridge.append(event_name or "message", data)
            return [], source_sequence
        choices = payload.get("choices") if isinstance(payload, Mapping) else None
        if not isinstance(choices, list) or not choices:
            bridge.append(event_name or "message", data)
            return [], source_sequence
        frames: list[str] = []
        next_sequence = source_sequence
        for choice in choices:
            if not isinstance(choice, Mapping):
                continue
            delta = choice.get("delta") or choice.get("message") or {}
            if not isinstance(delta, Mapping):
                continue
            content = delta.get("content")
            if not isinstance(content, str):
                continue
            new_frames, next_sequence = _append_openai_content(
                bridge,
                content=content,
                model=model,
                source_sequence=next_sequence,
            )
            frames.extend(new_frames)
        if not frames:
            # No content is still not permission to forward the original
            # payload.  Keep the validator/audit path for malformed shapes.
            bridge.append(event_name or "message", data)
        return frames, next_sequence
    # ``anila.meta`` and every other named event are untrusted and dropped.
    bridge.append(event_name, data)
    return [], source_sequence


async def _invocation_lock(invocation_id: str) -> _InvocationLockEntry:
    async with _INVOCATION_LOCKS_GUARD:
        entry = _INVOCATION_LOCKS.get(invocation_id)
        if entry is None:
            entry = _InvocationLockEntry(lock=asyncio.Lock())
            _INVOCATION_LOCKS[invocation_id] = entry
        entry.users += 1
        return entry


@asynccontextmanager
async def _invocation_guard(invocation_id: str):
    """Acquire one process-local invocation lock and retire it afterwards."""

    entry = await _invocation_lock(invocation_id)
    acquired = False
    try:
        await entry.lock.acquire()
        acquired = True
        yield entry.lock
    finally:
        if acquired:
            entry.lock.release()
        async with _INVOCATION_LOCKS_GUARD:
            entry.users -= 1
            if _INVOCATION_LOCKS.get(invocation_id) is entry and entry.users == 0:
                _INVOCATION_LOCKS.pop(invocation_id, None)


async def dispatch_nonstream(
    *,
    db: Session,
    authority: DispatchAuthority,
    messages: list[dict[str, Any]],
    grant_token: str,
) -> dict[str, Any]:
    async with _invocation_guard(authority.binding.invocation_id):
        bridge = _bridge(authority, db)
        _reject_completed_without_terminal(authority, bridge)
        existing = _terminal_payload(bridge)
        if existing is not None:
            return existing
        claim_dispatch = getattr(bridge.store, "claim_dispatch", None)
        if callable(claim_dispatch):
            try:
                claimed = bool(claim_dispatch(binding=bridge.context))
            except Exception as exc:
                db.rollback()
                raise HTTPException(status_code=409, detail="Agent invocation claim conflict") from exc
            if not claimed:
                db.rollback()
                existing = _terminal_payload(bridge)
                if existing is not None:
                    return existing
                raise HTTPException(status_code=409, detail="Agent invocation 已在執行")
        headers = build_agent_outbound_headers(db, authority, grant_token=grant_token)
        body = {
            "model": authority.agent.name,
            "messages": messages,
            "stream": False,
            "anila_session_id": authority.binding.session_id,
            "anila_binding": {
                "task_id": authority.binding.task_id,
                "run_id": authority.binding.run_id,
                "source_snapshot_id": authority.binding.source_snapshot_id,
                "trace_id": authority.binding.trace_id,
                "invocation_id": authority.binding.invocation_id,
                "session_id": authority.binding.session_id,
                "owner_id": authority.binding.owner_id,
                "agent_id": authority.binding.agent_id,
                "registry_snapshot_id": authority.binding.registry_snapshot_id,
                "registry_snapshot_revision": authority.binding.registry_snapshot_revision,
                "registry_snapshot_hash": authority.binding.registry_snapshot_hash,
                "manifest_revision": authority.binding.manifest_revision,
                "manifest_sha256": authority.binding.manifest_sha256,
                "grant_id": authority.binding.grant_id,
                "route_decision_id": authority.binding.route_decision_id,
                "policy_decision_id": authority.binding.policy_decision_id,
            },
        }
        try:
            async with httpx.AsyncClient(timeout=float(settings.LLM_TIMEOUT)) as client:
                response = await client.post(authority.endpoint_url, json=body, headers=headers)
                # Official Agent uses HTTP 202 to report a durable HITL pause.
                # It is deliberately not an OpenAI completion and must never
                # enter the ordinary content/terminal path.
                if getattr(response, "status_code", 200) == 202:
                    payload = response.json()
                    _append_agent_event_history(bridge, payload)
                    # The HTTP status/payload flag is not durable authority.
                    # CSP only exposes a pause after an actual BLOCKED event
                    # has been rebound into its SessionEventStore.
                    if _bridge_is_paused(bridge):
                        return _paused_payload(authority)
                    raise ValueError("Agent 202 response 缺少 durable blocked event")
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                raw = response.text
                payload = (
                    _aggregate_sse_to_chat_completion(raw, authority.agent.name)
                    if "text/event-stream" in content_type or raw.lstrip().startswith("data:")
                    else response.json()
                )
        except HTTPException:
            raise
        except Exception as exc:
            try:
                bridge.append_terminal(StepStatus.FAILED, safe_output_summary="Agent 呼叫失敗")
            except Exception:
                db.rollback()
            raise HTTPException(status_code=502, detail="Agent 呼叫失敗，已安全終止") from exc
        try:
            _append_agent_event_history(bridge, payload)
            if _bridge_is_paused(bridge):
                return _paused_payload(authority)
            content = _extract_agent_content(payload)
            # append_terminal enforces the same bounded safe-summary and
            # secret policy as streamed content.  Invalid output is terminal
            # failure, never a raw response passthrough.
            terminal_receipt = bridge.terminal_event()
            if terminal_receipt is None:
                terminal_receipt = bridge.append_terminal(
                    StepStatus.COMPLETED, safe_output_summary=content
                )
            elif terminal_receipt.status is not StepStatus.COMPLETED:
                return _terminal_payload(bridge) or _paused_payload(authority)
        except (EventConflictError, EventOrderError, TerminalConflictError) as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail="Agent dispatch terminal conflict") from exc
        except Exception as exc:
            try:
                bridge.append_terminal(
                    StepStatus.FAILED, safe_output_summary="Agent 回應驗證失敗"
                )
            except Exception:
                db.rollback()
            raise HTTPException(status_code=502, detail="Agent 回應未通過 CSP 驗證") from exc
    return _canonical_nonstream_response(
        content=content,
        authority=authority,
        terminal_event=terminal_receipt.event,
    )


async def dispatch_resume(
    *,
    db: Session,
    authority: DispatchAuthority,
    grant_token: str,
    idempotency_key: str,
    interrupt_id: str | None = None,
    answer: Any = None,
) -> dict[str, Any]:
    """Perform the formal CSP→Agent approve/resume transition.

    Resume is intentionally a sibling of normal dispatch, not a call to the
    legacy public ``/v1/agents/{id}/sessions/...`` proxy.  CSP reuses the same
    complete authority binding/grant, checks the durable blocked cursor before
    network I/O, sends only the per-agent ``csk-`` plus correlation/grant
    headers, then revalidates the Agent's canonical event projection into the
    CSP-owned :class:`SessionEventStore`.
    """

    key = _text(idempotency_key, field="X-ANILA-Idempotency-Key")
    if len(key) > 255:
        raise HTTPException(status_code=400, detail="X-ANILA-Idempotency-Key 過長")
    async with _invocation_guard(authority.binding.invocation_id):
        bridge = _bridge(authority, db)
        _reject_completed_without_terminal(authority, bridge)
        previous_key = _RESUME_IDEMPOTENCY_KEYS.get(authority.binding.run_id)
        if previous_key is not None and previous_key != key:
            raise HTTPException(status_code=409, detail="resume idempotency key conflicts with run")
        existing_terminal = bridge.terminal_event()
        if existing_terminal is not None:
            # Exact retries replay the one durable terminal without touching
            # Agent again.  A conflicting key cannot mutate this latch.
            return _terminal_payload(bridge) or _paused_payload(authority)
        if not _bridge_is_paused(bridge):
            raise HTTPException(status_code=409, detail="Task 尚未處於可恢復的 blocked 狀態")
        _RESUME_IDEMPOTENCY_KEYS[authority.binding.run_id] = key

        headers = build_agent_outbound_headers(
            db,
            authority,
            grant_token=grant_token,
            idempotency_key=key,
        )
        # R5 is an explicit binary approval seam.  The Agent validates this
        # mode and approves the persisted pending interruptions; no client-
        # supplied interrupt id/answer is accepted or silently ignored.
        if interrupt_id is not None or answer is not None:
            raise HTTPException(status_code=400, detail="resume 不接受 interrupt_id/answer")
        body: dict[str, Any] = {"approval_mode": "approve_all"}
        target = authority.agent.endpoint_url.rstrip("/") + f"/v1/tasks/{authority.binding.task_id}/approve"
        try:
            async with httpx.AsyncClient(timeout=float(settings.LLM_TIMEOUT)) as client:
                response = await client.post(target, json=body, headers=headers)
                if response.status_code >= 400:
                    # The Agent's 409 is the durable idempotency/lifecycle
                    # conflict contract.  Preserve that status; other
                    # downstream failures are hidden behind a safe 502.
                    if response.status_code == 409:
                        raise HTTPException(status_code=409, detail="Agent resume idempotency/lifecycle conflict")
                    if response.status_code in {401, 403}:
                        raise HTTPException(status_code=502, detail="Agent resume authentication failed")
                    raise HTTPException(status_code=502, detail="Agent resume failed")
                payload = response.json()
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=502, detail="Agent resume failed") from exc

        try:
            _append_agent_event_history(bridge, payload)
            # A 202/status marker without a canonical BLOCKED event is
            # malformed and must not create a resumable cursor.
            if _bridge_is_paused(bridge):
                return _paused_payload(authority)
            if getattr(response, "status_code", 200) == 202 or (
                isinstance(payload, Mapping) and payload.get("status") == "paused"
            ):
                raise ValueError("Agent resume pause response 缺少 durable blocked event")

            content = _extract_agent_content(payload)
            terminal_receipt = bridge.terminal_event()
            if terminal_receipt is None:
                terminal_receipt = bridge.append_terminal(
                    StepStatus.COMPLETED, safe_output_summary=content
                )
            elif terminal_receipt.status is not StepStatus.COMPLETED:
                return _terminal_payload(bridge) or _paused_payload(authority)
        except (EventConflictError, EventOrderError, TerminalConflictError) as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail="Agent resume event conflict") from exc
        except Exception as exc:
            try:
                if bridge.terminal_event() is None:
                    bridge.append_terminal(
                        StepStatus.FAILED, safe_output_summary="Agent resume response 驗證失敗"
                    )
            except Exception:
                db.rollback()
            raise HTTPException(status_code=502, detail="Agent resume response 未通過 CSP 驗證") from exc
        terminal_event = (
            terminal_receipt.event
            if hasattr(terminal_receipt, "event")
            else terminal_receipt
        )
        return _canonical_nonstream_response(
            content=content,
            authority=authority,
            terminal_event=terminal_event,
        )


async def dispatch_stream(
    *,
    db: Session,
    authority: DispatchAuthority,
    messages: list[dict[str, Any]],
    grant_token: str,
    after_cursor: int = 0,
) -> AsyncIterator[str]:
    async with _invocation_guard(authority.binding.invocation_id):
        # Refresh SQLAlchemy state only after taking the process-local guard;
        # a duplicate caller therefore waits for the winner's durable commit
        # before deciding whether to replay or claim a downstream call.
        bridge = _bridge(authority, db)
        _reject_completed_without_terminal(authority, bridge)
        existing_events = bridge.replay_events()
        if existing_events:
            for event in existing_events:
                if int(event.cursor) <= after_cursor:
                    continue
                yield _step_frame(event)
                content_frame = _openai_content_frame(event, model=authority.agent.name)
                if content_frame is not None:
                    yield content_frame
            if bridge.terminal_event() is not None:
                yield "data: [DONE]\n\n"
            return
        claim_dispatch = getattr(bridge.store, "claim_dispatch", None)
        if callable(claim_dispatch):
            try:
                claimed = bool(claim_dispatch(binding=bridge.context))
            except Exception as exc:
                db.rollback()
                raise HTTPException(status_code=409, detail="Agent invocation claim conflict") from exc
            if not claimed:
                db.rollback()
                for event in bridge.replay_events(after_cursor=after_cursor):
                    yield _step_frame(event)
                    content_frame = _openai_content_frame(event, model=authority.agent.name)
                    if content_frame is not None:
                        yield content_frame
                if bridge.terminal_event() is not None:
                    yield "data: [DONE]\n\n"
                return
        headers = build_agent_outbound_headers(db, authority, grant_token=grant_token)
        headers["X-ANILA-After-Cursor"] = str(after_cursor)
        body = {
            "model": authority.agent.name,
            "messages": messages,
            "stream": True,
            "anila_session_id": authority.binding.session_id,
            "anila_binding": {
                "task_id": authority.binding.task_id,
                "run_id": authority.binding.run_id,
                "source_snapshot_id": authority.binding.source_snapshot_id,
                "trace_id": authority.binding.trace_id,
                "invocation_id": authority.binding.invocation_id,
                "session_id": authority.binding.session_id,
                "owner_id": authority.binding.owner_id,
                "agent_id": authority.binding.agent_id,
                "registry_snapshot_id": authority.binding.registry_snapshot_id,
                "registry_snapshot_revision": authority.binding.registry_snapshot_revision,
                "registry_snapshot_hash": authority.binding.registry_snapshot_hash,
                "manifest_revision": authority.binding.manifest_revision,
                "manifest_sha256": authority.binding.manifest_sha256,
                "grant_id": authority.binding.grant_id,
                "route_decision_id": authority.binding.route_decision_id,
                "policy_decision_id": authority.binding.policy_decision_id,
            },
        }
        try:
            async with httpx.AsyncClient(timeout=float(settings.LLM_TIMEOUT)) as client:
                async with client.stream("POST", authority.endpoint_url, json=body, headers=headers) as response:
                    if response.status_code >= 400:
                        await response.aread()
                        raise HTTPException(status_code=502, detail="Agent 呼叫失敗，已安全終止")
                    lines: list[str] = []
                    source_sequence = 1
                    async for line in response.aiter_lines():
                        if line == "":
                            if not lines:
                                continue
                            block = "\n".join(lines)
                            lines = []
                            if _parse_sse_block(block)[1] == "[DONE]":
                                continue
                            frames, source_sequence = _normalize_agent_block(
                                bridge,
                                block=block,
                                model=authority.agent.name,
                                source_sequence=source_sequence,
                            )
                            for frame in frames:
                                yield frame
                            continue
                        lines.append(line)
                    if lines:
                        block = "\n".join(lines)
                        if _parse_sse_block(block)[1] != "[DONE]":
                            frames, _source_sequence = _normalize_agent_block(
                                bridge,
                                block=block,
                                model=authority.agent.name,
                                source_sequence=source_sequence,
                            )
                            for frame in frames:
                                yield frame
        except asyncio.CancelledError:
            try:
                bridge.append_terminal(StepStatus.CANCELLED, safe_output_summary="執行已取消")
            except Exception:
                db.rollback()
            raise
        except HTTPException:
            try:
                bridge.append_terminal(StepStatus.FAILED, safe_output_summary="Agent 呼叫失敗")
            except Exception:
                db.rollback()
            raise
        except Exception as exc:
            try:
                bridge.append_terminal(StepStatus.FAILED, safe_output_summary="Agent stream 失敗")
            except Exception:
                db.rollback()
            raise HTTPException(status_code=502, detail="Agent stream 失敗，已安全終止") from exc
        try:
            existing_terminal = bridge.terminal_event()
            # ``BLOCKED`` is a resumable pause, not a terminal state.  The
            # downstream Agent already emitted ``anila.step`` + ``[DONE]``;
            # writing a synthetic COMPLETED here would permanently destroy
            # HITL semantics and make approve/resume impossible.
            if existing_terminal is None and not _bridge_is_paused(bridge):
                terminal_receipt = bridge.append_terminal(
                    StepStatus.COMPLETED, safe_output_summary="執行完成"
                )
                yield _step_frame(terminal_receipt.event)
        except (EventConflictError, EventOrderError, TerminalConflictError) as exc:
            raise HTTPException(status_code=409, detail="Agent stream terminal conflict") from exc
        yield "data: [DONE]\n\n"


__all__ = [
    "DispatchAuthority",
    "DispatchBinding",
    "authorize_dispatch",
    "build_agent_outbound_headers",
    "dispatch_nonstream",
    "dispatch_stream",
]
