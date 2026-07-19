#!/usr/bin/env python3
"""Generate Gate 6 P9 enabled-inference callsite evidence.

The generator is deliberately a read-only adapter around the Gate 5 static
verifier and model-governance authority.  It never treats the repository
inventory or a disabled profile as an admission decision.  A production
profile must pass the existing source scanner, digest checks, authority
validation and out-of-band signature verification before this command emits
enabled callsites.

The output is canonical JSON.  ``content_sha256`` is the SHA-256 of the
canonical output object with that self-referential field removed.  Enabled
evidence binds ``generated_at`` to the single instant captured for this
verification.  CLI callers must omit ``--generated-at`` for enabled evidence,
whose hash intentionally reflects that verification instant.  Disabled
``NOT_ACCEPTANCE`` inspection may accept a fixed caller timestamp for
reproducible hashes.
"""

from __future__ import annotations

import argparse
import ipaddress
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Running this file directly places ``infra/policy/gate6`` on sys.path rather
# than the repository root.  Add the local shared package roots before
# importing the Gate 5 verifier; this keeps the CLI usable without requiring a
# caller-specific PYTHONPATH while preserving the verifier's import contract.
_REPO_HINT = Path(__file__).resolve().parents[3]
for _import_root in (_REPO_HINT, _REPO_HINT / "packages/anila-security/src"):
    if _import_root.is_dir() and str(_import_root) not in sys.path:
        sys.path.insert(0, str(_import_root))

from anila_security.model_governance import (  # noqa: E402
    ModelGovernanceError,
    VerifiedModelGovernanceAuthority,
    canonical_json,
    parse_rfc3339,
    sha256_hex,
)
from anila_security.production_acceptance_profile import (  # noqa: E402
    ProductionAcceptanceProfileError,
    VerifiedProductionAcceptanceProfile,
    verify_signed_production_acceptance_profile,
)
from infra.policy.gate5.check_model_governance import (  # noqa: E402
    ModelGovernancePolicyError,
    _load_json,
    verify as verify_gate5_model_governance,
)


P9_EVIDENCE_SCHEMA = "anila.gate6.p9.enabled-inference-callsite-evidence.v1"
P9_EVIDENCE_VERSION = "1.1.0"


def _provider_ids(
    authority: VerifiedModelGovernanceAuthority,
    callsite_id: str,
) -> tuple[str, ...]:
    """Return one callsite's provider IDs, or its explicit v1 legacy marker.

    The security package keeps the v1 artifact/deployment fields on
    ``CallsiteBinding`` for audit-only compatibility.  A v2 binding must use
    provider IDs; silently treating a missing/empty v2 list as a legacy
    binding would allow a profile to omit the provider authority entirely.
    """

    binding = authority.bindings.get(callsite_id)
    if binding is None:
        raise ModelGovernancePolicyError(
            f"enabled callsite authority is incomplete: {callsite_id}"
        )
    raw_ids = getattr(binding, "provider_binding_ids", ())
    if raw_ids is None:
        raw_ids = ()
    if not isinstance(raw_ids, (tuple, list)):
        raise ModelGovernancePolicyError(
            f"provider binding IDs are malformed for {callsite_id}"
        )
    ids = tuple(raw_ids)
    if len(ids) != len(set(ids)):
        raise ModelGovernancePolicyError(
            f"duplicate provider binding IDs for {callsite_id}"
        )
    if ids and ids != tuple(sorted(ids)):
        raise ModelGovernancePolicyError(
            f"provider binding IDs are not sorted for {callsite_id}"
        )

    legacy_artifact_id = getattr(binding, "model_artifact_id", None)
    legacy_deployment_id = getattr(binding, "deployment_id", None)
    if ids and (legacy_artifact_id is not None or legacy_deployment_id is not None):
        raise ModelGovernancePolicyError(
            f"provider binding IDs are inconsistent with legacy fields for {callsite_id}"
        )
    if not ids and (legacy_artifact_id is None) != (legacy_deployment_id is None):
        raise ModelGovernancePolicyError(
            f"legacy artifact/deployment binding is incomplete for {callsite_id}"
        )
    if not ids and legacy_artifact_id is None and authority.enabled:
        raise ModelGovernancePolicyError(
            f"missing provider binding IDs for enabled callsite {callsite_id}"
        )
    return ids


