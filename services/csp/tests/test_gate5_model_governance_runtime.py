from __future__ import annotations

import base64
import copy
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from anila_security.model_governance import canonical_json
from app.schemas.model_governance import (
    ObservedArtifactFacts,
    ObservedDeploymentFacts,
    ObservedGovernanceFacts,
    ObservedGpuTopology,
    ObservedHealthReadiness,
)
from app.services.model_governance_runtime import (
    ModelGovernanceRuntime,
    ModelGovernanceRuntimeError,
)


ROOT = Path(__file__).parents[3]
INVENTORY = ROOT / "infra/policy/gate5/model-governance-inventory.v1.json"
TEMPLATE = ROOT / "infra/policy/gate5/model-governance-profile.disabled-template.json"
NOW = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
ARTIFACT_DIGEST = "sha256:" + "a" * 64
IMAGE_DIGEST = "sha256:" + "b" * 64


class ReceiptSink:
    def __init__(self, *, fail: set[str] | None = None) -> None:
        self.events: list[tuple[str, dict]] = []
        self.fail = fail or set()

    def _record(self, kind: str, event: dict) -> str:
        if kind in self.fail:
            raise RuntimeError(f"synthetic {kind} failure")
        self.events.append((kind, event))
        return f"receipt-{kind}-{len(self.events)}"

    def record_pre_usage(self, event: dict) -> str:
        return self._record("pre_usage", event)

    def record_post_usage(self, event: dict) -> str:
        return self._record("post_usage", event)

    def record_pre_audit(self, event: dict) -> str:
        return self._record("pre_audit", event)

    def record_post_audit(self, event: dict) -> str:
        return self._record("post_audit", event)

    def compensate_pre_usage(self, event: dict) -> str:
        return self._record("compensate_pre_usage", event)

    def compensate_post_usage(self, event: dict) -> str:
        return self._record("compensate_post_usage", event)


def _inventory() -> dict:
    return json.loads(INVENTORY.read_text(encoding="utf-8"))


def _profile() -> dict:
    inventory = _inventory()
    profile = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    callsite_id = "r7.router.core"
    call = next(item for item in inventory["callsites"] if item["id"] == callsite_id)
    profile.update(
        {
            "enabled": True,
            "enabled_callsites": [callsite_id],
            "disabled_callsites": [
                item["id"]
                for item in inventory["callsites"]
                if item["id"] != callsite_id
            ],
            "valid_from": (NOW - timedelta(minutes=5)).isoformat(),
            "valid_until": (NOW + timedelta(days=7)).isoformat(),
            "approvers": [
                {"role": role, "subject": f"synthetic-{role}"}
                for role in ("system_owner", "data_owner", "security", "operations")
            ],
            "model_artifacts": [
                {
                    "artifact_id": "artifact.synthetic",
                    "model_family": "synthetic-llm",
                    "digest": ARTIFACT_DIGEST,
                    "revision": "rev-synthetic-1",
                    "license_id": "Apache-2.0",
                    "license_approved": True,
                    "license_approval_artifact_id": "legal.synthetic.v1",
                    "legal_approver_ids": ["legal"],
                }
            ],
            "deployments": [
                {
                    "deployment_id": "deployment.synthetic",
                    "artifact_id": "artifact.synthetic",
                    "image_digest": IMAGE_DIGEST,
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
                        "last_check": (NOW - timedelta(seconds=10)).isoformat(),
                        "healthy": True,
                        "ready": True,
                    },
                }
            ],
            "callsite_bindings": [
                {
                    "callsite_id": callsite_id,
                    "gateway_id": call["gateway_id"],
                    "classification_ceiling": call["classification_ceiling"],
                    "usage_sink": call["usage_sink"],
                    "audit_sink": call["audit_sink"],
                    "agent_scope": call["agent_scope"],
                    "model_artifact_id": "artifact.synthetic",
                    "deployment_id": "deployment.synthetic",
                }
            ],
        }
    )
    unsigned = dict(profile)
    unsigned.pop("profile_content_sha256", None)
    unsigned.pop("signatures", None)
    profile["profile_content_sha256"] = hashlib.sha256(
        canonical_json(unsigned)
    ).hexdigest()
    return profile


