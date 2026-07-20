"""Gate 2 G17 CRL and X.509 profile negative contracts."""
from __future__ import annotations

import pytest
from cryptography.x509.oid import SignatureAlgorithmOID

from app.services import card_auth
from app.services.card_auth import CardAuthError, verify_pkcs7_signature
from tests.synthetic_card_pki import (
    SYNTHETIC_POLICY_OID,
    create_synthetic_card_pki,
)


NONCE = b"gate2-card-policy-nonce"


def _trust(monkeypatch, pki, *, crl_cache=None):
    monkeypatch.setattr(card_auth, "_ca_anchor_cache", pki.anchor_cache())
    monkeypatch.setattr(
        card_auth,
        "_crl_cache",
        pki.crl_cache() if crl_cache is None else crl_cache,
    )
    monkeypatch.setattr(card_auth, "_crl_cache_source", None)
    monkeypatch.setattr(card_auth.settings, "CARD_CRL_REQUIRED", True)
    monkeypatch.setattr(
        card_auth.settings,
        "CARD_REQUIRED_CERT_POLICY_OIDS",
        SYNTHETIC_POLICY_OID,
    )


def test_revoked_signer_certificate_is_rejected(monkeypatch):
    pki = create_synthetic_card_pki()
    _trust(
        monkeypatch,
        pki,
        crl_cache=pki.crl_cache(
            revoked_serials={pki.signer_certificate.serial_number}
        ),
    )
    with pytest.raises(CardAuthError, match="撤銷"):
        verify_pkcs7_signature(pki.sign(NONCE), NONCE)


def test_airgap_pem_crl_loader_accepts_valid_operator_bundle(
    monkeypatch, tmp_path,
):
    pki = create_synthetic_card_pki()
    crl_path = tmp_path / "card-crl-bundle.pem"
    crl_path.write_bytes(pki.crl_pem())
    monkeypatch.setattr(card_auth, "_ca_anchor_cache", pki.anchor_cache())
    monkeypatch.setattr(card_auth, "_crl_cache", None)
    monkeypatch.setattr(card_auth, "_crl_cache_source", None)
    monkeypatch.setattr(card_auth.settings, "CARD_CRL_REQUIRED", True)
    monkeypatch.setattr(
        card_auth.settings, "CARD_CRL_BUNDLE_PATH", str(crl_path)
    )
    monkeypatch.setattr(
        card_auth.settings,
        "CARD_REQUIRED_CERT_POLICY_OIDS",
        SYNTHETIC_POLICY_OID,
    )

    claims = verify_pkcs7_signature(pki.sign(NONCE), NONCE)
    assert claims.employee_id


def test_rsa_pss_crl_uses_declared_parameters(monkeypatch):
    pki = create_synthetic_card_pki()
    pss_cache = pki.crl_cache(rsa_pss=True)
    _trust(monkeypatch, pki, crl_cache=pss_cache)

    claims = verify_pkcs7_signature(pki.sign(NONCE), NONCE)

    assert claims.employee_id
    assert all(
        crl.signature_algorithm_oid == SignatureAlgorithmOID.RSASSA_PSS
        for crls in pss_cache.values()
        for crl in crls
    )


def test_bad_signature_rsa_pss_crl_fails_closed(monkeypatch):
    pki = create_synthetic_card_pki()
    _trust(
        monkeypatch,
        pki,
        crl_cache=pki.crl_cache(rsa_pss=True, bad_signature=True),
    )

    with pytest.raises(CardAuthError, match="CRL"):
        verify_pkcs7_signature(pki.sign(NONCE), NONCE)


def test_rsa_pss_crl_without_parameters_has_no_pkcs1_fallback():
    class UnsupportedPssCrl:
        signature_algorithm_oid = SignatureAlgorithmOID.RSASSA_PSS
        signature_algorithm_parameters = None

    with pytest.raises(CardAuthError, match="RSA-PSS"):
        card_auth._rsa_padding_for_crl(UnsupportedPssCrl())


def test_rsa_pss_certificate_chain_uses_declared_parameters(monkeypatch):
    pki = create_synthetic_card_pki(certificate_rsa_pss=True)
    _trust(monkeypatch, pki)

    claims = verify_pkcs7_signature(pki.sign(NONCE), NONCE)

    assert claims.employee_id
    assert pki.signer_certificate.signature_algorithm_oid == (
        SignatureAlgorithmOID.RSASSA_PSS
    )
    assert pki.intermediate_certificate.signature_algorithm_oid == (
        SignatureAlgorithmOID.RSASSA_PSS
    )


def test_bad_signature_rsa_pss_certificate_chain_fails_closed(monkeypatch):
    pki = create_synthetic_card_pki(
        certificate_rsa_pss=True,
        bad_signer_certificate_signature=True,
    )
    _trust(monkeypatch, pki)

    with pytest.raises(CardAuthError, match="憑證鏈簽章"):
        verify_pkcs7_signature(pki.sign(NONCE), NONCE)


def test_rsa_pss_certificate_without_parameters_has_no_pkcs1_fallback(monkeypatch):
    pki = create_synthetic_card_pki()

    class UnsupportedPssCertificate:
        signature_algorithm_oid = SignatureAlgorithmOID.RSASSA_PSS
        signature_algorithm_parameters = None

    with pytest.raises(CardAuthError, match="RSA-PSS"):
        card_auth._verify_cert_signed_by(
            UnsupportedPssCertificate(),
            pki.intermediate_certificate,
        )


@pytest.mark.parametrize("crl_mode", ["missing", "stale", "bad_signature"])
def test_missing_stale_or_bad_signature_crl_fails_closed(monkeypatch, crl_mode):
    pki = create_synthetic_card_pki()
    if crl_mode == "missing":
        cache = {}
    elif crl_mode == "stale":
        cache = pki.crl_cache(stale=True)
    else:
        cache = pki.crl_cache(bad_signature=True)
    _trust(monkeypatch, pki, crl_cache=cache)
    with pytest.raises(CardAuthError):
        verify_pkcs7_signature(pki.sign(NONCE), NONCE)


@pytest.mark.parametrize(
    "profile",
    [
        {"signer_is_ca": True},
        {"signer_digital_signature": False},
        {"signer_eku": False},
        {"signer_policy_oid": "1.3.6.1.4.1.55555.99.99"},
        {"root_path_length": 0},
    ],
)
def test_invalid_basic_constraints_key_usage_eku_policy_or_pathlen_rejected(
    monkeypatch, profile,
):
    pki = create_synthetic_card_pki(**profile)
    _trust(monkeypatch, pki)
    with pytest.raises(CardAuthError):
        verify_pkcs7_signature(pki.sign(NONCE), NONCE)
