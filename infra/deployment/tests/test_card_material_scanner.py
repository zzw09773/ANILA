"""Fail-closed regression tests for the card/identity material scanner."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization


ROOT = Path(__file__).resolve().parents[3]
SCANNER_PATH = ROOT / "infra" / "security" / "scan_card_material.py"
SPEC = importlib.util.spec_from_file_location("scan_card_material", SCANNER_PATH)
assert SPEC is not None and SPEC.loader is not None
scanner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = scanner
SPEC.loader.exec_module(scanner)


def _public_ca_bundle() -> bytes:
    return (
        ROOT / "services" / "csp" / "app" / "services" / "cspki_ca_bundle.pem"
    ).read_bytes()


def test_non_allowlisted_pem_certificate_is_high_and_blocking() -> None:
    findings, allowed, skipped = scanner.scan_bytes(
        scope="current",
        path="fixtures/personal-card.pem",
        data=_public_ca_bundle(),
    )

    assert skipped is None
    assert allowed == []
    assert any(
        item["kind"] == "certificate_file" and item["severity"] == "high"
        for item in findings
    )


def test_der_certificate_is_found_before_binary_skip() -> None:
    pem_block = scanner.PEM_CERTIFICATE_BLOCK.findall(_public_ca_bundle())[0]
    certificate = scanner.x509.load_pem_x509_certificate(pem_block)
    der = certificate.public_bytes(serialization.Encoding.DER)

    findings, allowed, _skipped = scanner.scan_bytes(
        scope="current",
        path="fixtures/personal-card.cer",
        data=der,
    )

    assert allowed == []
    assert any(
        item["kind"] == "certificate_file" and item["severity"] == "high"
        for item in findings
    )


def test_only_the_pinned_all_ca_bundle_is_allowlisted() -> None:
    findings, allowed, skipped = scanner.scan_bytes(
        scope="current",
        path="services/csp/app/services/cspki_ca_bundle.pem",
        data=_public_ca_bundle(),
    )

    assert skipped is None
    assert findings == []
    assert len(allowed) == 1
    assert allowed[0]["kind"] == "public_ca_trust_anchor"


def test_invalid_content_at_allowlisted_path_fails_closed() -> None:
    findings, allowed, _skipped = scanner.scan_bytes(
        scope="current",
        path="services/csp/app/services/cspki_ca_bundle.pem",
        data=b"-----BEGIN CERTIFICATE-----\nnot-a-certificate\n",
    )

    assert allowed == []
    assert any(item["severity"] == "high" for item in findings)


def test_remote_verification_failure_has_distinct_nonzero_exit() -> None:
    assert scanner.determine_exit_code(
        blocking=[],
        no_fail_current=False,
        remote_verification={"status": "synchronized"},
    ) == 0
    for status in ("unavailable", "out_of_sync"):
        assert scanner.determine_exit_code(
            blocking=[],
            no_fail_current=True,
            remote_verification={"status": status},
        ) == scanner.REMOTE_VERIFICATION_FAILURE_EXIT


def test_current_high_finding_remains_blocking_without_remote_check() -> None:
    assert scanner.determine_exit_code(
        blocking=[{"severity": "high"}],
        no_fail_current=False,
        remote_verification=None,
    ) == 1


def test_ignored_private_key_is_still_scanned(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("*.pem\n", encoding="utf-8")
    ignored = tmp_path / "jwt-private.pem"
    ignored.write_text(
        "-----BEGIN PRIVATE KEY-----\n" + "A" * 256 + "\n-----END PRIVATE KEY-----\n",
        encoding="utf-8",
    )

    paths = set(scanner.worktree_paths(tmp_path))
    assert "jwt-private.pem" in paths
    findings, _allowed, _stats = scanner.scan_worktree(tmp_path)
    assert any(item["kind"] == "serialized_private_key" for item in findings)
