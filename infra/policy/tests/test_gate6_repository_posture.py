from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
P0_TEMPLATE = ROOT / "policy/gate6/production-acceptance-profile.disabled-template.json"

from infra.ci.check_gate6_repository_posture import (  # noqa: E402
    RepositoryPostureError,
    _assert_disabled_p0_template,
    _assert_no_production_evidence,
    _read_json,
    check_repository_posture,
)


def test_current_tree_confirms_no_approval_and_non_acceptance_p9() -> None:
    result = check_repository_posture(ROOT.parent)

    assert result["p0_approval"] is False
    assert result["p9"]["status"] == "NOT_ACCEPTANCE"
    assert result["p9"]["environment"] == "non-production"
    assert result["p9"]["gate6_pass"] is False
    assert result["p9"]["enabled_callsites"] == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("enabled", True),
        ("template_only", False),
        ("approval_status", "approved"),
        ("signatures", [{"role": "security", "signature": "fake"}]),
        ("profile_content_sha256", "a" * 64),
    ],
)
def test_p0_template_mutations_fail_closed(
    tmp_path: Path, field: str, value: object
) -> None:
    profile = json.loads(P0_TEMPLATE.read_text(encoding="utf-8"))
    profile[field] = value
    path = tmp_path / "production-acceptance-profile.json"
    path.write_text(json.dumps(profile), encoding="utf-8")

    with pytest.raises(RepositoryPostureError):
        _assert_disabled_p0_template(path)


def test_gate6_trust_store_filename_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    material = root / "infra/policy/gate6"
    material.mkdir(parents=True)
    p0 = material / "production-acceptance-profile.disabled-template.json"
    p0.write_text("{}", encoding="utf-8")
    (material / "production-acceptance-trust-store.json").write_text(
        json.dumps({"trusted_signers": {"security": "PEM"}}), encoding="utf-8"
    )

    with pytest.raises(RepositoryPostureError, match="trust/signature material"):
        _assert_no_production_evidence(root, p0)


@pytest.mark.parametrize(
    "filename",
    [
        "private.txt",
        "evidence.txt",
        "trusted_signers.txt",
        "signatures.txt",
        "keypair.txt",
        "private key.txt",
        "production evidence.txt",
    ],
)
def test_gate6_parent_and_non_json_credential_filename_are_rejected(
    tmp_path: Path, filename: str
) -> None:
    root = tmp_path / "repo"
    material = root / "infra/policy/gate6"
    material.mkdir(parents=True)
    p0 = material / "production-acceptance-profile.disabled-template.json"
    p0.write_text("{}", encoding="utf-8")
    (material / filename).write_text("not JSON", encoding="utf-8")

    with pytest.raises(RepositoryPostureError, match="trust/signature material"):
        _assert_no_production_evidence(root, p0)


@pytest.mark.parametrize(
    "filename",
    [
        "gate6-privateKey.json",
        "gate6-trustStore.json",
        "gate6-signed-profile.yaml",
        "gate6-TRUSTStore.yaml",
    ],
)
def test_gate6_compound_and_acronym_credential_markers_are_rejected(
    tmp_path: Path, filename: str
) -> None:
    root = tmp_path / "repo"
    material = root / "infra/policy/gate6"
    material.mkdir(parents=True)
    p0 = material / "production-acceptance-profile.disabled-template.json"
    p0.write_text("{}", encoding="utf-8")
    candidate = material / filename
    payload = json.dumps({"blob": "bm90LWEtcGVt"}) if candidate.suffix == ".json" else "opaque"
    candidate.write_text(payload, encoding="utf-8")

    with pytest.raises(RepositoryPostureError, match="trust/signature material"):
        _assert_no_production_evidence(root, p0)


@pytest.mark.parametrize(
    "filename",
    ["operator-notes.txt", "design-notes.txt", "monkey-compatibility.md"],
)
def test_unrelated_non_json_file_is_not_rejected(
    tmp_path: Path, filename: str
) -> None:
    root = tmp_path / "repo"
    material = root / "infra/policy/gate6"
    material.mkdir(parents=True)
    p0 = material / "production-acceptance-profile.disabled-template.json"
    p0.write_text("{}", encoding="utf-8")
    (material / filename).write_text("not JSON", encoding="utf-8")

    assert _assert_no_production_evidence(root, p0) == []


def test_gate6_enabled_json_evidence_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    material = root / "evidence"
    material.mkdir(parents=True)
    p0 = root / "infra/policy/gate6/production-acceptance-profile.disabled-template.json"
    p0.parent.mkdir(parents=True)
    p0.write_text("{}", encoding="utf-8")
    (material / "bundle.json").write_text(
        json.dumps(
            {
                "schema_version": "anila.gate6.production-acceptance.v1",
                "enabled": True,
                "template_only": False,
                "approval_status": "approved",
                "signatures": [],
                "profile_content_sha256": "a" * 64,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RepositoryPostureError, match="acceptance evidence"):
        _assert_no_production_evidence(root, p0)


def test_posture_json_loader_rejects_duplicate_keys_and_bom(tmp_path: Path) -> None:
    path = tmp_path / "material.json"
    path.write_text('{"trusted_signers": {}, "trusted_signers": {}}', encoding="utf-8")
    with pytest.raises(RepositoryPostureError, match="duplicate JSON key"):
        _read_json(path)

    path.write_bytes(b"\xef\xbb\xbf{}")
    with pytest.raises(RepositoryPostureError, match="UTF-8 BOM"):
        _read_json(path)


def test_gate6_nested_trust_store_and_pem_markers_are_rejected(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    material = root / "infra/policy/gate6"
    material.mkdir(parents=True)
    p0 = material / "production-acceptance-profile.disabled-template.json"
    p0.write_text("{}", encoding="utf-8")

    nested = root / "evidence/bundle.json"
    nested.parent.mkdir(parents=True)
    nested.write_text(
        json.dumps({"metadata": {"trusted_signers": {"security": "PEM"}}}),
        encoding="utf-8",
    )
    with pytest.raises(RepositoryPostureError, match="trust-store material"):
        _assert_no_production_evidence(root, p0)

    nested.write_text(
        json.dumps({"metadata": {"certificate": "-----BEGIN CERTIFICATE-----"}}),
        encoding="utf-8",
    )
    with pytest.raises(RepositoryPostureError, match="PEM key/certificate"):
        _assert_no_production_evidence(root, p0)
