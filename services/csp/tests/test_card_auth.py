"""card_auth.py 的單元測試。

測試素材是 ``cht/app.py`` mock 簽章 (鄒惠翔測試卡)。這份 signature 是寫死在
mock flask app 內的 base64 PKCS#7/CMS SignedData,eContent 固定為 ``b"TBS"``,
所有開發者刷 PIN=``123456`` 都會拿到同一份。

2026-06-12:card_auth 從「只解析」改為「真驗證」(簽章 + 憑證鏈到釘死的 CSPKI
CA + nonce 綁定)。本檔同步測新契約 + 把原本的 CVE(自簽偽造任意工號) 加進回歸守
護。
"""
from __future__ import annotations

import base64
import datetime

import pytest
from asn1crypto import cms
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from app.services.card_auth import (
    CardAuthError,
    CardClaims,
    InvalidSignatureError,
    verify_pkcs7_signature,
)


# 來源:cht/app.py 的 "signature" 欄位 (鄒惠翔測試卡的 PKCS#7 簽章,eContent=b"TBS")。
MOCK_SIGNATURE_B64 = (
    "MIIHNgYJKoZIhvcNAQcCoIIHJzCCByMCAQExDzANBglghkgBZQMEAgEFADASBgkqhkiG"
    "9w0BBwGgBQQDVEJToIIE6jCCBOYwggRsoAMCAQICEQCPfuzI3S+1D/OD79T2gKo5MAoG"
    "CCqGSM49BAMCMF4xCzAJBgNVBAYTAlRXMSQwIgYDVQQKDBvlnIvlrrbkuK3lsbHnp5Hl"
    "rbjnoJTnqbbpmaIxKTAnBgNVBAMMIOS4reenkemZouaGkeitieeuoeeQhuS4reW/gyAt"
    "IEcxMB4XDTI1MDIyMTA3NDkyN1oXDTMwMDIyMTA3NDkyN1owWTELMAkGA1UEBhMCVFcx"
    "JDAiBgNVBAoMG+Wci+WutuS4reWxseenkeWtuOeglOeptumZojESMBAGA1UEAwwJ6YSS"
    "5oOg57+UMRAwDgYDVQQFEwcxMDkwODY4MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIB"
    "CgKCAQEAuOEF9VKstNBNoFfQatdMYMcqTUbq43QTxQNygeorpuyXKLM6MPcLmJtNE9E5"
    "a3tFjZWz5VOFoy+pjTzOF9ApzdPmUwGDh1PpN/mUvDWC4lnMGBUC5dRn6hOf9V2RjU6u"
    "IMqj5z41MIzqoS3tN14aVw0gUPvGjm7n/fylnbmUYAvOe1HyVPHNdKr2cYLR5hvVeWKk"
    "Q7kDWRrbNDMR2Ml9oQaWK0ccbmYDSKRWdIQv8+DdhX2B9/8C+7Q1FE+ekLHyjppC6VwM"
    "RSP44iPszy0TBK833ZtNn1ybDsTJGjVE/odNDv2QbtiwrfNp8SSqVQdxVL51MIXCDM21"
    "5EEvBzzpMwIDAQABo4ICQzCCAj8wHwYDVR0jBBgwFoAUHZyJwv7uNVXVRo9VuPwDcVlM"
    "K+AwHQYDVR0OBBYEFFZrz2bFw4LO16MqMgjLiyuYtbE5MIGcBgNVHR8EgZQwgZEwTqBM"
    "oEqGSGh0dHA6Ly9yZXBvc2l0b3J5Lm5jc2lzdC5vcmcudHcvY3JsL05DU0lTVENBLU5D"
    "U0lTVC8xOTg1LTEvcGFydGl0aW9uLmNybDA/oD2gO4Y5aHR0cDovL3JlcG9zaXRvcnku"
    "bmNzaXN0Lm9yZy50dy9jcmwvTkNTSVNUQ0EvY29tcGxldGUuY3JsMHoGCCsGAQUFBwEB"
    "BG4wbDA+BggrBgEFBQcwAoYyaHR0cDovL3JlcG9zaXRvcnkubmNzaXN0Lm9yZy50dy9j"
    "ZXJ0cy9OQ1NJU1RDQS5jZXIwKgYIKwYBBQUHMAGGHmh0dHA6Ly9vY3NwLm5jc2lzdC5v"
    "cmcudHcvT0NTUDAXBgNVHSAEEDAOMAwGCmCGdmmGjSMAAwMwRQYDVR0RBD4wPIEUQzk1"
    "VEhTQG5jc2lzdC5vcmcudHegJAYKKwYBBAGCNxQCA6AWDBRDOTVUSFNAbmNzaXN0Lm9y"
    "Zy50dzAzBgNVHQkELDAqMBUGB2CGdgFkAgExCgYIYIZ2AWQDAQYwEQYHYIZ2AWQCMzEG"
    "DAQwMTk0MA4GA1UdDwEB/wQEAwIHgDAvBgNVHSUEKDAmBgRVHSUABggrBgEFBQcDBAYI"
    "KwYBBQUHAwIGCisGAQQBgjcUAgIwDAYDVR0TAQH/BAIwADAKBggqhkjOPQQDAgNoADBl"
    "AjBJD/+K/V+0drtJrWZ6T6T28bg4PdVPkYC5K0JmZmndMXDCVxp8kfWnRT+hO2qafW8C"
    "MQDjbPPahLh7Ek6fhAGB47L87U/NZPi/x1bS0kwnslND3mTzsiTHtNTlCAqxd1+pbr8x"
    "ggIJMIICBQIBATBzMF4xCzAJBgNVBAYTAlRXMSQwIgYDVQQKDBvlnIvlrrbkuK3lsbHn"
    "p5HlrbjnoJTnqbbpmaIxKTAnBgNVBAMMIOS4reenkemZouaGkeitieeuoeeQhuS4reW/"
    "gyAtIEcxAhEAj37syN0vtQ/zg+/U9oCqOTANBglghkgBZQMEAgEFAKBpMBgGCSqGSIb3"
    "DQEJAzELBgkqhkiG9w0BBwEwHAYJKoZIhvcNAQkFMQ8XDTI1MDUyNzA3MDcwMFowLwYJ"
    "KoZIhvcNAQkEMSIEIMnXsP3Gf/4Y4lexWlIlnQ8CvCWzhP2Tra9hBVOo1IL8MA0GCSqG"
    "SIb3DQEBAQUABIIBAHJ6EdR7sNClFlIVXPsWhmZcEolYqZ1jgbhrbHxHHvPc0fRPL3kM"
    "kNwOTzND6y0HHfq2BSlkNQl8EYuJ1JFHJM5HU1JJXNHPvSPGTCXhJCSRAlQW5qjkbTb1"
    "annuaIvyMt0+hbnLvDB8PlZxP/0RtRjBIVz3LvfbdX0shTTdd3VrA2uTCtYquTCy9uxb"
    "+aX8q5WKWPKB5EKKu/WcvWcUYXS6wTkhzwGi1YGzlDT0x803w9DYm5dQavUoqSHqa/sm"
    "3xDdlzcjtN+ERFST7EPGZusnCjYDPRTI2bEXyaWuFbFiO/MMWTc+6iJ6Q57SCqCB3/2N"
    "UXmRm+Co/pN9aSzpbeE="
)

