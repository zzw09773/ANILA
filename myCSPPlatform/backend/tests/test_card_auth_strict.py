"""``card_auth.verify_pkcs7_signature(mode=STRICT)`` 與 OCSP client 單元測試。

測試素材跟 LOOSE mode 相同：``cht/app.py:18`` 的鄒惠翔測試卡 PKCS#7 簽章。
這份簽章的 encapContent 是固定字串 ``"TBS"``（mock 寫死），所以：

- LOOSE mode：``expected_tbs`` 任意都通過（不驗）。
- STRICT mode：必須傳 ``expected_tbs="TBS"`` 才通過 — 驗 encapContent
  跟簽章數學的數學一致性。

Trust root：本測試用 ``backend/data/ncsist-ca-bundle.pem``（從
``scripts/extract_ncsist_ca_bundle.py`` 抽 mock pkcs11info 的 CSPKI
Root + Intermediate 產出），所以 chain 也驗得到 trust anchor。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from app.services.card_auth import (
    InvalidSignatureError,
    VerifyMode,
    verify_pkcs7_signature,
)
from app.services.card_revocation import (
    CertificateRevokedError,
    OcspUnavailableError,
    OcspUnknownError,
    check_ocsp,
)

from tests.test_card_auth import MOCK_SIGNATURE_B64


# Mock signature 內 encapContent 是固定字串 "TBS"（cht/login.html L67 寫死）。
MOCK_EXPECTED_TBS = "TBS"


@pytest.mark.unit
class TestStrictVerifyHappyPath:
    """STRICT mode 對 mock signature 應該全部通過。"""

    def test_correct_tbs_passes(self) -> None:
        claims = verify_pkcs7_signature(
            MOCK_SIGNATURE_B64,
            expected_tbs=MOCK_EXPECTED_TBS,
            mode=VerifyMode.STRICT,
        )
        assert claims.employee_id == "1090868"
        assert claims.display_name == "鄒惠翔"
        assert claims.email == "C95THS@ncsist.org.tw"


@pytest.mark.unit
class TestStrictVerifyAntiReplay:
    """STRICT mode 不接受錯的 expected_tbs — 防 replay attack。"""

    def test_wrong_tbs_rejected(self) -> None:
        with pytest.raises(InvalidSignatureError, match="不符|replay"):
            verify_pkcs7_signature(
                MOCK_SIGNATURE_B64,
                expected_tbs="not-the-real-tbs",
                mode=VerifyMode.STRICT,
            )

    def test_empty_tbs_rejected(self) -> None:
        with pytest.raises(InvalidSignatureError):
            verify_pkcs7_signature(
                MOCK_SIGNATURE_B64,
                expected_tbs="",
                mode=VerifyMode.STRICT,
            )

    def test_random_nonce_rejected(self) -> None:
        """模擬正常 challenge 流程：server 產 random nonce，但 mock 用 'TBS' 簽。

        這個 case 是 production 用 cht/ mock 走 STRICT mode 的必然結果 —
        mock 寫死 tbs，搭 random nonce 一定失敗。pinning 為告警：搬到
        production 想用 STRICT 必須換成真卡（或讓 mock 可動態 sign）。
        """
        import secrets
        random_nonce = secrets.token_urlsafe(32)
        with pytest.raises(InvalidSignatureError):
            verify_pkcs7_signature(
                MOCK_SIGNATURE_B64,
                expected_tbs=random_nonce,
                mode=VerifyMode.STRICT,
            )


@pytest.mark.unit
class TestStrictChainValidation:
    """STRICT mode 需要 CA bundle 才能驗 chain。"""

    def test_missing_ca_bundle_raises(self, tmp_path: Path) -> None:
        nonexistent = tmp_path / "does-not-exist.pem"
        with pytest.raises(InvalidSignatureError, match="CA bundle"):
            verify_pkcs7_signature(
                MOCK_SIGNATURE_B64,
                expected_tbs=MOCK_EXPECTED_TBS,
                mode=VerifyMode.STRICT,
                ca_bundle_path=nonexistent,
            )

    def test_empty_ca_bundle_raises(self, tmp_path: Path) -> None:
        empty_bundle = tmp_path / "empty.pem"
        empty_bundle.write_bytes(b"")
        with pytest.raises(InvalidSignatureError):
            verify_pkcs7_signature(
                MOCK_SIGNATURE_B64,
                expected_tbs=MOCK_EXPECTED_TBS,
                mode=VerifyMode.STRICT,
                ca_bundle_path=empty_bundle,
            )

    def test_unrelated_ca_bundle_rejects_chain(self, tmp_path: Path) -> None:
        """用 self-signed unrelated CA 當 trust anchor → chain 應該驗不過。"""
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from datetime import datetime, timedelta, timezone

        # 產一張完全無關的 self-signed CA cert
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name([
            x509.NameAttribute(x509.NameOID.COMMON_NAME, "Unrelated Root"),
        ])
        unrelated_cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256())
        )
        bundle_path = tmp_path / "unrelated.pem"
        bundle_path.write_bytes(
            unrelated_cert.public_bytes(serialization.Encoding.PEM)
        )

        with pytest.raises(InvalidSignatureError, match="chain"):
            verify_pkcs7_signature(
                MOCK_SIGNATURE_B64,
                expected_tbs=MOCK_EXPECTED_TBS,
                mode=VerifyMode.STRICT,
                ca_bundle_path=bundle_path,
            )


@pytest.mark.unit
class TestOcspClient:
    """``check_ocsp`` 對所有 OCSP responder 回應的反應。"""

    def _load_signer_and_issuer(self):
        """從 mock signature + default CA bundle 抽 signer + issuer cert。"""
        import base64
        from cryptography.hazmat.primitives.serialization import pkcs7

        from app.services.card_auth import _DEFAULT_CA_BUNDLE_PATH, _load_ca_bundle

        embedded = pkcs7.load_der_pkcs7_certificates(
            base64.b64decode(MOCK_SIGNATURE_B64)
        )
        signer = embedded[0]
        _, intermediates = _load_ca_bundle(_DEFAULT_CA_BUNDLE_PATH)
        issuer = next(
            c for c in (list(embedded) + intermediates)
            if c.subject == signer.issuer
        )
        return signer, issuer

    def _make_ocsp_response(self, cert_status):
        """產一個假的 OCSP response DER bytes (用 cryptography builder)。"""
        from datetime import datetime, timedelta, timezone
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509 import ocsp

        signer, issuer = self._load_signer_and_issuer()

        # 用一張 throw-away responder cert 簽 OCSP response（test only）
        responder_key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        )
        responder_cert = (
            x509.CertificateBuilder()
            .subject_name(issuer.subject)  # 假裝 responder 就是 issuer
            .issuer_name(issuer.subject)
            .public_key(responder_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(timezone.utc) - timedelta(hours=1))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
            .sign(responder_key, hashes.SHA256())
        )

        builder = ocsp.OCSPResponseBuilder()
        builder = builder.add_response(
            cert=signer,
            issuer=issuer,
            algorithm=hashes.SHA1(),
            cert_status=cert_status,
            this_update=datetime.now(timezone.utc),
            next_update=datetime.now(timezone.utc) + timedelta(hours=1),
            revocation_time=(
                datetime.now(timezone.utc) - timedelta(days=1)
                if cert_status == ocsp.OCSPCertStatus.REVOKED
                else None
            ),
            revocation_reason=None,
        ).responder_id(
            ocsp.OCSPResponderEncoding.HASH, responder_cert
        )
        response = builder.sign(responder_key, hashes.SHA256())
        return response.public_bytes(serialization.Encoding.DER)

    def test_good_response_returns_silently(self) -> None:
        from cryptography.x509 import ocsp
        signer, issuer = self._load_signer_and_issuer()
        ok_response = self._make_ocsp_response(ocsp.OCSPCertStatus.GOOD)

        # 不打網路 — 注入 fake poster
        check_ocsp(
            signer,
            issuer,
            responder_url="http://test/ocsp",
            http_post=lambda url, body: ok_response,
        )  # 沒 raise = pass

    def test_revoked_response_raises(self) -> None:
        from cryptography.x509 import ocsp
        signer, issuer = self._load_signer_and_issuer()
        revoked = self._make_ocsp_response(ocsp.OCSPCertStatus.REVOKED)

        with pytest.raises(CertificateRevokedError):
            check_ocsp(
                signer,
                issuer,
                responder_url="http://test/ocsp",
                http_post=lambda url, body: revoked,
            )

    def test_unknown_response_raises(self) -> None:
        from cryptography.x509 import ocsp
        signer, issuer = self._load_signer_and_issuer()
        unknown = self._make_ocsp_response(ocsp.OCSPCertStatus.UNKNOWN)

        with pytest.raises(OcspUnknownError):
            check_ocsp(
                signer,
                issuer,
                responder_url="http://test/ocsp",
                http_post=lambda url, body: unknown,
            )

    def test_http_failure_becomes_unavailable(self) -> None:
        signer, issuer = self._load_signer_and_issuer()

        def boom(url, body):
            raise httpx.ConnectError("DNS NXDOMAIN")

        with pytest.raises(OcspUnavailableError, match="連線失敗"):
            check_ocsp(
                signer,
                issuer,
                responder_url="http://ocsp.ncsist.org.tw/OCSP",
                http_post=boom,
            )

    def test_garbage_response_becomes_unavailable(self) -> None:
        signer, issuer = self._load_signer_and_issuer()
        with pytest.raises(OcspUnavailableError, match="DER"):
            check_ocsp(
                signer,
                issuer,
                responder_url="http://test/ocsp",
                http_post=lambda url, body: b"not a valid OCSP response",
            )

    def test_aia_extracts_responder_url_when_omitted(self) -> None:
        """signer cert 的 AIA extension 內已經包含 ocsp.ncsist.org.tw URL。"""
        from cryptography.x509 import ocsp
        signer, issuer = self._load_signer_and_issuer()
        ok = self._make_ocsp_response(ocsp.OCSPCertStatus.GOOD)
        captured_url = []

        def capturing_poster(url, body):
            captured_url.append(url)
            return ok

        check_ocsp(signer, issuer, http_post=capturing_poster)
        assert captured_url, "expected http_post to be called"
        assert "ocsp" in captured_url[0].lower()
