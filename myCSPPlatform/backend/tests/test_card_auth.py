"""card_auth.py 的單元測試。

測試素材是 ``cht/app.py:18`` 的 mock 簽章（鄒惠翔測試卡）。這份 signature
是同事 mock 在 ``cht/`` flask app 內寫死的 base64 PKCS#7 SignedData，所有
開發者刷 mock PIN=``123456`` 都會拿到同一份。

測試只覆蓋 LOOSE mode（dev 用）；STRICT mode 等依賴升級後再加。
"""
from __future__ import annotations

import pytest

from app.services.card_auth import (
    CardAuthError,
    CardClaims,
    InvalidSignatureError,
    VerifyMode,
    verify_pkcs7_signature,
)


# 來源：cht/app.py:18 的 "signature" 欄位（鄒惠翔測試卡的 PKCS#7 簽章）。
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


@pytest.mark.unit
class TestVerifyLoose:
    """LOOSE mode — 信任 mock，純抽 cert claims。"""

    def test_returns_card_claims(self) -> None:
        result = verify_pkcs7_signature(
            signature_b64=MOCK_SIGNATURE_B64,
            expected_tbs="any-nonce",
            mode=VerifyMode.LOOSE,
        )
        assert isinstance(result, CardClaims)

    def test_employee_id_from_subject_serial_number(self) -> None:
        claims = verify_pkcs7_signature(
            MOCK_SIGNATURE_B64, "nonce", mode=VerifyMode.LOOSE
        )
        assert claims.employee_id == "1090868"

    def test_display_name_from_common_name(self) -> None:
        claims = verify_pkcs7_signature(
            MOCK_SIGNATURE_B64, "nonce", mode=VerifyMode.LOOSE
        )
        assert claims.display_name == "鄒惠翔"

    def test_email_from_san_rfc822(self) -> None:
        claims = verify_pkcs7_signature(
            MOCK_SIGNATURE_B64, "nonce", mode=VerifyMode.LOOSE
        )
        assert claims.email == "C95THS@ncsist.org.tw"

    def test_card_serial_propagated_when_supplied(self) -> None:
        claims = verify_pkcs7_signature(
            MOCK_SIGNATURE_B64,
            "nonce",
            mode=VerifyMode.LOOSE,
            card_serial="CS00000000025247",
        )
        assert claims.card_serial == "CS00000000025247"

    def test_card_serial_none_when_omitted(self) -> None:
        claims = verify_pkcs7_signature(
            MOCK_SIGNATURE_B64, "nonce", mode=VerifyMode.LOOSE
        )
        assert claims.card_serial is None

    def test_claims_are_immutable(self) -> None:
        claims = verify_pkcs7_signature(
            MOCK_SIGNATURE_B64, "nonce", mode=VerifyMode.LOOSE
        )
        with pytest.raises((AttributeError, Exception)):
            claims.employee_id = "9999999"  # type: ignore[misc]


@pytest.mark.unit
class TestInvalidInput:
    def test_invalid_base64_raises(self) -> None:
        with pytest.raises(InvalidSignatureError):
            verify_pkcs7_signature("not-base64!!!", "nonce", mode=VerifyMode.LOOSE)

    def test_empty_string_raises(self) -> None:
        with pytest.raises(InvalidSignatureError):
            verify_pkcs7_signature("", "nonce", mode=VerifyMode.LOOSE)

    def test_random_bytes_raise(self) -> None:
        # Valid base64 但不是 PKCS#7 DER
        with pytest.raises(InvalidSignatureError):
            verify_pkcs7_signature("aGVsbG8gd29ybGQ=", "nonce", mode=VerifyMode.LOOSE)


@pytest.mark.unit
class TestStrictMode:
    def test_strict_mode_not_implemented_yet(self) -> None:
        # 等 cryptography 升級到 ≥ 43 才實作；目前 raise 確保不會誤開到 prod。
        with pytest.raises(NotImplementedError):
            verify_pkcs7_signature(
                MOCK_SIGNATURE_B64, "nonce", mode=VerifyMode.STRICT
            )


@pytest.mark.unit
def test_error_hierarchy() -> None:
    # 所有錯誤都應該繼承自 CardAuthError，方便上層 endpoint 統一捕捉。
    assert issubclass(InvalidSignatureError, CardAuthError)