def _legacy_artifact_deployment_ids(
    authority: VerifiedModelGovernanceAuthority,
    callsite_id: str,
) -> tuple[str, str] | None:
    """Return v1 artifact/deployment IDs when an audit-only shape supplies them."""

    binding = authority.bindings.get(callsite_id)
    if binding is None:
        raise ModelGovernancePolicyError(
            f"enabled callsite authority is incomplete: {callsite_id}"
        )
    artifact_id = getattr(binding, "model_artifact_id", None)
    deployment_id = getattr(binding, "deployment_id", None)
    if artifact_id is None and deployment_id is None:
        return None
    if not isinstance(artifact_id, str) or not isinstance(deployment_id, str):
        raise ModelGovernancePolicyError(
            f"legacy artifact/deployment binding is malformed for {callsite_id}"
        )
    return artifact_id, deployment_id


def _target_identity(target: Any, *, field: str) -> dict[str, Any]:
    """Project non-sensitive transport identity without endpoint material.

    A target's canonical hash is the binding identity.  ``kind``/``scheme`` /
    ``port_mode`` are deliberately coarse metadata so host:port and FQDN
    forms remain distinguishable without exposing host, IP, port or path.
    """

    host = getattr(target, "host", None)
    scheme = getattr(target, "scheme", None)
    port_mode = getattr(target, "port_mode", None)
    path = getattr(target, "path", None)
    target_hash = getattr(target, "sha256", None)
    if (
        not isinstance(host, str)
        or not host
        or (scheme is not None and not isinstance(scheme, str))
        or not isinstance(port_mode, str)
        or not isinstance(path, str)
        or not isinstance(target_hash, str)
    ):
        raise ModelGovernancePolicyError(f"{field} target snapshot is incomplete")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        kind = "fqdn" if "." in host else "host_port"
    else:
        kind = "ip_literal"
    return {
        "kind": kind,
        "scheme": scheme,
        "port_mode": port_mode,
        "path_present": bool(path),
    }


def _provider_snapshot_record(
    authority: VerifiedModelGovernanceAuthority,
    provider_binding_id: str,
) -> dict[str, Any]:
    """Project one verified v2 provider row without raw endpoint material."""

    provider_bindings = getattr(authority, "provider_bindings", None)
    if not isinstance(provider_bindings, Mapping):
        raise ModelGovernancePolicyError("provider binding authority is missing")
    provider = provider_bindings.get(provider_binding_id)
    if provider is None:
        raise ModelGovernancePolicyError(
            f"unknown provider binding ID: {provider_binding_id}"
        )

    transport = getattr(provider, "transport_target", None)
    transport_hash = getattr(provider, "transport_target_sha256", None)
    canonical_transport_hash = getattr(transport, "sha256", None)
    if (
        not isinstance(transport_hash, str)
        or not isinstance(canonical_transport_hash, str)
        or transport_hash != canonical_transport_hash
    ):
        raise ModelGovernancePolicyError(
            f"provider binding target hash mismatch: {provider_binding_id}"
        )
    upstream = getattr(provider, "upstream_transport_target", None)
    upstream_hash = getattr(provider, "upstream_transport_target_sha256", None)
    upstream_locality = getattr(provider, "upstream_provider_locality", None)
    if upstream is None:
        if upstream_hash is not None or upstream_locality is not None:
            raise ModelGovernancePolicyError(
                f"provider binding upstream snapshot is inconsistent: {provider_binding_id}"
            )
        upstream_identity = None
    else:
        canonical_upstream_hash = getattr(upstream, "sha256", None)
        if (
            not isinstance(upstream_hash, str)
            or not isinstance(canonical_upstream_hash, str)
            or upstream_hash != canonical_upstream_hash
            or not isinstance(upstream_locality, str)
        ):
            raise ModelGovernancePolicyError(
                f"provider binding upstream hash mismatch: {provider_binding_id}"
            )
        upstream_identity = _target_identity(
            upstream, field=f"provider_bindings[{provider_binding_id}].upstream"
        )

    artifact_id = getattr(provider, "model_artifact_id", None)
    deployment_id = getattr(provider, "deployment_id", None)
    if not isinstance(artifact_id, str) or not isinstance(deployment_id, str):
        raise ModelGovernancePolicyError(
            f"provider binding artifact/deployment identity is incomplete: {provider_binding_id}"
        )
    artifact = authority.model_artifacts.get(artifact_id)
    deployment = authority.deployments.get(deployment_id)
    if artifact is None or deployment is None:
        raise ModelGovernancePolicyError(
            f"provider binding artifact/deployment authority is incomplete: {provider_binding_id}"
        )
    if deployment.artifact_id != artifact.artifact_id:
        raise ModelGovernancePolicyError(
            f"provider binding artifact/deployment drift: {provider_binding_id}"
        )

    return {
        "provider_binding_id": provider_binding_id,
        "model_registry_id": provider.model_registry_id,
        "model_registry_name": provider.model_registry_name,
        "model_registry_revision": provider.model_registry_revision,
        "provider_locality": provider.provider_locality,
        "transport_target_sha256": transport_hash,
        "transport_target_identity": _target_identity(
            transport, field=f"provider_bindings[{provider_binding_id}]"
        ),
        "upstream_provider_locality": upstream_locality,
        "upstream_transport_target_sha256": upstream_hash,
        "upstream_transport_target_identity": upstream_identity,
        "egress_policy_id": provider.egress_policy_id,
        "upstream_egress_policy_id": provider.upstream_egress_policy_id,
        "model_artifact_id": artifact.artifact_id,
        "artifact_digest": artifact.digest,
        "artifact_revision": artifact.revision,
        "deployment_id": deployment.deployment_id,
        "deployment_digest": deployment.image_digest,
        "deployment_image_digest": deployment.image_digest,
    }


