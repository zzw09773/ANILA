from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
P0_TEMPLATE = ROOT / "policy/gate6/production-acceptance-profile.disabled-template.json"

from infra.ci.check_gate6_repository_posture import (  # noqa: E402
    RepositoryPostureError,
    _assert_disabled_p0_template,
    _assert_no_production_evidence,
    _is_gate6_scoped_path,
    _read_json,
    check_repository_posture,
)


def _pem_marker(label: str) -> str:
    """Build hostile fixture content without checking a PEM marker into this test."""

    return "-----BEGIN " + label + "-----"


def _init_git_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", str(root)],
        check=True,
        capture_output=True,
        text=True,
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


@pytest.mark.parametrize(
    ("nested", "message"),
    [
        ({"trusted_signers": {"security": "opaque"}}, "trust-store material"),
        ({"certificate": _pem_marker("RSA PRIVATE KEY")}, "PEM key/certificate"),
    ],
)
def test_p0_nested_sensitive_material_is_rejected_by_both_posture_paths(
    tmp_path: Path, nested: dict[str, object], message: str
) -> None:
    root = tmp_path / "repo"
    p0 = root / "infra/policy/gate6/production-acceptance-profile.disabled-template.json"
    p0.parent.mkdir(parents=True)
    profile = json.loads(P0_TEMPLATE.read_text(encoding="utf-8"))
    profile["production_topology"] = {"metadata": nested}
    p0.write_text(json.dumps(profile), encoding="utf-8")

    with pytest.raises(RepositoryPostureError, match=message):
        _assert_disabled_p0_template(p0)
    with pytest.raises(RepositoryPostureError, match=message):
        _assert_no_production_evidence(root, p0)


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


def test_gate6_scoped_text_pem_marker_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    material = root / "infra/policy/gate6"
    material.mkdir(parents=True)
    p0 = material / "production-acceptance-profile.disabled-template.json"
    p0.write_text("{}", encoding="utf-8")
    (material / "operator-notes.txt").write_text(
        _pem_marker("RSA PRIVATE KEY") + "\n", encoding="utf-8"
    )

    with pytest.raises(RepositoryPostureError, match="PEM key/certificate"):
        _assert_no_production_evidence(root, p0)


def test_gate6_scoped_text_without_sensitive_material_is_allowed(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    material = root / "infra/policy/gate6"
    material.mkdir(parents=True)
    p0 = material / "production-acceptance-profile.disabled-template.json"
    p0.write_text("{}", encoding="utf-8")
    (material / "operator-notes.txt").write_text(
        "The disabled template is not approval evidence.\n", encoding="utf-8"
    )

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


@pytest.mark.parametrize(
    ("relative", "payload"),
    [
        (
            "evidence/array.json",
            [
                {
                    "schema_version": "anila.gate6.production-acceptance.v1",
                    "enabled": True,
                    "template_only": False,
                    "approval_status": "approved",
                    "signatures": [],
                    "profile_content_sha256": "a" * 64,
                }
            ],
        ),
        (
            "evidence/nested-bundle.json",
            {
                "bundle": {
                    "profiles": [
                        {
                            "schema_version": "anila.gate6.production-acceptance.v1",
                            "enabled": True,
                            "template_only": False,
                            "approval_status": "approved",
                            "signatures": [],
                            "profile_content_sha256": "a" * 64,
                        }
                    ]
                }
            },
        ),
    ],
)
def test_gate6_profile_at_any_json_depth_is_rejected(
    tmp_path: Path, relative: str, payload: object
) -> None:
    root = tmp_path / "repo"
    p0 = root / "infra/policy/gate6/production-acceptance-profile.disabled-template.json"
    p0.parent.mkdir(parents=True)
    p0.write_text("{}", encoding="utf-8")
    evidence = root / relative
    evidence.parent.mkdir(parents=True)
    evidence.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RepositoryPostureError, match="acceptance evidence"):
        _assert_no_production_evidence(root, p0)


