"""card_auth.py 的單元測試。

測試素材是 ``cht/app.py`` mock 簽章所附帶的 real reader-issued certificate
(compatibility evidence:真實讀卡元件輸出能通過本 verifier)。這份 signature 是
寫死在 mock flask app 內的 base64 PKCS#7/CMS SignedData,eContent 固定為
``b"TBS"``,所有開發者刷 PIN=``123456`` 都會拿到同一份。

個人識別值(工號 / 姓名 / email)不寫死在斷言裡——測試時從 fixture 憑證本體
抽出期望值,再與 verifier 輸出比對,避免公開 repo 可 grep 到真人欄位,同時
維持「verifier 解析錯就紅」的強度。

2026-06-12:card_auth 從「只解析」改為「真驗證」(簽章 + 憑證鏈到釘死的 CSPKI
CA + nonce 綁定)。本檔同步測新契約 + 把原本的 CVE(自簽偽造任意工號) 加進回歸守
護。
"""
from __future__ import annotations

import base64
import datetime
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from asn1crypto import cms
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import ExtensionOID, NameOID

from app.config import settings
from app.services import card_auth
from app.services.card_auth import (
    CardAuthError,
    CardClaims,
    InvalidSignatureError,
    verify_pkcs7_signature,
)

# 借 cht/ mock 的簽章器產測試素材 —— 見 test_card_endpoints.py 同段說明。
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "cht"))
from cms_sign import build_pkcs7  # noqa: E402


# 來源:cht/app.py 的 "signature" 欄位 (real reader-issued PKCS#7,eContent=b"TBS")。
# DER 本體保持 byte-identical,作為相容性證據;勿改 blob。
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


@dataclass(frozen=True)
class _FixtureCertClaims:
    """從 MOCK_SIGNATURE_B64 內嵌 signer cert 獨立抽出的期望 claims。

    刻意走 cryptography / asn1crypto,不呼叫 ``card_auth._extract_claims``,
    以免 verifier 與期望值共用同一條解析路徑而變成自我驗證。
    """

    employee_id: str
    display_name: str
    email: str


def _fixture_cert_claims() -> _FixtureCertClaims:
    """Parse the embedded signer certificate and return its subject/SAN claims."""
    content_info = cms.ContentInfo.load(base64.b64decode(MOCK_SIGNATURE_B64))
    signed_data = content_info["content"]
    embedded: list[x509.Certificate] = []
    for choice in signed_data["certificates"]:
        if choice.name != "certificate":
            continue
        embedded.append(x509.load_der_x509_certificate(choice.chosen.dump()))
    if not embedded:
        raise AssertionError("fixture CMS 內找不到 signer 憑證")
    cert = embedded[0]

    serial_attrs = cert.subject.get_attributes_for_oid(NameOID.SERIAL_NUMBER)
    cn_attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    if not serial_attrs or not cn_attrs:
        raise AssertionError("fixture cert 缺 subject.serialNumber 或 commonName")
    try:
        san_ext = cert.extensions.get_extension_for_oid(
            ExtensionOID.SUBJECT_ALTERNATIVE_NAME
        )
    except x509.ExtensionNotFound as exc:
        raise AssertionError("fixture cert 缺 SAN") from exc
    rfc822 = san_ext.value.get_values_for_type(x509.RFC822Name)
    if not rfc822:
        raise AssertionError("fixture cert 缺 SAN.rfc822Name")
    return _FixtureCertClaims(
        employee_id=serial_attrs[0].value,
        display_name=cn_attrs[0].value,
        email=rfc822[0],
    )


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
        expected = _fixture_cert_claims()
        claims = verify_pkcs7_signature(MOCK_SIGNATURE_B64, MOCK_NONCE)
        assert claims.employee_id == expected.employee_id

    def test_display_name_from_common_name(self) -> None:
        expected = _fixture_cert_claims()
        claims = verify_pkcs7_signature(MOCK_SIGNATURE_B64, MOCK_NONCE)
        assert claims.display_name == expected.display_name

    def test_email_from_san_rfc822(self) -> None:
        expected = _fixture_cert_claims()
        claims = verify_pkcs7_signature(MOCK_SIGNATURE_B64, MOCK_NONCE)
        assert claims.email == expected.email

    def test_nonce_accepts_str_or_bytes(self) -> None:
        expected = _fixture_cert_claims()
        claims = verify_pkcs7_signature(MOCK_SIGNATURE_B64, "TBS")
        assert claims.employee_id == expected.employee_id

    def test_card_serial_propagated_when_supplied(self) -> None:
        claims = verify_pkcs7_signature(
            MOCK_SIGNATURE_B64, MOCK_NONCE, card_serial="CS00000000099999"
        )
        assert claims.card_serial == "CS00000000099999"

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


# ─── Dev 測試 CA 的 production fence ───────────────────────────────────────────
#
# 本機開發用的是 ``cht/`` mock 現生的測試 PKI,靠 ``CARD_CA_BUNDLE_PATH`` 把信任
# 錨換過去。信任錨可換這件事在 production 是致命的(誰把測試 root 弄上內網,
# 誰就能自簽一張任意工號的卡登入),所以 card_auth 認得測試 CA 的標記,
# 條件不足時**整包拒收**。下面三支就是守這件事。