def _callsite_authority_refs(
    authority: VerifiedModelGovernanceAuthority,
    callsite_id: str,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Return provider IDs and artifact/deployment IDs for one callsite."""

    ids = _provider_ids(authority, callsite_id)
    if ids:
        snapshots = [
            _provider_snapshot_record(authority, provider_binding_id)
            for provider_binding_id in ids
        ]
        artifact_ids = tuple(sorted({item["model_artifact_id"] for item in snapshots}))
        deployment_ids = tuple(sorted({item["deployment_id"] for item in snapshots}))
        return ids, artifact_ids, deployment_ids
    legacy = _legacy_artifact_deployment_ids(authority, callsite_id)
    if legacy is None:
        return (), (), ()
    return (), (legacy[0],), (legacy[1],)


def _utc_rfc3339(value: datetime) -> str:
    """Render an aware datetime in one stable UTC representation."""

    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalise_generated_at(
    generated_at: datetime | str | None,
    *,
    fallback: datetime | None,
) -> str:
    return _utc_rfc3339(_parse_generated_at(generated_at, fallback=fallback))


def _parse_generated_at(
    generated_at: datetime | str | None,
    *,
    fallback: datetime | None,
) -> datetime:
    value: datetime | str
    if generated_at is None:
        value = fallback or datetime.now(timezone.utc)
    else:
        value = generated_at
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ModelGovernancePolicyError("generated_at must include timezone")
        return value.astimezone(timezone.utc)
    try:
        return parse_rfc3339(value, "generated_at").astimezone(timezone.utc)
    except ModelGovernanceError as exc:
        raise ModelGovernancePolicyError(str(exc)) from exc


def _normalise_verification_now(now: datetime | None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ModelGovernancePolicyError("now must include timezone")
    return value.astimezone(timezone.utc)


def _assert_enabled_evidence_timestamp(
    *,
    generated_at: datetime,
    verification_now: datetime,
    authority: VerifiedModelGovernanceAuthority,
    acceptance: VerifiedProductionAcceptanceProfile,
) -> None:
    """Bind enabled evidence to this verification instant and its authorities."""

    if generated_at != verification_now:
        raise ModelGovernancePolicyError(
            "enabled evidence generated_at must equal the verification instant"
        )
    for label, valid_from, valid_until in (
        ("Gate 5 authority", authority.valid_from, authority.valid_until),
        ("P0 acceptance profile", acceptance.valid_from, acceptance.valid_until),
    ):
        if valid_from is None or valid_until is None:
            raise ModelGovernancePolicyError(
                f"{label} has no validity window for enabled evidence"
            )
        if not valid_from <= generated_at < valid_until:
            raise ModelGovernancePolicyError(
                f"generated_at is outside the {label} validity window"
            )


def _load_trust_store(path: Path | None) -> Mapping[str, Any] | None:
    """Load the optional out-of-band trust store through Gate 5's loader."""

    if path is None:
        return None
    return _load_json(path)


def _profile_digest_records(
    authority: VerifiedModelGovernanceAuthority,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return stable artifact and deployment digest records for enabled calls."""

    artifact_ids: set[str] = set()
    deployment_ids: set[str] = set()
    for callsite_id in authority.enabled_callsites:
        _, callsite_artifact_ids, callsite_deployment_ids = _callsite_authority_refs(
            authority, callsite_id
        )
        artifact_ids.update(callsite_artifact_ids)
        deployment_ids.update(callsite_deployment_ids)
    artifacts = [
        authority.model_artifacts[artifact_id]
        for artifact_id in sorted(artifact_ids)
    ]
    deployments = [
        authority.deployments[deployment_id]
        for deployment_id in sorted(deployment_ids)
    ]
    artifact_records = [
        {
            "artifact_id": artifact.artifact_id,
            "model_family": artifact.model_family,
            "digest": artifact.digest,
            "artifact_digest": artifact.digest,
            "revision": artifact.revision,
            "license_id": artifact.license_id,
            "license_approval_artifact_id": artifact.license_approval_artifact_id,
        }
        for artifact in artifacts
    ]
    deployment_records = [
        {
            "deployment_id": deployment.deployment_id,
            "artifact_id": deployment.artifact_id,
            "image_digest": deployment.image_digest,
            "digest": deployment.image_digest,
            "deployment_digest": deployment.image_digest,
        }
        for deployment in deployments
    ]
    return artifact_records, deployment_records


def _callsite_record(
    authority: VerifiedModelGovernanceAuthority,
    callsite_id: str,
) -> dict[str, Any]:
    """Project one verified callsite and its immutable profile binding."""

    # ``from_verified_payload`` proves these lookups are complete.  Keep the
    # explicit guards here so a future authority shape change fails closed
    # rather than producing partial evidence.
    callsite = authority.callsites.get(callsite_id)
    binding = authority.bindings.get(callsite_id)
    if callsite is None or binding is None:
        raise ModelGovernancePolicyError(
            f"enabled callsite authority is incomplete: {callsite_id}"
        )
    provider_ids, artifact_ids, deployment_ids = _callsite_authority_refs(
        authority, callsite_id
    )
    if not artifact_ids or not deployment_ids:
        raise ModelGovernancePolicyError(
            f"enabled callsite artifact/deployment authority is incomplete: {callsite_id}"
        )
    provider_snapshots = [
        _provider_snapshot_record(authority, provider_binding_id)
        for provider_binding_id in provider_ids
    ]
    primary_artifact_id = artifact_ids[0] if len(artifact_ids) == 1 else None
    primary_deployment_id = deployment_ids[0] if len(deployment_ids) == 1 else None
    primary_artifact = (
        authority.model_artifacts.get(primary_artifact_id)
        if primary_artifact_id is not None
        else None
    )
    primary_deployment = (
        authority.deployments.get(primary_deployment_id)
        if primary_deployment_id is not None
        else None
    )
    return {
        "id": callsite.callsite_id,
        "category": callsite.kind,
        "kind": callsite.kind,
        "component": callsite.service,
        "service": callsite.service,
        "owner": callsite.owner,
        "path": callsite.source,
        "source": callsite.source,
        "symbol": callsite.symbol,
        "gateway": callsite.gateway_id,
        "gateway_id": callsite.gateway_id,
        "raw_endpoint": callsite.raw_endpoint,
        "classification_ceiling": callsite.classification_ceiling,
        "usage_sink": callsite.usage_sink,
        "audit_sink": callsite.audit_sink,
        "agent_scope": list(callsite.agent_scope),
        "sink_kinds": list(callsite.sink_kinds),
        "provider_binding_ids": list(provider_ids),
        "provider_snapshots": provider_snapshots,
        # Keep the old scalar projection for one-to-one bindings.  v2 permits
        # multiple providers, so plural IDs are authoritative in that case.
        "model_artifact_id": primary_artifact_id,
        "model_artifact_ids": list(artifact_ids),
        "artifact_digest": primary_artifact.digest if primary_artifact else None,
        "artifact_revision": primary_artifact.revision if primary_artifact else None,
        "deployment_id": primary_deployment_id,
        "deployment_ids": list(deployment_ids),
        "deployment_digest": (
            primary_deployment.image_digest if primary_deployment else None
        ),
        "deployment_image_digest": (
            primary_deployment.image_digest if primary_deployment else None
        ),
    }


def _evidence_hash(evidence: Mapping[str, Any]) -> str:
    content = dict(evidence)
    content.pop("content_sha256", None)
    return sha256_hex(content)


def _verify_p0_acceptance_profile(
    *,
    profile_path: Path | None,
    trust_store_path: Path | None,
    inventory: Mapping[str, Any],
    authority: VerifiedModelGovernanceAuthority,
    now: datetime | None,
) -> VerifiedProductionAcceptanceProfile:
    """Verify P0 and bind its enabled-callsite projection to Gate 5 authority."""

    if profile_path is None or trust_store_path is None:
        raise ModelGovernancePolicyError(
            "enabled production evidence requires --acceptance-profile and "
            "--acceptance-trust-store"
        )
    try:
        verified = verify_signed_production_acceptance_profile(
            profile_path,
            trust_store_path,
            now=now,
            expected_inventory_sha256=authority.inventory_sha256,
            expected_inventory_version=str(inventory["inventory_version"]),
        )
    except ProductionAcceptanceProfileError as exc:
        raise ModelGovernancePolicyError(f"P0 acceptance profile verification failed: {exc}") from exc

    p0_inventory = verified.profile.get("enabled_inference_callsite_inventory")
    if not isinstance(p0_inventory, Mapping):
        raise ModelGovernancePolicyError(
            "P0 enabled_inference_callsite_inventory is missing"
        )
    expected_schema = inventory.get("schema_version")
    if p0_inventory.get("schema_version") != expected_schema:
        raise ModelGovernancePolicyError(
            "P0 enabled inference callsite inventory schema_version mismatch"
        )
    if p0_inventory.get("version") != inventory.get("inventory_version"):
        raise ModelGovernancePolicyError(
            "P0 enabled inference callsite inventory version mismatch"
        )
    if p0_inventory.get("sha256") != authority.inventory_sha256:
        raise ModelGovernancePolicyError(
            "P0 enabled inference callsite inventory sha256 mismatch"
        )
    p0_ids = p0_inventory.get("callsite_ids")
    if not isinstance(p0_ids, list) or set(p0_ids) != set(authority.enabled_callsites):
        raise ModelGovernancePolicyError(
            "P0 enabled inference callsite inventory callsite_ids mismatch"
        )
    return verified


def generate_evidence(
    *,
    inventory_path: Path,
    profile_path: Path,
    trust_store_path: Path | None = None,
    acceptance_profile_path: Path | None = None,
    acceptance_trust_store_path: Path | None = None,
    repo_root: Path,
    allow_disabled_template: bool = False,
    now: datetime | None = None,
    generated_at: datetime | str | None = None,
) -> dict[str, Any]:
    """Verify Gate 5 material and return canonical P9 evidence as a mapping.

    The function performs no writes.  Enabled/production evidence requires a
    separately signed Gate 6 P0 acceptance profile and trust store; its
    callsite inventory is cross-checked against the Gate 5 authority.  The
    enabled ``generated_at`` is the same single UTC-normalized instant used by
    both verifiers; an earlier or later caller value is rejected.  The
    ``allow_disabled_template`` exception is intended only for tests and
    repository evidence inspection; when used, the output is explicitly
    marked ``non-production``/``NOT_ACCEPTANCE`` and contains no enabled
    callsites.
    """

    inventory_path = Path(inventory_path)
    profile_path = Path(profile_path)
    if trust_store_path is not None:
        trust_store_path = Path(trust_store_path)
    if acceptance_profile_path is not None:
        acceptance_profile_path = Path(acceptance_profile_path)
    if acceptance_trust_store_path is not None:
        acceptance_trust_store_path = Path(acceptance_trust_store_path)
    repo_root = Path(repo_root)
    verification_now = _normalise_verification_now(now)

    # Run the existing static verifier first.  It owns source scanning,
    # inventory/profile shape checks, inventory binding and fail-closed policy
    # errors.  We then construct the same immutable authority to obtain the
    # audited projections needed for evidence.
    verify_gate5_model_governance(
        inventory_path=inventory_path,
        profile_path=profile_path,
        trust_store_path=trust_store_path,
        repo_root=repo_root,
        allow_disabled_template=allow_disabled_template,
        now=verification_now,
    )
    inventory = _load_json(inventory_path)
    profile = _load_json(profile_path)
    trust_store = _load_trust_store(trust_store_path)
    try:
        authority = VerifiedModelGovernanceAuthority.from_verified_payload(
            inventory=inventory,
            profile=profile,
            trust_store=trust_store,
            allow_disabled_template=allow_disabled_template,
            now=verification_now,
        )
    except ModelGovernanceError as exc:
        raise ModelGovernancePolicyError(str(exc)) from exc

    # A P0 signed acceptance profile is mandatory for every enabled/production
    # export.  The disabled repository template is the sole explicit
    # test-only exception and deliberately carries no P0 approval.
    acceptance = (
        _verify_p0_acceptance_profile(
            profile_path=acceptance_profile_path,
            trust_store_path=acceptance_trust_store_path,
            inventory=inventory,
            authority=authority,
            now=verification_now,
        )
        if authority.enabled
        else None
    )

    # The authority's verified callsite list is the only source of enabled
    # IDs.  Never trust profile IDs directly when constructing the output.
    generated_at_value = _parse_generated_at(
        generated_at, fallback=verification_now
    )
    is_production_profile = authority.enabled
    if is_production_profile:
        if acceptance is None:
            raise ModelGovernancePolicyError(
                "enabled production evidence requires a verified P0 acceptance profile"
            )
        _assert_enabled_evidence_timestamp(
            generated_at=generated_at_value,
            verification_now=verification_now,
            authority=authority,
            acceptance=acceptance,
        )

    enabled_records = [
        _callsite_record(authority, callsite_id)
        for callsite_id in sorted(authority.enabled_callsites)
    ]
    provider_binding_ids = sorted(
        {
            provider_binding_id
            for record in enabled_records
            for provider_binding_id in record["provider_binding_ids"]
        }
    )
    provider_snapshots = [
        _provider_snapshot_record(authority, provider_binding_id)
        for provider_binding_id in provider_binding_ids
    ]
    artifact_records, deployment_records = _profile_digest_records(authority)
    generated = _utc_rfc3339(generated_at_value)
    status = "VERIFIED" if is_production_profile else "NOT_ACCEPTANCE"
    environment = "production" if is_production_profile else "non-production"
    valid_from = (
        _utc_rfc3339(authority.valid_from) if authority.valid_from is not None else None
    )
    valid_until = (
        _utc_rfc3339(authority.valid_until) if authority.valid_until is not None else None
    )
    acceptance_record: dict[str, Any] | None = None
    if acceptance is not None:
        p0_inventory = acceptance.profile["enabled_inference_callsite_inventory"]
        acceptance_record = {
            "profile_id": acceptance.profile_id,
            "profile_version": acceptance.profile_version,
            "content_sha256": acceptance.profile_content_sha256,
            "signer_roles": sorted(acceptance.signer_roles),
            "valid_from": _utc_rfc3339(acceptance.valid_from),
            "valid_until": _utc_rfc3339(acceptance.valid_until),
            "enabled_inference_callsite_inventory": dict(p0_inventory),
        }

    evidence: dict[str, Any] = {
        "schema_version": P9_EVIDENCE_SCHEMA,
        "evidence_version": P9_EVIDENCE_VERSION,
        "generated_at": generated,
        "status": status,
        "acceptance_status": status,
        "environment": environment,
        # This artifact is an input to Gate 6 P9, not a declaration that the
        # complete P0-P9 acceptance has passed.
        "gate6_pass": False,
        "profile_enabled": is_production_profile,
        "acceptance_profile": acceptance_record,
        "acceptance_profile_id": acceptance.profile_id if acceptance else None,
        "acceptance_profile_version": acceptance.profile_version if acceptance else None,
        "acceptance_profile_content_sha256": (
            acceptance.profile_content_sha256 if acceptance else None
        ),
        "acceptance_profile_signer_roles": (
            sorted(acceptance.signer_roles) if acceptance else []
        ),
        "source_inventory": {
            "inventory_id": inventory["inventory_id"],
            "inventory_version": inventory["inventory_version"],
            "sha256": authority.inventory_sha256,
        },
        "source_inventory_sha256": authority.inventory_sha256,
        "inventory_sha256": authority.inventory_sha256,
        "signed_profile": {
            "profile_id": authority.profile_id,
            "profile_version": authority.profile_version,
            "content_sha256": authority.profile_content_sha256,
            "inventory_sha256": authority.inventory_sha256,
            "inventory_id": inventory["inventory_id"],
            "inventory_version": inventory["inventory_version"],
            "valid_from": valid_from,
            "valid_until": valid_until,
            "artifact_digests": artifact_records,
            "deployment_digests": deployment_records,
        },
        "authority_identity": {
            "profile_id": authority.profile_id,
            "profile_version": authority.profile_version,
            "profile_content_sha256": authority.profile_content_sha256,
            "inventory_id": inventory["inventory_id"],
            "inventory_version": inventory["inventory_version"],
            "inventory_sha256": authority.inventory_sha256,
        },
        "profile_id": authority.profile_id,
        "profile_version": authority.profile_version,
        "signed_profile_content_sha256": authority.profile_content_sha256,
        "profile_content_sha256": authority.profile_content_sha256,
        "artifact_digests": artifact_records,
        "deployment_digests": deployment_records,
        "provider_binding_ids": provider_binding_ids,
        "provider_snapshots": provider_snapshots,
        "enabled_callsite_count": len(enabled_records),
        "enabled_callsites": enabled_records,
    }
    evidence["content_sha256"] = _evidence_hash(evidence)
    return evidence


def render_canonical_evidence(evidence: Mapping[str, Any]) -> bytes:
    """Render evidence with canonical JSON and one trailing newline."""

    if not isinstance(evidence, Mapping):
        raise TypeError("evidence must be an object")
    expected_hash = evidence.get("content_sha256")
    if not isinstance(expected_hash, str) or _evidence_hash(evidence) != expected_hash:
        raise ModelGovernancePolicyError("evidence content_sha256 mismatch")
    return canonical_json(evidence) + b"\n"


def _write_output(payload: bytes, output_path: Path | None) -> None:
    if output_path is None or output_path == Path("-"):
        sys.stdout.buffer.write(payload)
        return
    Path(output_path).write_bytes(payload)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate verified Gate 6 P9 enabled inference callsite evidence."
    )
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--trust-store", type=Path)
    parser.add_argument(
        "--acceptance-profile",
        type=Path,
        help="signed Gate 6 P0 production-acceptance profile (required when enabled)",
    )
    parser.add_argument(
        "--acceptance-trust-store",
        type=Path,
        help="out-of-band five-signer trust store for the P0 profile",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        required=True,
        help="repository source root scanned by the Gate 5 verifier",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="write canonical evidence to this path (default: stdout)",
    )
    parser.add_argument(
        "--generated-at",
        help=(
            "RFC3339 timestamp; enabled evidence must omit --generated-at so the "
            "verification instant is captured once; fixed timestamps are only for "
            "disabled NOT_ACCEPTANCE inspection"
        ),
    )
    parser.add_argument(
        "--allow-disabled-template",
        "--test-only-allow-disabled-template",
        dest="allow_disabled_template",
        action="store_true",
        help=(
            "TEST-ONLY: inspect the disabled repository template; output is "
            "explicitly non-production/NOT_ACCEPTANCE"
        ),
    )
    args = parser.parse_args(argv)
    try:
        evidence = generate_evidence(
            inventory_path=args.inventory,
            profile_path=args.profile,
            trust_store_path=args.trust_store,
            acceptance_profile_path=args.acceptance_profile,
            acceptance_trust_store_path=args.acceptance_trust_store,
            repo_root=args.repo_root,
            allow_disabled_template=args.allow_disabled_template,
            generated_at=args.generated_at,
        )
        _write_output(render_canonical_evidence(evidence), args.output)
    except (ModelGovernancePolicyError, ModelGovernanceError, OSError, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