@pytest.mark.parametrize(
    ("relative", "content"),
    [
        (
            "evidence/p9-root.json",
            json.dumps(
                {
                    "schema_version": (
                        "anila.gate6.p9.enabled-inference-callsite-evidence.v1"
                    ),
                    "status": "VERIFIED",
                    "environment": "production",
                }
            ),
        ),
        (
            "evidence/p9-array.json",
            json.dumps(
                [
                    {
                        "schema_version": (
                            "anila.gate6.p9.enabled-inference-callsite-evidence.v1"
                        )
                    }
                ]
            ),
        ),
        (
            "evidence/p9-nested.json",
            json.dumps(
                {
                    "bundle": {
                        "records": [
                            {
                                "schema_version": (
                                    "anila.gate6.p9.enabled-inference-callsite-evidence.v1"
                                )
                            }
                        ]
                    }
                }
            ),
        ),
        (
            "evidence/p9-records.jsonl",
            "\n".join(
                [
                    json.dumps({"kind": "metadata"}),
                    json.dumps(
                        {
                            "schema_version": (
                                "anila.gate6.p9.enabled-inference-callsite-evidence.v1"
                            )
                        }
                    ),
                ]
            )
            + "\n",
        ),
    ],
)
def test_p9_evidence_at_any_json_depth_is_rejected(
    tmp_path: Path, relative: str, content: str
) -> None:
    root = tmp_path / "repo"
    p0 = root / "infra/policy/gate6/production-acceptance-profile.disabled-template.json"
    p0.parent.mkdir(parents=True)
    p0.write_text("{}", encoding="utf-8")
    evidence = root / relative
    evidence.parent.mkdir(parents=True)
    evidence.write_text(content, encoding="utf-8")

    with pytest.raises(RepositoryPostureError, match="P9 evidence"):
        _assert_no_production_evidence(root, p0)


@pytest.mark.parametrize(
    ("relative", "content", "message"),
    [
        ("build/gate6/private.pem", _pem_marker("PRIVATE KEY"), "trust/signature"),
        (
            "node_modules/cache/p9-evidence.json",
            json.dumps(
                {
                    "schema_version": (
                        "anila.gate6.p9.enabled-inference-callsite-evidence.v1"
                    ),
                    "status": "VERIFIED",
                    "environment": "production",
                }
            ),
            "P9 evidence",
        ),
    ],
)
def test_git_tracked_material_in_generated_directories_is_rejected(
    tmp_path: Path, relative: str, content: str, message: str
) -> None:
    root = tmp_path / "repo"
    _init_git_repo(root)
    p0 = root / "infra/policy/gate6/production-acceptance-profile.disabled-template.json"
    p0.parent.mkdir(parents=True)
    p0.write_text("{}", encoding="utf-8")
    candidate = root / relative
    candidate.parent.mkdir(parents=True)
    candidate.write_text(content, encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(root), "add", "-f", relative],
        check=True,
        capture_output=True,
        text=True,
    )

    with pytest.raises(RepositoryPostureError, match=message):
        _assert_no_production_evidence(root, p0)


