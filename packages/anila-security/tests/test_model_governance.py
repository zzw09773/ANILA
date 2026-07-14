from __future__ import annotations

from datetime import datetime, timezone

import pytest

from anila_security.model_governance import (
    CLASSIFICATION_ORDER,
    Deployment,
    InferenceCallsite,
    ModelArtifact,
    ModelGovernanceError,
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
