"""Focused tests for the CSP ExecutionGrant mint/verify seam."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("SECRET_KEY", "test-execution-grant-secret")

import pytest
from anila_contracts import AgentManifest, PolicyGateResult, RouteDecision
from anila_contracts.contexts import AuthAssurance
from fastapi import HTTPException
from jose import jwt
from pydantic import ValidationError
from fastapi.testclient import TestClient

from app.models.auth_session import AuthRefreshToken, AuthSession
from app.models.agent import Agent, UserAgentPermission
from app.models.model_registry import ModelRegistry
from app.models.service_client import ServiceClient
from app.models.source_snapshot import SourceSnapshot
from app.models.task import Task, TaskRun
from app.models.user import User
from app.schemas.agent_registry import (
    AgentRegistryBaseModel,
    AgentRegistryEntry,
    AgentRegistrySnapshot,
)
from app.schemas.execution_grant import ExecutionGrantMintRequest
from app.services.agent_credential_service import CallerIdentity
from app.services.agent_dispatch_service import _binding_from_request
from app.services.execution_grant_service import (
    EXECUTION_GRANT_TYPE,
    ExecutionGrantMintDenied,
    ExecutionGrantVerificationError,
    mint_execution_grant,
    verify_execution_grant_token,
)
from app.services.agent_readiness import manifest_sha256
from app.services.service_token_envelope import (
    compute_lookup_hash,
    decode_service_token_envelope,
    encode_service_token_envelope,
    generate_service_token,
)
from app.utils.security import ALGORITHM, get_private_key


FIXTURES = Path(__file__).parents[3] / "packages" / "anila-contracts" / "tests" / "fixtures"
SNAPSHOT_ID = "c" * 64


def _manifest() -> AgentManifest:
    import json

    return AgentManifest.model_validate(
        json.loads((FIXTURES / "agent-manifest-v1.json").read_text(encoding="utf-8"))
    )


def _manifest_identity() -> tuple[str, str]:
    digest = manifest_sha256(_manifest())
    return digest, f"sha256:{digest}"


def _snapshot(now: datetime) -> tuple[AgentRegistrySnapshot, AgentRegistryEntry]:
    manifest = _manifest()
    manifest_hash, manifest_revision = _manifest_identity()
    entry = AgentRegistryEntry(
        agent_id="research-agent",
        registry_id=7,
        name="research-agent",
        snapshot_id=SNAPSHOT_ID,
        is_active=True,
        manifest=manifest,
        manifest_sha256=manifest_hash,
        manifest_revision=manifest_revision,
        approval_status="approved",
        health_status="healthy",
        health_checked_at=now,
        audit_level="full_trace",
        trace_callback_mode="sse_and_post",
        trace_test_passed_at=now,
        trace_test_governance_fingerprint="d" * 64,
        classification_ceiling="機密",
        default_classification_level="無機密",
        base_model=AgentRegistryBaseModel(
            id=7,
            name="mock-model",
            display_name="mock-model",
            is_active=True,
            health_status="healthy",
            classification_ceiling="機密",
        ),
        ready_for_dispatch=True,
        approved=True,
        health_ready=True,
        trace_test_passed=True,
        manifest_valid=True,
        endpoint_via_csp=True,
        approval_required=False,
        required_obligations=("FULL_TRACE",),
        model_gateway="csp",
    )
    snapshot = AgentRegistrySnapshot(
        schema_version="agent-registry/v1",
        snapshot_id=SNAPSHOT_ID,
        snapshot_revision=SNAPSHOT_ID,
        snapshot_hash=SNAPSHOT_ID,
        registry_snapshot_id=SNAPSHOT_ID,
        fetched_at=now - timedelta(seconds=1),
        expires_at=now + timedelta(seconds=30),
        caller_user_id=1,
        agents=(entry,),
    )
    return snapshot, entry


def _request(now: datetime) -> ExecutionGrantMintRequest:
    manifest_hash, manifest_revision = _manifest_identity()
    route = RouteDecision(
        schema_version="route-decision/v1",
        decision_id="rd-001",
        route_type="single_agent",
        registry_snapshot_id=SNAPSHOT_ID,
        required_capabilities=("retrieval",),
        candidate_agent_ids=("research-agent",),
        selected_agent_id="research-agent",
        reason_codes=("CAPABILITY_MATCH",),
        confidence=0.9,
        rewritten_query="query",
        constraints={"max_steps": 8, "timeout_ms": 120000},
        fallback="clarify",
    )
    policy = PolicyGateResult(
        schema_version="policy-gate/v1",
        allowed=True,
        decision_id="pg-rd-001",
        route_decision_id="rd-001",
        registry_snapshot_id=SNAPSHOT_ID,
        target_agent_id="research-agent",
        effective_classification="無機密",
        required_scopes=("agent:invoke",),
        obligations=("FULL_TRACE",),
        approval_required=False,
        reason_codes=("READY_FOR_DISPATCH",),
    )
    return ExecutionGrantMintRequest(
        task_id=1,
        run_id=1,
        source_snapshot_id=1,
        trace_id="trace-1",
        invocation_id="invocation-1",
        # Conversation identity is deliberately distinct from the durable
        # authentication-session SID carried in ``auth_assurance``.
        session_id="conversation-session-001",
        registry_snapshot_id=SNAPSHOT_ID,
        registry_snapshot_revision=SNAPSHOT_ID,
        registry_snapshot_hash=SNAPSHOT_ID,
        target_agent_id="research-agent",
        manifest_revision=manifest_revision,
        manifest_sha256=manifest_hash,
        classification="無機密",
        auth_assurance=AuthAssurance(
            sid="s" * 40,
            amr=("pwd",),
            acr="urn:anila:acr:password",
            auth_time=now - timedelta(seconds=10),
            break_glass=False,
        ),
        model_binding=manifest_model_binding(),
        allowed_capabilities=("retrieval",),
        allowed_scopes=("agent:invoke",),
        route_decision=route,
        policy_result=policy,
        ttl_seconds=60,
    )


def manifest_model_binding():
    return _manifest().model_binding


def _db_state(db, now: datetime) -> CallerIdentity:
    from tests.conftest import make_user

    user = make_user(db)
    task = Task(
        title="grant",
        task_type="query",
        requester_user_id=user.id,
        status="running",
        source_scope="none",
        source_snapshot_id=None,
        classification_level="無機密",
        trace_id="trace-1",
        legacy_runtime_call=False,
    )
    db.add(task)
    db.flush()
    snapshot = SourceSnapshot(
        task_id=task.id,
        origin="none",
        source_scope="none",
        classification_level="無機密",
    )
    db.add(snapshot)
    db.flush()
    task.source_snapshot_id = snapshot.id
    db.add(
        TaskRun(
            task_id=task.id,
            run_sequence=1,
            dispatch_target="agent",
            status="running",
            classification_level="無機密",
            started_at=now - timedelta(seconds=1),
        )
    )
    db.add(
        AuthSession(
            sid="s" * 40,
            user_id=user.id,
            refresh_family_id="f" * 40,
            amr_json='["pwd"]',
            acr="urn:anila:acr:password",
            auth_time=now - timedelta(seconds=10),
            break_glass=False,
        )
    )
    db.add(
        AuthRefreshToken(
            jti_hash="j" * 64,
            sid="s" * 40,
            generation=0,
            issued_at=now - timedelta(seconds=10),
            expires_at=now + timedelta(hours=1),
        )
    )
    raw_token = generate_service_token()
    client = ServiceClient(
        client_name="router-primary",
        client_type="router",
        service_token_envelope=encode_service_token_envelope(raw_token),
        service_token_lookup_hash=compute_lookup_hash(raw_token),
        is_legacy=False,
        is_active=True,
    )
    db.add(client)
    db.commit()
    db.refresh(client)
    return CallerIdentity(
        kind="service_client",
        agent_id=None,
        service_client_id=client.id,
        credential_id=client.id,
        is_legacy=False,
        used_previous_token=False,
    )


def _patch_registry(monkeypatch, now: datetime):
    snapshot, entry = _snapshot(now)
    monkeypatch.setattr(
        "app.services.execution_grant_service.build_registry_snapshot",
        lambda *args, **kwargs: snapshot,
    )
    return snapshot, entry


def _minted(db, monkeypatch, now: datetime):
    caller = _db_state(db, now)
    _patch_registry(monkeypatch, now)
    response = mint_execution_grant(
        db,
        request=_request(now),
        caller=caller,
        caller_user_id=1,
        now=now,
    )
    return caller, response


def _request_with(request: ExecutionGrantMintRequest, **updates) -> ExecutionGrantMintRequest:
    payload = request.model_dump(mode="json")
    payload.update(updates)
    return ExecutionGrantMintRequest.model_validate(payload)


def _client_row(db) -> ServiceClient:
    return db.query(ServiceClient).one()


def _session_row(db) -> AuthSession:
    return db.query(AuthSession).one()


def test_mint_signs_and_verify_rechecks_the_inner_bindings(db, monkeypatch) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    caller = _db_state(db, now)
    _patch_registry(monkeypatch, now)
    request = _request(now)
    assert request.session_id != request.auth_assurance.sid

    response = mint_execution_grant(
        db,
        request=request,
        caller=caller,
        caller_user_id=1,
        now=now,
    )
    assert response.token
    assert response.grant.ttl_seconds == 60
    assert response.grant.session_id == request.session_id
    assert response.grant.auth_assurance.sid == request.auth_assurance.sid
    parsed = verify_execution_grant_token(response.token, now=now + timedelta(seconds=1))
    assert parsed.grant_id == response.grant.grant_id
    assert parsed.target.id == "research-agent"


def test_mint_rejects_forged_auth_session_sid_even_with_valid_conversation_session(
    db, monkeypatch
) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    caller = _db_state(db, now)
    _patch_registry(monkeypatch, now)
    request = _request(now)
    forged_assurance = {
        **request.auth_assurance.model_dump(mode="json"),
        "sid": "forged-auth-session",
    }

    with pytest.raises(ExecutionGrantMintDenied):
        mint_execution_grant(
            db,
            request=_request_with(request, auth_assurance=forged_assurance),
            caller=caller,
            caller_user_id=1,
            now=now,
        )


def _dispatch_evidence(grant):
    headers = {
        "x-anila-caller-user-id": "1",
        "x-anila-owner-id": "1",
        "x-anila-task-id": str(grant.task_id),
        "x-anila-run-id": str(grant.run_id),
        "x-anila-source-snapshot-id": str(grant.source_snapshot_id),
        "x-anila-trace-id": grant.trace_id,
        "x-anila-invocation-id": grant.invocation_id,
        "x-anila-session-id": grant.session_id,
        "x-anila-agent-id": grant.target.id,
        "x-anila-registry-snapshot-id": grant.registry_snapshot_id,
        "x-anila-registry-snapshot-revision": grant.registry_snapshot_id,
        "x-anila-registry-snapshot-hash": grant.registry_snapshot_id,
        "x-anila-agent-manifest-revision": grant.manifest_revision,
        "x-anila-agent-manifest-sha256": grant.manifest_revision.removeprefix("sha256:"),
        "x-anila-execution-grant-id": grant.grant_id,
        "x-anila-route-decision-id": grant.route_decision_id,
        "x-anila-policy-decision-id": grant.policy_decision_id,
        "x-anila-classification-level": "無機密",
    }
    payload = {
        "task_id": grant.task_id,
        "run_id": grant.run_id,
        "source_snapshot_id": grant.source_snapshot_id,
        "trace_id": grant.trace_id,
        "invocation_id": grant.invocation_id,
        "session_id": grant.session_id,
        "agent_id": grant.target.id,
        "registry_snapshot_id": grant.registry_snapshot_id,
        "registry_snapshot_revision": grant.registry_snapshot_id,
        "registry_snapshot_hash": grant.registry_snapshot_id,
        "manifest_revision": grant.manifest_revision,
        "manifest_sha256": grant.manifest_revision.removeprefix("sha256:"),
        "grant_id": grant.grant_id,
        "route_decision_id": grant.route_decision_id,
        "policy_decision_id": grant.policy_decision_id,
        "owner_id": 1,
    }
    return headers, payload


def test_mint_to_authorize_preserves_distinct_session_and_auth_sid_with_numeric_headers(
    db, monkeypatch
) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    caller = _db_state(db, now)
    _patch_registry(monkeypatch, now)
    request = _request(now)
    response = mint_execution_grant(
        db,
        request=request,
        caller=caller,
        caller_user_id=1,
        now=now,
    )
    headers, payload = _dispatch_evidence(response.grant)

    binding = _binding_from_request(
        headers=headers,
        binding_payload=payload,
        grant=response.grant,
    )
    assert binding.session_id == request.session_id
    assert response.grant.auth_assurance.sid == request.auth_assurance.sid
    assert binding.session_id != response.grant.auth_assurance.sid
    assert binding.task_id == response.grant.task_id
    assert binding.run_id == response.grant.run_id
    assert binding.source_snapshot_id == response.grant.source_snapshot_id


def test_authorize_rejects_numeric_header_mismatch_against_signed_grant(db, monkeypatch) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    caller = _db_state(db, now)
    _patch_registry(monkeypatch, now)
    response = mint_execution_grant(
        db,
        request=_request(now),
        caller=caller,
        caller_user_id=1,
        now=now,
    )
    headers, payload = _dispatch_evidence(response.grant)
    headers["x-anila-task-id"] = "999"

    with pytest.raises(HTTPException) as caught:
        _binding_from_request(
            headers=headers,
            binding_payload=payload,
            grant=response.grant,
        )
    assert caught.value.status_code == 403


@pytest.mark.parametrize(
    ("mutator", "needle"),
    [
        (lambda p: p.update(aud="wrong-audience"), "token"),
        (lambda p: p.update(type="wrong-type"), "token"),
        (lambda p: p["grant"].update(task_id=99), "task_id"),
    ],
)
def test_tampered_signed_envelope_is_rejected(db, monkeypatch, mutator, needle) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    caller = _db_state(db, now)
    _patch_registry(monkeypatch, now)
    response = mint_execution_grant(
        db,
        request=_request(now),
        caller=caller,
        caller_user_id=1,
        now=now,
    )
    payload = jwt.decode(
        response.token,
        get_private_key(),
        algorithms=[ALGORITHM],
        options={"verify_signature": False, "verify_exp": False, "verify_aud": False},
    )
    mutator(payload)
    token = jwt.encode(
        payload,
        get_private_key(),
        algorithm=ALGORITHM,
        headers={"kid": "anila-v1", "typ": EXECUTION_GRANT_TYPE},
    )
    with pytest.raises(ExecutionGrantVerificationError):
        verify_execution_grant_token(token, now=now + timedelta(seconds=1))


def test_mint_rejects_non_router_client_and_revoked_session(db, monkeypatch) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    caller = _db_state(db, now)
    _patch_registry(monkeypatch, now)
    db.query(ServiceClient).update({ServiceClient.client_type: "worker"})
    db.commit()
    with pytest.raises(ExecutionGrantMintDenied):
        mint_execution_grant(
            db,
            request=_request(now),
            caller=caller,
            caller_user_id=1,
            now=now,
        )


def test_mint_rejects_revoked_auth_session(db, monkeypatch) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    caller = _db_state(db, now)
    _patch_registry(monkeypatch, now)
    session = _session_row(db)
    session.revoked_at = now
    db.commit()
    with pytest.raises(ExecutionGrantMintDenied):
        mint_execution_grant(
            db,
            request=_request(now),
            caller=caller,
            caller_user_id=1,
            now=now,
        )


def test_internal_mint_endpoint_returns_only_signed_envelope_or_generic_403(
    db, monkeypatch
) -> None:
    from app.database import get_db
    from app.main import app

    now = datetime.now(timezone.utc).replace(microsecond=0)
    _db_state(db, now)
    _patch_registry(monkeypatch, now)
    raw_token = decode_service_token_envelope(_client_row(db).service_token_envelope)
    assert raw_token is not None

    def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as http:
            headers = {
                "X-CSP-Service-Token": raw_token,
                "X-ANILA-Caller-User-Id": "1",
            }
            accepted = http.post(
                "/internal/v1/execution-grants/mint",
                headers=headers,
                json=_request(now).model_dump(mode="json"),
            )
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["token"]
            assert accepted.json()["grant"]["grant_id"].startswith("eg-")

            rejected_header = http.post(
                "/internal/v1/execution-grants/mint",
                headers={**headers, "X-ANILA-Caller-User-Id": " "},
                json=_request(now).model_dump(mode="json"),
            )
            assert rejected_header.status_code == 400
            assert "token" not in rejected_header.json()

            _client_row(db).client_type = "worker"
            db.commit()
            rejected_client = http.post(
                "/internal/v1/execution-grants/mint",
                headers=headers,
                json=_request(now).model_dump(mode="json"),
            )
            assert rejected_client.status_code == 403
            assert rejected_client.json() == {
                "detail": "ExecutionGrant mint denied"
            }
            assert "eyJ" not in rejected_client.text

            malformed = _request(now).model_dump(mode="json")
            malformed["unexpected"] = True
            invalid_contract = http.post(
                "/internal/v1/execution-grants/mint",
                headers=headers,
                json=malformed,
            )
            assert invalid_contract.status_code == 422
            assert "token" not in invalid_contract.json()
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_mint_request_rejects_ttl_above_sixty_seconds() -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with pytest.raises(ValidationError):
        _request_with(_request(now), ttl_seconds=61)


@pytest.mark.parametrize("raw", [None, "", " ", " +1", "+1", "-1", "01", "True", "1.0"])
def test_caller_user_id_header_is_positive_decimal_without_whitespace_or_sign(raw) -> None:
    from app.api.execution_grants import _resolve_caller_user_id

    with pytest.raises(HTTPException):
        _resolve_caller_user_id(raw)

    assert _resolve_caller_user_id("1") == 1


@pytest.mark.parametrize(
    "mutator",
    [
        lambda client, _now: setattr(client, "client_type", "worker"),
        lambda client, _now: setattr(client, "is_active", False),
        lambda client, _now: setattr(client, "is_legacy", True),
        lambda client, now: setattr(client, "revoked_at", now),
    ],
)
def test_mint_rejects_every_non_router_service_client_state(db, monkeypatch, mutator) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    caller = _db_state(db, now)
    _patch_registry(monkeypatch, now)
    mutator(_client_row(db), now)
    db.commit()
    with pytest.raises(ExecutionGrantMintDenied):
        mint_execution_grant(
            db,
            request=_request(now),
            caller=caller,
            caller_user_id=1,
            now=now,
        )


def test_mint_rejects_unknown_agent_credential_and_caller_user_states(db, monkeypatch) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    caller = _db_state(db, now)
    _patch_registry(monkeypatch, now)
    request = _request(now)

    with pytest.raises(ExecutionGrantMintDenied):
        mint_execution_grant(
            db,
            request=request,
            caller=CallerIdentity(
                kind="agent",
                agent_id=7,
                service_client_id=None,
                credential_id=7,
                is_legacy=False,
                used_previous_token=False,
            ),
            caller_user_id=1,
            now=now,
        )

    user = db.query(User).one()
    user.is_active = False
    db.commit()
    with pytest.raises(ExecutionGrantMintDenied):
        mint_execution_grant(
            db,
            request=request,
            caller=caller,
            caller_user_id=1,
            now=now,
        )

    user.is_active = True
    user.is_approved = False
    db.commit()
    with pytest.raises(ExecutionGrantMintDenied):
        mint_execution_grant(
            db,
            request=request,
            caller=caller,
            caller_user_id=1,
            now=now,
        )

    user.is_approved = True
    db.commit()
    with pytest.raises(ExecutionGrantMintDenied):
        mint_execution_grant(
            db,
            request=request,
            caller=caller,
            caller_user_id=2,
            now=now,
        )


@pytest.mark.parametrize("state", ["missing", "revoked", "expired", "consumed", "future"])
def test_mint_rejects_expired_or_mismatched_auth_assurance(db, monkeypatch, state) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    caller = _db_state(db, now)
    _patch_registry(monkeypatch, now)
    request = _request(now)
    session = _session_row(db)
    refresh = db.query(AuthRefreshToken).one()
    if state == "missing":
        db.delete(session)
    elif state == "revoked":
        session.revoked_at = now
    elif state == "expired":
        refresh.expires_at = now - timedelta(seconds=1)
    elif state == "consumed":
        refresh.consumed_at = now
    elif state == "future":
        session.auth_time = now + timedelta(minutes=10)
    db.commit()
    if state == "future":
        request = _request_with(
            request,
            auth_assurance={
                **request.auth_assurance.model_dump(mode="json"),
                "auth_time": (now + timedelta(minutes=10)).isoformat(),
            },
        )
    with pytest.raises(ExecutionGrantMintDenied):
        mint_execution_grant(
            db,
            request=request,
            caller=caller,
            caller_user_id=1,
            now=now,
        )

    if state == "missing":
        return
    # A valid durable session with contradictory Router evidence is also
    # rejected, independently of refresh expiry.
    if state == "revoked":
        session.revoked_at = None
        db.commit()
        bad_assurance = {
            **request.auth_assurance.model_dump(mode="json"),
            "amr": ["sc"],
            "acr": "urn:anila:acr:smart-card",
        }
        with pytest.raises(ExecutionGrantMintDenied):
            mint_execution_grant(
                db,
                request=_request_with(request, auth_assurance=bad_assurance),
                caller=caller,
                caller_user_id=1,
                now=now,
            )


@pytest.mark.parametrize(
    "mutator",
    [
        lambda task, _run, _snapshot, _now: setattr(task, "legacy_runtime_call", True),
        lambda task, _run, _snapshot, _now: setattr(task, "status", "completed"),
        lambda task, _run, _snapshot, _now: setattr(task, "requester_user_id", 999),
        lambda task, _run, _snapshot, _now: setattr(task, "trace_id", "other-trace"),
        lambda task, _run, _snapshot, _now: setattr(task, "source_snapshot_id", 999),
        lambda task, _run, _snapshot, _now: setattr(task, "classification_level", "機密"),
        lambda _task, run, _snapshot, _now: setattr(run, "status", "completed"),
        lambda _task, run, _snapshot, _now: setattr(run, "dispatch_target", "model"),
        lambda _task, _run, snapshot, _now: setattr(snapshot, "task_id", 999),
    ],
)
def test_mint_rejects_task_and_run_spine_mismatches(db, monkeypatch, mutator) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    caller = _db_state(db, now)
    _patch_registry(monkeypatch, now)
    task = db.query(Task).one()
    run = db.query(TaskRun).one()
    snapshot = db.query(SourceSnapshot).one()
    mutator(task, run, snapshot, now)
    db.commit()
    with pytest.raises(ExecutionGrantMintDenied):
        mint_execution_grant(
            db,
            request=_request(now),
            caller=caller,
            caller_user_id=1,
            now=now,
        )


def test_mint_rejects_missing_run_and_foreign_source_snapshot(db, monkeypatch) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    caller = _db_state(db, now)
    _patch_registry(monkeypatch, now)
    with pytest.raises(ExecutionGrantMintDenied):
        mint_execution_grant(
            db,
            request=_request_with(_request(now), run_id=999),
            caller=caller,
            caller_user_id=1,
            now=now,
        )
    with pytest.raises(ExecutionGrantMintDenied):
        mint_execution_grant(
            db,
            request=_request_with(_request(now), source_snapshot_id=999),
            caller=caller,
            caller_user_id=1,
            now=now,
        )


@pytest.mark.parametrize("field", ["registry_snapshot_id", "registry_snapshot_revision", "registry_snapshot_hash"])
def test_mint_rejects_stale_registry_generation_evidence(db, monkeypatch, field) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    caller = _db_state(db, now)
    snapshot, _entry = _patch_registry(monkeypatch, now)
    request = _request(now)
    update = {field: "e" * 64}
    if field == "registry_snapshot_id":
        update[field] = "e" * 64
    with pytest.raises(ExecutionGrantMintDenied):
        mint_execution_grant(
            db,
            request=_request_with(request, **update),
            caller=caller,
            caller_user_id=1,
            now=now,
        )
    assert snapshot.registry_snapshot_id == SNAPSHOT_ID


@pytest.mark.parametrize("field", ["manifest_revision", "manifest_sha256"])
def test_mint_rejects_stale_manifest_identity(db, monkeypatch, field) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    caller = _db_state(db, now)
    _patch_registry(monkeypatch, now)
    request = _request(now)
    update = {field: "e" * 64}
    if field == "manifest_revision":
        update[field] = "sha256:" + "e" * 64
    with pytest.raises(ExecutionGrantMintDenied):
        mint_execution_grant(
            db,
            request=_request_with(request, **update),
            caller=caller,
            caller_user_id=1,
            now=now,
        )


@pytest.mark.parametrize("field", ["ready_for_dispatch", "manifest_valid", "endpoint_via_csp"])
def test_mint_rejects_registry_readiness_toctou(db, monkeypatch, field) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    caller = _db_state(db, now)
    snapshot, entry = _snapshot(now)
    stale_entry = entry.model_copy(update={field: False})
    stale_snapshot = snapshot.model_copy(update={"agents": (stale_entry,)})
    monkeypatch.setattr(
        "app.services.execution_grant_service.build_registry_snapshot",
        lambda *args, **kwargs: stale_snapshot,
    )
    with pytest.raises(ExecutionGrantMintDenied):
        mint_execution_grant(
            db,
            request=_request(now),
            caller=caller,
            caller_user_id=1,
            now=now,
        )


def test_mint_rejects_model_binding_and_classification_toctou(db, monkeypatch) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    caller = _db_state(db, now)
    snapshot, entry = _snapshot(now)
    changed_binding = entry.manifest.model_binding.model_copy(
        update={"model_revision": "deployment-other"}
    )
    changed_manifest = entry.manifest.model_copy(update={"model_binding": changed_binding})
    changed_entry = entry.model_copy(update={"manifest": changed_manifest})
    changed_snapshot = snapshot.model_copy(update={"agents": (changed_entry,)})
    monkeypatch.setattr(
        "app.services.execution_grant_service.build_registry_snapshot",
        lambda *args, **kwargs: changed_snapshot,
    )
    with pytest.raises(ExecutionGrantMintDenied):
        mint_execution_grant(
            db,
            request=_request(now),
            caller=caller,
            caller_user_id=1,
            now=now,
        )

    low_entry = entry.model_copy(update={"classification_ceiling": "無機密"})
    low_snapshot = snapshot.model_copy(update={"agents": (low_entry,)})
    monkeypatch.setattr(
        "app.services.execution_grant_service.build_registry_snapshot",
        lambda *args, **kwargs: low_snapshot,
    )
    # Raise the request and durable Task/Run latch together so the registry
    # ceiling is the first authority that can deny the candidate.
    task = db.query(Task).one()
    run = db.query(TaskRun).one()
    task.classification_level = "機密"
    run.classification_level = "機密"
    db.commit()
    with pytest.raises(ExecutionGrantMintDenied):
        mint_execution_grant(
            db,
            request=_request_with(
                _request(now),
                classification="機密",
                policy_result={
                    **_request(now).policy_result.model_dump(mode="json"),
                    "effective_classification": "機密",
                },
            ),
            caller=caller,
            caller_user_id=1,
            now=now,
        )


def test_mint_rejects_route_decision_shapes_and_capability_mismatch(db, monkeypatch) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    caller = _db_state(db, now)
    _patch_registry(monkeypatch, now)
    request = _request(now)

    direct = RouteDecision(
        schema_version="route-decision/v1",
        decision_id="rd-direct",
        route_type="direct_answer",
        registry_snapshot_id=SNAPSHOT_ID,
        required_capabilities=(),
        candidate_agent_ids=(),
        reason_codes=("DIRECT",),
        confidence=0.9,
        constraints={"max_steps": 1, "timeout_ms": 1000},
        fallback="clarify",
    )
    with pytest.raises(ExecutionGrantMintDenied):
        mint_execution_grant(
            db,
            request=_request_with(request, route_decision=direct),
            caller=caller,
            caller_user_id=1,
            now=now,
        )

    unknown = request.route_decision.model_copy(
        update={"selected_agent_id": "unknown-agent", "candidate_agent_ids": ("unknown-agent",)}
    )
    with pytest.raises(ExecutionGrantMintDenied):
        mint_execution_grant(
            db,
            request=_request_with(request, route_decision=unknown),
            caller=caller,
            caller_user_id=1,
            now=now,
        )

    with pytest.raises(ExecutionGrantMintDenied):
        mint_execution_grant(
            db,
            request=_request_with(request, allowed_capabilities=("streaming",)),
            caller=caller,
            caller_user_id=1,
            now=now,
        )


def test_mint_rejects_policy_denial_approval_id_scope_and_obligation_mismatch(
    db, monkeypatch
) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    caller = _db_state(db, now)
    _patch_registry(monkeypatch, now)
    request = _request(now)
    variants = [
        {"allowed": False, "approval_required": True, "reason_codes": ("DENY",)},
        {"allowed": False, "approval_required": True, "reason_codes": ("APPROVAL_REQUIRED",)},
        {"decision_id": "pg-other"},
        {"required_scopes": ("other:scope",)},
        {"obligations": ("OTHER",)},
    ]
    for update in variants:
        policy_payload = request.policy_result.model_dump(mode="json")
        policy_payload.update(update)
        if update.get("allowed") is False:
            policy_payload["approval_required"] = bool(update.get("approval_required"))
        policy = PolicyGateResult.model_validate(policy_payload)
        with pytest.raises(ExecutionGrantMintDenied):
            mint_execution_grant(
                db,
                request=_request_with(request, policy_result=policy.model_dump(mode="json")),
                caller=caller,
                caller_user_id=1,
                now=now,
            )


def _unsigned_payload(token: str) -> dict:
    return jwt.decode(
        token,
        get_private_key(),
        algorithms=[ALGORITHM],
        options={
            "verify_signature": False,
            "verify_exp": False,
            "verify_iat": False,
            "verify_aud": False,
            "verify_iss": False,
        },
    )


def _resign(payload: dict, *, kid: str = "anila-v1", typ: str = EXECUTION_GRANT_TYPE, alg: str | None = None) -> str:
    headers = {"kid": kid, "typ": typ}
    if alg is not None:
        headers["alg"] = alg
    return jwt.encode(payload, get_private_key(), algorithm=ALGORITHM, headers=headers)


@pytest.mark.parametrize(
    "headers",
    [
        {"kid": "wrong-kid", "typ": EXECUTION_GRANT_TYPE},
        {"kid": "anila-v1", "typ": "wrong-type"},
        {"kid": "anila-v1", "typ": EXECUTION_GRANT_TYPE, "alg": "HS256"},
    ],
)
def test_verify_rejects_wrong_kid_alg_or_type(db, monkeypatch, headers) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    _caller, response = _minted(db, monkeypatch, now)
    with pytest.raises(ExecutionGrantVerificationError):
        verify_execution_grant_token(
            _resign(_unsigned_payload(response.token), **headers),
            now=now + timedelta(seconds=1),
        )


@pytest.mark.parametrize(
    "field_value",
    [
        ("aud", "wrong-audience"),
        ("type", "wrong-type"),
        ("iss", "wrong-issuer"),
        ("jti", ""),
    ],
)
def test_verify_rejects_wrong_aud_type_iss_or_empty_jti(db, monkeypatch, field_value) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    _caller, response = _minted(db, monkeypatch, now)
    payload = _unsigned_payload(response.token)
    payload[field_value[0]] = field_value[1]
    with pytest.raises(ExecutionGrantVerificationError):
        verify_execution_grant_token(
            _resign(payload),
            now=now + timedelta(seconds=1),
        )


def test_verify_rejects_expired_not_yet_active_and_bad_signature(db, monkeypatch) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    _caller, response = _minted(db, monkeypatch, now)
    with pytest.raises(ExecutionGrantVerificationError):
        verify_execution_grant_token(response.token, now=response.grant.expires_at)
    with pytest.raises(ExecutionGrantVerificationError):
        verify_execution_grant_token(response.token, now=response.grant.issued_at - timedelta(seconds=1))
    parts = response.token.split(".")
    parts[2] = ("a" if parts[2][0] != "a" else "b") + parts[2][1:]
    tampered = ".".join(parts)
    with pytest.raises(ExecutionGrantVerificationError):
        verify_execution_grant_token(tampered, now=now + timedelta(seconds=1))


@pytest.mark.parametrize("field", ["task_id", "run_id", "trace_id", "registry_snapshot_id", "target_agent_id"])
def test_verify_rejects_top_level_inner_binding_mismatch(db, monkeypatch, field) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    _caller, response = _minted(db, monkeypatch, now)
    payload = _unsigned_payload(response.token)
    if field in {"task_id", "run_id"}:
        payload[field] = 999
    else:
        payload[field] = "other-binding"
    with pytest.raises(ExecutionGrantVerificationError):
        verify_execution_grant_token(
            _resign(payload),
            now=now + timedelta(seconds=1),
        )


def test_verify_rejects_manifest_hash_revision_mismatch(db, monkeypatch) -> None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    _caller, response = _minted(db, monkeypatch, now)
    payload = _unsigned_payload(response.token)
    payload["manifest_revision"] = "sha256:" + "e" * 64
    with pytest.raises(ExecutionGrantVerificationError):
        verify_execution_grant_token(
            _resign(payload),
            now=now + timedelta(seconds=1),
        )


def test_mint_happy_path_recomputes_a_real_caller_scoped_registry(db, monkeypatch) -> None:
    """The positive path must not depend on a Router-supplied fake snapshot."""

    from app.services.agent_readiness import governance_fingerprint, manifest_revision
    from app.services.agent_registry import build_registry_snapshot

    now = datetime.now(timezone.utc).replace(microsecond=0)
    caller = _db_state(db, now)
    user = db.query(User).one()
    manifest = _manifest()
    model = ModelRegistry(
        name="real-model",
        display_name="real-model",
        model_type="llm",
        endpoint_url="https://model.internal/v1",
        is_active=True,
        health_status="healthy",
        health_checked_at=now,
        classification_ceiling="機密",
    )
    db.add(model)
    db.flush()
    manifest = manifest.model_copy(
        update={
            "model_binding": manifest.model_binding.model_copy(
                update={"model_id": model.id}
            ),
            "base_model_id": model.id,
        }
    )
    digest = manifest_sha256(manifest)
    agent = Agent(
        name="research-agent",
        owner_user_id=user.id,
        base_model_id=model.id,
        endpoint_url="https://agent.internal",
        api_version="v1",
        runtime_type="anila_agent",
        agent_version=manifest.version,
        description_for_router=manifest.description_for_router,
        manifest_json=manifest.model_dump(mode="json"),
        manifest_sha256=digest,
        manifest_revision=manifest_revision(manifest),
        trace_callback_mode="sse_and_post",
        health_status="healthy",
        health_checked_at=now,
        approval_status="approved",
        audit_level="full_trace",
        classification_ceiling="機密",
        default_classification_level="無機密",
        trace_test_passed_at=now,
        is_active=True,
    )
    db.add(agent)
    db.flush()
    db.add(UserAgentPermission(user_id=user.id, agent_id=agent.id))
    fingerprint = governance_fingerprint(agent, base_model=model)
    agent.trace_test_governance_fingerprint = fingerprint
    agent.trace_test_report = {
        "passed": True,
        "checked_at": now.isoformat(),
        "governance_fingerprint": fingerprint,
    }
    db.commit()
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "agent.internal")

    current = build_registry_snapshot(db, user_id=user.id, now=now)
    assert len(current.agents) == 1
    entry = current.agents[0]
    assert entry.ready_for_dispatch, entry.reason_codes
    base = _request(now)
    route = base.route_decision.model_copy(
        update={"registry_snapshot_id": current.registry_snapshot_id}
    )
    policy = base.policy_result.model_copy(
        update={
            "registry_snapshot_id": current.registry_snapshot_id,
            "target_agent_id": entry.agent_id,
        }
    )
    request = _request_with(
        base,
        registry_snapshot_id=current.registry_snapshot_id,
        registry_snapshot_revision=current.snapshot_revision,
        registry_snapshot_hash=current.snapshot_hash,
        target_agent_id=entry.agent_id,
        manifest_revision=entry.manifest_revision,
        manifest_sha256=entry.manifest_sha256,
        model_binding=entry.manifest.model_binding.model_dump(mode="json"),
        route_decision=route.model_dump(mode="json"),
        policy_result=policy.model_dump(mode="json"),
    )
    response = mint_execution_grant(
        db,
        request=request,
        caller=caller,
        caller_user_id=user.id,
        now=now,
    )
    assert response.grant.registry_snapshot_id == current.registry_snapshot_id
    assert verify_execution_grant_token(response.token, now=now + timedelta(seconds=1))
