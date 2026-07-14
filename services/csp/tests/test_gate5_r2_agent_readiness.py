"""Gate 5 R2 tests for CSP-owned Agent readiness and registry admission."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from anila_contracts import AgentManifest
from app.config import settings
from app.main import app
from app.models.agent import Agent, UserAgentPermission
from app.services.agent_credential_service import CallerIdentity
from app.services.agent_readiness import (
    AGENT_INACTIVE,
    AUDIT_LEVEL_NOT_FULL_TRACE,
    BASE_MODEL_CEILING_INVALID,
    BASE_MODEL_HEALTH_STALE,
    BASE_MODEL_HEALTH_UNKNOWN,
    BASE_MODEL_HEALTH_UNHEALTHY,
    BASE_MODEL_INACTIVE,
    BASE_MODEL_MISSING,
    CLASSIFICATION_DEFAULT_EXCEEDS_CEILING,
    CLASSIFICATION_DEFAULT_MISMATCH,
    CLASSIFICATION_CEILING_INVALID,
    ENDPOINT_SSRF_DENIED,
    HEALTH_STALE,
    HEALTH_UNKNOWN,
    HEALTH_UNHEALTHY,
    MANIFEST_AGENT_ID_MISMATCH,
    MANIFEST_HASH_MISSING,
    MANIFEST_HASH_MISMATCH,
    MANIFEST_INVALID,
    MANIFEST_AGENT_VERSION_MISMATCH,
    MANIFEST_API_VERSION_MISMATCH,
    MANIFEST_RUNTIME_TYPE_MISMATCH,
    MANIFEST_MISSING,
    MANIFEST_REVISION_MISSING,
    MANIFEST_REVISION_MISMATCH,
    MODEL_BINDING_INVALID,
    REQUEST_CLASSIFICATION_EXCEEDS_CEILING,
    TRACE_CALLBACK_POSTURE_INVALID,
    TRACE_PROTOCOL_INVALID,
    TRACE_TEST_FINGERPRINT_MISSING,
    TRACE_TEST_FINGERPRINT_MISMATCH,
    TRACE_TEST_MISSING,
    TRACE_TEST_STALE,
    evaluate_agent_readiness,
    governance_fingerprint,
    invalidate_trace_evidence,
    manifest_revision,
    manifest_sha256,
)
from app.services.agent_registry import build_registry_snapshot
from app.schemas.agent_registry import AgentRegistrySnapshot
from app.services.proxy.service import _lock_registry_admission
from app.services.proxy.service import proxy_request
from app.services.proxy import service as proxy_service
from app.api.proxy import resume_agent_session
from app.services.auth_service import verify_service_token
from tests.conftest import make_agent, make_model, make_user


def _manifest(*, agent_id: int, model_id: int) -> dict:
    return {
        "schema_version": "agent-manifest/v1",
        "agent_id": str(agent_id),
        "name": "R2 readiness agent",
        "version": "1.0.0",
        "runtime_type": "openai_compatible_agent",
        "api_version": "v1",
        "supported_task_types": ["chat"],
        "description_for_router": "A canonical R2 test agent",
        "input_schema": {},
        "output_schema": {},
        "capabilities": {"retrieval": False, "tools": [], "streaming": True},
        "event_protocols": ["step-event/v1"],
        "required_scopes": ["agent.invoke"],
        "classification": {"ceiling": "無機密", "default": "無機密"},
        "full_trace_required": True,
        "supports_streaming": True,
        "supports_resume": False,
        "supports_cancel": False,
        "supports_idempotency": True,
        "model_binding": {"model_id": model_id, "gateway": "csp"},
        "base_model_id": model_id,
    }


def _ready_agent(db):
    now = datetime.now(timezone.utc)
    owner = make_user(db, username="r2-owner", role="admin")
    model = make_model(db, name="r2-model")
    model.health_status = "healthy"
    model.health_checked_at = now
    model.classification_ceiling = "無機密"
    agent = make_agent(db, owner, name="r2-agent", approval_status="approved")
    agent.base_model_id = model.id
    agent.is_active = True
    agent.endpoint_url = "http://agent:9100"
    agent.health_status = "healthy"
    agent.health_checked_at = now
    agent.audit_level = "full_trace"
    agent.trace_callback_mode = "sse_and_post"
    agent.classification_ceiling = "無機密"
    agent.default_classification_level = "無機密"
    canonical = AgentManifest.model_validate(
        _manifest(agent_id=agent.id, model_id=model.id)
    )
    agent.runtime_type = canonical.runtime_type.value
    agent.api_version = canonical.api_version
    agent.agent_version = canonical.version
    agent.manifest_json = canonical.model_dump(mode="json")
    agent.manifest_sha256 = manifest_sha256(canonical)
    agent.manifest_revision = manifest_revision(canonical)
    agent.trace_test_passed_at = now
    db.flush()
    fingerprint = governance_fingerprint(agent, base_model=model)
    agent.trace_test_governance_fingerprint = fingerprint
    agent.trace_test_report = {
        "passed": True,
        "checked_at": now.isoformat(),
        "governance_fingerprint": fingerprint,
    }
    db.commit()
    db.refresh(agent)
    db.refresh(model)
    return owner, model, agent, now


def _allow_agent_endpoint(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_AGENT_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "agent")


def test_ready_predicate_requires_fresh_health_and_bound_trace_evidence(db, monkeypatch):
    _allow_agent_endpoint(monkeypatch)
    _owner, _model, agent, now = _ready_agent(db)

    decision = evaluate_agent_readiness(agent, db=db, now=now)
    assert decision.ready_for_dispatch is True
    assert decision.reason_codes == ()

    agent.health_checked_at = now - timedelta(
        seconds=settings.AGENT_HEALTH_FRESHNESS_SECONDS + 1
    )
    stale = evaluate_agent_readiness(agent, db=db, now=now)
    assert stale.ready_for_dispatch is False
    assert HEALTH_STALE in stale.reason_codes

    agent.health_checked_at = now
    agent.trace_test_governance_fingerprint = "0" * 64
    changed = evaluate_agent_readiness(agent, db=db, now=now)
    assert TRACE_TEST_FINGERPRINT_MISMATCH in changed.reason_codes


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda agent, model, now: setattr(agent, "is_active", False), AGENT_INACTIVE),
        (lambda agent, model, now: setattr(agent, "approval_status", "pending_connection_test"), "NOT_APPROVED"),
        (lambda agent, model, now: setattr(agent, "health_status", "unknown"), HEALTH_UNKNOWN),
        (lambda agent, model, now: setattr(agent, "health_status", "unhealthy"), HEALTH_UNHEALTHY),
        (
            lambda agent, model, now: setattr(
                agent,
                "health_checked_at",
                now - timedelta(seconds=settings.AGENT_HEALTH_FRESHNESS_SECONDS + 1),
            ),
            HEALTH_STALE,
        ),
    ],
)
def test_readiness_approval_and_health_matrix(db, monkeypatch, mutate, expected):
    _allow_agent_endpoint(monkeypatch)
    _owner, model, agent, now = _ready_agent(db)
    mutate(agent, model, now)
    decision = evaluate_agent_readiness(agent, db=db, now=now)
    assert decision.ready_for_dispatch is False
    assert expected in decision.reason_codes


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda agent, model, now: setattr(agent, "manifest_json", None), MANIFEST_MISSING),
        (
            lambda agent, model, now: setattr(
                agent, "manifest_json", {"schema_version": "agent-manifest/v1"}
            ),
            MANIFEST_INVALID,
        ),
        (lambda agent, model, now: setattr(agent, "manifest_sha256", None), MANIFEST_HASH_MISSING),
        (lambda agent, model, now: setattr(agent, "manifest_sha256", "0" * 64), MANIFEST_HASH_MISMATCH),
        (lambda agent, model, now: setattr(agent, "manifest_revision", None), MANIFEST_REVISION_MISSING),
        (
            lambda agent, model, now: setattr(agent, "manifest_revision", "sha256:" + "0" * 64),
            MANIFEST_REVISION_MISMATCH,
        ),
        (
            lambda agent, model, now: agent.manifest_json.__setitem__("agent_id", "other-agent"),
            MANIFEST_AGENT_ID_MISMATCH,
        ),
    ],
)
def test_readiness_manifest_matrix(db, monkeypatch, mutate, expected):
    _allow_agent_endpoint(monkeypatch)
    _owner, model, agent, now = _ready_agent(db)
    mutate(agent, model, now)
    decision = evaluate_agent_readiness(agent, db=db, now=now)
    assert decision.ready_for_dispatch is False
    assert expected in decision.reason_codes


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda agent, model, now: setattr(agent, "audit_level", "summary"), AUDIT_LEVEL_NOT_FULL_TRACE),
        (lambda agent, model, now: setattr(agent, "trace_callback_mode", None), TRACE_CALLBACK_POSTURE_INVALID),
        (
            lambda agent, model, now: agent.manifest_json.__setitem__(
                "full_trace_required", False
            ),
            AUDIT_LEVEL_NOT_FULL_TRACE,
        ),
        (
            lambda agent, model, now: agent.manifest_json.__setitem__(
                "event_protocols", ["other/v1"]
            ),
            TRACE_PROTOCOL_INVALID,
        ),
    ],
)
def test_readiness_trace_posture_matrix(db, monkeypatch, mutate, expected):
    _allow_agent_endpoint(monkeypatch)
    _owner, model, agent, now = _ready_agent(db)
    mutate(agent, model, now)
    decision = evaluate_agent_readiness(agent, db=db, now=now)
    assert decision.ready_for_dispatch is False
    assert expected in decision.reason_codes


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda agent, model, now: (
                setattr(agent, "trace_test_passed_at", None),
                setattr(agent, "trace_test_report", None),
            ),
            TRACE_TEST_MISSING,
        ),
        (
            lambda agent, model, now: setattr(
                agent,
                "trace_test_passed_at",
                now - timedelta(seconds=settings.AGENT_TRACE_TEST_FRESHNESS_SECONDS + 1),
            ),
            TRACE_TEST_STALE,
        ),
        (
            lambda agent, model, now: setattr(agent, "trace_test_governance_fingerprint", None),
            TRACE_TEST_FINGERPRINT_MISSING,
        ),
        (
            lambda agent, model, now: agent.trace_test_report.__setitem__(
                "governance_fingerprint", "0" * 64
            ),
            TRACE_TEST_FINGERPRINT_MISMATCH,
        ),
    ],
)
def test_readiness_trace_evidence_matrix(db, monkeypatch, mutate, expected):
    _allow_agent_endpoint(monkeypatch)
    _owner, model, agent, now = _ready_agent(db)
    mutate(agent, model, now)
    decision = evaluate_agent_readiness(agent, db=db, now=now)
    assert decision.ready_for_dispatch is False
    assert expected in decision.reason_codes


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda agent, model, now: setattr(agent, "base_model_id", None), BASE_MODEL_MISSING),
        (lambda agent, model, now: setattr(model, "is_active", False), BASE_MODEL_INACTIVE),
        (lambda agent, model, now: setattr(model, "health_status", "unhealthy"), BASE_MODEL_HEALTH_UNHEALTHY),
        (lambda agent, model, now: setattr(model, "health_status", "unknown"), BASE_MODEL_HEALTH_UNKNOWN),
        (
            lambda agent, model, now: setattr(
                model,
                "health_checked_at",
                now - timedelta(seconds=settings.MODEL_HEALTH_FRESHNESS_SECONDS + 1),
            ),
            BASE_MODEL_HEALTH_STALE,
        ),
        (lambda agent, model, now: setattr(model, "classification_ceiling", "bad"), BASE_MODEL_CEILING_INVALID),
        (lambda agent, model, now: agent.manifest_json.__setitem__("model_binding", None), MODEL_BINDING_INVALID),
    ],
)
def test_readiness_model_binding_matrix(db, monkeypatch, mutate, expected):
    _allow_agent_endpoint(monkeypatch)
    _owner, model, agent, now = _ready_agent(db)
    mutate(agent, model, now)
    decision = evaluate_agent_readiness(agent, db=db, now=now)
    assert decision.ready_for_dispatch is False
    assert expected in decision.reason_codes


def test_readiness_rejects_named_manifest_model_binding(db, monkeypatch):
    _allow_agent_endpoint(monkeypatch)
    _owner, model, agent, now = _ready_agent(db)
    manifest = dict(agent.manifest_json)
    manifest.pop("base_model_id", None)
    manifest["model_binding"] = {
        "model_id": model.name,
        "gateway": "csp",
    }
    agent.manifest_json = manifest
    decision = evaluate_agent_readiness(agent, db=db, now=now)
    assert decision.ready_for_dispatch is False
    assert MODEL_BINDING_INVALID in decision.reason_codes


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("runtime_type", MANIFEST_RUNTIME_TYPE_MISMATCH),
        ("api_version", MANIFEST_API_VERSION_MISMATCH),
        ("agent_version", MANIFEST_AGENT_VERSION_MISMATCH),
    ],
)
def test_readiness_rejects_manifest_csp_row_drift(db, monkeypatch, field, expected):
    _allow_agent_endpoint(monkeypatch)
    _owner, _model, agent, now = _ready_agent(db)
    setattr(agent, field, "drifted")
    decision = evaluate_agent_readiness(agent, db=db, now=now)
    assert decision.ready_for_dispatch is False
    assert expected in decision.reason_codes


@pytest.mark.parametrize(
    ("agent_ceiling", "manifest_ceiling", "model_ceiling", "expected"),
    [
        ("機密", "營業秘密", "極機密", "營業秘密"),
        ("無機密", "機密", "營業秘密", "無機密"),
        ("極機密", "機密", "營業秘密", "營業秘密"),
    ],
)
def test_registry_projects_lowest_effective_classification_ceiling(
    db,
    monkeypatch,
    agent_ceiling,
    manifest_ceiling,
    model_ceiling,
    expected,
):
    _allow_agent_endpoint(monkeypatch)
    _owner, model, agent, now = _ready_agent(db)
    agent.classification_ceiling = agent_ceiling
    model.classification_ceiling = model_ceiling
    manifest = dict(agent.manifest_json)
    manifest["classification"] = dict(manifest["classification"])
    manifest["classification"]["ceiling"] = manifest_ceiling
    agent.manifest_json = manifest
    db.commit()
    snapshot = build_registry_snapshot(db, user_id=agent.owner_user_id, now=now)
    assert snapshot.agents[0].classification_ceiling == expected


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (
            lambda agent, model, now: setattr(agent, "classification_ceiling", "bad"),
            CLASSIFICATION_CEILING_INVALID,
        ),
        (
            lambda agent, model, now: setattr(agent, "default_classification_level", "機密"),
            CLASSIFICATION_DEFAULT_MISMATCH,
        ),
        (
            lambda agent, model, now: setattr(agent, "default_classification_level", "機密"),
            CLASSIFICATION_DEFAULT_EXCEEDS_CEILING,
        ),
    ],
)
def test_readiness_classification_matrix(db, monkeypatch, mutate, expected):
    _allow_agent_endpoint(monkeypatch)
    _owner, model, agent, now = _ready_agent(db)
    mutate(agent, model, now)
    decision = evaluate_agent_readiness(agent, db=db, now=now)
    assert decision.ready_for_dispatch is False
    assert expected in decision.reason_codes


def test_readiness_rejects_requested_classification_above_ceiling(db, monkeypatch):
    _allow_agent_endpoint(monkeypatch)
    _owner, _model, agent, now = _ready_agent(db)
    decision = evaluate_agent_readiness(
        agent, db=db, now=now, requested_classification="機密"
    )
    assert decision.ready_for_dispatch is False
    assert REQUEST_CLASSIFICATION_EXCEEDS_CEILING in decision.reason_codes


def test_endpoint_ssrf_is_a_readiness_denial(db, monkeypatch):
    _owner, _model, agent, now = _ready_agent(db)
    agent.endpoint_url = "http://127.0.0.1:9100"
    decision = evaluate_agent_readiness(agent, db=db, now=now)
    assert decision.ready_for_dispatch is False
    assert ENDPOINT_SSRF_DENIED in decision.reason_codes


def test_governance_fingerprint_ignores_health_probe_timestamps(db, monkeypatch):
    _allow_agent_endpoint(monkeypatch)
    _owner, model, agent, now = _ready_agent(db)
    before = governance_fingerprint(agent, base_model=model)
    model.health_status = "healthy"
    model.health_checked_at = now + timedelta(seconds=5)
    db.commit()
    after_probe = governance_fingerprint(agent, base_model=model)
    assert after_probe == before

    model.endpoint_url = "http://model-changed:8080"
    db.commit()
    assert governance_fingerprint(agent, base_model=model) != before


def test_governance_update_invalidates_trace_evidence_and_approval(db):
    _owner, _model, agent, _now = _ready_agent(db)
    invalidate_trace_evidence(agent)
    assert agent.approval_status == "pending_connection_test"
    assert agent.trace_test_passed_at is None
    assert agent.trace_test_report is None
    assert agent.trace_test_governance_fingerprint is None


def test_registration_and_update_use_content_revision_not_manifest_version(
    client, db, monkeypatch
):
    _allow_agent_endpoint(monkeypatch)
    owner = make_user(db, username="r2-register", role="developer")
    model = make_model(db, name="r2-register-model")
    token = client.post(
        "/api/auth/login",
        json={"username": owner.username, "password": "password"},
    ).json()["access_token"]
    payload = _manifest(agent_id=999, model_id=model.id)
    payload["agent_id"] = "r2-register-agent"
    response = client.post(
        "/api/agents/register",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": "r2-register-agent",
            "endpoint_url": "http://agent:9100",
            "description_for_router": "r2",
            "base_model_id": model.id,
            "manifest": payload,
        },
    )
    assert response.status_code == 200, response.text
    original_revision = response.json()["manifest_revision"]
    assert original_revision.startswith("sha256:")
    assert original_revision != payload["version"]

    payload["description_for_router"] = "r2 changed without a version bump"
    update = client.put(
        f"/api/agents/{response.json()['id']}",
        headers={"Authorization": f"Bearer {token}"},
        json={"manifest": payload},
    )
    assert update.status_code == 200, update.text
    assert update.json()["manifest_revision"] != original_revision
    db_agent = db.get(Agent, response.json()["id"])
    # The update itself is a governance mutation, so any old trace evidence
    # and approval state are invalidated even though the semantic version is
    # unchanged.
    assert db_agent.trace_test_passed_at is None
    assert db_agent.approval_status == "pending_connection_test"


def test_registry_snapshot_is_caller_scoped_safe_and_revisioned(db, monkeypatch):
    _allow_agent_endpoint(monkeypatch)
    owner, _model, agent, now = _ready_agent(db)
    first = build_registry_snapshot(db, user_id=owner.id, now=now)
    second = build_registry_snapshot(
        db, user_id=owner.id, now=now + timedelta(seconds=1)
    )
    assert first.snapshot_revision == second.snapshot_revision
    assert first.agents[0].ready_for_dispatch is True
    assert first.agents[0].manifest_revision == agent.manifest_revision
    assert first.agents[0].approval_required is False
    assert "endpoint_url" not in first.agents[0].model_dump()
    assert "endpoint_url" not in first.agents[0].base_model.model_dump()

    # Probe timestamps are deliberately excluded from the generation hash;
    # the current response still carries fresh timestamps for readiness UI.
    model = _model
    model.health_checked_at = now + timedelta(seconds=2)
    agent.health_checked_at = now + timedelta(seconds=2)
    db.commit()
    after_probe = build_registry_snapshot(
        db, user_id=owner.id, now=now + timedelta(seconds=2)
    )
    assert after_probe.snapshot_id == first.snapshot_id
    _model.health_status = "unhealthy"
    db.commit()
    after_state_change = build_registry_snapshot(
        db, user_id=owner.id, now=now + timedelta(seconds=3)
    )
    assert after_state_change.snapshot_id != first.snapshot_id


def test_registry_schema_is_strict_versioned_and_frozen():
    snapshot_hash = "a" * 64
    with pytest.raises(ValidationError):
        AgentRegistrySnapshot.model_validate(
            {
                "schema_version": "agent-registry/v2",
                "snapshot_id": snapshot_hash,
                "snapshot_revision": snapshot_hash,
                "snapshot_hash": snapshot_hash,
                "registry_snapshot_id": snapshot_hash,
                "fetched_at": datetime.now(timezone.utc),
                "expires_at": datetime.now(timezone.utc) + timedelta(seconds=1),
                "caller_user_id": 1,
                "agents": [],
            }
        )
    snapshot = AgentRegistrySnapshot(
        schema_version="agent-registry/v1",
        snapshot_id=snapshot_hash,
        snapshot_revision=snapshot_hash,
        snapshot_hash=snapshot_hash,
        registry_snapshot_id=snapshot_hash,
        fetched_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=1),
        caller_user_id=1,
        agents=[],
    )
    with pytest.raises(ValidationError):
        snapshot.snapshot_id = "sha256:y"
    with pytest.raises(AttributeError):
        snapshot.agents.append(None)  # type: ignore[union-attr]
    with pytest.raises(ValidationError):
        AgentRegistrySnapshot.model_validate(
            {
                "schema_version": "agent-registry/v1",
                "snapshot_id": snapshot_hash,
                "snapshot_revision": snapshot_hash,
                "snapshot_hash": snapshot_hash,
                "registry_snapshot_id": "b" * 64,
                "fetched_at": datetime.now(timezone.utc),
                "expires_at": datetime.now(timezone.utc) + timedelta(seconds=1),
                "caller_user_id": 1,
                "agents": [],
            }
        )
    with pytest.raises(ValidationError):
        AgentRegistrySnapshot.model_validate(
            {
                "schema_version": "agent-registry/v1",
                "snapshot_id": snapshot_hash,
                "snapshot_revision": snapshot_hash,
                "snapshot_hash": snapshot_hash,
                "registry_snapshot_id": snapshot_hash,
                "fetched_at": datetime.now(timezone.utc),
                "expires_at": datetime.now(timezone.utc) + timedelta(seconds=1),
                "caller_user_id": 1,
                "agents": [],
                "unexpected": True,
            }
        )


@pytest.mark.parametrize(
    "field_value",
    [
        ("approved", False),
        ("health_ready", False),
        ("trace_test_passed", False),
        ("manifest_valid", False),
        ("endpoint_via_csp", False),
        ("is_active", False),
        ("approval_status", "pending_security_review"),
        ("health_status", "unhealthy"),
        ("health_checked_at", None),
        ("trace_test_passed_at", None),
        ("trace_test_governance_fingerprint", ""),
        ("classification_ceiling", ""),
        ("default_classification_level", ""),
        ("model_gateway", ""),
        ("base_model", None),
        ("manifest", None),
        ("manifest_sha256", ""),
        ("manifest_revision", ""),
    ],
)
def test_registry_entry_ready_state_is_self_consistent(db, monkeypatch, field_value):
    _allow_agent_endpoint(monkeypatch)
    owner, _model, _agent, now = _ready_agent(db)
    entry = build_registry_snapshot(db, user_id=owner.id, now=now).agents[0]
    payload = entry.model_dump()
    payload[field_value[0]] = field_value[1]
    with pytest.raises(ValidationError):
        type(entry).model_validate(payload)


@pytest.mark.parametrize(
    "field_value",
    [
        ("manifest_sha256", "sha256:" + "a" * 64),
        ("manifest_revision", "1.0.0"),
    ],
)
def test_registry_entry_rejects_noncanonical_content_identity(db, monkeypatch, field_value):
    _allow_agent_endpoint(monkeypatch)
    owner, _model, _agent, now = _ready_agent(db)
    entry = build_registry_snapshot(db, user_id=owner.id, now=now).agents[0]
    payload = entry.model_dump()
    payload[field_value[0]] = field_value[1]
    with pytest.raises(ValidationError):
        type(entry).model_validate(payload)


def test_final_agent_sink_requires_snapshot_in_formal_profile(db, monkeypatch):
    _allow_agent_endpoint(monkeypatch)
    owner, model, agent, now = _ready_agent(db)
    monkeypatch.setattr(settings, "ALLOW_LEGACY_AGENT_DISPATCH", False)
    with pytest.raises(HTTPException) as exc_info:
        _lock_registry_admission(
            governance_db=db,
            registry_model_id=None,
            registry_agent_id=agent.id,
            registry_endpoint_url=agent.endpoint_url,
            admitted_classification_level="無機密",
            registry_user_id=owner.id,
        )
    assert exc_info.value.status_code == 403
    assert "snapshot" in str(exc_info.value.detail)

    snapshot = build_registry_snapshot(db, user_id=owner.id, now=now)
    _lock_registry_admission(
        governance_db=db,
        registry_model_id=None,
        registry_agent_id=agent.id,
        registry_endpoint_url=agent.endpoint_url,
        admitted_classification_level="無機密",
        registry_user_id=owner.id,
        registry_snapshot_id=snapshot.snapshot_id,
        registry_snapshot_revision=snapshot.snapshot_revision,
        registry_snapshot_hash=snapshot.snapshot_hash,
        registry_manifest_revision=agent.manifest_revision,
        registry_manifest_sha256=agent.manifest_sha256,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"registry_snapshot_revision": "sha256:stale"},
        {
            "registry_snapshot_revision": "sha256:stale",
            "registry_manifest_revision": "sha256:stale",
            "registry_manifest_sha256": "0" * 64,
        },
    ],
)
async def test_final_proxy_sink_rejects_bad_registry_evidence_before_network(
    db, monkeypatch, headers
):
    _allow_agent_endpoint(monkeypatch)
    owner, _model, agent, _now = _ready_agent(db)
    monkeypatch.setattr(settings, "ALLOW_LEGACY_AGENT_DISPATCH", False)
    called = False

    async def _must_not_run(**kwargs):
        nonlocal called
        called = True
        raise AssertionError("downstream agent network must not run")

    monkeypatch.setattr(proxy_service, "_proxy_request_impl", _must_not_run)
    target = SimpleNamespace(
        id=agent.id,
        name=agent.name,
        model_type="agent",
        endpoint_url=agent.endpoint_url,
    )
    with pytest.raises(HTTPException):
        await proxy_request(
            model=target,
            api_key_id=1,
            user_id=owner.id,
            department_id=None,
            request_body={"messages": []},
            endpoint_path="/v1/chat/completions",
            target_agent_id=agent.id,
            inference_callsite_id="csp.agent_dispatch",
            governance_db=db,
            admitted_classification_level="無機密",
            registry_user_id=owner.id,
            **headers,
        )
    assert called is False


@pytest.mark.asyncio
async def test_legacy_partial_registry_evidence_rejects_before_network(
    db, monkeypatch
):
    """ALLOW_LEGACY is headerless-only; partial evidence cannot reach HTTP."""

    _allow_agent_endpoint(monkeypatch)
    owner, _model, agent, _now = _ready_agent(db)
    monkeypatch.setattr(settings, "ALLOW_LEGACY_AGENT_DISPATCH", True)
    called = False

    async def _must_not_run(**kwargs):
        nonlocal called
        called = True
        raise AssertionError("partial registry evidence reached network")

    monkeypatch.setattr(proxy_service, "_proxy_request_impl", _must_not_run)
    target = SimpleNamespace(
        id=agent.id,
        name=agent.name,
        model_type="agent",
        endpoint_url=agent.endpoint_url,
    )
    with pytest.raises(HTTPException):
        await proxy_request(
            model=target,
            api_key_id=1,
            user_id=owner.id,
            department_id=None,
            request_body={"messages": []},
            endpoint_path="/v1/chat/completions",
            target_agent_id=agent.id,
            inference_callsite_id="csp.agent_dispatch",
            governance_db=db,
            admitted_classification_level="無機密",
            registry_user_id=owner.id,
            # One evidence field is enough to activate the formal validator;
            # all five fields must be supplied and match.
            registry_snapshot_revision="a" * 64,
        )
    assert called is False


@pytest.mark.asyncio
async def test_final_proxy_sink_rejects_inactive_agent_before_network(db, monkeypatch):
    _allow_agent_endpoint(monkeypatch)
    owner, _model, agent, now = _ready_agent(db)
    agent.is_active = False
    db.commit()
    snapshot = build_registry_snapshot(db, user_id=owner.id, now=now)
    called = False

    async def _must_not_run(**kwargs):
        nonlocal called
        called = True
        raise AssertionError("downstream agent network must not run")

    monkeypatch.setattr(proxy_service, "_proxy_request_impl", _must_not_run)
    target = SimpleNamespace(
        id=agent.id,
        name=agent.name,
        model_type="agent",
        endpoint_url=agent.endpoint_url,
    )
    with pytest.raises(HTTPException):
        await proxy_request(
            model=target,
            api_key_id=1,
            user_id=owner.id,
            department_id=None,
            request_body={"messages": []},
            endpoint_path="/v1/chat/completions",
            target_agent_id=agent.id,
            inference_callsite_id="csp.agent_dispatch",
            governance_db=db,
            admitted_classification_level="無機密",
            registry_user_id=owner.id,
            registry_snapshot_id=snapshot.snapshot_id,
            registry_snapshot_revision=snapshot.snapshot_revision,
            registry_snapshot_hash=snapshot.snapshot_hash,
            registry_manifest_revision=agent.manifest_revision,
            registry_manifest_sha256=agent.manifest_sha256,
        )
    assert called is False


@pytest.mark.asyncio
async def test_formal_resume_fails_closed_before_agent_lookup_or_network(monkeypatch):
    monkeypatch.setattr(settings, "ALLOW_LEGACY_AGENT_DISPATCH", False)
    with pytest.raises(HTTPException) as exc_info:
        await resume_agent_session(
            "agent",
            "session",
            request=None,
            caller=None,
            db=None,
        )
    assert exc_info.value.status_code == 409


def test_explicit_legacy_dispatch_flag_only_bypasses_snapshot_header(db, monkeypatch):
    _allow_agent_endpoint(monkeypatch)
    _owner, _model, agent, _now = _ready_agent(db)
    monkeypatch.setattr(settings, "ALLOW_LEGACY_AGENT_DISPATCH", True)
    _lock_registry_admission(
        governance_db=db,
        registry_model_id=None,
        registry_agent_id=agent.id,
        registry_endpoint_url=agent.endpoint_url,
        admitted_classification_level="無機密",
    )


def test_legacy_flag_does_not_bypass_partial_registry_evidence(db, monkeypatch):
    _allow_agent_endpoint(monkeypatch)
    _owner, _model, agent, _now = _ready_agent(db)
    monkeypatch.setattr(settings, "ALLOW_LEGACY_AGENT_DISPATCH", True)
    with pytest.raises(HTTPException):
        _lock_registry_admission(
            governance_db=db,
            registry_model_id=None,
            registry_agent_id=agent.id,
            registry_endpoint_url=agent.endpoint_url,
            admitted_classification_level="無機密",
            registry_snapshot_revision="a" * 64,
        )


def test_final_sink_requires_caller_scoped_target_membership(db, monkeypatch):
    _allow_agent_endpoint(monkeypatch)
    _owner, _model, agent, now = _ready_agent(db)
    caller = make_user(db, username="r2-nonadmin-caller", role="developer")
    snapshot = build_registry_snapshot(db, user_id=caller.id, now=now)
    assert snapshot.agents == ()
    monkeypatch.setattr(settings, "ALLOW_LEGACY_AGENT_DISPATCH", False)
    with pytest.raises(HTTPException) as exc_info:
        _lock_registry_admission(
            governance_db=db,
            registry_model_id=None,
            registry_agent_id=agent.id,
            registry_endpoint_url=agent.endpoint_url,
            admitted_classification_level="無機密",
            registry_user_id=caller.id,
            registry_snapshot_id=snapshot.snapshot_id,
            registry_snapshot_revision=snapshot.snapshot_revision,
            registry_snapshot_hash=snapshot.snapshot_hash,
            registry_manifest_revision=agent.manifest_revision,
            registry_manifest_sha256=agent.manifest_sha256,
        )
    assert exc_info.value.status_code == 403


def test_final_sink_rechecks_permission_and_target_identity_on_current_snapshot(
    db, monkeypatch
):
    _allow_agent_endpoint(monkeypatch)
    _owner, _model, agent, now = _ready_agent(db)
    caller = make_user(db, username="r2-scoped-caller", role="developer")
    db.add(UserAgentPermission(user_id=caller.id, agent_id=agent.id))
    db.commit()
    snapshot = build_registry_snapshot(db, user_id=caller.id, now=now)
    assert snapshot.agents[0].registry_id == agent.id
    monkeypatch.setattr(settings, "ALLOW_LEGACY_AGENT_DISPATCH", False)
    db.delete(db.query(UserAgentPermission).filter_by(user_id=caller.id, agent_id=agent.id).one())
    db.commit()
    with pytest.raises(HTTPException) as exc_info:
        _lock_registry_admission(
            governance_db=db,
            registry_model_id=None,
            registry_agent_id=agent.id,
            registry_endpoint_url=agent.endpoint_url,
            admitted_classification_level="無機密",
            registry_user_id=caller.id,
            registry_snapshot_id=snapshot.snapshot_id,
            registry_snapshot_revision=snapshot.snapshot_revision,
            registry_snapshot_hash=snapshot.snapshot_hash,
            registry_manifest_revision=agent.manifest_revision,
            registry_manifest_sha256=agent.manifest_sha256,
        )
    assert exc_info.value.status_code in {403, 409}


def test_internal_registry_requires_named_service_client_and_user_context(
    client, db, monkeypatch
):
    _allow_agent_endpoint(monkeypatch)
    owner, _model, _agent, _now = _ready_agent(db)

    named_router = CallerIdentity(
        kind="service_client",
        agent_id=None,
        service_client_id=1,
        credential_id=1,
        is_legacy=False,
        used_previous_token=False,
    )
    app.dependency_overrides[verify_service_token] = lambda: named_router
    try:
        legacy_employee_header = client.get(
            "/internal/v1/agents/registry",
            headers={"X-ANILA-User-Id": str(owner.id)},
        )
        assert legacy_employee_header.status_code == 400
        response = client.get(
            "/internal/v1/agents/registry",
            headers={"X-ANILA-Caller-User-Id": str(owner.id)},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["snapshot_id"] == payload["snapshot_revision"]
        assert payload["agents"][0]["snapshot_id"] == payload["snapshot_id"]
        assert "endpoint_url" not in payload["agents"][0]
    finally:
        app.dependency_overrides.pop(verify_service_token, None)


@pytest.mark.parametrize(
    "identity",
    [
        CallerIdentity(
            kind="agent",
            agent_id=1,
            service_client_id=None,
            credential_id=1,
            is_legacy=False,
            used_previous_token=False,
        ),
        CallerIdentity(
            kind="service_client",
            agent_id=None,
            service_client_id=1,
            credential_id=1,
            is_legacy=True,
            used_previous_token=False,
        ),
    ],
)
def test_internal_registry_rejects_agent_or_legacy_identity(client, identity):
    app.dependency_overrides[verify_service_token] = lambda: identity
    try:
        response = client.get(
            "/internal/v1/agents/registry",
            headers={"X-ANILA-Caller-User-Id": "1"},
        )
        assert response.status_code == 403
    finally:
        app.dependency_overrides.pop(verify_service_token, None)


def test_internal_registry_requires_user_context(client):
    identity = CallerIdentity(
        kind="service_client",
        agent_id=None,
        service_client_id=1,
        credential_id=1,
        is_legacy=False,
        used_previous_token=False,
    )
    app.dependency_overrides[verify_service_token] = lambda: identity
    try:
        response = client.get("/internal/v1/agents/registry")
        assert response.status_code == 400
    finally:
        app.dependency_overrides.pop(verify_service_token, None)