def _write_signed_material(tmp_path: Path, profile: dict) -> tuple[Path, Path]:
    payload = dict(profile)
    payload.pop("signatures", None)
    signatures: list[dict[str, str]] = []
    trusted: dict[str, str] = {}
    for role in ("system_owner", "data_owner", "security", "operations"):
        key = Ed25519PrivateKey.generate()
        trusted[role] = (
            key.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode("ascii")
        )
        signatures.append(
            {
                "role": role,
                "signature": base64.b64encode(key.sign(canonical_json(payload))).decode(
                    "ascii"
                ),
            }
        )
    profile["signatures"] = signatures
    profile_path = tmp_path / "profile.json"
    trust_path = tmp_path / "trust.json"
    profile_path.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    trust_path.write_text(json.dumps({"trusted_signers": trusted}), encoding="utf-8")
    return profile_path, trust_path


def _write_observed(tmp_path: Path, profile: dict) -> Path:
    artifact = profile["model_artifacts"][0]
    deployment = profile["deployments"][0]
    facts = ObservedGovernanceFacts(
        schema_version="anila.gate5.model-governance.observed.v1",
        artifacts=(
            ObservedArtifactFacts(
                artifact_id=artifact["artifact_id"],
                digest=artifact["digest"],
                revision=artifact["revision"],
            ),
        ),
        deployments=(
            ObservedDeploymentFacts(
                deployment_id=deployment["deployment_id"],
                artifact_id=deployment["artifact_id"],
                image_digest=deployment["image_digest"],
                gpu_topology=ObservedGpuTopology(**deployment["gpu_topology"]),
                health_readiness=ObservedHealthReadiness(
                    **deployment["health_readiness"]
                ),
            ),
        ),
    )
    path = tmp_path / "observed.json"
    path.write_text(json.dumps(facts.model_dump(mode="json")), encoding="utf-8")
    return path


