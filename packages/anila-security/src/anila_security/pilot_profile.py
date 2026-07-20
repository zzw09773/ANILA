"""Cryptographic verification for Gate 2 machine-readable pilot profiles."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

REQUIRED_PILOT_SIGNERS = frozenset(
    {"system_owner", "data_owner", "pki_owner", "security"}
)
REQUIRED_DISABLED_CAPABILITIES = frozenset(
    {"studio", "artifact", "export", "flux", "prompt_generator",
     "relation_llm", "judge", "third_party_agents"}
)


class PilotProfileError(ValueError):
    """The profile is incomplete, unsigned, stale relative to inventory, or unsafe."""


_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")
_IMAGE_CONTENT_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_PILOT_CEILINGS = frozenset({"無機密", "營業秘密"})
_TARGET_CEILINGS = frozenset({"無機密", "營業秘密", "機密", "極機密"})
_TARGET_TYPES_BY_CALLSITE = {
    "csp.chat_model": frozenset({"llm"}),
    "csp.server_retrieval_embedding": frozenset({"embedding"}),
    "csp.memory_extract": frozenset({"llm"}),
    "csp.memory_embedding": frozenset({"embedding"}),
}


@dataclass(frozen=True, slots=True)
class PilotTarget:
    """One exact registry target authorized by all pilot signers."""

    callsite: str
    name: str
    model_type: str
    endpoint_url: str
    classification_ceiling: str


@dataclass(frozen=True, slots=True)
class VerifiedPilotAdmission:
    """Immutable runtime authority extracted from a verified signed profile."""

    profile_id: str
    enabled_callsites: frozenset[str]
    allowed_targets: tuple[PilotTarget, ...]
    collection_ids: frozenset[int]
    data_classification_ceiling: str
    valid_from: datetime
    valid_until: datetime

    def target_allowed(
        self,
        *,
        callsite: str,
        name: str,
        model_type: str,
        endpoint_url: str,
        classification_ceiling: str,
    ) -> bool:
        return PilotTarget(
            callsite=callsite,
            name=name,
            model_type=model_type,
            endpoint_url=endpoint_url,
            classification_ceiling=classification_ceiling,
        ) in self.allowed_targets


def _unique_nonempty_strings(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise PilotProfileError(f"{field} must be a list")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise PilotProfileError(f"{field} must contain non-empty strings")
    if len(value) != len(set(value)):
        raise PilotProfileError(f"{field} contains duplicates")
    return value


def _bounded_int(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PilotProfileError(f"{field} must be an integer")
    if not minimum <= value <= maximum:
        raise PilotProfileError(f"{field} outside allowed range")
    return value


def _aware_rfc3339(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise PilotProfileError(f"{field} must be RFC3339")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PilotProfileError(f"{field} must be RFC3339") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PilotProfileError(f"{field} must include timezone")
    return parsed.astimezone(timezone.utc)


def _validate_target_url(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise PilotProfileError("allowed target endpoint_url must be a canonical URL")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise PilotProfileError("allowed target endpoint_url must be an absolute base URL")
    return value


def _validate_allowed_targets(
    value: Any, *, enabled_callsites: set[str], pilot_enabled: bool,
) -> tuple[PilotTarget, ...]:
    if not isinstance(value, list):
        raise PilotProfileError("allowed_targets must be a list")
    if not pilot_enabled:
        if value:
            raise PilotProfileError("disabled template cannot authorize targets")
        return ()
    targets: list[PilotTarget] = []
    target_keys: set[tuple[str, str]] = set()
    required_keys = {
        "callsite", "name", "model_type", "endpoint_url",
        "classification_ceiling",
    }
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != required_keys:
            raise PilotProfileError("allowed target has unknown or missing fields")
        callsite = raw.get("callsite")
        name = raw.get("name")
        model_type = raw.get("model_type")
        ceiling = raw.get("classification_ceiling")
        if callsite not in enabled_callsites:
            raise PilotProfileError("allowed target callsite is not enabled")
        if not isinstance(name, str) or not name or name != name.strip() or len(name) > 200:
            raise PilotProfileError("allowed target name is invalid")
        if not isinstance(model_type, str) or not model_type.strip():
            raise PilotProfileError("allowed target model_type is invalid")
        allowed_types = _TARGET_TYPES_BY_CALLSITE.get(callsite)
        if allowed_types is not None and model_type not in allowed_types:
            raise PilotProfileError("allowed target model_type does not match callsite")
        if ceiling not in _TARGET_CEILINGS or not isinstance(ceiling, str):
            raise PilotProfileError("allowed target classification_ceiling is invalid")
        key = (callsite, name)
        if key in target_keys:
            raise PilotProfileError("allowed target callsite/name is duplicated")
        target_keys.add(key)
        targets.append(PilotTarget(
            callsite=callsite,
            name=name,
            model_type=model_type,
            endpoint_url=_validate_target_url(raw.get("endpoint_url")),
            classification_ceiling=ceiling,
        ))
    missing = enabled_callsites - {target.callsite for target in targets}
    if missing:
        raise PilotProfileError(
            f"enabled callsites missing exact allowed targets: {sorted(missing)}"
        )
    return tuple(targets)


def _validate_profile_contract(
    profile: dict[str, Any],
) -> tuple[list[str], list[str], tuple[PilotTarget, ...], datetime | None, datetime | None]:
    if not isinstance(profile.get("pilot_enabled"), bool):
        raise PilotProfileError("pilot_enabled must be boolean")
    profile_id = profile.get("profile_id")
    if not isinstance(profile_id, str) or not _PROFILE_ID_RE.fullmatch(profile_id):
        raise PilotProfileError("invalid profile_id")
    ceiling = profile.get("data_classification_ceiling")
    if ceiling not in _PILOT_CEILINGS or not isinstance(ceiling, str):
        raise PilotProfileError(
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
    artifacts = profile.get("deployment_artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != {"csp_image_id"}:
        raise PilotProfileError(
            "deployment_artifacts must contain only csp_image_id"
        )
    if not profile["pilot_enabled"]:
        if artifacts["csp_image_id"] is not None:
            raise PilotProfileError("disabled template cannot bind a CSP image")
        targets = _validate_allowed_targets(
            profile.get("allowed_targets"),
            enabled_callsites=set(enabled),
            pilot_enabled=False,
        )
        return enabled, disabled, targets, None, None

    csp_image_id = artifacts["csp_image_id"]
    if not isinstance(csp_image_id, str) or not _IMAGE_CONTENT_ID_RE.fullmatch(
        csp_image_id
    ):
        raise PilotProfileError(
            "enabled pilot requires a sha256 CSP image content ID"
        )

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
        raise PilotProfileError("withdrawal_procedure must be non-empty")
    collection_ids = profile.get("collection_ids")
    if not isinstance(collection_ids, list) or not collection_ids:
        raise PilotProfileError("enabled pilot requires collection_ids")
    if any(
        isinstance(item, bool) or not isinstance(item, int) or item <= 0
        for item in collection_ids
    ) or len(collection_ids) != len(set(collection_ids)):
        raise PilotProfileError("collection_ids must be unique positive integers")
    if len(collection_ids) > 1000:
        raise PilotProfileError("collection_ids exceeds pilot scope limit")
    valid_from = _aware_rfc3339(profile.get("valid_from"), "valid_from")
    valid_until = _aware_rfc3339(profile.get("valid_until"), "valid_until")
    now = datetime.now(timezone.utc)
    if not valid_from <= now < valid_until:
        raise PilotProfileError("pilot profile is not currently effective")
    if (
        valid_until <= valid_from
        or (valid_until - valid_from).total_seconds() > 180 * 86400
    ):
        raise PilotProfileError("pilot validity interval is invalid or too long")
    targets = _validate_allowed_targets(
        profile.get("allowed_targets"),
        enabled_callsites=set(enabled),
        pilot_enabled=True,
    )
    return enabled, disabled, targets, valid_from, valid_until


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _read(path: str | Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PilotProfileError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise PilotProfileError(f"{path} root must be an object")
    return value


def verify_signed_pilot_profile(
    *, profile_path: str | Path, inventory_path: str | Path,
    trust_store_path: str | Path, expected_csp_image_id: str,
) -> VerifiedPilotAdmission:
    """Verify the profile and return its complete immutable runtime authority.

    Trust anchors come only from ``trust_store_path``. Public keys embedded in
    a profile, if any, have no authority.
    """
    profile = _read(profile_path)
    inventory = _read(inventory_path)
    trust = _read(trust_store_path)
    # v3 adds signer-bound executable identity.  v1/v2 profiles did not bind
    # the CSP image content ID and therefore cannot be upgraded implicitly.
    if profile.get("schema_version") != "anila.gate2.signed-pilot.v3":
        raise PilotProfileError("unknown profile schema")
    if inventory.get("schema_version") != "anila.gate2.inference-callsites.v1":
        raise PilotProfileError("unknown inventory schema")
    digest = hashlib.sha256(_canonical(inventory)).hexdigest()
    if profile.get("inventory_sha256") != digest:
        raise PilotProfileError("pilot profile inventory hash mismatch")
    calls = inventory.get("callsites")
    if not isinstance(calls, list) or not calls:
        raise PilotProfileError("empty inference inventory")
    call_ids = [entry.get("id") for entry in calls if isinstance(entry, dict)]
    if (
        len(call_ids) != len(calls)
        or any(not isinstance(item, str) or not item.strip() for item in call_ids)
        or len(call_ids) != len(set(call_ids))
    ):
        raise PilotProfileError("invalid or duplicate callsite inventory")
    by_id = {entry["id"]: entry for entry in calls}
    enabled, disabled, targets, valid_from, valid_until = _validate_profile_contract(profile)
    if not _IMAGE_CONTENT_ID_RE.fullmatch(expected_csp_image_id):
        raise PilotProfileError("runtime CSP image content ID is missing or invalid")
    if profile["deployment_artifacts"]["csp_image_id"] != expected_csp_image_id:
        raise PilotProfileError("pilot profile CSP image content ID mismatch")
    if set(enabled) & set(disabled) or set(enabled) | set(disabled) != set(by_id):
        raise PilotProfileError("profile must partition every inventory callsite")
    if not profile.get("pilot_enabled") or not enabled:
        raise PilotProfileError("disabled template is not a pilot approval")
    for call_id in enabled:
        entry = by_id[call_id]
        if not entry.get("pilot_eligible") or entry.get("mode") != "via_csp":
            raise PilotProfileError(f"unconverged callsite enabled: {call_id}")
        controls = entry.get("controls")
        if not isinstance(controls, dict) or any(
            controls.get(name) is not True
            for name in (
                "csp_mediated", "task", "classification_ceiling", "usage", "audit"
            )
        ):
            raise PilotProfileError(f"incomplete callsite controls: {call_id}")
    if not REQUIRED_DISABLED_CAPABILITIES <= set(
        profile.get("disabled_capabilities") or []
    ):
        raise PilotProfileError("Gate 3/unconverged capabilities are not all disabled")
    for field in (
        "revocation_sla_seconds", "lost_card_sla_seconds", "retention_days",
        "withdrawal_procedure",
    ):
        if profile.get(field) in (None, ""):
            raise PilotProfileError(f"missing required pilot field: {field}")
    if profile.get("pki_stale_policy") != "fail_closed":
        raise PilotProfileError("PKI stale policy must fail closed")

    unsigned = dict(profile)
    signatures = unsigned.pop("signatures", None)
    trusted = trust.get("trusted_signers")
    if not isinstance(signatures, list) or not isinstance(trusted, dict):
        raise PilotProfileError("signature list or trusted_signers missing")
    payload = _canonical(unsigned)
    seen: set[str] = set()
    key_fingerprints: set[str] = set()
    for entry in signatures:
        if not isinstance(entry, dict):
            raise PilotProfileError("invalid signature entry")
        role = entry.get("role")
        if role not in REQUIRED_PILOT_SIGNERS or role in seen:
            raise PilotProfileError(f"invalid/duplicate signer role: {role!r}")
        pem = trusted.get(role)
        if not isinstance(pem, str):
            raise PilotProfileError(f"missing trusted key for {role}")
        try:
            key = serialization.load_pem_public_key(pem.encode("ascii"))
            if not isinstance(key, Ed25519PublicKey):
                raise TypeError("trusted key is not Ed25519")
            fingerprint = hashlib.sha256(
                key.public_bytes(
                    serialization.Encoding.DER,
                    serialization.PublicFormat.SubjectPublicKeyInfo,
                )
            ).hexdigest()
            if fingerprint in key_fingerprints:
                raise TypeError("trusted signer roles must use distinct keys")
            key.verify(
                base64.b64decode(entry["signature"], validate=True), payload
            )
        except Exception as exc:
            raise PilotProfileError(f"invalid signature for {role}: {exc}") from exc
        seen.add(role)
        key_fingerprints.add(fingerprint)
    if seen != set(REQUIRED_PILOT_SIGNERS):
        raise PilotProfileError(
            f"missing signer roles: {sorted(REQUIRED_PILOT_SIGNERS - seen)}"
        )
    if valid_from is None or valid_until is None:  # guarded by pilot_enabled above
        raise PilotProfileError("enabled pilot validity interval is missing")
    return VerifiedPilotAdmission(
        profile_id=str(profile["profile_id"]),
        enabled_callsites=frozenset(enabled),
        allowed_targets=targets,
        collection_ids=frozenset(int(item) for item in profile["collection_ids"]),
        data_classification_ceiling=str(profile["data_classification_ceiling"]),
        valid_from=valid_from,
        valid_until=valid_until,
    )
