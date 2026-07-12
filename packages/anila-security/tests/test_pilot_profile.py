from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from anila_security import PilotProfileError, verify_signed_pilot_profile


def _canonical(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()


def _files(tmp_path, *, overrides=None, same_key: bool = False):
    inventory = {
        "schema_version": "anila.gate2.inference-callsites.v1",
        "callsites": [
            {
                "id": "csp.chat_model", "mode": "via_csp", "pilot_eligible": True,
                "controls": {
                    "csp_mediated": True, "task": True,
                    "classification_ceiling": True, "usage": True, "audit": True,
                },
            },
            {"id": "bare.model", "mode": "direct_unconverged", "pilot_eligible": False},
        ],
    }
    profile = {
        "schema_version": "anila.gate2.signed-pilot.v1",
        "profile_id": "synthetic-test",
        "pilot_enabled": True,
        "data_classification_ceiling": "營業秘密",
        "inventory_sha256": hashlib.sha256(_canonical(inventory)).hexdigest(),
        "enabled_callsites": ["csp.chat_model"],
        "disabled_callsites": ["bare.model"],
        "disabled_capabilities": [
            "studio", "artifact", "export", "flux", "prompt_generator",
            "relation_llm", "judge", "third_party_agents",
        ],
        "revocation_sla_seconds": 60,
        "lost_card_sla_seconds": 300,
        "pki_stale_policy": "fail_closed",
        "retention_days": 30,
        "withdrawal_procedure": "revoke profile",
        "collection_ids": [1],
        "valid_from": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
        "valid_until": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
    }
    if overrides:
        profile.update(overrides)
    payload = _canonical(profile)
    trust = {}
    signatures = []
    shared_key = Ed25519PrivateKey.generate() if same_key else None
    for role in ("system_owner", "data_owner", "pki_owner", "security"):
        key = shared_key or Ed25519PrivateKey.generate()
        trust[role] = key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()
        signatures.append({
            "role": role,
            "signature": base64.b64encode(key.sign(payload)).decode(),
        })
    profile["signatures"] = signatures
    paths = [tmp_path / name for name in ("profile.json", "inventory.json", "trust.json")]
    paths[0].write_text(json.dumps(profile), encoding="utf-8")
    paths[1].write_text(json.dumps(inventory), encoding="utf-8")
    paths[2].write_text(json.dumps({"trusted_signers": trust}), encoding="utf-8")
    return paths


def test_four_role_signature_and_inventory_binding(tmp_path) -> None:
    profile, inventory, trust = _files(tmp_path)
    assert verify_signed_pilot_profile(
        profile_path=profile, inventory_path=inventory, trust_store_path=trust
    ) == frozenset({"csp.chat_model"})

    value = json.loads(profile.read_text())
    value["enabled_callsites"] = ["bare.model"]
    value["disabled_callsites"] = ["csp.chat_model"]
    profile.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(PilotProfileError, match="unconverged"):
        verify_signed_pilot_profile(
            profile_path=profile, inventory_path=inventory, trust_store_path=trust
        )


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"revocation_sla_seconds": False}, "integer"),
        ({"retention_days": -3}, "range"),
        ({"data_classification_ceiling": "機密"}, "classification"),
        ({"disabled_callsites": ["bare.model", "bare.model"]}, "duplicates"),
        ({"collection_ids": [1, 1]}, "collection_ids"),
        ({"valid_until": "2000-01-01T00:00:00Z"}, "effective"),
    ],
)
def test_runtime_verifier_rejects_malformed_signed_contract(
    tmp_path, overrides, match,
) -> None:
    profile, inventory, trust = _files(tmp_path, overrides=overrides)
    with pytest.raises(PilotProfileError, match=match):
        verify_signed_pilot_profile(
            profile_path=profile, inventory_path=inventory, trust_store_path=trust
        )


def test_runtime_verifier_rejects_same_key_for_all_roles(tmp_path) -> None:
    profile, inventory, trust = _files(tmp_path, same_key=True)
    with pytest.raises(PilotProfileError, match="distinct keys"):
        verify_signed_pilot_profile(
            profile_path=profile, inventory_path=inventory, trust_store_path=trust
        )
