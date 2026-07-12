#!/usr/bin/env python3
"""Fail-closed Gate 2 signed-pilot inference inventory verifier.

The repository ships only a disabled template.  Enabling a pilot requires a
separate profile plus an out-of-band trusted-key store; embedded keys are never
trusted.  Every enabled callsite must be CSP mediated, classification-gated,
task linked, usage accounted and audited.  Ineligible/direct callsites can
never be enabled by a signature.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REQUIRED_SIGNERS = {"system_owner", "data_owner", "pki_owner", "security"}
REQUIRED_DISABLED = {
    "studio", "artifact", "export", "flux", "prompt_generator",
    "relation_llm", "judge", "third_party_agents",
}


class PilotPolicyError(RuntimeError):
    pass


_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")
_PILOT_CEILINGS = {"無機密", "營業秘密"}


def _unique_nonempty_strings(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise PilotPolicyError(f"{field} must be a list")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise PilotPolicyError(f"{field} must contain non-empty strings")
    if len(value) != len(set(value)):
        raise PilotPolicyError(f"{field} contains duplicates")
    return value


def _bounded_int(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PilotPolicyError(f"{field} must be an integer")
    if not minimum <= value <= maximum:
        raise PilotPolicyError(f"{field} outside allowed range")
    return value


def _aware_rfc3339(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise PilotPolicyError(f"{field} must be RFC3339")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PilotPolicyError(f"{field} must be RFC3339") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PilotPolicyError(f"{field} must include timezone")
    return parsed.astimezone(timezone.utc)


def _validate_profile_contract(profile: dict[str, Any]) -> tuple[list[str], list[str]]:
    if not isinstance(profile.get("pilot_enabled"), bool):
        raise PilotPolicyError("pilot_enabled must be boolean")
    profile_id = profile.get("profile_id")
    if not isinstance(profile_id, str) or not _PROFILE_ID_RE.fullmatch(profile_id):
        raise PilotPolicyError("invalid profile_id")
    ceiling = profile.get("data_classification_ceiling")
    if not isinstance(ceiling, str) or ceiling not in _PILOT_CEILINGS:
        raise PilotPolicyError(
            "data_classification_ceiling must be 無機密 or 營業秘密"
        )
    enabled = _unique_nonempty_strings(
        profile.get("enabled_callsites"), "enabled_callsites"
    )
    disabled = _unique_nonempty_strings(
        profile.get("disabled_callsites"), "disabled_callsites"
    )
    _unique_nonempty_strings(
        profile.get("disabled_capabilities"), "disabled_capabilities"
    )
    if not profile["pilot_enabled"]:
        return enabled, disabled
    _bounded_int(
        profile.get("revocation_sla_seconds"),
        "revocation_sla_seconds", minimum=1, maximum=86400,
    )
    _bounded_int(
        profile.get("lost_card_sla_seconds"),
        "lost_card_sla_seconds", minimum=1, maximum=86400,
    )
    _bounded_int(
        profile.get("retention_days"),
        "retention_days", minimum=1, maximum=3650,
    )
    withdrawal = profile.get("withdrawal_procedure")
    if (
        not isinstance(withdrawal, str)
        or not withdrawal.strip()
        or len(withdrawal) > 2000
    ):
        raise PilotPolicyError("withdrawal_procedure must be non-empty")
    collection_ids = profile.get("collection_ids")
    if not isinstance(collection_ids, list) or not collection_ids:
        raise PilotPolicyError("enabled pilot requires collection_ids")
    if any(
        isinstance(item, bool) or not isinstance(item, int) or item <= 0
        for item in collection_ids
    ) or len(collection_ids) != len(set(collection_ids)):
        raise PilotPolicyError("collection_ids must be unique positive integers")
    if len(collection_ids) > 1000:
        raise PilotPolicyError("collection_ids exceeds pilot scope limit")
    valid_from = _aware_rfc3339(profile.get("valid_from"), "valid_from")
    valid_until = _aware_rfc3339(profile.get("valid_until"), "valid_until")
    now = datetime.now(timezone.utc)
    if not valid_from <= now < valid_until:
        raise PilotPolicyError("pilot profile is not currently effective")
    if valid_until <= valid_from or (valid_until - valid_from).total_seconds() > 180 * 86400:
        raise PilotPolicyError("pilot validity interval is invalid or too long")
    return enabled, disabled


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def inventory_hash(inventory: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(inventory)).hexdigest()


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PilotPolicyError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PilotPolicyError(f"{path} root must be an object")
    return value


def _verify_signatures(profile: dict[str, Any], trust_store: dict[str, Any]) -> None:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise PilotPolicyError("cryptography is required for signed pilot verification") from exc
    unsigned = dict(profile)
    signatures = unsigned.pop("signatures", None)
    if not isinstance(signatures, list):
        raise PilotPolicyError("signatures must be a list")
    payload = _canonical(unsigned)
    seen: set[str] = set()
    key_fingerprints: set[str] = set()
    trusted = trust_store.get("trusted_signers")
    if not isinstance(trusted, dict):
        raise PilotPolicyError("trust store must contain trusted_signers")
    for entry in signatures:
        if not isinstance(entry, dict):
            raise PilotPolicyError("signature entry must be an object")
        role = entry.get("role")
        if role in seen or role not in REQUIRED_SIGNERS:
            raise PilotPolicyError(f"invalid or duplicate signer role: {role!r}")
        pem = trusted.get(role)
        if not isinstance(pem, str):
            raise PilotPolicyError(f"missing trusted key for {role}")
        try:
            key = serialization.load_pem_public_key(pem.encode("ascii"))
            if not isinstance(key, Ed25519PublicKey):
                raise TypeError("not Ed25519")
            fingerprint = hashlib.sha256(
                key.public_bytes(
                    serialization.Encoding.DER,
                    serialization.PublicFormat.SubjectPublicKeyInfo,
                )
            ).hexdigest()
            if fingerprint in key_fingerprints:
                raise TypeError("trusted signer roles must use distinct keys")
            key.verify(base64.b64decode(entry["signature"], validate=True), payload)
        except Exception as exc:
            raise PilotPolicyError(f"invalid signature for {role}: {exc}") from exc
        seen.add(role)
        key_fingerprints.add(fingerprint)
    if seen != REQUIRED_SIGNERS:
        raise PilotPolicyError(f"missing signer roles: {sorted(REQUIRED_SIGNERS - seen)}")


def verify(
    *, inventory_path: Path, profile_path: Path,
    trust_store_path: Path | None = None, allow_disabled_template: bool = False,
) -> dict[str, Any]:
    inventory = _load(inventory_path)
    profile = _load(profile_path)
    if inventory.get("schema_version") != "anila.gate2.inference-callsites.v1":
        raise PilotPolicyError("unknown inventory schema")
    if profile.get("schema_version") != "anila.gate2.signed-pilot.v1":
        raise PilotPolicyError("unknown pilot profile schema")
    calls = inventory.get("callsites")
    if not isinstance(calls, list) or not calls:
        raise PilotPolicyError("inventory callsites must be non-empty")
    by_id: dict[str, dict[str, Any]] = {}
    repo_root = inventory_path.parents[3]
    for call in calls:
        if (
            not isinstance(call, dict)
            or not isinstance(call.get("id"), str)
            or not call["id"].strip()
        ):
            raise PilotPolicyError("invalid callsite entry")
        if call["id"] in by_id:
            raise PilotPolicyError(f"duplicate callsite {call['id']}")
        source = repo_root / call.get("source", "")
        if not source.is_file():
            raise PilotPolicyError(f"callsite source missing: {call.get('source')}")
        by_id[call["id"]] = call
    enabled, disabled = _validate_profile_contract(profile)
    # The other three independent layers are repository-owned and cannot be
    # relaxed by a signed profile: registry denylist, browser capability
    # constants, and worker/formal-compose hard stops.
    denylist = _load(inventory_path.with_name("pilot-registry-denylist.v1.json"))
    denied = set(denylist.get("denied_callsites") or [])
    ineligible = {cid for cid, c in by_id.items() if not c.get("pilot_eligible")}
    if not ineligible <= denied:
        raise PilotPolicyError(
            f"registry denylist missing callsites: {sorted(ineligible - denied)}"
        )
    ui_source = (repo_root / "apps/anilalm/src/config/pilotCapabilities.ts").read_text(
        encoding="utf-8"
    )
    if "const pilotMode = import.meta.env.VITE_ANILA_PILOT_MODE === 'true'" not in ui_source:
        raise PilotPolicyError("UI pilot posture is not a build-time mode")
    for ui_key in (
        "studio", "artifact", "export", "flux", "promptGenerator",
        "relationLlm", "judge", "thirdPartyAgents",
    ):
        if f"{ui_key}: !pilotMode" not in ui_source:
            raise PilotPolicyError(f"UI capability is not pilot-gated: {ui_key}")
    compose = (repo_root / "infra/compose/platform.yml").read_text(encoding="utf-8")
    pilot_compose = (repo_root / "infra/compose/gate2-pilot.yml").read_text(
        encoding="utf-8"
    )
    if 'VITE_ANILA_PILOT_MODE: "false"' not in compose:
        raise PilotPolicyError("base UI posture no longer preserves non-pilot capabilities")
    if 'GATE2_ALLOW_UNCONVERGED_INFERENCE: "false"' not in pilot_compose:
        raise PilotPolicyError("worker unconverged-inference flag is not hard false")
    if 'ENABLE_PILOT_PROMPT_GENERATOR: "false"' not in pilot_compose:
        raise PilotPolicyError("CSP prompt-generator flag is not hard false")
    if 'ENABLE_IMAGE_CAPTIONS: "false"' not in pilot_compose:
        raise PilotPolicyError("ingestion VLM caption callsite is not hard false")
    for service_profile in (
        'profiles: ["gate2-data-admin"]',
        'profiles: ["gate3-artifacts"]',
    ):
        if service_profile not in pilot_compose:
            raise PilotPolicyError(f"pilot service profile missing: {service_profile}")
    if 'VITE_ANILA_PILOT_MODE: "true"' not in pilot_compose:
        raise PilotPolicyError("pilot UI image is not compiled in pilot mode")
    worker_settings = (
        repo_root / "services/ingestion-worker/src/ingestion_worker/settings.py"
    ).read_text(encoding="utf-8")
    if "gate2_allow_unconverged_inference: bool = Field(default=False)" not in worker_settings:
        raise PilotPolicyError("worker config default is not fail-closed")
    if profile.get("inventory_sha256") != inventory_hash(inventory):
        raise PilotPolicyError("profile inventory_sha256 mismatch")
    if set(enabled) & set(disabled) or set(enabled) | set(disabled) != set(by_id):
        raise PilotPolicyError("profile must partition every inventory callsite exactly once")
    for call_id in enabled:
        call = by_id[call_id]
        if not call.get("pilot_eligible") or call.get("mode") != "via_csp":
            raise PilotPolicyError(f"unconverged callsite cannot be enabled: {call_id}")
        controls = call.get("controls")
        required_controls = {
            "csp_mediated", "task", "classification_ceiling", "usage", "audit"
        }
        if not isinstance(controls, dict) or any(
            controls.get(name) is not True for name in required_controls
        ):
            raise PilotPolicyError(
                f"callsite lacks required runtime accounting controls: {call_id}"
            )
    if profile.get("pilot_enabled"):
        if not enabled:
            raise PilotPolicyError("enabled pilot has no inference callsites")
        if not REQUIRED_DISABLED <= set(profile.get("disabled_capabilities") or []):
            raise PilotPolicyError("Gate 3 capabilities must remain disabled")
        for field in (
            "revocation_sla_seconds", "lost_card_sla_seconds", "retention_days",
            "withdrawal_procedure",
        ):
            if profile.get(field) in (None, ""):
                raise PilotPolicyError(f"enabled pilot missing {field}")
        if profile.get("pki_stale_policy") != "fail_closed":
            raise PilotPolicyError("pilot PKI stale policy must fail closed")
        if trust_store_path is None:
            raise PilotPolicyError("enabled pilot requires an out-of-band trust store")
        _verify_signatures(profile, _load(trust_store_path))
    elif not allow_disabled_template:
        raise PilotPolicyError("disabled template is evidence only, not an approval")
    elif enabled or profile.get("signatures"):
        raise PilotPolicyError("disabled template cannot enable or carry signatures")
    return {"callsites": len(by_id), "enabled": len(enabled), "pilot_enabled": bool(profile.get("pilot_enabled"))}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--trust-store", type=Path)
    parser.add_argument("--allow-disabled-template", action="store_true")
    args = parser.parse_args()
    try:
        result = verify(
            inventory_path=args.inventory, profile_path=args.profile,
            trust_store_path=args.trust_store,
            allow_disabled_template=args.allow_disabled_template,
        )
    except PilotPolicyError as exc:
        print(f"FAIL: {exc}")
        return 1
    print(f"PASS: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
