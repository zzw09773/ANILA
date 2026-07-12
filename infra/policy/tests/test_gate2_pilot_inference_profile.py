from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from infra.policy.gate2.check_pilot_inference_profile import (
    PilotPolicyError,
    _canonical,
    inventory_hash,
    verify,
)

ROOT = Path(__file__).parents[3]
INVENTORY = ROOT / "infra/policy/gate2/inference-callsites.v1.json"
TEMPLATE = ROOT / "infra/policy/gate2/pilot-profile.disabled-template.json"
_INVENTORY_VALUE = json.loads(INVENTORY.read_text(encoding="utf-8"))
INELIGIBLE = [
    entry["id"] for entry in _INVENTORY_VALUE["callsites"]
    if not entry["pilot_eligible"]
]


def _signed_files(
    tmp_path: Path, *, enabled: list[str], overrides: dict | None = None,
    same_key: bool = False,
) -> tuple[Path, Path]:
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    all_ids = [entry["id"] for entry in inventory["callsites"]]
    profile = {
        "schema_version": "anila.gate2.signed-pilot.v1",
        "profile_id": "synthetic-test-only",
        "pilot_enabled": True,
        "data_classification_ceiling": "營業秘密",
        "inventory_sha256": inventory_hash(inventory),
        "enabled_callsites": enabled,
        "disabled_callsites": [item for item in all_ids if item not in enabled],
        "disabled_capabilities": [
            "studio", "artifact", "export", "flux", "prompt_generator",
            "relation_llm", "judge", "third_party_agents",
        ],
        "revocation_sla_seconds": 60,
        "lost_card_sla_seconds": 300,
        "pki_stale_policy": "fail_closed",
        "retention_days": 30,
        "withdrawal_procedure": "owner revokes profile and disables pilot mode",
        "collection_ids": [1],
        "allowed_targets": [
            {
                "callsite": callsite,
                "name": f"synthetic-{index}",
                "model_type": (
                    "embedding" if callsite.endswith("embedding") else "llm"
                ),
                "endpoint_url": f"http://synthetic-{index}:8000",
                "classification_ceiling": "營業秘密",
            }
            for index, callsite in enumerate(enabled, start=1)
        ],
        "valid_from": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
        "valid_until": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
    }
    if overrides:
        profile.update(overrides)
    signatures = []
    trust: dict[str, str] = {}
    payload = _canonical(profile)
    shared_key = Ed25519PrivateKey.generate() if same_key else None
    for role in ("system_owner", "data_owner", "pki_owner", "security"):
        private = shared_key or Ed25519PrivateKey.generate()
        public = private.public_key()
        trust[role] = public.public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")
        signatures.append({
            "role": role,
            "signature": base64.b64encode(private.sign(payload)).decode("ascii"),
        })
    profile["signatures"] = signatures
    profile_path = tmp_path / "profile.json"
    trust_path = tmp_path / "trust.json"
    profile_path.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    trust_path.write_text(json.dumps({"trusted_signers": trust}), encoding="utf-8")
    return profile_path, trust_path


def test_disabled_repository_template_is_evidence_not_approval() -> None:
    result = verify(
        inventory_path=INVENTORY,
        profile_path=TEMPLATE,
        allow_disabled_template=True,
    )
    assert result == {"callsites": 25, "enabled": 0, "pilot_enabled": False}
    with pytest.raises(PilotPolicyError, match="not an approval"):
        verify(inventory_path=INVENTORY, profile_path=TEMPLATE)


def test_four_independent_signers_can_enable_only_converged_callsite(
    tmp_path: Path,
) -> None:
    profile, trust = _signed_files(tmp_path, enabled=["csp.chat_model"])
    result = verify(
        inventory_path=INVENTORY, profile_path=profile, trust_store_path=trust
    )
    assert result["enabled"] == 1


@pytest.mark.parametrize(
    "callsite", INELIGIBLE,
)
def test_bare_or_unattributed_egress_cannot_be_signed_into_pilot(
    tmp_path: Path, callsite: str,
) -> None:
    profile, trust = _signed_files(tmp_path, enabled=[callsite])
    with pytest.raises(PilotPolicyError, match="unconverged"):
        verify(
            inventory_path=INVENTORY,
            profile_path=profile,
            trust_store_path=trust,
        )


def test_signature_or_inventory_mutation_fails_closed(tmp_path: Path) -> None:
    profile_path, trust = _signed_files(tmp_path, enabled=["csp.chat_model"])
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["retention_days"] = 31
    profile_path.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(PilotPolicyError, match="invalid signature"):
        verify(
            inventory_path=INVENTORY,
            profile_path=profile_path,
            trust_store_path=trust,
        )


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"revocation_sla_seconds": True}, "integer"),
        ({"lost_card_sla_seconds": -1}, "range"),
        ({"retention_days": -1}, "range"),
        ({"data_classification_ceiling": "未知"}, "classification"),
        ({"data_classification_ceiling": "機密"}, "classification"),
        ({"enabled_callsites": ["csp.chat_model", "csp.chat_model"]}, "duplicates"),
        ({"collection_ids": [1, 1]}, "collection_ids"),
        ({"collection_ids": [True]}, "collection_ids"),
        ({"allowed_targets": []}, "allowed targets"),
        ({"valid_from": "2026-01-01"}, "timezone"),
        ({"valid_until": "2000-01-01T00:00:00+00:00"}, "effective"),
    ],
)
def test_enabled_profile_scalar_schema_fails_closed(
    tmp_path: Path, overrides: dict, match: str,
) -> None:
    profile, trust = _signed_files(
        tmp_path, enabled=["csp.chat_model"], overrides=overrides
    )
    with pytest.raises(PilotPolicyError, match=match):
        verify(
            inventory_path=INVENTORY,
            profile_path=profile,
            trust_store_path=trust,
        )


def test_four_roles_must_use_four_distinct_trust_keys(tmp_path: Path) -> None:
    profile, trust = _signed_files(
        tmp_path, enabled=["csp.chat_model"], same_key=True
    )
    with pytest.raises(PilotPolicyError, match="distinct keys"):
        verify(
            inventory_path=INVENTORY,
            profile_path=profile,
            trust_store_path=trust,
        )
