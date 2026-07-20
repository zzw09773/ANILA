"""Ephemeral synthetic smart-card PKI used by the local CHT emulator.

All keys exist only in process memory. The repository contains no serialized
private key, user certificate, CMS signature, real employee identity, or card
serial. Restarting the emulator creates a new test CA, so developers must use
the CA bundle exposed by that running instance.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


SYNTHETIC_EMPLOYEE_ID = "990000001"
SYNTHETIC_DISPLAY_NAME = "Synthetic Card User"
SYNTHETIC_EMAIL = "synthetic.card.user@example.invalid"
SYNTHETIC_CARD_SERIAL = "SYNTH-CARD-0001"
SYNTHETIC_POLICY_OID = x509.ObjectIdentifier("1.3.6.1.4.1.55555.1.1")


def _name(common_name: str, employee_id: str | None = None) -> x509.Name:
    attributes = [
        x509.NameAttribute(NameOID.COUNTRY_NAME, "ZZ"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ANILA Synthetic Test Lab"),
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
    ]
    if employee_id:
        attributes.append(x509.NameAttribute(NameOID.SERIAL_NUMBER, employee_id))
    return x509.Name(attributes)


def _certificate(
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
        .not_valid_after(now + timedelta(days=365))
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
    if not is_ca:
        builder = builder.add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=False,
        ).add_extension(
            x509.CertificatePolicies(
                [x509.PolicyInformation(SYNTHETIC_POLICY_OID, None)]
            ),
            critical=False,
        )
    return builder.sign(issuer_key, hashes.SHA256())


@dataclass(frozen=True)
class SyntheticCardIdentity:
    root_certificate: x509.Certificate
    intermediate_certificate: x509.Certificate
    signer_certificate: x509.Certificate
    signer_private_key: rsa.RSAPrivateKey

    def sign(self, tbs: str | bytes) -> str:
        data = tbs.encode("utf-8") if isinstance(tbs, str) else tbs
        signed = (
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
        return base64.b64encode(signed).decode("ascii")

    @property
    def signer_certificate_b64(self) -> str:
        der = self.signer_certificate.public_bytes(serialization.Encoding.DER)
        return base64.b64encode(der).decode("ascii")

    @property
    def ca_bundle_pem(self) -> bytes:
        return b"".join(
            certificate.public_bytes(serialization.Encoding.PEM)
            for certificate in (
                self.root_certificate,
                self.intermediate_certificate,
            )
        )

    def anchor_cache(self):
        anchors = {
            self.root_certificate.subject.public_bytes(): self.root_certificate,
            self.intermediate_certificate.subject.public_bytes(): (
                self.intermediate_certificate
            ),
        }
        return anchors, [self.root_certificate]


def create_synthetic_card_identity() -> SyntheticCardIdentity:
    root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    root_name = _name("ANILA Synthetic Root CA")
    root_certificate = _certificate(
        subject=root_name,
        issuer=root_name,
        public_key=root_key.public_key(),
        issuer_key=root_key,
        serial_number=3001,
        is_ca=True,
        path_length=1,
    )

    intermediate_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    intermediate_name = _name("ANILA Synthetic Issuing CA")
    intermediate_certificate = _certificate(
        subject=intermediate_name,
        issuer=root_name,
        public_key=intermediate_key.public_key(),
        issuer_key=root_key,
        serial_number=3002,
        is_ca=True,
        path_length=0,
    )

    signer_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    signer_certificate = _certificate(
        subject=_name(SYNTHETIC_DISPLAY_NAME, SYNTHETIC_EMPLOYEE_ID),
        issuer=intermediate_name,
        public_key=signer_key.public_key(),
        issuer_key=intermediate_key,
        serial_number=3003,
        is_ca=False,
        email=SYNTHETIC_EMAIL,
    )
    return SyntheticCardIdentity(
        root_certificate=root_certificate,
        intermediate_certificate=intermediate_certificate,
        signer_certificate=signer_certificate,
        signer_private_key=signer_key,
    )


SYNTHETIC_CARD = create_synthetic_card_identity()
SYNTHETIC_SIGNER_CERT_SERIAL = format(
    SYNTHETIC_CARD.signer_certificate.serial_number,
    "X",
)
