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
