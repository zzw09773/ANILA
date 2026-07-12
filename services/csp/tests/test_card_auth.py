"""Synthetic PKI security tests for card-auth CMS verification.

Every key/certificate is generated in memory and every signature covers the
actual nonce. No real identity, certificate, card serial, or signature fixture
is stored in the repository.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509.oid import NameOID

from app.services.card_auth import (
    CardAuthError,
    CardClaims,
    InvalidSignatureError,
    verify_pkcs7_signature,
)
from tests.synthetic_card_pki import (
    SYNTHETIC_CARD,
    SYNTHETIC_CARD_SERIAL,
    SYNTHETIC_DISPLAY_NAME,
    SYNTHETIC_EMAIL,
    SYNTHETIC_EMPLOYEE_ID,
    SYNTHETIC_SECOND_EMPLOYEE_ID,
)


pytestmark = pytest.mark.usefixtures("synthetic_card_trust")
MOCK_NONCE = b"synthetic-fixed-nonce"
# Backward-compatible name for tests that only need a valid synthetic CMS.
MOCK_SIGNATURE_B64 = SYNTHETIC_CARD.sign(MOCK_NONCE)


def _forged_signed_cms(employee_id: str, nonce: bytes = MOCK_NONCE) -> str:
    """Create a cryptographically valid CMS under an untrusted self-signed CA."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "ZZ"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Untrusted Example Lab"),
            x509.NameAttribute(NameOID.COMMON_NAME, "Forged Synthetic User"),
            x509.NameAttribute(NameOID.SERIAL_NUMBER, employee_id),
        ]
    )
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(2001)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.RFC822Name("forged.user@example.invalid")]
            ),
            False,
        )
        .sign(key, hashes.SHA256())
    )
    der = (
        pkcs7.PKCS7SignatureBuilder()
        .set_data(nonce)
        .add_signer(cert, key, hashes.SHA256())
        .sign(serialization.Encoding.DER, [pkcs7.PKCS7Options.Binary])
    )
    return base64.b64encode(der).decode("ascii")


class TestVerifyValidSignature:
    def test_returns_card_claims(self) -> None:
        result = verify_pkcs7_signature(MOCK_SIGNATURE_B64, MOCK_NONCE)
        assert isinstance(result, CardClaims)

    def test_employee_id_from_subject_serial_number(self) -> None:
        claims = verify_pkcs7_signature(MOCK_SIGNATURE_B64, MOCK_NONCE)
        assert claims.employee_id == SYNTHETIC_EMPLOYEE_ID

    def test_display_name_from_common_name(self) -> None:
        claims = verify_pkcs7_signature(MOCK_SIGNATURE_B64, MOCK_NONCE)
        assert claims.display_name == SYNTHETIC_DISPLAY_NAME

    def test_email_from_san_rfc822(self) -> None:
        claims = verify_pkcs7_signature(MOCK_SIGNATURE_B64, MOCK_NONCE)
        assert claims.email == SYNTHETIC_EMAIL

    def test_nonce_accepts_str_or_bytes(self) -> None:
        signature = SYNTHETIC_CARD.sign("synthetic-string-nonce")
        claims = verify_pkcs7_signature(signature, "synthetic-string-nonce")
        assert claims.employee_id == SYNTHETIC_EMPLOYEE_ID

    def test_card_serial_propagated_when_supplied(self) -> None:
        claims = verify_pkcs7_signature(
            MOCK_SIGNATURE_B64,
            MOCK_NONCE,
            card_serial=SYNTHETIC_CARD_SERIAL,
        )
        assert claims.card_serial == SYNTHETIC_CARD_SERIAL

    def test_card_serial_none_when_omitted(self) -> None:
        claims = verify_pkcs7_signature(MOCK_SIGNATURE_B64, MOCK_NONCE)
        assert claims.card_serial is None

    def test_claims_are_immutable(self) -> None:
        claims = verify_pkcs7_signature(MOCK_SIGNATURE_B64, MOCK_NONCE)
        with pytest.raises(Exception):
            claims.employee_id = SYNTHETIC_SECOND_EMPLOYEE_ID  # type: ignore[misc]


class TestRejectsForgeryAndReplay:
    def test_self_signed_forgery_with_owner_id_rejected(self) -> None:
        forged = _forged_signed_cms(SYNTHETIC_SECOND_EMPLOYEE_ID)
        with pytest.raises(InvalidSignatureError):
            verify_pkcs7_signature(forged, MOCK_NONCE)

    def test_self_signed_forgery_any_id_rejected(self) -> None:
        forged = _forged_signed_cms("990000003")
        with pytest.raises(InvalidSignatureError):
            verify_pkcs7_signature(forged, MOCK_NONCE)

    def test_wrong_nonce_rejected(self) -> None:
        with pytest.raises(InvalidSignatureError):
            verify_pkcs7_signature(MOCK_SIGNATURE_B64, b"a-different-nonce")

    def test_tampered_signature_rejected(self) -> None:
        raw = bytearray(base64.b64decode(MOCK_SIGNATURE_B64))
        raw[-1] ^= 0xFF
        with pytest.raises(InvalidSignatureError):
            verify_pkcs7_signature(base64.b64encode(bytes(raw)).decode(), MOCK_NONCE)


class TestInvalidInput:
    @pytest.mark.parametrize("payload", ["not-base64!!!", "", "aGVsbG8="])
    def test_invalid_payload_raises(self, payload: str) -> None:
        with pytest.raises(InvalidSignatureError):
            verify_pkcs7_signature(payload, MOCK_NONCE)


def test_error_hierarchy() -> None:
    assert issubclass(InvalidSignatureError, CardAuthError)