def _runtime(
    tmp_path: Path,
    *,
    usage_sink: ReceiptSink | None = None,
    audit_sink: ReceiptSink | None = None,
    profile: dict | None = None,
) -> tuple[ModelGovernanceRuntime, dict, Path, Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    inventory = _inventory()
    profile = copy.deepcopy(profile or _profile())
    profile_path, trust_path = _write_signed_material(tmp_path, profile)
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(
        json.dumps(inventory, ensure_ascii=False), encoding="utf-8"
    )
    observed_path = _write_observed(tmp_path, profile)
    runtime = ModelGovernanceRuntime(
        enabled=True,
        inventory_path=inventory_path,
        profile_path=profile_path,
        trust_store_path=trust_path,
        observed_facts_path=observed_path,
        gateway_endpoint="https://csp-model-gateway/v1",
        usage_sink=usage_sink,
        audit_sink=audit_sink,
    )
    return runtime, profile, profile_path, trust_path, observed_path


def _facts(profile: dict) -> tuple[dict, dict]:
    return profile["model_artifacts"][0], profile["deployments"][0]


def test_bootstrap_authorize_and_post_receipts_are_required(tmp_path: Path) -> None:
    usage = ReceiptSink()
    audit = ReceiptSink()
    runtime, profile, *_ = _runtime(tmp_path, usage_sink=usage, audit_sink=audit)
    artifact, deployment = _facts(profile)

    readiness = runtime.bootstrap(now=NOW)
    assert readiness.ready is True
    authorization = runtime.authorize_model_invocation(
        "r7.router.core",
        "機密",
        "router",
        "https://csp-model-gateway/v1/chat/completions",
        artifact,
        deployment,
        invocation_id="inv-synthetic-1",
        now=NOW,
    )
    assert authorization.pre_usage_receipt.startswith("receipt-pre_usage")
    assert authorization.pre_audit_receipt.startswith("receipt-pre_audit")

    completion = runtime.record_post_usage(
        authorization,
        {"prompt_tokens": 3, "completion_tokens": 5},
        now=NOW,
    )
    assert completion.invocation_id == "inv-synthetic-1"
    assert {kind for kind, _ in usage.events} == {"pre_usage", "post_usage"}
    assert {kind for kind, _ in audit.events} == {"pre_audit", "post_audit"}


def test_missing_or_disabled_material_is_not_ready(tmp_path: Path) -> None:
    runtime = ModelGovernanceRuntime(
        enabled=True,
        inventory_path=tmp_path / "missing-inventory.json",
        profile_path=tmp_path / "missing-profile.json",
        trust_store_path=tmp_path / "missing-trust.json",
        observed_facts_path=tmp_path / "missing-observed.json",
        gateway_endpoint="https://csp-model-gateway/v1",
    )
    readiness = runtime.bootstrap(now=NOW)
    assert readiness.ready is False
    assert "missing" in readiness.reason

    template = json.loads(TEMPLATE.read_text(encoding="utf-8"))
    inventory = _inventory()
    profile_path = tmp_path / "disabled-profile.json"
    profile_path.write_text(json.dumps(template), encoding="utf-8")
    inventory_path = tmp_path / "inventory.json"
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    trust_path = tmp_path / "trust.json"
    trust_path.write_text(json.dumps({"trusted_signers": {}}), encoding="utf-8")
    observed_path = tmp_path / "observed.json"
    observed_path.write_text(
        json.dumps({"schema_version": "anila.gate5.model-governance.observed.v1"}),
        encoding="utf-8",
    )
    disabled = ModelGovernanceRuntime(
        enabled=True,
        inventory_path=inventory_path,
        profile_path=profile_path,
        trust_store_path=trust_path,
        observed_facts_path=observed_path,
        gateway_endpoint="https://csp-model-gateway/v1",
    )
    assert disabled.bootstrap(now=NOW).ready is False
    assert "disabled" in disabled.readiness.reason


def test_production_posture_cannot_leave_governance_disabled(tmp_path: Path) -> None:
    settings = SimpleNamespace(
        ANILA_DEPLOYMENT_PROFILE="prod-intranet-card",
        GATE5_MODEL_GOVERNANCE_ENABLED=False,
        GATE5_MODEL_GOVERNANCE_STARTUP_REQUIRED=False,
        GATE5_MODEL_GOVERNANCE_INVENTORY_PATH=str(tmp_path / "missing-inventory.json"),
        GATE5_MODEL_GOVERNANCE_PROFILE_PATH=str(tmp_path / "missing-profile.json"),
        GATE5_MODEL_GOVERNANCE_TRUST_STORE_PATH=str(tmp_path / "missing-trust.json"),
        GATE5_MODEL_GOVERNANCE_OBSERVED_FACTS_PATH=str(
            tmp_path / "missing-observed.json"
        ),
        GATE5_MODEL_GATEWAY_ENDPOINT="",
    )
    runtime = ModelGovernanceRuntime.from_settings(settings)

    assert runtime.enabled is True
    assert runtime.startup_required is True
    assert runtime.bootstrap(now=NOW).ready is False


def test_formal_readiness_response_is_503_when_governance_not_ready(
    monkeypatch,
) -> None:
    import app.main as main_module

    monkeypatch.setattr(
        main_module.settings, "ANILA_DEPLOYMENT_PROFILE", "prod-intranet-card"
    )
    monkeypatch.setattr(main_module.settings, "GATE5_MODEL_GOVERNANCE_ENABLED", False)
    application = SimpleNamespace(
        state=SimpleNamespace(
            migration_status="succeeded",
            migration_error=None,
            ingestion_relay_task=SimpleNamespace(done=lambda: False),
            model_governance_readiness=main_module.ModelGovernanceReadiness(
                status="not_ready",
                ready=False,
                reason="missing signed profile",
                checked_at=NOW,
            ),
        )
    )

    response = main_module._readiness_response(application)

    assert response.status_code == 503
    assert b'"model_governance"' in response.body


def test_stale_health_and_observed_gpu_or_image_drift_fail_closed(
    tmp_path: Path,
) -> None:
    stale_profile = _profile()
    stale_profile["deployments"][0]["health_readiness"]["last_check"] = (
        NOW - timedelta(minutes=10)
    ).isoformat()
    runtime, _, *_ = _runtime(tmp_path, profile=stale_profile)
    assert runtime.bootstrap(now=NOW).ready is False

    usage = ReceiptSink()
    audit = ReceiptSink()
    runtime, profile, _, _, observed_path = _runtime(
        tmp_path / "drift", usage_sink=usage, audit_sink=audit
    )
    payload = json.loads(observed_path.read_text(encoding="utf-8"))
    payload["deployments"][0]["image_digest"] = "sha256:" + "c" * 64
    observed_path.write_text(json.dumps(payload), encoding="utf-8")
    artifact, deployment = _facts(profile)
    with pytest.raises(ModelGovernanceRuntimeError, match="not ready|drift"):
        runtime.authorize_model_invocation(
            "r7.router.core",
            "機密",
            "router",
            "https://csp-model-gateway/v1/chat/completions",
            artifact,
            deployment,
            now=NOW,
        )


def test_rotation_revocation_and_toc_tou_reload_are_fail_closed(tmp_path: Path) -> None:
    usage = ReceiptSink()
    audit = ReceiptSink()
    runtime, profile, profile_path, _, _ = _runtime(
        tmp_path, usage_sink=usage, audit_sink=audit
    )
    artifact, deployment = _facts(profile)
    authorization = runtime.authorize_model_invocation(
        "r7.router.core",
        "機密",
        "router",
        "https://csp-model-gateway/v1/chat/completions",
        artifact,
        deployment,
        now=NOW,
    )

    revoked = copy.deepcopy(profile)
    revoked["enabled"] = False
    revoked["enabled_callsites"] = []
    revoked["disabled_callsites"] = [item["id"] for item in _inventory()["callsites"]]
    revoked["callsite_bindings"] = []
    revoked["model_artifacts"] = []
    revoked["deployments"] = []
    revoked["valid_from"] = None
    revoked["valid_until"] = None
    revoked["approvers"] = []
    revoked["signatures"] = []
    unsigned = dict(revoked)
    unsigned.pop("profile_content_sha256", None)
    unsigned.pop("signatures", None)
    revoked["profile_content_sha256"] = hashlib.sha256(
        canonical_json(unsigned)
    ).hexdigest()
    profile_path.write_text(json.dumps(revoked), encoding="utf-8")

    with pytest.raises(ModelGovernanceRuntimeError, match="not ready|disabled"):
        runtime.authorize_model_invocation(
            "r7.router.core",
            "機密",
            "router",
            "https://csp-model-gateway/v1/chat/completions",
            artifact,
            deployment,
            now=NOW,
        )
    with pytest.raises(ModelGovernanceRuntimeError, match="not ready|disabled"):
        runtime.record_post_usage(authorization, {"prompt_tokens": 1}, now=NOW)


def test_raw_endpoint_scope_and_receipt_failures_never_authorize(
    tmp_path: Path,
) -> None:
    profile = _profile()
    artifact, deployment = _facts(profile)
    usage = ReceiptSink()
    audit = ReceiptSink()
    runtime, _, *_ = _runtime(
        tmp_path, usage_sink=usage, audit_sink=audit, profile=profile
    )
    with pytest.raises(ModelGovernanceRuntimeError, match="endpoint"):
        runtime.authorize_model_invocation(
            "r7.router.core",
            "機密",
            "router",
            "http://raw-model:8000/v1/chat/completions",
            artifact,
            deployment,
            now=NOW,
        )
    with pytest.raises(ModelGovernanceRuntimeError, match="selector"):
        runtime.authorize_model_invocation(
            "r7.router.core",
            "機密",
            "registered-agent",
            "https://csp-model-gateway/v1/chat/completions",
            artifact,
            deployment,
            now=NOW,
        )
    assert usage.events == []
    assert audit.events == []

    failing_usage = ReceiptSink()
    failing_audit = ReceiptSink(fail={"pre_audit"})
    runtime, _, *_ = _runtime(
        tmp_path / "receipts", usage_sink=failing_usage, audit_sink=failing_audit
    )
    with pytest.raises(
        ModelGovernanceRuntimeError, match="pre-receipt transaction failed"
    ):
        runtime.authorize_model_invocation(
            "r7.router.core",
            "機密",
            "router",
            "https://csp-model-gateway/v1/chat/completions",
            artifact,
            deployment,
            now=NOW,
        )
    assert any(kind == "compensate_pre_usage" for kind, _ in failing_usage.events)
