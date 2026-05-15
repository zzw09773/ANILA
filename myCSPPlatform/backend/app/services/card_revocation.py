"""中科院憑證卡 OCSP 撤銷檢查 client（branch SSO STRICT mode 配套）。

設計要點
========

- 純函式：``check_ocsp(signer_cert, issuer_cert)``，不碰 settings / DB / HTTP
  以外的 side effect，方便 unit test 用 monkeypatch 替換 ``_post_ocsp_request``。
- 預設 **不啟用**：``settings.CARD_CHECK_REVOCATION=False`` 時 caller 不會
  呼叫本模組；外網 dev 環境因 ``ocsp.ncsist.org.tw`` DNS NXDOMAIN 而打不到，
  所以只在內網 production 才開。
- Responder URL 三段優先序：
    1. caller 顯式傳入的 ``responder_url``（測試 / override）
    2. signer cert 的 AIA extension 內的 OCSP URL（標準）
    3. raise ``OcspUnavailableError`` 走 caller 的 fail-open / fail-close 政策

行為對應
========

OCSP responder 回傳:
- ``good``     → return（None）
- ``revoked``  → raise ``CertificateRevokedError``
- ``unknown``  → raise ``OcspUnknownError``（policy 自決：production 通常 fail-close）

連線 / 解析失敗 → raise ``OcspUnavailableError``（policy 自決）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509 import ocsp
from cryptography.x509.oid import AuthorityInformationAccessOID, ExtensionOID


logger = logging.getLogger(__name__)


_OCSP_HTTP_TIMEOUT_SECONDS = 5.0
_OCSP_REQUEST_HEADERS = {
    "Content-Type": "application/ocsp-request",
    "Accept": "application/ocsp-response",
}


# ─── Public exceptions ─────────────────────────────────────────────────────────


class RevocationCheckError(Exception):
    """OCSP 檢查層的 base error。"""


class OcspUnavailableError(RevocationCheckError):
    """無法取得 OCSP 結果（responder 連不上 / cert 缺 AIA / 解析失敗）。"""


class OcspUnknownError(RevocationCheckError):
    """OCSP responder 回 ``unknown`` — 對該 cert 沒有狀態紀錄。"""


class CertificateRevokedError(RevocationCheckError):
    """OCSP responder 回 ``revoked`` — cert 已被撤銷。"""

    def __init__(self, message: str, revoked_at: datetime | None = None):
        super().__init__(message)
        self.revoked_at = revoked_at


# ─── Public API ────────────────────────────────────────────────────────────────


HttpPoster = Callable[[str, bytes], bytes]
"""可注入的 HTTP POST，方便 test mock（避免真的打網路）。

簽章：``(url, body) -> response_body``。預設由 ``_post_ocsp_request`` 提供。
"""


def check_ocsp(
    signer_cert: x509.Certificate,
    issuer_cert: x509.Certificate,
    *,
    responder_url: str | None = None,
    http_post: HttpPoster | None = None,
) -> None:
    """對 signer_cert 跑 OCSP，正常無 raise 即代表 ``good``。

    Args:
        signer_cert: 被檢查的 cert（end-entity，例：員工卡 signer）。
        issuer_cert: signer 的直接簽發者（例：CSPKI Intermediate CA）。
        responder_url: 覆寫 responder URL。None 時自動從 signer cert AIA 抽。
        http_post: 注入的 HTTP poster（test 用）；None 時用預設 httpx 實作。

    Raises:
        OcspUnavailableError: responder URL 找不到 / 連線失敗 / 解析失敗。
        OcspUnknownError: responder 回 unknown。
        CertificateRevokedError: responder 回 revoked。
    """
    url = responder_url or _ocsp_responder_url_from_aia(signer_cert)
    if not url:
        raise OcspUnavailableError(
            "signer cert 缺 AIA extension（找不到 OCSP responder URL）"
        )

    request_body = _build_ocsp_request(signer_cert, issuer_cert)
    poster = http_post or _post_ocsp_request

    try:
        response_body = poster(url, request_body)
    except httpx.HTTPError as exc:
        raise OcspUnavailableError(
            f"OCSP responder {url} 連線失敗: {exc}"
        ) from exc

    try:
        response = ocsp.load_der_ocsp_response(response_body)
    except ValueError as exc:
        raise OcspUnavailableError(
            f"OCSP response DER 解析失敗: {exc}"
        ) from exc

    if response.response_status != ocsp.OCSPResponseStatus.SUCCESSFUL:
        raise OcspUnavailableError(
            f"OCSP responder 回 status={response.response_status.name}"
        )

    cert_status = response.certificate_status
    if cert_status == ocsp.OCSPCertStatus.GOOD:
        logger.debug(
            "OCSP good for cert serial=%s",
            f"{signer_cert.serial_number:x}",
        )
        return
    if cert_status == ocsp.OCSPCertStatus.REVOKED:
        raise CertificateRevokedError(
            f"cert serial={signer_cert.serial_number:x} 已被撤銷",
            revoked_at=_revocation_time(response),
        )
    if cert_status == ocsp.OCSPCertStatus.UNKNOWN:
        raise OcspUnknownError(
            f"OCSP responder 對 cert serial={signer_cert.serial_number:x} "
            "回 unknown — 不在發證單位 DB 內"
        )
    raise OcspUnavailableError(
        f"未預期的 OCSP cert status: {cert_status!r}"
    )


# ─── Internal helpers ──────────────────────────────────────────────────────────


def _ocsp_responder_url_from_aia(cert: x509.Certificate) -> str | None:
    """從 cert 的 Authority Information Access extension 抽 OCSP responder URL。"""
    try:
        aia_ext = cert.extensions.get_extension_for_oid(
            ExtensionOID.AUTHORITY_INFORMATION_ACCESS
        )
    except x509.ExtensionNotFound:
        return None

    for desc in aia_ext.value:
        if desc.access_method == AuthorityInformationAccessOID.OCSP:
            location = desc.access_location
            if isinstance(location, x509.UniformResourceIdentifier):
                return location.value
    return None


def _build_ocsp_request(
    signer_cert: x509.Certificate, issuer_cert: x509.Certificate
) -> bytes:
    """構建 OCSP request DER bytes。CSPKI 用 SHA1 hash key/name (RFC 6960 default)。"""
    builder = ocsp.OCSPRequestBuilder()
    builder = builder.add_certificate(signer_cert, issuer_cert, hashes.SHA1())
    return builder.build().public_bytes(Encoding.DER)


def _post_ocsp_request(url: str, body: bytes) -> bytes:
    """預設 HTTP poster — 同步 httpx 5s timeout。"""
    with httpx.Client(timeout=_OCSP_HTTP_TIMEOUT_SECONDS) as client:
        resp = client.post(url, content=body, headers=_OCSP_REQUEST_HEADERS)
    resp.raise_for_status()
    return resp.content


def _revocation_time(response: ocsp.OCSPResponse) -> datetime | None:
    """從 OCSP response 抽撤銷時間（若有）。"""
    try:
        rt = response.revocation_time_utc
    except AttributeError:
        rt = response.revocation_time  # cryptography 41.x fallback
        if rt is not None and rt.tzinfo is None:
            rt = rt.replace(tzinfo=timezone.utc)
    return rt
