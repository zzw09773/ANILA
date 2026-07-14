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
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


SYNTHETIC_EMPLOYEE_ID = "990000001"
SYNTHETIC_SECOND_EMPLOYEE_ID = "990000002"
SYNTHETIC_DISPLAY_NAME = "Synthetic Card User"
SYNTHETIC_EMAIL = "synthetic.card.user@example.invalid"
SYNTHETIC_CARD_SERIAL = "SYNTH-CARD-0001"
SYNTHETIC_POLICY_OID = "1.3.6.1.4.1.55555.1.1"


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
    digital_signature: bool = True,
    eku_oids: list[x509.ObjectIdentifier] | None = None,
    policy_oids: list[x509.ObjectIdentifier] | None = None,
    rsa_pss: bool = False,
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
                digital_signature=digital_signature,
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
    if eku_oids is not None:
        builder = builder.add_extension(
            x509.ExtendedKeyUsage(eku_oids), critical=False
        )
    if policy_oids is not None:
        builder = builder.add_extension(
            x509.CertificatePolicies(
                [x509.PolicyInformation(oid, None) for oid in policy_oids]
            ),
            critical=False,
        )
    rsa_padding = (
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=hashes.SHA256().digest_size,
        )
        if rsa_pss
        else None
    )
    return builder.sign(
        issuer_key,
        hashes.SHA256(),
        rsa_padding=rsa_padding,
    )


@dataclass(frozen=True)
class SyntheticCardPKI:
    root_certificate: x509.Certificate
    intermediate_certificate: x509.Certificate
    signer_certificate: x509.Certificate
    root_private_key: rsa.RSAPrivateKey
    intermediate_private_key: rsa.RSAPrivateKey
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

    def crl_cache(
        self,
        *,
        revoked_serials: set[int] | None = None,
        stale: bool = False,
        bad_signature: bool = False,
        rsa_pss: bool = False,
    ):
        """Return issuer-subject keyed CRLs generated entirely in memory."""
        now = datetime.now(timezone.utc)
        last_update = now - (
            timedelta(hours=48) if stale else timedelta(minutes=5)
        )
        next_update = now + timedelta(days=1)

        def build(issuer, signing_key, serials=()):
            builder = (
                x509.CertificateRevocationListBuilder()
                .issuer_name(issuer.subject)
                .last_update(last_update)
                .next_update(next_update)
            )
            for serial in serials:
                revoked = (
                    x509.RevokedCertificateBuilder()
                    .serial_number(serial)
                    .revocation_date(now - timedelta(minutes=1))
                    .build()
                )
                builder = builder.add_revoked_certificate(revoked)
            rsa_padding = (
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=hashes.SHA256().digest_size,
                )
                if rsa_pss
                else None
            )
            return builder.sign(
                signing_key,
                hashes.SHA256(),
                rsa_padding=rsa_padding,
            )

        root_crl = build(self.root_certificate, self.root_private_key)
        intermediate_crl = build(
            self.intermediate_certificate,
            self.root_private_key if bad_signature else self.intermediate_private_key,
            revoked_serials or set(),
        )
        return {
            self.root_certificate.subject.public_bytes(): [root_crl],
            self.intermediate_certificate.subject.public_bytes(): [intermediate_crl],
        }

    def crl_pem(self, **kwargs) -> bytes:
        cache = self.crl_cache(**kwargs)
        return b"".join(
            crl.public_bytes(serialization.Encoding.PEM)
            for crls in cache.values()
            for crl in crls
        )


def create_synthetic_card_pki(
    *,
    root_path_length: int | None = 1,
    signer_is_ca: bool = False,
    signer_digital_signature: bool = True,
    signer_eku: bool = True,
    signer_policy_oid: str = SYNTHETIC_POLICY_OID,
    certificate_rsa_pss: bool = False,
    bad_signer_certificate_signature: bool = False,
) -> SyntheticCardPKI:
    root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    root_name = _name("ANILA Synthetic Root CA")
    root_certificate = _issue_certificate(
        subject=root_name,
        issuer=root_name,
        public_key=root_key.public_key(),
        issuer_key=root_key,
        serial_number=1001,
        is_ca=True,
        path_length=root_path_length,
        rsa_pss=certificate_rsa_pss,
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
        rsa_pss=certificate_rsa_pss,
    )

    signer_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    signer_certificate = _issue_certificate(
        subject=_name(SYNTHETIC_DISPLAY_NAME, SYNTHETIC_EMPLOYEE_ID),
        issuer=intermediate_name,
        public_key=signer_key.public_key(),
        issuer_key=(
            root_key if bad_signer_certificate_signature else intermediate_key
        ),
        serial_number=1003,
        is_ca=signer_is_ca,
        email=SYNTHETIC_EMAIL,
        digital_signature=signer_digital_signature,
        eku_oids=[ExtendedKeyUsageOID.CLIENT_AUTH] if signer_eku else None,
        policy_oids=[x509.ObjectIdentifier(signer_policy_oid)],
        rsa_pss=certificate_rsa_pss,
    )
    return SyntheticCardPKI(
        root_certificate=root_certificate,
        intermediate_certificate=intermediate_certificate,
        signer_certificate=signer_certificate,
        root_private_key=root_key,
        intermediate_private_key=intermediate_key,
        signer_private_key=signer_key,
    )


SYNTHETIC_CARD = create_synthetic_card_pki()
