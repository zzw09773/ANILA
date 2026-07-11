"""Ephemeral, entirely synthetic PKI for card-auth security tests.

No certificate, signature, employee identity, or private key is serialized in
the repository. Keys and certificates are generated in memory once per test
process; CMS signatures are produced for the actual challenge nonce.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509.oid import NameOID


SYNTHETIC_EMPLOYEE_ID = "990000001"
SYNTHETIC_SECOND_EMPLOYEE_ID = "990000002"
SYNTHETIC_DISPLAY_NAME = "Synthetic Card User"
SYNTHETIC_EMAIL = "synthetic.card.user@example.invalid"
SYNTHETIC_CARD_SERIAL = "SYNTH-CARD-0001"


def _name(common_name: str, employee_id: str | None = None) -> x509.Name:
    attributes = [
        x509.NameAttribute(NameOID.COUNTRY_NAME, "ZZ"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ANILA Synthetic Test Lab"),
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
    ]
    if employee_id:
        attributes.append(x509.NameAttribute(NameOID.SERIAL_NUMBER, employee_id))
    return x509.Name(attributes)


def _issue_certificate(
    *,
    subject: x509.Name,
    issuer: x509.Name,
    public_key,
    issuer_key,
    serial_number: int,
    is_ca: bool,
    path_length: int | None = None,
    email: str | None = None,
) -> x509.Certificate:
    now = datetime.now(timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(public_key)
        .serial_number(serial_number)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(
            x509.BasicConstraints(ca=is_ca, path_length=path_length), critical=True
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=not is_ca,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=is_ca,
                crl_sign=is_ca,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
    )
    if email:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.RFC822Name(email)]), critical=False
        )
    return builder.sign(issuer_key, hashes.SHA256())


@dataclass(frozen=True)
class SyntheticCardPKI:
    root_certificate: x509.Certificate
    intermediate_certificate: x509.Certificate
    signer_certificate: x509.Certificate
    signer_private_key: rsa.RSAPrivateKey

    def sign(self, nonce: str | bytes) -> str:
        data = nonce.encode("utf-8") if isinstance(nonce, str) else nonce
        der = (
            pkcs7.PKCS7SignatureBuilder()
            .set_data(data)
            .add_signer(
                self.signer_certificate,
                self.signer_private_key,
                hashes.SHA256(),
            )
            .add_certificate(self.intermediate_certificate)
            .sign(serialization.Encoding.DER, [pkcs7.PKCS7Options.Binary])
        )
        return base64.b64encode(der).decode("ascii")

    def anchor_cache(self):
        anchors = {
            self.root_certificate.subject.public_bytes(): self.root_certificate,
            self.intermediate_certificate.subject.public_bytes(): (
                self.intermediate_certificate
            ),
        }
        return anchors, [self.root_certificate]


def create_synthetic_card_pki() -> SyntheticCardPKI:
    root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    root_name = _name("ANILA Synthetic Root CA")
    root_certificate = _issue_certificate(
        subject=root_name,
        issuer=root_name,
        public_key=root_key.public_key(),
        issuer_key=root_key,
        serial_number=1001,
        is_ca=True,
        path_length=1,
    )

    intermediate_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    intermediate_name = _name("ANILA Synthetic Issuing CA")
    intermediate_certificate = _issue_certificate(
        subject=intermediate_name,
        issuer=root_name,
        public_key=intermediate_key.public_key(),
        issuer_key=root_key,
        serial_number=1002,
        is_ca=True,
        path_length=0,
    )

    signer_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    signer_certificate = _issue_certificate(
        subject=_name(SYNTHETIC_DISPLAY_NAME, SYNTHETIC_EMPLOYEE_ID),
        issuer=intermediate_name,
        public_key=signer_key.public_key(),
        issuer_key=intermediate_key,
        serial_number=1003,
        is_ca=False,
        email=SYNTHETIC_EMAIL,
    )
    return SyntheticCardPKI(
        root_certificate=root_certificate,
        intermediate_certificate=intermediate_certificate,
        signer_certificate=signer_certificate,
        signer_private_key=signer_key,
    )


SYNTHETIC_CARD = create_synthetic_card_pki()