# mock 卡簽的 eContent(challenge nonce 在 dev mock 下固定為這串)。
MOCK_NONCE = b"TBS"


def _forged_signed_cms(employee_id: str, nonce: bytes = MOCK_NONCE) -> str:
    """攻擊者自簽一張 cert(填任意 serialNumber)並包成 CMS SignedData 簽 ``nonce``。

    這是原 CVE 的攻擊載荷:舊版只解析憑證、不驗鏈,故任何自簽憑證都被當可信。
    新版必須因「憑證鏈連不到釘死的 CSPKI CA」而拒絕。
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "TW"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "國家中山科學研究院"),
            x509.NameAttribute(NameOID.COMMON_NAME, "駭客"),
            x509.NameAttribute(NameOID.SERIAL_NUMBER, employee_id),
        ]
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(1234)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName([x509.RFC822Name("hacker@ncsist.org.tw")]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    der = cert.public_bytes(serialization.Encoding.DER)
    signed_data = cms.SignedData(
        {
            "version": "v1",
            "digest_algorithms": [{"algorithm": "sha256"}],
            "encap_content_info": {"content_type": "data", "content": nonce},
            "certificates": [cms.CertificateChoices.load(der)],
            "signer_infos": [
                {
                    "version": "v1",
                    "sid": cms.SignerIdentifier(
                        {
                            "issuer_and_serial_number": {
                                "issuer": cms.Certificate.load(der)["tbs_certificate"][
                                    "issuer"
                                ],
                                "serial_number": 1234,
                            }
                        }
                    ),
                    "digest_algorithm": {"algorithm": "sha256"},
                    "signature_algorithm": {"algorithm": "rsassa_pkcs1v15"},
                    "signature": key.sign(nonce, padding.PKCS1v15(), hashes.SHA256()),
                }
            ],
        }
    )
    info = cms.ContentInfo({"content_type": "signed_data", "content": signed_data})
    return base64.b64encode(info.dump()).decode()


@pytest.mark.unit
class TestVerifyValidSignature:
    """真實卡簽 + 正確 nonce 才回 claims。"""

    def test_returns_card_claims(self) -> None:
        result = verify_pkcs7_signature(MOCK_SIGNATURE_B64, MOCK_NONCE)
        assert isinstance(result, CardClaims)

    def test_employee_id_from_subject_serial_number(self) -> None:
        claims = verify_pkcs7_signature(MOCK_SIGNATURE_B64, MOCK_NONCE)
        assert claims.employee_id == "1090868"

    def test_display_name_from_common_name(self) -> None:
        claims = verify_pkcs7_signature(MOCK_SIGNATURE_B64, MOCK_NONCE)
        assert claims.display_name == "鄒惠翔"

    def test_email_from_san_rfc822(self) -> None:
        claims = verify_pkcs7_signature(MOCK_SIGNATURE_B64, MOCK_NONCE)
        assert claims.email == "C95THS@ncsist.org.tw"

    def test_nonce_accepts_str_or_bytes(self) -> None:
        claims = verify_pkcs7_signature(MOCK_SIGNATURE_B64, "TBS")
        assert claims.employee_id == "1090868"

    def test_card_serial_propagated_when_supplied(self) -> None:
        claims = verify_pkcs7_signature(
            MOCK_SIGNATURE_B64, MOCK_NONCE, card_serial="CS00000000025247"
        )
        assert claims.card_serial == "CS00000000025247"

    def test_card_serial_none_when_omitted(self) -> None:
        claims = verify_pkcs7_signature(MOCK_SIGNATURE_B64, MOCK_NONCE)
        assert claims.card_serial is None

    def test_claims_are_immutable(self) -> None:
        claims = verify_pkcs7_signature(MOCK_SIGNATURE_B64, MOCK_NONCE)
        with pytest.raises(Exception):
            claims.employee_id = "9999999"  # type: ignore[misc]


@pytest.mark.unit
class TestRejectsForgeryAndReplay:
    """新驗證契約的核心:偽造 / 重放 / 竄改一律拒絕。"""

    def test_self_signed_forgery_with_owner_id_rejected(self) -> None:
        """原 CVE 回歸守護:自簽憑證填 owner 工號 → 必須因鏈驗失敗被拒。"""
        forged = _forged_signed_cms("1147259")
        with pytest.raises(InvalidSignatureError):
            verify_pkcs7_signature(forged, MOCK_NONCE)

    def test_self_signed_forgery_any_id_rejected(self) -> None:
        forged = _forged_signed_cms("9999999")
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


@pytest.mark.unit
class TestInvalidInput:
    def test_invalid_base64_raises(self) -> None:
        with pytest.raises(InvalidSignatureError):
            verify_pkcs7_signature("not-base64!!!", MOCK_NONCE)

    def test_empty_string_raises(self) -> None:
        with pytest.raises(InvalidSignatureError):
            verify_pkcs7_signature("", MOCK_NONCE)

    def test_random_bytes_raise(self) -> None:
        # Valid base64 但不是 PKCS#7 DER
        with pytest.raises(InvalidSignatureError):
            verify_pkcs7_signature("aGVsbG8gd29ybGQ=", MOCK_NONCE)


@pytest.mark.unit
def test_error_hierarchy() -> None:
    # 所有錯誤都應該繼承自 CardAuthError,方便上層 endpoint 統一捕捉。
    assert issubclass(InvalidSignatureError, CardAuthError)