def test_git_untracked_non_generated_p9_evidence_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _init_git_repo(root)
    p0 = root / "infra/policy/gate6/production-acceptance-profile.disabled-template.json"
    p0.parent.mkdir(parents=True)
    p0.write_text("{}", encoding="utf-8")
    evidence = root / "docs/p9-evidence.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text(
        json.dumps(
            {
                "schema_version": (
                    "anila.gate6.p9.enabled-inference-callsite-evidence.v1"
                )
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RepositoryPostureError, match="P9 evidence"):
        _assert_no_production_evidence(root, p0)


def test_git_untracked_generated_directory_is_not_recursively_scanned(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    _init_git_repo(root)
    p0 = root / "infra/policy/gate6/production-acceptance-profile.disabled-template.json"
    p0.parent.mkdir(parents=True)
    p0.write_text("{}", encoding="utf-8")
    generated = root / "node_modules/gate6/operator-notes.bin"
    generated.parent.mkdir(parents=True)
    generated.write_bytes(b"x" * (1024 * 1024 + 1))

    assert _assert_no_production_evidence(root, p0) == []


def test_gate6_nested_profile_in_disabled_template_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    p0 = root / "infra/policy/gate6/production-acceptance-profile.disabled-template.json"
    p0.parent.mkdir(parents=True)
    template = json.loads(P0_TEMPLATE.read_text(encoding="utf-8"))
    template["nested_bundle"] = [
        {
            "schema_version": "anila.gate6.production-acceptance.v1",
            "enabled": True,
            "template_only": False,
            "approval_status": "approved",
            "signatures": [],
            "profile_content_sha256": "a" * 64,
        }
    ]
    p0.write_text(json.dumps(template), encoding="utf-8")

    with pytest.raises(RepositoryPostureError, match="acceptance evidence"):
        _assert_no_production_evidence(root, p0)


def test_nested_disabled_profile_with_production_values_is_rejected(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    p0 = root / "infra/policy/gate6/production-acceptance-profile.disabled-template.json"
    p0.parent.mkdir(parents=True)
    p0.write_text("{}", encoding="utf-8")
    nested_profile = json.loads(P0_TEMPLATE.read_text(encoding="utf-8"))
    nested_profile["production_topology"] = {"topology_id": "actual-production"}
    nested_profile["rto_rpo"] = {"rto_seconds": 60, "rpo_seconds": 0}
    bundle = root / "evidence/nested-disabled-bundle.json"
    bundle.parent.mkdir(parents=True)
    bundle.write_text(
        json.dumps({"metadata": {"profiles": [nested_profile]}}),
        encoding="utf-8",
    )

    with pytest.raises(RepositoryPostureError, match="acceptance evidence"):
        _assert_no_production_evidence(root, p0)


@pytest.mark.parametrize(
    "filename",
    [
        "2026-07-16-gate6-operator-notes.txt",
        "2026-07-16-production-acceptance-operator-notes.txt",
    ],
)
def test_prefixed_gate6_handoff_text_pem_marker_is_rejected(
    tmp_path: Path, filename: str
) -> None:
    root = tmp_path / "repo"
    p0 = root / "infra/policy/gate6/production-acceptance-profile.disabled-template.json"
    p0.parent.mkdir(parents=True)
    p0.write_text("{}", encoding="utf-8")
    handoff = root / "docs/handoffs" / filename
    handoff.parent.mkdir(parents=True)
    handoff.write_text(_pem_marker("RSA PRIVATE KEY") + "\n", encoding="utf-8")

    with pytest.raises(RepositoryPostureError, match="PEM key/certificate"):
        _assert_no_production_evidence(root, p0)


def test_pem_marker_in_nested_json_member_name_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    p0 = root / "infra/policy/gate6/production-acceptance-profile.disabled-template.json"
    p0.parent.mkdir(parents=True)
    p0.write_text("{}", encoding="utf-8")
    bundle = root / "evidence/bundle.json"
    bundle.parent.mkdir(parents=True)
    bundle.write_text(
        json.dumps({"metadata": [{_pem_marker("PRIVATE KEY"): "opaque"}]}),
        encoding="utf-8",
    )

    with pytest.raises(RepositoryPostureError, match="PEM key/certificate"):
        _assert_no_production_evidence(root, p0)


def test_posture_json_loader_rejects_duplicate_keys_and_bom(tmp_path: Path) -> None:
    path = tmp_path / "material.json"
    path.write_text('{"trusted_signers": {}, "trusted_signers": {}}', encoding="utf-8")
    with pytest.raises(RepositoryPostureError, match="duplicate JSON key"):
        _read_json(path)

    path.write_bytes(b"\xef\xbb\xbf{}")
    with pytest.raises(RepositoryPostureError, match="UTF-8 BOM"):
        _read_json(path)


@pytest.mark.parametrize("suffix", [".json", ".jsonl"])
@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_non_standard_json_constants_fail_closed(
    tmp_path: Path, suffix: str, constant: str
) -> None:
    root = tmp_path / "repo"
    p0 = root / "infra/policy/gate6/production-acceptance-profile.disabled-template.json"
    p0.parent.mkdir(parents=True)
    p0.write_text("{}", encoding="utf-8")
    bundle = root / "evidence" / f"bundle{suffix}"
    bundle.parent.mkdir(parents=True)
    payload = f'{{"value": {constant}}}'
    bundle.write_text(payload if suffix == ".json" else payload + "\n", encoding="utf-8")

    with pytest.raises(RepositoryPostureError, match="invalid JSON material"):
        _assert_no_production_evidence(root, p0)


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
        json.dumps({"metadata": {"certificate": _pem_marker("CERTIFICATE")}}),
        encoding="utf-8",
    )
    with pytest.raises(RepositoryPostureError, match="PEM key/certificate"):
        _assert_no_production_evidence(root, p0)


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (
            {"certificate": _pem_marker("PRIVATE KEY")},
            "PEM key/certificate",
        ),
        ({"metadata": {"trusted_signers": {"security": "opaque"}}}, "trust-store"),
    ],
)
def test_jsonl_sensitive_records_are_rejected(
    tmp_path: Path, record: dict[str, object], message: str
) -> None:
    root = tmp_path / "repo"
    p0 = root / "infra/policy/gate6/production-acceptance-profile.disabled-template.json"
    p0.parent.mkdir(parents=True)
    p0.write_text("{}", encoding="utf-8")
    bundle = root / "evidence/bundle.jsonl"
    bundle.parent.mkdir(parents=True)
    bundle.write_text(
        json.dumps({"kind": "metadata"}) + "\n" + json.dumps(record) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RepositoryPostureError, match=message):
        _assert_no_production_evidence(root, p0)


