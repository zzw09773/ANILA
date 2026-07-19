"""Fail-closed CSP runtime bootstrap and model invocation admission.

The signed inventory/profile remains the only model-governance authority.  This
module owns the CSP-side lifecycle around it: explicit file loading, atomic
reload on every admission (which closes profile/trust-store TOCTOU windows),
observed deployment drift checks, gateway endpoint validation, and durable
usage/audit receipts.  It intentionally does not perform model network I/O or
modify any proxy callsite; callers must hold the returned immutable admission
before making a gateway request.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from anila_security.model_governance import (
    Deployment,
    ModelGovernanceAuthorization,
    ModelGovernanceError,
    ModelArtifact,
    ProviderBinding,
    VerifiedModelGovernanceAuthority,
)

from app.schemas.model_governance import (
    ModelGovernanceReadiness,
    ObservedArtifactFacts,
    ObservedDeploymentFacts,
    ObservedGovernanceFacts,
)


OBSERVED_GOVERNANCE_SCHEMA = "anila.gate5.model-governance.observed.v1"


def governance_required_for_settings(settings: Any) -> bool:
    """Return whether the deployment posture may run without Gate 5 material."""

    profile = str(getattr(settings, "ANILA_DEPLOYMENT_PROFILE", "")).strip().lower()
    return profile in {"production", "prod"} or profile.startswith("prod-")


class ModelGovernanceRuntimeError(RuntimeError):
    """Raised when runtime model admission cannot be proven safe."""


class DurableReceiptSink(Protocol):
    """Durable sink required at the model gateway boundary.

    Implementations normally persist to CSP usage/audit tables or an
    equivalent transactional outbox.  Returning a non-empty receipt token is
    part of the protocol: a successful method call without a durable receipt
    is not an authorization.
    """

    def record_pre_usage(self, event: Mapping[str, Any]) -> object: ...

    def record_post_usage(self, event: Mapping[str, Any]) -> object: ...

    def record_pre_audit(self, event: Mapping[str, Any]) -> object: ...

    def record_post_audit(self, event: Mapping[str, Any]) -> object: ...

    def compensate_pre_usage(self, event: Mapping[str, Any]) -> object: ...

    def compensate_post_usage(self, event: Mapping[str, Any]) -> object: ...

    def record_failure_audit(self, event: Mapping[str, Any]) -> object: ...


@dataclass(frozen=True, slots=True)
class ModelInvocationAuthorization:
    """Immutable pre-network admission plus durable pre-receipts."""

    invocation_id: str
    endpoint: str
    governance: ModelGovernanceAuthorization
    authority_profile_content_sha256: str
    authority_inventory_sha256: str
    pre_usage_receipt: str
    pre_audit_receipt: str
    authorized_at: datetime

    @property
    def callsite_id(self) -> str:
        return self.governance.callsite_id

    @property
    def classification(self) -> str:
        return self.governance.classification

    @property
    def agent_scope(self) -> tuple[str, ...]:
        return self.governance.agent_scope

    @property
    def artifact(self) -> ModelArtifact:
        return self.governance.model_artifact

    @property
    def deployment(self) -> Deployment:
        return self.governance.deployment


@dataclass(frozen=True, slots=True)
class ModelInvocationCompletion:
    """Immutable post-network usage/audit receipts."""

    invocation_id: str
    post_usage_receipt: str
    post_audit_receipt: str
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class _RuntimePaths:
    inventory: Path | None
    profile: Path | None
    trust_store: Path | None
    observed_facts: Path | None


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ModelGovernanceRuntimeError("runtime now must include timezone")
    return current.astimezone(timezone.utc)


def _path(value: object, field: str) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ModelGovernanceRuntimeError(f"{field} must be an explicit path string")
    text = value.strip()
    return Path(text) if text else None


def _json_object(path: Path | None, field: str) -> dict[str, Any]:
    if path is None:
        raise ModelGovernanceRuntimeError(f"{field} path is not configured")
    if not path.is_file():
        raise ModelGovernanceRuntimeError(f"{field} file is missing: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ModelGovernanceRuntimeError(f"cannot read {field}: {path}") from exc
    if not isinstance(raw, dict):
        raise ModelGovernanceRuntimeError(f"{field} must contain a JSON object")
    return raw


def _receipt_token(value: object, *, kind: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, Mapping):
        durable = value.get("durable")
        if durable is False:
            raise ModelGovernanceRuntimeError(f"{kind} receipt is not durable")
        for key in ("receipt_id", "id", "token"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    raise ModelGovernanceRuntimeError(f"{kind} sink returned no durable receipt")


def _same_artifact(expected: ModelArtifact, observed: ObservedArtifactFacts) -> None:
    if (
        expected.artifact_id != observed.artifact_id
        or expected.digest != observed.digest
        or expected.revision != observed.revision
    ):
        raise ModelGovernanceRuntimeError(
            f"observed model artifact drift for {expected.artifact_id}"
        )


def _same_deployment(
    expected: Deployment, observed: ObservedDeploymentFacts, *, now: datetime
) -> None:
    topology = observed.gpu_topology
    health = observed.health_readiness
    if expected.deployment_id != observed.deployment_id:
        raise ModelGovernanceRuntimeError("observed deployment id drift")
    if expected.artifact_id != observed.artifact_id:
        raise ModelGovernanceRuntimeError(
            f"observed deployment artifact drift for {expected.deployment_id}"
        )
    if expected.image_digest != observed.image_digest:
        raise ModelGovernanceRuntimeError(
            f"observed image digest drift for {expected.deployment_id}"
        )
    if (
        expected.gpu_vendor != topology.vendor
        or expected.gpu_count != topology.count
        or expected.gpu_memory_gib != topology.memory_gib
        or expected.gpu_compute_capability != topology.compute_capability
    ):
        raise ModelGovernanceRuntimeError(
            f"observed GPU topology drift for {expected.deployment_id}"
        )
    if (
        expected.health_url != health.health_url
        or expected.readiness_url != health.readiness_url
        or expected.readiness_freshness_seconds != health.freshness_seconds
        or expected.healthy != health.healthy
        or expected.ready != health.ready
    ):
        raise ModelGovernanceRuntimeError(
            f"observed health/readiness contract drift for {expected.deployment_id}"
        )
    last_check = health.last_check.astimezone(timezone.utc)
    age = (now - last_check).total_seconds()
    if age < 0 or age > health.freshness_seconds:
        raise ModelGovernanceRuntimeError(
            f"observed health/readiness evidence is stale for {expected.deployment_id}"
        )
    if not health.healthy or not health.ready:
        raise ModelGovernanceRuntimeError(
            f"observed deployment is not healthy/ready: {expected.deployment_id}"
        )


class ModelGovernanceRuntime:
    """CSP model-governance bootstrap and pre/post receipt protocol."""

    def __init__(
        self,
        *,
        enabled: bool,
        inventory_path: str | Path | None,
        profile_path: str | Path | None,
        trust_store_path: str | Path | None,
        observed_facts_path: str | Path | None,
        gateway_endpoint: str,
        usage_sink: DurableReceiptSink | None = None,
        audit_sink: DurableReceiptSink | None = None,
        startup_required: bool = False,
    ) -> None:
        self.enabled = bool(enabled)
        self.paths = _RuntimePaths(
            inventory=_path(
                str(inventory_path)
                if isinstance(inventory_path, Path)
                else inventory_path,
                "inventory",
            ),
            profile=_path(
                str(profile_path) if isinstance(profile_path, Path) else profile_path,
                "profile",
            ),
            trust_store=_path(
                str(trust_store_path)
                if isinstance(trust_store_path, Path)
                else trust_store_path,
                "trust_store",
            ),
            observed_facts=_path(
                str(observed_facts_path)
                if isinstance(observed_facts_path, Path)
                else observed_facts_path,
                "observed_facts",
            ),
        )
        if not isinstance(gateway_endpoint, str):
            raise ModelGovernanceRuntimeError("gateway_endpoint must be a string")
        self.gateway_endpoint = gateway_endpoint.strip()
        self.usage_sink = usage_sink
        self.audit_sink = audit_sink
        self.startup_required = bool(startup_required)
        self._lock = threading.RLock()
        self._authority: VerifiedModelGovernanceAuthority | None = None
        self._observed: ObservedGovernanceFacts | None = None
        self._source_signature: tuple[str, ...] | None = None
        self._readiness = self._not_configured()

    @classmethod
    def from_settings(cls, settings: Any) -> "ModelGovernanceRuntime":
        """Build runtime paths only from explicit settings/env values."""

        formal = governance_required_for_settings(settings)
        return cls(
            enabled=bool(getattr(settings, "GATE5_MODEL_GOVERNANCE_ENABLED", False))
            or formal,
            inventory_path=getattr(
                settings, "GATE5_MODEL_GOVERNANCE_INVENTORY_PATH", ""
            ),
            profile_path=getattr(settings, "GATE5_MODEL_GOVERNANCE_PROFILE_PATH", ""),
            trust_store_path=getattr(
                settings, "GATE5_MODEL_GOVERNANCE_TRUST_STORE_PATH", ""
            ),
            observed_facts_path=(
                getattr(settings, "GATE5_MODEL_GOVERNANCE_OBSERVED_FACTS_PATH", "")
                or getattr(
                    settings,
                    "GATE5_MODEL_GOVERNANCE_OBSERVED_DEPLOYMENT_FACTS_PATH",
                    "",
                )
            ),
            gateway_endpoint=getattr(settings, "GATE5_MODEL_GATEWAY_ENDPOINT", ""),
            startup_required=(
                bool(
                    getattr(settings, "GATE5_MODEL_GOVERNANCE_STARTUP_REQUIRED", False)
                )
                or formal
            ),
        )

    @staticmethod
    def _not_configured() -> ModelGovernanceReadiness:
        return ModelGovernanceReadiness(
            status="not_configured",
            ready=True,
            reason="Gate 5 model governance is disabled by deployment configuration",
            checked_at=datetime.now(timezone.utc),
        )

    @property
    def readiness(self) -> ModelGovernanceReadiness:
        with self._lock:
            return self._readiness

    @property
    def authority(self) -> VerifiedModelGovernanceAuthority | None:
        with self._lock:
            return self._authority

    @property
    def observed_facts(self) -> ObservedGovernanceFacts | None:
        with self._lock:
            return self._observed

    def _signature(self) -> tuple[str, ...]:
        values: list[str] = []
        for label, path in (
            ("inventory", self.paths.inventory),
            ("profile", self.paths.profile),
            ("trust_store", self.paths.trust_store),
            ("observed_facts", self.paths.observed_facts),
        ):
            if path is None:
                values.append(f"{label}:missing")
                continue
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as exc:
                raise ModelGovernanceRuntimeError(
                    f"cannot fingerprint {label}: {path}"
                ) from exc
            values.append(f"{label}:{digest}")
        return tuple(values)

    def _read_observed(self) -> ObservedGovernanceFacts:
        payload = _json_object(self.paths.observed_facts, "observed_facts")
        try:
            observed = ObservedGovernanceFacts.model_validate(payload)
        except Exception as exc:
            raise ModelGovernanceRuntimeError(
                "observed deployment facts are malformed"
            ) from exc
        if observed.schema_version != OBSERVED_GOVERNANCE_SCHEMA:
            raise ModelGovernanceRuntimeError("unknown observed governance schema")
        if len({item.artifact_id for item in observed.artifacts}) != len(
            observed.artifacts
        ):
            raise ModelGovernanceRuntimeError("observed artifact ids must be unique")
        if len({item.deployment_id for item in observed.deployments}) != len(
            observed.deployments
        ):
            raise ModelGovernanceRuntimeError("observed deployment ids must be unique")
        return observed

    @staticmethod
    def _verify_observed(
        authority: VerifiedModelGovernanceAuthority,
        observed: ObservedGovernanceFacts,
        *,
        now: datetime,
    ) -> None:
        artifacts = {item.artifact_id: item for item in observed.artifacts}
        deployments = {item.deployment_id: item for item in observed.deployments}
        fact_pairs: dict[tuple[str, str], str] = {}
        for binding in authority.bindings.values():
            if binding.provider_binding_ids:
                for provider_binding_id in binding.provider_binding_ids:
                    provider = authority.provider_bindings.get(provider_binding_id)
                    if provider is None:
                        raise ModelGovernanceRuntimeError(
                            f"callsite binding {binding.callsite_id} references an "
                            "unknown provider binding"
                        )
                    fact_pairs.setdefault(
                        (provider.model_artifact_id, provider.deployment_id),
                        binding.callsite_id,
                    )
                continue
            if binding.model_artifact_id is None or binding.deployment_id is None:
                raise ModelGovernanceRuntimeError(
                    f"callsite binding {binding.callsite_id} has no model facts"
                )
            fact_pairs.setdefault(
                (binding.model_artifact_id, binding.deployment_id),
                binding.callsite_id,
            )

        for (artifact_id, deployment_id), callsite_id in fact_pairs.items():
            artifact = authority.model_artifacts.get(artifact_id)
            deployment = authority.deployments.get(deployment_id)
            if artifact is None or deployment is None:
                raise ModelGovernanceRuntimeError(
                    f"verified model facts are missing for callsite binding {callsite_id}"
                )
            observed_artifact = artifacts.get(artifact.artifact_id)
            observed_deployment = deployments.get(deployment.deployment_id)
            if observed_artifact is None or observed_deployment is None:
                raise ModelGovernanceRuntimeError(
                    f"missing observed facts for callsite binding {callsite_id}"
                )
            _same_artifact(artifact, observed_artifact)
            _same_deployment(deployment, observed_deployment, now=now)

    def invocation_facts(
        self,
        callsite_id: str,
        *,
        provider_binding_id: str | None = None,
        now: datetime | None = None,
    ) -> tuple[ObservedArtifactFacts, ObservedDeploymentFacts]:
        """Return the current verified artifact/deployment pair for a callsite.

        Callers must not copy model facts from an untrusted registry row.  The
        pair is projected from the authority and the observed evidence loaded
        by the same atomic bootstrap used for admission.
        """

        current = _now(now)
        with self._lock:
            authority = self._require_ready(now=current)
            binding = authority.bindings.get(callsite_id)
            if binding is None:
                raise ModelGovernanceRuntimeError(
                    f"callsite {callsite_id!r} is not enabled in verified profile"
                )
            observed = self._observed
            if observed is None:
                raise ModelGovernanceRuntimeError(
                    "observed governance facts are unavailable"
                )
            if provider_binding_id is None and binding.provider_binding_ids:
                raise ModelGovernanceRuntimeError(
                    "provider binding identity is required for v2 callsite facts"
                )
            if provider_binding_id is None:
                if binding.model_artifact_id is None or binding.deployment_id is None:
                    raise ModelGovernanceRuntimeError(
                        "legacy callsite binding has no model facts"
                    )
                artifact_id = binding.model_artifact_id
                deployment_id = binding.deployment_id
            else:
                provider_binding = authority.provider_bindings.get(provider_binding_id)
                if provider_binding is None:
                    raise ModelGovernanceRuntimeError(
                        "provider binding is not present in verified authority"
                    )
                artifact_id = provider_binding.model_artifact_id
                deployment_id = provider_binding.deployment_id
            artifact = authority.model_artifacts[artifact_id]
            deployment = authority.deployments[deployment_id]
            observed_artifact = next(
                (item for item in observed.artifacts if item.artifact_id == artifact.artifact_id),
                None,
            )
            observed_deployment = next(
                (item for item in observed.deployments if item.deployment_id == deployment.deployment_id),
                None,
            )
            if observed_artifact is None or observed_deployment is None:
                raise ModelGovernanceRuntimeError(
                    f"observed facts are missing for callsite {callsite_id!r}"
                )
            return observed_artifact, observed_deployment

    @staticmethod
    def _provider_binding_matches_model(
        provider: ProviderBinding,
        model: Any,
    ) -> bool:
        """Compare one signed provider snapshot with one registry row exactly."""

        target = getattr(model, "transport_target", None)
        upstream_target = getattr(model, "upstream_transport_target", None)
        return (
            provider.model_registry_id == str(getattr(model, "id", ""))
            and provider.model_registry_name == str(getattr(model, "name", ""))
            and provider.model_registry_revision
            == getattr(model, "model_registry_revision", None)
            and provider.provider_locality
            == getattr(model, "provider_locality", None)
            and isinstance(target, Mapping)
            and dict(target) == provider.transport_target.to_dict()
            and provider.transport_target_sha256
            == getattr(model, "transport_target_sha256", None)
            and str(getattr(model, "endpoint_url", ""))
            == provider.transport_target.canonical
            and provider.upstream_provider_locality
            == getattr(model, "upstream_provider_locality", None)
            and (
                (provider.upstream_transport_target is None and upstream_target is None)
                or (
                    provider.upstream_transport_target is not None
                    and isinstance(upstream_target, Mapping)
                    and dict(upstream_target)
                    == provider.upstream_transport_target.to_dict()
                )
            )
            and provider.upstream_transport_target_sha256
            == getattr(model, "upstream_transport_target_sha256", None)
            and provider.egress_policy_id == getattr(model, "egress_policy_id", None)
            and provider.upstream_egress_policy_id
            == getattr(model, "upstream_egress_policy_id", None)
        )

    @classmethod
    def _resolve_provider_binding_from_authority(
        cls,
        authority: VerifiedModelGovernanceAuthority,
        model: Any,
    ) -> ProviderBinding:
        """Resolve one provider row from one already-loaded authority.

        Keeping this helper free of ``_require_ready`` is deliberate: callers
        that also need callsite/artifact authorization must resolve all of
        those facts from the same authority object.  Reloading the signed
        profile between these steps would permit a provider id to survive a
        profile replacement while its target or revision changes.
        """

        if getattr(model, "provider_locality", None) in {None, "unclassified"}:
            raise ModelGovernanceRuntimeError(
                "unclassified model registry row has no provider authority"
            )
        identity_matches = [
            provider
            for provider in authority.provider_bindings.values()
            if provider.model_registry_id == str(getattr(model, "id", ""))
            and provider.model_registry_name == str(getattr(model, "name", ""))
        ]
        if not identity_matches:
            raise ModelGovernanceRuntimeError(
                "model registry row is absent from verified provider authority"
            )
        if len(identity_matches) != 1:
            raise ModelGovernanceRuntimeError(
                "model registry row has multiple verified provider bindings"
            )
        provider = identity_matches[0]
        if not cls._provider_binding_matches_model(provider, model):
            raise ModelGovernanceRuntimeError(
                "model registry provider snapshot differs from verified authority"
            )
        return provider

    @staticmethod
    def _invocation_facts_from_authority(
        authority: VerifiedModelGovernanceAuthority,
        observed: ObservedGovernanceFacts | None,
        callsite_id: str,
        *,
        provider_binding_id: str | None,
    ) -> tuple[ObservedArtifactFacts, ObservedDeploymentFacts]:
        """Project observed facts without reloading the authority."""

        binding = authority.bindings.get(callsite_id)
        if binding is None:
            raise ModelGovernanceRuntimeError(
                f"callsite {callsite_id!r} is not enabled in verified profile"
            )
        if observed is None:
            raise ModelGovernanceRuntimeError(
                "observed governance facts are unavailable"
            )
        if provider_binding_id is None and binding.provider_binding_ids:
            raise ModelGovernanceRuntimeError(
                "provider binding identity is required for v2 callsite facts"
            )
        if provider_binding_id is None:
            if binding.model_artifact_id is None or binding.deployment_id is None:
                raise ModelGovernanceRuntimeError(
                    "legacy callsite binding has no model facts"
                )
            artifact_id = binding.model_artifact_id
            deployment_id = binding.deployment_id
        else:
            provider_binding = authority.provider_bindings.get(provider_binding_id)
            if provider_binding is None:
                raise ModelGovernanceRuntimeError(
                    "provider binding is not present in verified authority"
                )
            artifact_id = provider_binding.model_artifact_id
            deployment_id = provider_binding.deployment_id
        artifact = authority.model_artifacts[artifact_id]
        deployment = authority.deployments[deployment_id]
        observed_artifact = next(
            (item for item in observed.artifacts if item.artifact_id == artifact.artifact_id),
            None,
        )
        observed_deployment = next(
            (item for item in observed.deployments if item.deployment_id == deployment.deployment_id),
            None,
        )
        if observed_artifact is None or observed_deployment is None:
            raise ModelGovernanceRuntimeError(
                f"observed facts are missing for callsite {callsite_id!r}"
            )
        return observed_artifact, observed_deployment

    def resolve_provider_binding(
        self,
        model: Any,
        *,
        now: datetime | None = None,
    ) -> ProviderBinding:
        """Resolve exactly one signed provider snapshot for a registry row.

        This admission is deliberately independent of a callsite.  Activation
        and probes use it directly; invocation admission additionally asks the
        authority to prove that the resolved id is allowed by the callsite.
        """

        current = _now(now)
        with self._lock:
            authority = self._require_ready(now=current)
            return self._resolve_provider_binding_from_authority(authority, model)

    def bootstrap(self, *, now: datetime | None = None) -> ModelGovernanceReadiness:
        """Atomically load and verify all mounted governance material."""

        current = _now(now)
        with self._lock:
            if not self.enabled:
                self._authority = None
                self._observed = None
                self._source_signature = None
                self._readiness = self._not_configured().model_copy(
                    update={"checked_at": current}
                )
                return self._readiness
            self._authority = None
            self._observed = None
            self._source_signature = None
            try:
                before_signature = self._signature()
                inventory = _json_object(self.paths.inventory, "inventory")
                profile = _json_object(self.paths.profile, "profile")
                trust_store = _json_object(self.paths.trust_store, "trust_store")
                authority = VerifiedModelGovernanceAuthority.from_verified_payload(
                    inventory=inventory,
                    profile=profile,
                    trust_store=trust_store,
                    allow_disabled_template=True,
                    now=current,
                )
                if not authority.enabled:
                    raise ModelGovernanceRuntimeError(
                        "verified model-governance profile is disabled"
                    )
                observed = self._read_observed()
                self._verify_observed(authority, observed, now=current)
                signature = self._signature()
                if signature != before_signature:
                    raise ModelGovernanceRuntimeError(
                        "governance material changed during bootstrap; retry required"
                    )
            except Exception as exc:
                reason = str(exc).strip() or type(exc).__name__
                self._readiness = ModelGovernanceReadiness(
                    status="not_ready",
                    ready=False,
                    reason=reason[:512],
                    checked_at=current,
                )
                return self._readiness
            self._authority = authority
            self._observed = observed
            self._source_signature = signature
            self._readiness = ModelGovernanceReadiness(
                status="ready",
                ready=True,
                reason="verified profile, signatures, deployment facts and readiness",
                checked_at=current,
                profile_id=authority.profile_id,
                profile_version=authority.profile_version,
                profile_content_sha256=authority.profile_content_sha256,
            )
            return self._readiness

    reload = bootstrap

    def _require_ready(self, *, now: datetime) -> VerifiedModelGovernanceAuthority:
        readiness = self.bootstrap(now=now)
        if not readiness.ready:
            raise ModelGovernanceRuntimeError(
                f"model-governance runtime is not ready: {readiness.reason}"
            )
        authority = self._authority
        if authority is None:
            raise ModelGovernanceRuntimeError("verified authority is unavailable")
        return authority

    def _validate_endpoint(self, endpoint: str) -> str:
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ModelGovernanceRuntimeError("model endpoint is required")
        if not self.gateway_endpoint:
            raise ModelGovernanceRuntimeError("CSP gateway endpoint is not configured")
        expected = urlsplit(self.gateway_endpoint)
        actual = urlsplit(endpoint.strip())
        if (
            expected.scheme not in {"http", "https"}
            or not expected.netloc
            or actual.scheme != expected.scheme
            or actual.netloc != expected.netloc
            or actual.username is not None
            or actual.password is not None
            or actual.query
            or actual.fragment
        ):
            raise ModelGovernanceRuntimeError("raw or untrusted model endpoint denied")
        expected_path = expected.path.rstrip("/")
        actual_path = actual.path.rstrip("/")
        if not expected_path or not (
            actual_path == expected_path or actual_path.startswith(expected_path + "/")
        ):
            raise ModelGovernanceRuntimeError(
                "model endpoint is outside CSP gateway path"
            )
        return endpoint.strip()

    @staticmethod
    def _coerce_artifact(
        value: ObservedArtifactFacts | Mapping[str, Any],
    ) -> ObservedArtifactFacts:
        if isinstance(value, ObservedArtifactFacts):
            return value
        try:
            if isinstance(value, Mapping):
                # Callers may pass the signed artifact object.  Runtime
                # admission consumes only its observed identity projection;
                # all other fields remain authority-owned and are verified
                # from the loaded profile.
                value = {
                    key: value[key]
                    for key in ("artifact_id", "digest", "revision")
                    if key in value
                }
            return ObservedArtifactFacts.model_validate(value)
        except Exception as exc:
            raise ModelGovernanceRuntimeError("artifact facts are malformed") from exc

    @staticmethod
    def _coerce_deployment(
        value: ObservedDeploymentFacts | Mapping[str, Any],
    ) -> ObservedDeploymentFacts:
        if isinstance(value, ObservedDeploymentFacts):
            return value
        try:
            return ObservedDeploymentFacts.model_validate(value)
        except Exception as exc:
            raise ModelGovernanceRuntimeError("deployment facts are malformed") from exc

    @staticmethod
    def _event(
        *,
        authorization: ModelGovernanceAuthorization,
        invocation_id: str,
        endpoint: str,
        phase: Literal["pre", "post"],
        usage: Mapping[str, Any] | None = None,
        outcome: Literal["success", "failure"] | None = None,
        receipt_context: Mapping[str, Any] | None = None,
        profile_content_sha256: str,
        inventory_sha256: str,
        pre_usage_receipt: str | None = None,
        pre_audit_receipt: str | None = None,
    ) -> dict[str, Any]:
        event: dict[str, Any] = {
            "schema_version": "anila.gate5.model-governance.receipt.v1",
            "phase": phase,
            "invocation_id": invocation_id,
            "callsite_id": authorization.callsite_id,
            "classification": authorization.classification,
            "gateway_id": authorization.gateway_id,
            "endpoint": endpoint,
            "artifact_id": authorization.model_artifact.artifact_id,
            "deployment_id": authorization.deployment.deployment_id,
            "profile_content_sha256": profile_content_sha256,
            "inventory_sha256": inventory_sha256,
        }
        provider = authorization.provider_binding
        if provider is not None:
            event.update(
                {
                    "provider_binding_id": provider.provider_binding_id,
                    "model_registry_revision": provider.model_registry_revision,
                    "provider_locality": provider.provider_locality,
                    "transport_target_sha256": provider.transport_target_sha256,
                    "upstream_provider_locality": provider.upstream_provider_locality,
                    "upstream_transport_target_sha256": (
                        provider.upstream_transport_target_sha256
                    ),
                    "egress_policy_id": provider.egress_policy_id,
                    "upstream_egress_policy_id": provider.upstream_egress_policy_id,
                }
            )
        if usage is not None:
            event["usage"] = dict(usage)
        if outcome is not None:
            event["outcome"] = outcome
        if receipt_context is not None:
            event["receipt_context"] = dict(receipt_context)
        if phase == "post":
            if not pre_usage_receipt or not pre_audit_receipt:
                raise ModelGovernanceRuntimeError(
                    "immutable invocation authorization lacks durable pre receipts"
                )
            event["pre_usage_receipt"] = pre_usage_receipt
            event["pre_audit_receipt"] = pre_audit_receipt
        return event

    @staticmethod
    def _write_receipt(
        sink: DurableReceiptSink | None,
        method_name: str,
        event: Mapping[str, Any],
        *,
        kind: str,
    ) -> str:
        if sink is None:
            raise ModelGovernanceRuntimeError(f"{kind} durable sink is not configured")
        method = getattr(sink, method_name, None)
        if not callable(method):
            raise ModelGovernanceRuntimeError(
                f"{kind} durable sink protocol is incomplete"
            )
        try:
            result = method(dict(event))
        except Exception as exc:
            raise ModelGovernanceRuntimeError(f"{kind} durable receipt failed") from exc
        return _receipt_token(result, kind=kind)

    @staticmethod
    def _compensate_receipt(
        sink: DurableReceiptSink | None,
        event: Mapping[str, Any],
        *,
        method_name: str,
        kind: str,
    ) -> str:
        if sink is None:
            raise ModelGovernanceRuntimeError(
                f"{kind} compensation sink is not configured"
            )
        method = getattr(sink, method_name, None)
        if not callable(method):
            raise ModelGovernanceRuntimeError(
                f"{kind} compensation protocol is incomplete"
            )
        try:
            result = method(dict(event))
        except Exception as exc:
            raise ModelGovernanceRuntimeError(f"{kind} compensation failed") from exc
        return _receipt_token(result, kind=kind)

    def authorize_model_invocation(
        self,
        callsite_id: str,
        classification: Any,
        agent_id: str | None,
        endpoint: str,
        artifact_facts: ObservedArtifactFacts | Mapping[str, Any] | None = None,
        deployment_facts: ObservedDeploymentFacts | Mapping[str, Any] | None = None,
        *,
        registry_model: Any | None = None,
        provider_binding_id: str | None = None,
        invocation_id: str | None = None,
        now: datetime | None = None,
        usage_sink: DurableReceiptSink | None = None,
        audit_sink: DurableReceiptSink | None = None,
        receipt_context: Mapping[str, Any] | None = None,
    ) -> ModelInvocationAuthorization:
        """Authorize one model invocation only after durable pre-receipts."""

        current = _now(now)
        with self._lock:
            # Load signed profile, inventory and observed deployment facts once
            # for the complete admission.  In particular, do not call
            # resolve_provider_binding()/invocation_facts() here: each public
            # helper performs its own bootstrap and could observe a different
            # profile after an operator replaces the mounted file.
            authority = self._require_ready(now=current)
            safe_endpoint = self._validate_endpoint(endpoint)
            observed = self._observed
            if registry_model is not None:
                provider = self._resolve_provider_binding_from_authority(
                    authority, registry_model
                )
                if (
                    provider_binding_id is not None
                    and provider_binding_id != provider.provider_binding_id
                ):
                    raise ModelGovernanceRuntimeError(
                        "provider binding identity changed during admission"
                    )
                provider_binding_id = provider.provider_binding_id
                artifact, deployment = self._invocation_facts_from_authority(
                    authority,
                    observed,
                    callsite_id,
                    provider_binding_id=provider_binding_id,
                )
            else:
                if artifact_facts is None or deployment_facts is None:
                    raise ModelGovernanceRuntimeError(
                        "artifact and deployment facts are required"
                    )
                artifact = self._coerce_artifact(artifact_facts)
                deployment = self._coerce_deployment(deployment_facts)
                if observed is None:
                    raise ModelGovernanceRuntimeError(
                        "observed governance facts are unavailable"
                    )
                known_artifact = next(
                    (
                        item
                        for item in observed.artifacts
                        if item.artifact_id == artifact.artifact_id
                    ),
                    None,
                )
                known_deployment = next(
                    (
                        item
                        for item in observed.deployments
                        if item.deployment_id == deployment.deployment_id
                    ),
                    None,
                )
                if known_artifact != artifact or known_deployment != deployment:
                    raise ModelGovernanceRuntimeError(
                        "invocation facts are not the current observed facts"
                    )
            invocation = invocation_id or f"inv-{uuid.uuid4().hex}"
            if not isinstance(invocation, str) or not invocation.strip():
                raise ModelGovernanceRuntimeError("invocation_id must be non-empty")
            try:
                governance = authority.authorize(
                    callsite_id=callsite_id,
                    classification=classification,
                    model_artifact_id=artifact.artifact_id,
                    deployment_id=deployment.deployment_id,
                    agent_id=agent_id,
                    now=current,
                    artifact_digest=artifact.digest,
                    artifact_revision=artifact.revision,
                    deployment_image_digest=deployment.image_digest,
                    provider_binding_id=provider_binding_id,
                )
            except ModelGovernanceError as exc:
                raise ModelGovernanceRuntimeError(str(exc)) from exc
            event = self._event(
                authorization=governance,
                invocation_id=invocation,
                endpoint=safe_endpoint,
                phase="pre",
                receipt_context=receipt_context,
                profile_content_sha256=authority.profile_content_sha256,
                inventory_sha256=authority.inventory_sha256,
            )
            effective_usage_sink = usage_sink or self.usage_sink
            effective_audit_sink = audit_sink or self.audit_sink
            if effective_usage_sink is None or not callable(
                getattr(effective_usage_sink, "compensate_pre_usage", None)
            ):
                raise ModelGovernanceRuntimeError(
                    "usage sink lacks transactional pre-receipt compensation"
                )
            usage_receipt = self._write_receipt(
                effective_usage_sink,
                "record_pre_usage",
                event,
                kind="usage pre",
            )
            try:
                audit_receipt = self._write_receipt(
                    effective_audit_sink,
                    "record_pre_audit",
                    event,
                    kind="audit pre",
                )
            except ModelGovernanceRuntimeError as exc:
                try:
                    self._compensate_receipt(
                        effective_usage_sink,
                        event,
                        method_name="compensate_pre_usage",
                        kind="usage pre",
                    )
                except ModelGovernanceRuntimeError as compensation_exc:
                    raise ModelGovernanceRuntimeError(
                        "pre-receipt transaction failed and compensation failed"
                    ) from compensation_exc
                raise ModelGovernanceRuntimeError(
                    "pre-receipt transaction failed; usage receipt compensated"
                ) from exc
            return ModelInvocationAuthorization(
                invocation_id=invocation,
                endpoint=safe_endpoint,
                governance=governance,
                authority_profile_content_sha256=authority.profile_content_sha256,
                authority_inventory_sha256=authority.inventory_sha256,
                pre_usage_receipt=usage_receipt,
                pre_audit_receipt=audit_receipt,
                authorized_at=current,
            )

    def record_post_usage(
        self,
        authorization: ModelInvocationAuthorization,
        usage: Mapping[str, Any],
        *,
        now: datetime | None = None,
        usage_sink: DurableReceiptSink | None = None,
        audit_sink: DurableReceiptSink | None = None,
        receipt_context: Mapping[str, Any] | None = None,
    ) -> ModelInvocationCompletion:
        """Persist post-network usage and audit receipts, fail-closed."""

        if not isinstance(authorization, ModelInvocationAuthorization):
            raise ModelGovernanceRuntimeError(
                "invalid immutable invocation authorization"
            )
        if not isinstance(usage, Mapping):
            raise ModelGovernanceRuntimeError("usage payload must be an object")
        current = _now(now)
        with self._lock:
            # The outbound request has already happened.  Re-running current
            # authority admission here can strand its durable pre-receipt if
            # the profile rotates, expires or is revoked in flight.  Closure
            # is instead bound to the immutable authorization snapshot and
            # its durable pre-receipt ids; the sink verifies that snapshot
            # against the invocation ledger before changing terminal state.
            event = self._event(
                authorization=authorization.governance,
                invocation_id=authorization.invocation_id,
                endpoint=authorization.endpoint,
                phase="post",
                usage=usage,
                outcome="success",
                receipt_context=receipt_context,
                profile_content_sha256=authorization.authority_profile_content_sha256,
                inventory_sha256=authorization.authority_inventory_sha256,
                pre_usage_receipt=authorization.pre_usage_receipt,
                pre_audit_receipt=authorization.pre_audit_receipt,
            )
            effective_usage_sink = usage_sink or self.usage_sink
            effective_audit_sink = audit_sink or self.audit_sink
            if effective_usage_sink is None or not callable(
                getattr(effective_usage_sink, "compensate_post_usage", None)
            ):
                raise ModelGovernanceRuntimeError(
                    "usage sink lacks transactional post-receipt compensation"
                )
            try:
                usage_receipt = self._write_receipt(
                    effective_usage_sink,
                    "record_post_usage",
                    event,
                    kind="usage post",
                )
            except ModelGovernanceRuntimeError as exc:
                event["outcome"] = "failure"
                event["error"] = str(exc)[:512]
                try:
                    self._write_receipt(
                        effective_audit_sink,
                        "record_failure_audit",
                        event,
                        kind="audit post-failure",
                    )
                except ModelGovernanceRuntimeError as audit_exc:
                    raise ModelGovernanceRuntimeError(
                        "post usage receipt failed and failure audit failed"
                    ) from audit_exc
                raise ModelGovernanceRuntimeError(
                    "post usage receipt failed; failure audit committed"
                ) from exc
            try:
                audit_receipt = self._write_receipt(
                    effective_audit_sink,
                    "record_post_audit",
                    event,
                    kind="audit post",
                )
            except ModelGovernanceRuntimeError as exc:
                try:
                    self._compensate_receipt(
                        effective_usage_sink,
                        event,
                        method_name="compensate_post_usage",
                        kind="usage post",
                    )
                except ModelGovernanceRuntimeError as compensation_exc:
                    raise ModelGovernanceRuntimeError(
                        "post-receipt transaction failed and compensation failed"
                    ) from compensation_exc
                raise ModelGovernanceRuntimeError(
                    "post-receipt transaction failed; usage receipt compensated"
                ) from exc
            return ModelInvocationCompletion(
                invocation_id=authorization.invocation_id,
                post_usage_receipt=usage_receipt,
                post_audit_receipt=audit_receipt,
                completed_at=current,
            )

    def record_post_failure(
        self,
        authorization: ModelInvocationAuthorization,
        error: BaseException | str,
        *,
        now: datetime | None = None,
        usage_sink: DurableReceiptSink | None = None,
        audit_sink: DurableReceiptSink | None = None,
        receipt_context: Mapping[str, Any] | None = None,
    ) -> ModelInvocationCompletion:
        """Close an admitted call with zero/partial usage and a failure audit."""

        if not isinstance(authorization, ModelInvocationAuthorization):
            raise ModelGovernanceRuntimeError(
                "invalid immutable invocation authorization"
            )
        current = _now(now)
        with self._lock:
            # Failure closure follows the same immutable authorization rule as
            # success closure.  Current authority still controls every new
            # admission, but cannot make an already-authorized ledger row
            # permanently non-terminal after outbound I/O.
            event = self._event(
                authorization=authorization.governance,
                invocation_id=authorization.invocation_id,
                endpoint=authorization.endpoint,
                phase="post",
                usage={"status": "failed", "error": str(error)[:512]},
                outcome="failure",
                receipt_context=receipt_context,
                profile_content_sha256=authorization.authority_profile_content_sha256,
                inventory_sha256=authorization.authority_inventory_sha256,
                pre_usage_receipt=authorization.pre_usage_receipt,
                pre_audit_receipt=authorization.pre_audit_receipt,
            )
            effective_usage_sink = usage_sink or self.usage_sink
            effective_audit_sink = audit_sink or self.audit_sink
            if effective_usage_sink is None or not callable(
                getattr(effective_usage_sink, "compensate_post_usage", None)
            ):
                raise ModelGovernanceRuntimeError(
                    "usage sink lacks transactional post-receipt compensation"
                )
            try:
                usage_receipt = self._write_receipt(
                    effective_usage_sink,
                    "record_post_usage",
                    event,
                    kind="usage failure post",
                )
                audit_receipt = self._write_receipt(
                    effective_audit_sink,
                    "record_post_audit",
                    event,
                    kind="audit failure post",
                )
            except ModelGovernanceRuntimeError as exc:
                event["error"] = str(exc)[:512]
                try:
                    self._compensate_receipt(
                        effective_usage_sink,
                        event,
                        method_name="compensate_post_usage",
                        kind="usage failure post",
                    )
                except ModelGovernanceRuntimeError as compensation_exc:
                    raise ModelGovernanceRuntimeError(
                        "failure receipt transaction failed and compensation failed"
                    ) from compensation_exc
                raise ModelGovernanceRuntimeError(
                    "failure receipt transaction failed; usage receipt compensated"
                ) from exc
            return ModelInvocationCompletion(
                invocation_id=authorization.invocation_id,
                post_usage_receipt=usage_receipt,
                post_audit_receipt=audit_receipt,
                completed_at=current,
            )

    complete_model_invocation = record_post_usage


__all__ = [
    "DurableReceiptSink",
    "ModelGovernanceRuntime",
    "ModelGovernanceRuntimeError",
    "ModelInvocationAuthorization",
    "ModelInvocationCompletion",
    "OBSERVED_GOVERNANCE_SCHEMA",
]
