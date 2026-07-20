#!/usr/bin/env python3
"""Generate ephemeral, synthetic Gate 5 material for Docker smoke tests.

The generated profile is deliberately synthetic and must never be copied into
a production bundle.  Ed25519 private keys are created only in memory, used to
sign the profile, and discarded; the output contains public trust keys only.
The default enabled callsite is the CSP memory extraction/embedding seam,
which currently has an executable empty agent scope.  The public and shared
CSP proxy seams can be selected explicitly for authority/schema tests, but
their scoped admission requires caller-agent context that the current proxy
path does not supply.  No Router, raw-model, or FLUX/legal approval is
implied by this fixture.
"""

from __future__ import annotations

import argparse
import base64
import copy
import json
import sys
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[3]
SECURITY_SRC = ROOT / "packages" / "anila-security" / "src"
if str(SECURITY_SRC) not in sys.path:
    sys.path.insert(0, str(SECURITY_SRC))

from anila_security.model_governance import (  # noqa: E402
    ProviderLocality,
    TransportTarget,
    canonical_json,
    inventory_content_sha256,
    profile_content_sha256,
    transport_target_sha256,
)


INVENTORY_PATH = ROOT / "infra/policy/gate5/model-governance-inventory.v1.json"
TEMPLATE_PATH = ROOT / "infra/policy/gate5/model-governance-profile.disabled-template.json"
OBSERVED_SCHEMA = "anila.gate5.model-governance.observed.v1"
ROLES = ("system_owner", "data_owner", "security", "operations")
DEFAULT_CALLSITE_IDS = (
    "r7.csp.memory",
)
SYNTHETIC_PROFILE_ID = "synthetic-gate5-smoke-not-production"
SYNTHETIC_PROVIDER_BINDING_ID = "provider.gate5-synthetic"
SYNTHETIC_MODEL_REGISTRY_ID = "model.gate5-synthetic"
SYNTHETIC_MODEL_REGISTRY_NAME = "synthetic-llm"
SYNTHETIC_MODEL_REGISTRY_REVISION = "synthetic-registry-1"
SYNTHETIC_PROVIDER_TARGET = "synthetic-model:8000"


def _selected_callsites(
    inventory: dict,
    *,
    callsite_ids: Sequence[str] | None,
    callsite_id: str | None,
) -> list[dict]:
    """Resolve and validate explicit synthetic callsites.

    ``callsite_id`` is retained as a compatibility alias for callers of the
    original single-callsite helper.  New callers should use ``callsite_ids``
    (and the CLI's repeatable ``--callsite-id`` option).
    """

    if callsite_ids is not None and callsite_id is not None:
        raise ValueError("pass callsite_ids or callsite_id, not both")
    selected_ids = tuple(
        callsite_ids
        if callsite_ids is not None
        else ((callsite_id,) if callsite_id is not None else DEFAULT_CALLSITE_IDS)
    )
    if not selected_ids:
        raise ValueError("at least one callsite must be selected")
    if len(selected_ids) != len(set(selected_ids)):
        raise ValueError("callsite ids must be unique")

    by_id = {item["id"]: item for item in inventory["callsites"]}
    unknown = [item for item in selected_ids if item not in by_id]
    if unknown:
        raise ValueError(f"unknown callsite id(s): {', '.join(unknown)}")

    calls = [by_id[item] for item in selected_ids]
    invalid = [
        call["id"]
        for call in calls
        if call["raw_endpoint"]
        or call["gateway_id"] != "csp-model-gateway"
        or not call["usage_sink"]
        or not call["audit_sink"]
        or call["id"].startswith("r7.flux.")
    ]
    if invalid:
        raise ValueError(
            "synthetic material only supports non-FLUX CSP callsites with "
            f"durable usage/audit sinks: {', '.join(invalid)}"
        )
    return calls


