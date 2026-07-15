#!/usr/bin/env python3
"""Generate Gate 6 P9 enabled-inference callsite evidence.

The generator is deliberately a read-only adapter around the Gate 5 static
verifier and model-governance authority.  It never treats the repository
inventory or a disabled profile as an admission decision.  A production
profile must pass the existing source scanner, digest checks, authority
validation and out-of-band signature verification before this command emits
enabled callsites.

The output is canonical JSON.  ``content_sha256`` is the SHA-256 of the
canonical output object with that self-referential field removed.  Callers may
provide ``--generated-at`` (or ``generated_at`` to :func:`generate_evidence`)
when reproducible evidence hashes are required.
"""

from __future__ import annotations

import argparse
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
P9_EVIDENCE_VERSION = "1.0.0"


def _utc_rfc3339(value: datetime) -> str:
    """Render an aware datetime in one stable UTC representation."""

    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalise_generated_at(
    generated_at: datetime | str | None,
    *,
    fallback: datetime | None,
) -> str:
    value: datetime | str
    if generated_at is None:
        value = fallback or datetime.now(timezone.utc)
    else:
        value = generated_at
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ModelGovernancePolicyError("generated_at must include timezone")
        return _utc_rfc3339(value)
    try:
        return _utc_rfc3339(parse_rfc3339(value, "generated_at"))
    except ModelGovernanceError as exc:
        raise ModelGovernancePolicyError(str(exc)) from exc


def _load_trust_store(path: Path | None) -> Mapping[str, Any] | None:
    """Load the optional out-of-band trust store through Gate 5's loader."""

    if path is None:
        return None
    return _load_json(path)


def _profile_digest_records(
    authority: VerifiedModelGovernanceAuthority,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return stable artifact and deployment digest records for enabled calls."""

    artifact_ids = {
        authority.bindings[callsite_id].model_artifact_id
        for callsite_id in authority.enabled_callsites
    }
    deployment_ids = {
        authority.bindings[callsite_id].deployment_id
        for callsite_id in authority.enabled_callsites
    }
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
    artifact = authority.model_artifacts.get(binding.model_artifact_id)
    deployment = authority.deployments.get(binding.deployment_id)
    if artifact is None or deployment is None:
        raise ModelGovernancePolicyError(
            f"enabled callsite artifact/deployment authority is incomplete: {callsite_id}"
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
        "model_artifact_id": artifact.artifact_id,
        "artifact_digest": artifact.digest,
        "artifact_revision": artifact.revision,
        "deployment_id": deployment.deployment_id,
        "deployment_digest": deployment.image_digest,
        "deployment_image_digest": deployment.image_digest,
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
        now=now,
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
            now=now,
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
            now=now,
        )
        if authority.enabled
        else None
    )

    # The authority's verified callsite list is the only source of enabled
    # IDs.  Never trust profile IDs directly when constructing the output.
    enabled_records = [
        _callsite_record(authority, callsite_id)
        for callsite_id in sorted(authority.enabled_callsites)
    ]
    artifact_records, deployment_records = _profile_digest_records(authority)
    generated = _normalise_generated_at(generated_at, fallback=now)
    is_production_profile = authority.enabled
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
            "valid_from": valid_from,
            "valid_until": valid_until,
            "artifact_digests": artifact_records,
            "deployment_digests": deployment_records,
        },
        "profile_id": authority.profile_id,
        "profile_version": authority.profile_version,
        "signed_profile_content_sha256": authority.profile_content_sha256,
        "profile_content_sha256": authority.profile_content_sha256,
        "artifact_digests": artifact_records,
        "deployment_digests": deployment_records,
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
        help="RFC3339 timestamp; provide this for deterministic content hashes",
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