def test_json_array_root_is_scanned_for_pem_markers(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    p0 = root / "infra/policy/gate6/production-acceptance-profile.disabled-template.json"
    p0.parent.mkdir(parents=True)
    p0.write_text("{}", encoding="utf-8")
    bundle = root / "evidence/bundle.json"
    bundle.parent.mkdir(parents=True)
    bundle.write_text(
        json.dumps([{"metadata": _pem_marker("PRIVATE KEY")}]),
        encoding="utf-8",
    )

    with pytest.raises(RepositoryPostureError, match="PEM key/certificate"):
        _assert_no_production_evidence(root, p0)


@pytest.mark.parametrize(
    ("filename", "raw", "message"),
    [
        ("bundle.json", b"\xef\xbb\xbf{}", "UTF-8 BOM"),
        ("bundle.json", b'{"metadata":', "invalid JSON material"),
        ("bundle.json", b'{"metadata": "ok"}\xff', "invalid JSON material"),
        (
            "bundle.jsonl",
            b'{"metadata": 1}\n{"metadata": 1, "metadata": 2}\n',
            "duplicate JSON key",
        ),
        ("bundle.jsonl", b'{"metadata": 1}\n\n', "invalid JSONL material"),
    ],
)
def test_invalid_json_like_material_fails_closed(
    tmp_path: Path, filename: str, raw: bytes, message: str
) -> None:
    root = tmp_path / "repo"
    p0 = root / "infra/policy/gate6/production-acceptance-profile.disabled-template.json"
    p0.parent.mkdir(parents=True)
    p0.write_text("{}", encoding="utf-8")
    bundle = root / "evidence" / filename
    bundle.parent.mkdir(parents=True)
    bundle.write_bytes(raw)

    with pytest.raises(RepositoryPostureError, match=message):
        _assert_no_production_evidence(root, p0)


def test_jsonl_without_sensitive_material_is_allowed(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    p0 = root / "infra/policy/gate6/production-acceptance-profile.disabled-template.json"
    p0.parent.mkdir(parents=True)
    p0.write_text("{}", encoding="utf-8")
    bundle = root / "evidence/bundle.jsonl"
    bundle.parent.mkdir(parents=True)
    bundle.write_text(
        "\n".join(
            [
                json.dumps({"kind": "metadata", "value": "safe"}),
                json.dumps(["safe", {"nested": True}]),
                json.dumps("safe scalar"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert _assert_no_production_evidence(root, p0) == []


@pytest.mark.parametrize(
    "relative",
    [
        "gate-6/private.pem",
        "gate_6/signing-key.txt",
        "Gate 6/private-key.pem",
        "docs/gate6PrivateKey.pem",
        "infra/policy/gate/6/private.pem",
        "docs/Gate\u00a06/private.pem",
        "production acceptance/private.pem",
    ],
)
def test_gate6_separator_variants_are_scoped_for_material_checks(
    tmp_path: Path, relative: str
) -> None:
    root = tmp_path / "repo"
    p0 = root / "infra/policy/gate6/production-acceptance-profile.disabled-template.json"
    p0.parent.mkdir(parents=True)
    p0.write_text("{}", encoding="utf-8")
    material = root / relative
    material.parent.mkdir(parents=True)
    material.write_text(_pem_marker("PRIVATE KEY"), encoding="utf-8")

    with pytest.raises(RepositoryPostureError):
        _assert_no_production_evidence(root, p0)


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        ("docs/productionacceptance/report.json", True),
        ("docs/notproductionacceptancex/report.json", False),
    ],
)
def test_gate6_compound_scope_token_is_exact(relative: str, expected: bool) -> None:
    assert _is_gate6_scoped_path(relative) is expected


def test_unscoped_malformed_json_without_credential_marker_is_skipped(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    p0 = root / "infra/policy/gate6/production-acceptance-profile.disabled-template.json"
    p0.parent.mkdir(parents=True)
    p0.write_text("{}", encoding="utf-8")
    unrelated = root / "vendor/package-metadata.json"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text('{"version":', encoding="utf-8")

    assert _assert_no_production_evidence(root, p0) == []


@pytest.mark.parametrize(
    "relative",
    [
        "docs/productionacceptance/report.json",
        "config/trusted-signers.json",
    ],
)
def test_scoped_or_credential_marked_malformed_json_fails_closed(
    tmp_path: Path, relative: str
) -> None:
    root = tmp_path / "repo"
    p0 = root / "infra/policy/gate6/production-acceptance-profile.disabled-template.json"
    p0.parent.mkdir(parents=True)
    p0.write_text("{}", encoding="utf-8")
    candidate = root / relative
    candidate.parent.mkdir(parents=True)
    candidate.write_text('{"value":', encoding="utf-8")

    with pytest.raises(RepositoryPostureError, match="invalid JSON material"):
        _assert_no_production_evidence(root, p0)


def test_valid_unscoped_json_with_sensitive_material_still_fails_closed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    p0 = root / "infra/policy/gate6/production-acceptance-profile.disabled-template.json"
    p0.parent.mkdir(parents=True)
    p0.write_text("{}", encoding="utf-8")
    unrelated = root / "vendor/package-metadata.json"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text(
        json.dumps({"trusted_signers": {"security": "opaque"}}),
        encoding="utf-8",
    )

    with pytest.raises(RepositoryPostureError, match="trust-store material"):
        _assert_no_production_evidence(root, p0)


@pytest.mark.parametrize(
    ("filename", "contents"),
    [
        ("investigate6-notes.bin", b"\xff"),
        ("navigate6-notes.bin", b"x" * (1024 * 1024 + 1)),
        ("gate60-notes.bin", b"x" * (1024 * 1024 + 1)),
    ],
)
def test_gate6_marker_does_not_match_inside_unrelated_words(
    tmp_path: Path, filename: str, contents: bytes
) -> None:
    root = tmp_path / "repo"
    p0 = root / "infra/policy/gate6/production-acceptance-profile.disabled-template.json"
    p0.parent.mkdir(parents=True)
    p0.write_text("{}", encoding="utf-8")
    unrelated = root / "docs" / filename
    unrelated.parent.mkdir(parents=True)
    unrelated.write_bytes(contents)

    assert _assert_no_production_evidence(root, p0) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("production_topology", {"topology_id": "actual"}),
        ("rto_rpo", {"rto_seconds": 1, "rpo_seconds": 1}),
        ("workflow_matrix", [{"workflow_id": "actual"}]),
        ("signer_roles", []),
        ("valid_from", "2026-07-16T00:00:00Z"),
    ],
)
def test_p0_template_sentinel_production_fields_fail_closed(
    tmp_path: Path, field: str, value: object
) -> None:
    profile = json.loads(P0_TEMPLATE.read_text(encoding="utf-8"))
    profile[field] = value
    path = tmp_path / "repo/infra/policy/gate6/production-acceptance-profile.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(profile), encoding="utf-8")

    with pytest.raises(RepositoryPostureError, match="sentinel"):
        _assert_disabled_p0_template(path)
