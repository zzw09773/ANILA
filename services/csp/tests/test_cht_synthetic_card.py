"""Contract test for the local CHT emulator's ephemeral synthetic PKI."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.services import card_auth
from app.services.card_auth import InvalidSignatureError, verify_pkcs7_signature


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cht.synthetic_pki import (  # noqa: E402
    SYNTHETIC_CARD,
    SYNTHETIC_CARD_SERIAL,
    SYNTHETIC_DISPLAY_NAME,
    SYNTHETIC_EMAIL,
    SYNTHETIC_EMPLOYEE_ID,
    SYNTHETIC_SIGNER_CERT_SERIAL,
)


def test_cht_emulator_is_bound_to_localhost_with_read_only_source_mount():
    compose = (REPO_ROOT / "cht" / "docker-compose.yaml").read_text(
        encoding="utf-8"
    )

    assert "127.0.0.1:16888:16888" in compose
    assert ".:/workspace:ro" in compose
    assert '\n            - "16888:16888"' not in compose


def test_cht_emulator_signs_actual_nonce_with_trusted_synthetic_chain(monkeypatch):
    nonce = "challenge-created-by-csp"
    monkeypatch.setattr(card_auth, "_ca_anchor_cache", SYNTHETIC_CARD.anchor_cache())
    monkeypatch.setattr(card_auth, "_SKIP_NONCE_BINDING", False)

    claims = verify_pkcs7_signature(
        SYNTHETIC_CARD.sign(nonce),
        nonce,
        card_serial=SYNTHETIC_CARD_SERIAL,
    )

    assert claims.employee_id == SYNTHETIC_EMPLOYEE_ID
    assert claims.display_name == SYNTHETIC_DISPLAY_NAME
    assert claims.email == SYNTHETIC_EMAIL
    assert claims.card_serial == SYNTHETIC_SIGNER_CERT_SERIAL

    with pytest.raises(InvalidSignatureError, match="nonce"):
        verify_pkcs7_signature(SYNTHETIC_CARD.sign(nonce), "different-challenge")
