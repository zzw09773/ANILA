from __future__ import annotations

from datetime import datetime, timezone

import pytest

from anila_security.model_governance import (
    _AUTHORITY_CONSTRUCTOR_TOKEN,
    CLASSIFICATION_ORDER,
    CallsiteBinding,
    Deployment,
    InferenceCallsite,
    ModelArtifact,
    ModelGovernanceError,
    VerifiedModelGovernanceAuthority,
    classification_rank,
    parse_rfc3339,
    parse_classification_level,
)


def test_model_artifact_contract_requires_digest_revision_and_license() -> None:
    artifact = ModelArtifact.from_dict(
        {
            "artifact_id": "artifact.synthetic",
            "model_family": "synthetic-llm",
            "digest": "sha256:" + "a" * 64,
            "revision": "rev-1",
            "license_id": "Apache-2.0",
            "license_approved": True,
            "license_approval_artifact_id": "legal.synthetic.v1",
            "legal_approver_ids": ["legal"],
        }
    )
    assert artifact.revision == "rev-1"
    assert artifact.digest.startswith("sha256:")


def test_deployment_contract_requires_gpu_and_fresh_readiness() -> None:
    deployment = Deployment.from_dict(
        {
            "deployment_id": "deployment.synthetic",
            "artifact_id": "artifact.synthetic",
            "image_digest": "sha256:" + "b" * 64,
            "gpu_topology": {
                "vendor": "NVIDIA",
                "count": 1,
                "memory_gib": 80,
                "compute_capability": "sm_90",
            },
            "health_readiness": {
                "health_url": "/health",
                "readiness_url": "/ready",
                "freshness_seconds": 60,
                "last_check": "2026-07-15T12:00:00Z",
                "healthy": True,
                "ready": True,
            },
        }
    )
    assert deployment.gpu_count == 1
    assert deployment.last_health_check == datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)


def test_callsite_contract_rejects_raw_unknown_endpoint_field() -> None:
    value = {
        "id": "r7.synthetic",
        "owner": "synthetic",
        "service": "synthetic",
        "source": "services/synthetic.py",
        "symbol": "invoke",
        "kind": "synthetic",
        "enabled": False,
        "gateway_id": None,
        "raw_endpoint": True,
        "classification_ceiling": "營業秘密",
        "usage_sink": None,
        "audit_sink": None,
        "agent_scope": [],
        "sink_kinds": ["chat_completions"],
        "endpoint_url": "http://model.invalid",
    }
    with pytest.raises(ModelGovernanceError, match="unknown or missing fields"):
        InferenceCallsite.from_dict(value)


def test_scalar_contracts_fail_closed() -> None:
    with pytest.raises(ModelGovernanceError, match="sha256"):
        ModelArtifact.from_dict(
            {
                "artifact_id": "artifact.synthetic",
                "model_family": "synthetic",
                "digest": "not-a-digest",
                "revision": "rev-1",
                "license_id": "Apache-2.0",
                "license_approved": True,
                "license_approval_artifact_id": "legal.synthetic.v1",
                "legal_approver_ids": ["legal"],
            }
        )
    with pytest.raises(ModelGovernanceError, match="timezone"):
        parse_rfc3339("2026-07-15", "last_check")


def test_classification_order_matches_canonical_contract_when_available() -> None:
    contracts = pytest.importorskip("anila_contracts.classification")
    canonical = tuple(level.value for level in contracts.ClassificationLevel)
    assert canonical == CLASSIFICATION_ORDER
    for rank, level in enumerate(contracts.ClassificationLevel):
        assert parse_classification_level(level) == level.value
        assert classification_rank(level) == rank


def _scoped_authority(scope: tuple[str, ...]) -> VerifiedModelGovernanceAuthority:
    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    callsite = InferenceCallsite(
        callsite_id="r7.synthetic.scoped",
        owner="tests",
        service="synthetic",
        source="tests",
        symbol="invoke",
        kind="synthetic",
        enabled=False,
        gateway_id="csp-model-gateway",
        raw_endpoint=False,
        classification_ceiling="極機密",
        usage_sink="csp.token_usage",
        audit_sink="csp.audit_log",
        agent_scope=scope,
        sink_kinds=("chat_completions",),
    )
    binding = CallsiteBinding(
        callsite_id=callsite.callsite_id,
        gateway_id="csp-model-gateway",
        classification_ceiling="極機密",
        usage_sink="csp.token_usage",
        audit_sink="csp.audit_log",
        agent_scope=scope,
        model_artifact_id="artifact.synthetic",
        deployment_id="deployment.synthetic",
    )
    artifact = ModelArtifact(
        artifact_id="artifact.synthetic",
        model_family="synthetic",
        digest="sha256:" + "a" * 64,
        revision="rev-1",
        license_id="Apache-2.0",
        license_approved=True,
        license_approval_artifact_id="legal.synthetic.v1",
        legal_approver_ids=("legal",),
    )
    deployment = Deployment(
        deployment_id="deployment.synthetic",
        artifact_id=artifact.artifact_id,
        image_digest="sha256:" + "b" * 64,
        gpu_vendor="NVIDIA",
        gpu_count=1,
        gpu_memory_gib=80,
        gpu_compute_capability="sm_90",
        health_url="/health",
        readiness_url="/ready",
        readiness_freshness_seconds=60,
        last_health_check=now,
        healthy=True,
        ready=True,
    )
    return VerifiedModelGovernanceAuthority(
        _token=_AUTHORITY_CONSTRUCTOR_TOKEN,
        profile_id="profile.synthetic",
        profile_version="v1",
        profile_content_sha256="a" * 64,
        inventory_sha256="b" * 64,
        enabled=True,
        enabled_callsites=(callsite.callsite_id,),
        disabled_callsites=(),
        callsites={callsite.callsite_id: callsite},
        bindings={binding.callsite_id: binding},
        model_artifacts={artifact.artifact_id: artifact},
        deployments={deployment.deployment_id: deployment},
        valid_from=now,
        valid_until=now.replace(hour=13),
    )


def test_registered_agent_scope_is_category_not_literal_id() -> None:
    authority = _scoped_authority(("registered-agent",))
    admitted = authority.authorize(
        "r7.synthetic.scoped",
        "無機密",
        "artifact.synthetic",
        "deployment.synthetic",
        agent_id="research-agent",
        now=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
    )
    assert admitted.agent_scope == ("registered-agent",)

    with pytest.raises(ModelGovernanceError, match="selector"):
        authority.authorize(
            "r7.synthetic.scoped",
            "無機密",
            "artifact.synthetic",
            "deployment.synthetic",
            agent_id="registered-agent",
            now=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
        )


def test_exact_agent_scope_remains_exact_and_missing_context_fails_closed() -> None:
    authority = _scoped_authority(("research-agent",))
    with pytest.raises(ModelGovernanceError, match="outside"):
        authority.authorize(
            "r7.synthetic.scoped",
            "無機密",
            "artifact.synthetic",
            "deployment.synthetic",
            agent_id=None,
            now=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
        )
    with pytest.raises(ModelGovernanceError, match="outside"):
        authority.authorize(
            "r7.synthetic.scoped",
            "無機密",
            "artifact.synthetic",
            "deployment.synthetic",
            agent_id="other-agent",
            now=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
        )
    admitted = authority.authorize(
        "r7.synthetic.scoped",
        "無機密",
        "artifact.synthetic",
        "deployment.synthetic",
        agent_id="research-agent",
        now=datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc),
    )
    assert admitted.agent_scope == ("research-agent",)
