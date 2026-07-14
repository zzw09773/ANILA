"""CSP authority for minting and verifying ExecutionGrant envelopes.

The Router supplies unsigned, structured evidence.  CSP re-reads every
durable authority input that it owns (Task/TaskRun, SourceSnapshot,
AuthSession and the caller-scoped Agent registry) before signing a short-lived
JWT.  The transport JWT is deliberately separate from the inner
``anila-contracts`` grant so downstream consumers can verify it with the CSP
JWKS without importing the CSP application.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, Final, NoReturn

from anila_contracts import ExecutionGrant, PolicyGateResult, RouteDecision
from anila_contracts._types import InvocationTargetKind
from anila_contracts.agents import ManifestCapabilities
from anila_contracts.classification import Classification, ClassificationLevel
from anila_contracts.contexts import AuthAssurance
from anila_contracts.routing import RouteType
from jose import JWTError, jwt
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.config import settings
from app.models.auth_session import AuthRefreshToken, AuthSession
from app.models.service_client import ServiceClient
from app.models.source_snapshot import SourceSnapshot
from app.models.task import Task, TaskRun
from app.models.user import User
from app.schemas.agent_registry import AgentRegistryEntry, AgentRegistrySnapshot
from app.schemas.execution_grant import ExecutionGrantMintRequest, ExecutionGrantMintResponse
from app.services.agent_credential_service import CallerIdentity
from app.services.agent_registry import build_registry_snapshot
from app.services.agent_readiness import manifest_sha256
from app.services.auth_service import _assurance_from_session
from app.utils.security import ALGORITHM, get_private_key, get_public_key


EXECUTION_GRANT_AUDIENCE: Final[str] = "anila-execution-grant"
EXECUTION_GRANT_TYPE: Final[str] = "anila-execution-grant"
EXECUTION_GRANT_ENVELOPE_SCHEMA_VERSION: Final[str] = "execution-grant-envelope/v1"
MAX_EXECUTION_GRANT_TTL_SECONDS: Final[int] = 60


class ExecutionGrantMintDenied(ValueError):
    """Raised when CSP cannot establish every grant binding."""


class ExecutionGrantVerificationError(ValueError):
    """Raised when a signed grant transport envelope is invalid."""


def _deny(reason: str) -> NoReturn:
    # Keep the reason useful to internal callers/tests while the HTTP adapter
    # intentionally maps it to a generic 403 response.
    raise ExecutionGrantMintDenied(reason)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _positive_id(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        _deny(f"{field_name} 無效")
    return value


def _stored_classification(value: object, *, field_name: str) -> ClassificationLevel:
    try:
        return Classification.from_storage(str(value))
    except (TypeError, ValueError) as exc:
        _deny(f"{field_name} 分類狀態無效")
        raise AssertionError("unreachable") from exc


def _require_router_client(db: Session, caller: CallerIdentity) -> ServiceClient:
    """Resolve exactly one active, named Router service-client row."""

    if (
        caller.kind != "service_client"
        or caller.is_legacy
        or caller.service_client_id is None
        or caller.service_client_id <= 0
    ):
        _deny("ExecutionGrant 只允許具名 Router service client")
    client = (
        db.query(ServiceClient)
        .filter(ServiceClient.id == caller.service_client_id)
        .populate_existing()
        .with_for_update()
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
        _deny("ExecutionGrant 只允許 active 的具名 Router service client")
    return client


def _durable_auth_assurance(
    db: Session,
    *,
    request: ExecutionGrantMintRequest,
    caller_user_id: int,
    now: datetime,
) -> AuthAssurance:
    if request.session_id != request.auth_assurance.sid:
        _deny("session_id 與 auth_assurance.sid 不一致")

    session = (
        db.query(AuthSession)
        .filter(AuthSession.sid == request.auth_assurance.sid)
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if session is None or session.user_id != caller_user_id or session.revoked_at is not None:
        _deny("AuthSession 不存在、已撤銷或不屬於 caller")

    # AuthSession deliberately has no synthetic expiry column.  Its durable
    # refresh-family rows are the CSP authority for session lifetime: require
    # at least one current, unconsumed, unrevoked refresh token whose expiry
    # is still in the future.  This prevents a stale sid from minting a new
    # grant after every refresh credential has expired.
    refresh_rows = (
        db.query(AuthRefreshToken)
        .filter(
            AuthRefreshToken.sid == session.sid,
            AuthRefreshToken.consumed_at.is_(None),
            AuthRefreshToken.revoked_at.is_(None),
        )
        .populate_existing()
        .with_for_update()
        .all()
    )
    if not any(
        isinstance(row.expires_at, datetime) and _as_utc(row.expires_at) > now
        for row in refresh_rows
    ):
        _deny("AuthSession refresh family 已過期")

    user = (
        db.query(User)
        .filter(User.id == caller_user_id)
        .populate_existing()
        .one_or_none()
    )
    if user is None or not bool(user.is_active) or not bool(user.is_approved):
        _deny("AuthSession caller user 不可用")

    try:
        methods, acr, auth_time_epoch, break_glass, _ticket, break_glass_expires = (
            _assurance_from_session(session)
        )
    except (TypeError, ValueError) as exc:
        _deny("AuthSession assurance 無效")
        raise AssertionError("unreachable") from exc

    if break_glass_expires is not None and _as_utc(break_glass_expires) <= now:
        _deny("AuthSession assurance 已過期")

    durable = AuthAssurance(
        sid=session.sid,
        amr=methods,
        acr=acr,
        auth_time=datetime.fromtimestamp(auth_time_epoch, tz=timezone.utc),
        break_glass=break_glass,
    )
    if durable != request.auth_assurance:
        _deny("auth_assurance 與 durable AuthSession 不一致")
    if durable.auth_time > now:
        _deny("AuthSession auth_time 尚未生效")
    return durable


def _locked_task_and_run(
    db: Session, *, request: ExecutionGrantMintRequest, caller_user_id: int
) -> tuple[Task, TaskRun, SourceSnapshot]:
    task = (
        db.query(Task)
        .filter(Task.id == request.task_id)
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if task is None:
        _deny("Task 不存在")
    if task.requester_user_id != caller_user_id:
        _deny("Task requester 與 caller 不一致")
    if bool(task.legacy_runtime_call):
        _deny("legacy runtime Task 不得 mint ExecutionGrant")
    if task.status != "running":
        _deny("Task 尚未處於 running")
    if task.trace_id != request.trace_id:
        _deny("Task trace_id 不一致")
    if task.source_snapshot_id != request.source_snapshot_id:
        _deny("Task source_snapshot_id 不一致")

    task_level = _stored_classification(
        task.classification_level, field_name="Task.classification_level"
    )
    if task_level is not request.classification:
        _deny("Task classification 不一致")

    run = (
        db.query(TaskRun)
        .filter(TaskRun.id == request.run_id, TaskRun.task_id == task.id)
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if run is None or run.status != "running" or run.dispatch_target != "agent":
        _deny("TaskRun 不存在、非 running 或非 Agent dispatch")
    run_level = _stored_classification(
        run.classification_level, field_name="TaskRun.classification_level"
    )
    if run_level is not request.classification:
        _deny("TaskRun classification 不一致")

    snapshot = (
        db.query(SourceSnapshot)
        .filter(
            SourceSnapshot.id == request.source_snapshot_id,
            SourceSnapshot.task_id == task.id,
        )
        .populate_existing()
        .with_for_update()
        .one_or_none()
    )
    if snapshot is None:
        _deny("SourceSnapshot 不存在或不屬於 Task")
    snapshot_level = _stored_classification(
        snapshot.classification_level, field_name="SourceSnapshot.classification_level"
    )
    if snapshot_level > task_level:
        _deny("SourceSnapshot classification 高於 Task latch")
    return task, run, snapshot


def _current_registry_entry(
    db: Session,
    *,
    request: ExecutionGrantMintRequest,
    caller_user_id: int,
    now: datetime,
) -> tuple[AgentRegistrySnapshot, AgentRegistryEntry]:
    try:
        snapshot = build_registry_snapshot(db, user_id=caller_user_id, now=now)
    except (TypeError, ValueError) as exc:
        _deny("caller-scoped registry snapshot 無效")
        raise AssertionError("unreachable") from exc

    if _as_utc(snapshot.expires_at) <= now:
        _deny("registry snapshot 已過期")
    if (
        snapshot.registry_snapshot_id != request.registry_snapshot_id
        or snapshot.snapshot_revision != request.registry_snapshot_revision
        or snapshot.snapshot_hash != request.registry_snapshot_hash
    ):
        _deny("registry snapshot 已變更或證據不一致")

    matches = [entry for entry in snapshot.agents if entry.agent_id == request.target_agent_id]
    if len(matches) != 1:
        _deny("target Agent 不在 caller-scoped registry snapshot")
    entry = matches[0]
    if entry.snapshot_id != snapshot.registry_snapshot_id:
        _deny("Agent entry snapshot 不一致")
    if (
        not entry.ready_for_dispatch
        or not entry.manifest_valid
        or not entry.endpoint_via_csp
        or entry.model_gateway != "csp"
        or entry.manifest is None
        or not entry.manifest_revision
        or not entry.manifest_sha256
    ):
        _deny("Agent 未通過 ready_for_dispatch / CSP endpoint gate")
    if (
        entry.manifest_revision != request.manifest_revision
        or entry.manifest_sha256 != request.manifest_sha256
    ):
        _deny("Agent manifest identity 不一致")
    if entry.manifest is None:
        _deny("Agent manifest 缺失")
    try:
        computed_manifest_hash = manifest_sha256(entry.manifest)
    except (TypeError, ValueError) as exc:
        _deny("Agent manifest content identity 無效")
        raise AssertionError("unreachable") from exc
    if (
        entry.manifest_sha256 != computed_manifest_hash
        or entry.manifest_revision != f"sha256:{computed_manifest_hash}"
        or request.manifest_sha256 != computed_manifest_hash
        or request.manifest_revision != f"sha256:{computed_manifest_hash}"
    ):
        _deny("manifest_sha256 與 manifest_revision content identity 不一致")

    manifest_binding = entry.manifest.model_binding
    if manifest_binding is None or manifest_binding.gateway != "csp":
        _deny("Agent manifest 缺少 CSP model binding")
    if manifest_binding != request.model_binding:
        _deny("model_binding 與 current Agent manifest 不一致")
    if (
        isinstance(manifest_binding.model_id, int)
        and (entry.base_model is None or entry.base_model.id != manifest_binding.model_id)
    ):
        _deny("model_binding 與 current base model 不一致")

    ceiling = _stored_classification(
        entry.classification_ceiling, field_name="registry classification_ceiling"
    )
    if request.classification > ceiling:
        _deny("classification 超過 Agent current ceiling")
    if (
        request.model_binding.classification_ceiling is not None
        and request.classification > request.model_binding.classification_ceiling
    ):
        _deny("classification 超過 model binding ceiling")

    return snapshot, entry


def _manifest_capabilities(entry: AgentRegistryEntry) -> set[str]:
    manifest = entry.manifest
    if manifest is None:
        return set()
    capabilities = manifest.capabilities
    if isinstance(capabilities, tuple):
        return set(capabilities)
    if isinstance(capabilities, ManifestCapabilities):
        values = set(capabilities.tools)
        if capabilities.retrieval:
            values.add("retrieval")
        if capabilities.streaming:
            values.add("streaming")
        return values
    return set()


def _validate_decisions(
    request: ExecutionGrantMintRequest,
    *,
    entry: AgentRegistryEntry,
) -> None:
    decision: RouteDecision = request.route_decision
    result: PolicyGateResult = request.policy_result
    snapshot_id = request.registry_snapshot_id

    if decision.route_type is not RouteType.SINGLE_AGENT:
        _deny("只有 single_agent RouteDecision 可 mint")
    if decision.registry_snapshot_id != snapshot_id:
        _deny("RouteDecision registry snapshot 不一致")
    if decision.selected_agent_id != request.target_agent_id:
        _deny("RouteDecision selected agent 不一致")
    if request.target_agent_id not in decision.candidate_agent_ids:
        _deny("RouteDecision target 不在 candidates")
    if tuple(request.allowed_capabilities) != decision.required_capabilities:
        _deny("allowed_capabilities 與 RouteDecision 不一致")
    if not set(decision.required_capabilities).issubset(_manifest_capabilities(entry)):
        _deny("RouteDecision capability 超出 current manifest")

    if not result.allowed or result.approval_required:
        _deny("PolicyGateResult 必須是 allowed 且非 approval_required")
    if result.decision_id != f"pg-{decision.decision_id}":
        _deny("PolicyGateResult decision_id 不符合 RouteDecision")
    if result.route_decision_id != decision.decision_id:
        _deny("PolicyGateResult route_decision_id 不一致")
    if result.registry_snapshot_id != snapshot_id:
        _deny("PolicyGateResult registry snapshot 不一致")
    if result.target_agent_id != request.target_agent_id:
        _deny("PolicyGateResult target agent 不一致")
    if result.effective_classification is not request.classification:
        _deny("PolicyGateResult classification 不一致")
    if tuple(request.allowed_scopes) != result.required_scopes:
        _deny("allowed_scopes 與 PolicyGateResult 不一致")
    if entry.manifest is None or tuple(result.required_scopes) != tuple(entry.manifest.required_scopes):
        _deny("PolicyGateResult scopes 與 current manifest 不一致")
    if tuple(result.obligations) != tuple(entry.required_obligations):
        _deny("PolicyGateResult obligations 與 current registry 不一致")


def _new_grant_id() -> str:
    return f"eg-{secrets.token_urlsafe(24)}"


def _envelope_payload(grant: ExecutionGrant, *, manifest_sha256: str, jti: str) -> dict[str, Any]:
    issued_at = int(grant.issued_at.timestamp())
    expires_at = int(grant.expires_at.timestamp())
    return {
        "type": EXECUTION_GRANT_TYPE,
        "grant": grant.model_dump(mode="json"),
        # Keep binding claims at the transport layer as well as in the inner
        # object.  Final sinks can reject a mismatched envelope before they
        # deserialize the full grant.
        "grant_id": grant.grant_id,
        "task_id": grant.task_id,
        "run_id": grant.run_id,
        "trace_id": grant.trace_id,
        "invocation_id": grant.invocation_id,
        "source_snapshot_id": grant.source_snapshot_id,
        "registry_snapshot_id": grant.registry_snapshot_id,
        "route_decision_id": grant.route_decision_id,
        "policy_decision_id": grant.policy_decision_id,
        "target_agent_id": grant.target.id,
        "manifest_revision": grant.manifest_revision,
        "manifest_sha256": manifest_sha256,
        "session_id": grant.session_id,
        "iat": issued_at,
        "exp": expires_at,
        "iss": settings.JWT_ISSUER,
        "aud": EXECUTION_GRANT_AUDIENCE,
        "jti": jti,
    }


def mint_execution_grant(
    db: Session,
    *,
    request: ExecutionGrantMintRequest,
    caller: CallerIdentity,
    caller_user_id: int,
    now: datetime | None = None,
) -> ExecutionGrantMintResponse:
    """Revalidate CSP authority and mint one RS256 ExecutionGrant envelope."""

    resolved_user_id = _positive_id(caller_user_id, field_name="caller_user_id")
    _require_router_client(db, caller)
    instant = _as_utc(now or datetime.now(timezone.utc))

    # The parent Task is always locked before its TaskRun, matching the
    # canonical task-link/finalization lock order and preventing a grant from
    # racing a concurrent cancel/finalize.
    _task, _run, _snapshot = _locked_task_and_run(
        db, request=request, caller_user_id=resolved_user_id
    )
    durable_assurance = _durable_auth_assurance(
        db,
        request=request,
        caller_user_id=resolved_user_id,
        now=instant,
    )
    registry_snapshot, entry = _current_registry_entry(
        db,
        request=request,
        caller_user_id=resolved_user_id,
        now=instant,
    )
    _validate_decisions(request, entry=entry)

    # JWT NumericDate values are integer seconds.  Flooring the inner grant
    # timestamps makes ``iat``/``exp`` and the signed inner values exactly
    # comparable, without adding an accidental microsecond of lifetime.
    issued_at = datetime.fromtimestamp(int(instant.timestamp()), tz=timezone.utc)
    expires_at = issued_at + timedelta(seconds=request.ttl_seconds)
    grant = ExecutionGrant(
        schema_version="execution-grant/v1",
        grant_id=_new_grant_id(),
        task_id=request.task_id,
        run_id=request.run_id,
        trace_id=request.trace_id,
        invocation_id=request.invocation_id,
        source_snapshot_id=request.source_snapshot_id,
        route_decision_id=request.route_decision.decision_id,
        policy_decision_id=request.policy_result.decision_id,
        registry_snapshot_id=registry_snapshot.registry_snapshot_id,
        classification=request.classification,
        auth_assurance=durable_assurance,
        target={
            "kind": InvocationTargetKind.AGENT,
            "id": request.target_agent_id,
            "model_binding": request.model_binding,
        },
        manifest_revision=request.manifest_revision,
        allowed_capabilities=request.allowed_capabilities,
        allowed_scopes=request.allowed_scopes,
        issued_at=issued_at,
        expires_at=expires_at,
        session_id=request.session_id,
    )
    jti = secrets.token_urlsafe(32)
    token = jwt.encode(
        _envelope_payload(grant, manifest_sha256=request.manifest_sha256, jti=jti),
        get_private_key(),
        algorithm=ALGORITHM,
        headers={"kid": settings.JWT_KID, "typ": EXECUTION_GRANT_TYPE},
    )
    return ExecutionGrantMintResponse(
        schema_version=EXECUTION_GRANT_ENVELOPE_SCHEMA_VERSION,
        token=token,
        grant=grant,
    )


def _claim_string(payload: Mapping[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise ExecutionGrantVerificationError(f"grant envelope {name} 無效")
    return value


def _claim_positive_int(payload: Mapping[str, Any], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ExecutionGrantVerificationError(f"grant envelope {name} 無效")
    return value


def verify_execution_grant_token(
    token: str,
    *,
    now: datetime | None = None,
) -> ExecutionGrant:
    """Verify a CSP-issued grant and return its strictly bound inner grant.

    This function performs cryptographic and envelope/inner binding checks;
    the final CSP sink must still re-check current Task/TaskRun, registry and
    policy state before dispatching.  Every malformed or stale token raises
    the same typed error and never returns a partially parsed grant.
    """

    if not isinstance(token, str) or not token.strip():
        raise ExecutionGrantVerificationError("ExecutionGrant token 無效")
    try:
        header = jwt.get_unverified_header(token)
        if (
            header.get("alg") != ALGORITHM
            or header.get("kid") != settings.JWT_KID
            or header.get("typ") != EXECUTION_GRANT_TYPE
        ):
            raise ExecutionGrantVerificationError("ExecutionGrant header 無效")
        payload = jwt.decode(
            token,
            get_public_key(),
            algorithms=[ALGORITHM],
            issuer=settings.JWT_ISSUER,
            audience=EXECUTION_GRANT_AUDIENCE,
            options={
                "require_exp": True,
                "require_iat": True,
                "require_iss": True,
                "require_aud": True,
                "require_jti": True,
                # ``now`` is injectable for deterministic final-sink tests;
                # expiry/iat are checked against it below rather than jose's
                # process wall clock.
                "verify_exp": False,
                "verify_iat": False,
            },
        )
    except ExecutionGrantVerificationError:
        raise
    except (JWTError, TypeError, ValueError) as exc:
        raise ExecutionGrantVerificationError("ExecutionGrant token 驗證失敗") from exc

    if not isinstance(payload, dict):
        raise ExecutionGrantVerificationError("ExecutionGrant payload 無效")
    if payload.get("type") != EXECUTION_GRANT_TYPE:
        raise ExecutionGrantVerificationError("ExecutionGrant type 無效")
    if payload.get("iss") != settings.JWT_ISSUER:
        raise ExecutionGrantVerificationError("ExecutionGrant issuer 無效")
    if payload.get("aud") != EXECUTION_GRANT_AUDIENCE:
        raise ExecutionGrantVerificationError("ExecutionGrant audience 無效")
    required = {
        "type",
        "grant",
        "grant_id",
        "task_id",
        "run_id",
        "trace_id",
        "invocation_id",
        "source_snapshot_id",
        "registry_snapshot_id",
        "route_decision_id",
        "policy_decision_id",
        "target_agent_id",
        "manifest_revision",
        "manifest_sha256",
        "session_id",
        "iat",
        "exp",
        "iss",
        "aud",
        "jti",
    }
    if set(payload) != required:
        raise ExecutionGrantVerificationError("ExecutionGrant envelope 欄位不完整")

    raw_grant = payload.get("grant")
    if not isinstance(raw_grant, Mapping):
        raise ExecutionGrantVerificationError("ExecutionGrant inner grant 無效")
    try:
        grant = ExecutionGrant.model_validate(raw_grant)
    except (ValidationError, TypeError, ValueError) as exc:
        raise ExecutionGrantVerificationError("ExecutionGrant inner grant 無效") from exc

    iat = payload.get("iat")
    exp = payload.get("exp")
    if (
        isinstance(iat, bool)
        or not isinstance(iat, int)
        or isinstance(exp, bool)
        or not isinstance(exp, int)
        or exp <= iat
        or exp - iat > MAX_EXECUTION_GRANT_TTL_SECONDS
    ):
        raise ExecutionGrantVerificationError("ExecutionGrant TTL/iat/exp 無效")
    if not isinstance(payload.get("jti"), str) or not payload["jti"]:
        raise ExecutionGrantVerificationError("ExecutionGrant jti 無效")
    if grant.issued_at != datetime.fromtimestamp(iat, tz=timezone.utc):
        raise ExecutionGrantVerificationError("ExecutionGrant issued_at 與 iat 不一致")
    if grant.expires_at != datetime.fromtimestamp(exp, tz=timezone.utc):
        raise ExecutionGrantVerificationError("ExecutionGrant expires_at 與 exp 不一致")
    if grant.ttl_seconds > MAX_EXECUTION_GRANT_TTL_SECONDS:
        raise ExecutionGrantVerificationError("ExecutionGrant TTL 超過 CSP 上限")

    if _claim_string(payload, "grant_id") != grant.grant_id:
        raise ExecutionGrantVerificationError("ExecutionGrant grant_id 不一致")
    for name in ("task_id", "run_id", "source_snapshot_id"):
        if _claim_positive_int(payload, name) != getattr(grant, name):
            raise ExecutionGrantVerificationError(f"ExecutionGrant {name} 不一致")
    for name in (
        "trace_id",
        "invocation_id",
        "registry_snapshot_id",
        "route_decision_id",
        "policy_decision_id",
        "target_agent_id",
        "manifest_revision",
        "manifest_sha256",
    ):
        value = _claim_string(payload, name)
        expected = (
            grant.target.id
            if name == "target_agent_id"
            else grant.manifest_revision
            if name == "manifest_revision"
            else getattr(grant, name, None)
        )
        # manifest_sha256 is transport-only and therefore has no inner field;
        # all other fields above must match the inner grant.
        if name != "manifest_sha256" and value != expected:
            raise ExecutionGrantVerificationError(f"ExecutionGrant {name} 不一致")
    if payload.get("session_id") != grant.session_id:
        raise ExecutionGrantVerificationError("ExecutionGrant session_id 不一致")
    manifest_revision = _claim_string(payload, "manifest_revision")
    manifest_sha256 = _claim_string(payload, "manifest_sha256")
    if manifest_revision != f"sha256:{manifest_sha256}":
        raise ExecutionGrantVerificationError(
            "ExecutionGrant manifest content identity 不一致"
        )
    if grant.target.kind is not InvocationTargetKind.AGENT:
        raise ExecutionGrantVerificationError("ExecutionGrant target 必須是 Agent")

    instant = _as_utc(now or datetime.now(timezone.utc))
    try:
        grant.assert_active_at(instant)
    except ValueError as exc:
        raise ExecutionGrantVerificationError("ExecutionGrant 不在有效期限") from exc
    return grant


__all__ = [
    "EXECUTION_GRANT_AUDIENCE",
    "EXECUTION_GRANT_ENVELOPE_SCHEMA_VERSION",
    "EXECUTION_GRANT_TYPE",
    "ExecutionGrantMintDenied",
    "ExecutionGrantVerificationError",
    "MAX_EXECUTION_GRANT_TTL_SECONDS",
    "mint_execution_grant",
    "verify_execution_grant_token",
]