def _dev_marked_pki(tmp_path):
    """生一組帶 dev 標記的 root + 卡片,回 (bundle_path, key, cert)。

    刻意用 ``card_auth._DEV_TEST_CA_MARKER_ORG`` 本人而不是複製一份字串 ——
    標記字串在 cht/cms_sign.py 與 card_auth.py 兩邊必須一致,寫死副本會讓
    這支測試在標記改掉時仍然綠。
    """
    root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    root_name = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "TW"),
            x509.NameAttribute(
                NameOID.ORGANIZATION_NAME, card_auth._DEV_TEST_CA_MARKER_ORG
            ),
            x509.NameAttribute(NameOID.COMMON_NAME, "Dev Test Root"),
        ]
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    root_cert = (
        x509.CertificateBuilder()
        .subject_name(root_name)
        .issuer_name(root_name)
        .public_key(root_key.public_key())
        .serial_number(1)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(root_key, hashes.SHA256())
    )
    card_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    card_cert = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name(
                [
                    x509.NameAttribute(NameOID.COUNTRY_NAME, "TW"),
                    x509.NameAttribute(NameOID.ORGANIZATION_NAME, "MOCK ORG"),
                    x509.NameAttribute(NameOID.COMMON_NAME, "測試人員（MOCK 假卡）"),
                    x509.NameAttribute(NameOID.SERIAL_NUMBER, "9999999"),
                ]
            )
        )
        .issuer_name(root_name)
        .public_key(card_key.public_key())
        .serial_number(2)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.RFC822Name("mock-card-9999999@example.invalid")]
            ),
            critical=False,
        )
        .sign(root_key, hashes.SHA256())
    )
    bundle = tmp_path / "dev_ca_bundle.pem"
    bundle.write_bytes(root_cert.public_bytes(serialization.Encoding.PEM))
    return bundle, card_key, card_cert


@pytest.fixture
def _fresh_anchor_cache(monkeypatch):
    """``_ca_anchor_cache`` 是 module global,不清掉就讀不到新 bundle。"""
    monkeypatch.setattr(card_auth, "_ca_anchor_cache", None)


@pytest.mark.unit
def test_dev_test_ca_rejected_without_explicit_optin(
    tmp_path, monkeypatch, _fresh_anchor_cache
):
    """沒開 ``CARD_DEV_TRUST_TEST_CA`` → 即使 bundle 指過去也不准載入。

    這是 production 的常態:平台只會設 CARD_CA_BUNDLE_PATH(換 CA 用),
    不會有人去設 CARD_DEV_TRUST_TEST_CA。
    """
    bundle, key, cert = _dev_marked_pki(tmp_path)
    monkeypatch.setenv("CARD_CA_BUNDLE_PATH", str(bundle))
    monkeypatch.delenv("CARD_DEV_TRUST_TEST_CA", raising=False)
    monkeypatch.setattr(settings, "ANILA_AUTH_MODE", "password")

    sig = base64.b64encode(build_pkcs7(b"n1", key, cert)).decode()
    with pytest.raises(card_auth.CardConfigError) as exc:
        verify_pkcs7_signature(sig, b"n1")
    assert card_auth._DEV_TEST_CA_MARKER_ORG in str(exc.value)


@pytest.mark.unit
def test_dev_test_ca_rejected_when_card_login_is_the_only_way_in(
    tmp_path, monkeypatch, _fresh_anchor_cache
):
    """``ANILA_AUTH_MODE=card-only`` = 內網正式部署 → 連開了旗標也不准。

    這是「有人把 dev 的 .env 整份帶上內網」那個情境:旗標跟著複製過去了,
    但 card-only 這個 production 特徵擋得住。
    """
    bundle, key, cert = _dev_marked_pki(tmp_path)
    monkeypatch.setenv("CARD_CA_BUNDLE_PATH", str(bundle))
    monkeypatch.setenv("CARD_DEV_TRUST_TEST_CA", "1")
    monkeypatch.setattr(settings, "ANILA_AUTH_MODE", "card-only")

    sig = base64.b64encode(build_pkcs7(b"n1", key, cert)).decode()
    with pytest.raises(card_auth.CardConfigError) as exc:
        verify_pkcs7_signature(sig, b"n1")
    assert "ANILA_AUTH_MODE=card-only" in str(exc.value)


@pytest.mark.unit
def test_dev_test_ca_accepted_on_a_dev_machine(
    tmp_path, monkeypatch, _fresh_anchor_cache
):
    """兩個條件都成立 → 本機刷得進去,而且 nonce 綁定照樣行使。

    fence 只擋 production,不能把開發機也一起擋死 —— 上一版本機刷不了卡,
    整條登入路徑沒人走得完,就是這樣來的。
    """
    bundle, key, cert = _dev_marked_pki(tmp_path)
    monkeypatch.setenv("CARD_CA_BUNDLE_PATH", str(bundle))
    monkeypatch.setenv("CARD_DEV_TRUST_TEST_CA", "1")
    monkeypatch.setattr(settings, "ANILA_AUTH_MODE", "password")

    claims = verify_pkcs7_signature(
        base64.b64encode(build_pkcs7(b"the-real-nonce", key, cert)).decode(),
        b"the-real-nonce",
    )
    assert claims.employee_id == "9999999"

    # 換一條 nonce 就必須被擋 —— 證明 fence 放行的是「換信任錨」,
    # 不是「連 nonce 綁定也一起放掉」。
    with pytest.raises(InvalidSignatureError):
        verify_pkcs7_signature(
            base64.b64encode(build_pkcs7(b"the-real-nonce", key, cert)).decode(),
            b"a-different-nonce",
        )