def _load(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def generate(
    output_dir: Path,
    *,
    callsite_ids: Sequence[str] | None = None,
    callsite_id: str | None = None,
) -> dict[str, Path]:
    """Write synthetic public material and return the four paths.

    Every enabled callsite shares one synthetic artifact/deployment pair so
    tests exercise multi-callsite authority with the same signed facts.  The
    output remains test-only and contains no private signing key.
    """

    inventory = _load(INVENTORY_PATH)
    template = _load(TEMPLATE_PATH)
    calls = _selected_callsites(
        inventory,
        callsite_ids=callsite_ids,
        callsite_id=callsite_id,
    )
    selected_ids = [call["id"] for call in calls]
    now = datetime.now(timezone.utc).replace(microsecond=0)
    artifact = {
        "artifact_id": "artifact.gate5-synthetic",
        "model_family": "synthetic-llm",
        "digest": "sha256:" + "a" * 64,
        "revision": "synthetic-smoke-1",
        "license_id": "Apache-2.0",
        "license_approved": True,
        "license_approval_artifact_id": "legal.synthetic-smoke.v1",
        "legal_approver_ids": ["synthetic-legal"],
    }
    deployment = {
        "deployment_id": "deployment.gate5-synthetic",
        "artifact_id": artifact["artifact_id"],
        "image_digest": "sha256:" + "b" * 64,
        "gpu_topology": {
            "vendor": "synthetic",
            "count": 1,
            "memory_gib": 1,
            "compute_capability": "synthetic",
        },
        "health_readiness": {
            "health_url": "/health",
            "readiness_url": "/ready",
            "freshness_seconds": 3600,
            "last_check": (now - timedelta(seconds=5)).isoformat(),
            "healthy": True,
            "ready": True,
        },
    }
    provider_target = TransportTarget.parse(
        SYNTHETIC_PROVIDER_TARGET,
        dns_policy="none",
        provider_locality=ProviderLocality.INTERNAL_ISOLATED,
    )
    provider_binding = {
        "provider_binding_id": SYNTHETIC_PROVIDER_BINDING_ID,
        "model_registry_id": SYNTHETIC_MODEL_REGISTRY_ID,
        "model_registry_name": SYNTHETIC_MODEL_REGISTRY_NAME,
        "model_registry_revision": SYNTHETIC_MODEL_REGISTRY_REVISION,
        "provider_locality": ProviderLocality.INTERNAL_ISOLATED.value,
        "transport_target": provider_target.to_dict(),
        "transport_target_sha256": transport_target_sha256(provider_target),
        "upstream_provider_locality": None,
        "upstream_transport_target": None,
        "upstream_transport_target_sha256": None,
        "egress_policy_id": None,
        "upstream_egress_policy_id": None,
        "model_artifact_id": artifact["artifact_id"],
        "deployment_id": deployment["deployment_id"],
    }
    profile = copy.deepcopy(template)
    profile.update(
        {
            "profile_id": SYNTHETIC_PROFILE_ID,
            "profile_version": "synthetic-1.0.0",
            "enabled": True,
            "enabled_callsites": selected_ids,
            "disabled_callsites": [
                item["id"]
                for item in inventory["callsites"]
                if item["id"] not in selected_ids
            ],
            "callsite_bindings": [
                {
                    "callsite_id": call["id"],
                    "gateway_id": call["gateway_id"],
                    "classification_ceiling": call["classification_ceiling"],
                    "usage_sink": call["usage_sink"],
                    "audit_sink": call["audit_sink"],
                    "agent_scope": call["agent_scope"],
                    "provider_binding_ids": [SYNTHETIC_PROVIDER_BINDING_ID],
                }
                for call in calls
            ],
            "provider_bindings": [provider_binding],
            "model_artifacts": [artifact],
            "deployments": [deployment],
            "valid_from": (now - timedelta(minutes=5)).isoformat(),
            "valid_until": (now + timedelta(hours=1)).isoformat(),
            "approvers": [
                {"role": role, "subject": f"synthetic-{role}"}
                for role in ROLES
            ],
            "signatures": [],
        }
    )
    profile["inventory_sha256"] = inventory_content_sha256(inventory)
    profile["profile_content_sha256"] = profile_content_sha256(profile)

    unsigned = dict(profile)
    unsigned.pop("signatures", None)
    trusted: dict[str, str] = {}
    signatures: list[dict[str, str]] = []
    for role in ROLES:
        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        trusted[role] = public_key.decode("ascii")
        signatures.append(
            {
                "role": role,
                "signature": base64.b64encode(
                    private_key.sign(canonical_json(unsigned))
                ).decode("ascii"),
            }
        )
    profile["signatures"] = signatures

    observed = {
        "schema_version": OBSERVED_SCHEMA,
        "artifacts": [
            {
                "artifact_id": artifact["artifact_id"],
                "digest": artifact["digest"],
                "revision": artifact["revision"],
            }
        ],
        "deployments": [deployment],
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "inventory": output_dir / "inventory.json",
        "profile": output_dir / "profile.json",
        "trust_store": output_dir / "trust-store.json",
        "observed_facts": output_dir / "observed-facts.json",
    }
    paths["inventory"].write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    paths["profile"].write_text(
        json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    paths["trust_store"].write_text(
        json.dumps({"trusted_signers": trusted}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    paths["observed_facts"].write_text(
        json.dumps(observed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return paths


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="temporary directory outside the repository preferred",
    )
    parser.add_argument(
        "--callsite-id",
        dest="callsite_ids",
        action="append",
        help=(
            "enable one non-FLUX CSP callsite (repeat for multiple); "
            "defaults to executable r7.csp.memory"
        ),
    )
    args = parser.parse_args()
    paths = generate(args.output_dir, callsite_ids=args.callsite_ids)
    for name, path in paths.items():
        print(f"{name}={path}")
    print(
        "synthetic test-only material; not an operator production approval; "
        "no private key was written"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
