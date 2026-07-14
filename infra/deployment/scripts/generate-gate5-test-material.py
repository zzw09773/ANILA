#!/usr/bin/env python3
"""Generate ephemeral, synthetic Gate 5 material for Docker smoke tests.

The generated profile is deliberately synthetic and must never be copied into
a production bundle.  Ed25519 private keys are created only in memory, used to
sign the profile, and discarded; the output contains public trust keys only.
The default enabled callsite is the Router CSP path, so no FLUX/legal approval
is implied by this fixture.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[3]
SECURITY_SRC = ROOT / "packages" / "anila-security" / "src"
if str(SECURITY_SRC) not in sys.path:
    sys.path.insert(0, str(SECURITY_SRC))

from anila_security.model_governance import (  # noqa: E402
    canonical_json,
    inventory_content_sha256,
    profile_content_sha256,
)


INVENTORY_PATH = ROOT / "infra/policy/gate5/model-governance-inventory.v1.json"
TEMPLATE_PATH = ROOT / "infra/policy/gate5/model-governance-profile.disabled-template.json"
OBSERVED_SCHEMA = "anila.gate5.model-governance.observed.v1"
ROLES = ("system_owner", "data_owner", "security", "operations")


def _load(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def generate(output_dir: Path, *, callsite_id: str = "r7.router.core") -> dict[str, Path]:
    """Write synthetic public material and return the four paths."""

    inventory = _load(INVENTORY_PATH)
    template = _load(TEMPLATE_PATH)
    call = next(
        item for item in inventory["callsites"] if item["id"] == callsite_id
    )
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
    profile = copy.deepcopy(template)
    profile.update(
        {
            "profile_id": "synthetic-gate5-smoke-not-production",
            "profile_version": "synthetic-1.0.0",
            "enabled": True,
            "enabled_callsites": [callsite_id],
            "disabled_callsites": [
                item["id"]
                for item in inventory["callsites"]
                if item["id"] != callsite_id
            ],
            "callsite_bindings": [
                {
                    "callsite_id": callsite_id,
                    "gateway_id": call["gateway_id"],
                    "classification_ceiling": call["classification_ceiling"],
                    "usage_sink": call["usage_sink"],
                    "audit_sink": call["audit_sink"],
                    "agent_scope": call["agent_scope"],
                    "model_artifact_id": artifact["artifact_id"],
                    "deployment_id": deployment["deployment_id"],
                }
            ],
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
    parser.add_argument("--callsite-id", default="r7.router.core")
    args = parser.parse_args()
    paths = generate(args.output_dir, callsite_id=args.callsite_id)
    for name, path in paths.items():
        print(f"{name}={path}")
    print("synthetic material only; no private key was written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
