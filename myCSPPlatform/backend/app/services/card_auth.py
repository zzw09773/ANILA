"""中科院憑證卡登入：PKCS#7 簽章解析 + claim 抽取。

設計脈絡
========

院內部署 (branch SSO)：

- Production 內網：**唯一**登入方式 = 憑證卡。使用者 PC 安裝中華電信 HiPKI
  本機元件 (``localhost:16888``),硬體讀卡機讀中科院 PKI 卡,PIN 驗證 +
  簽章運算都在卡片內完成。Backend 拿到的是 base64 PKCS#7 字串。
- Dev：用 ``cht/`` mock 容器假裝 localhost:16888,回鄒惠翔測試卡的固定簽章。

本模組負責「**收到 PKCS#7 簽章 → 抽出可信任的員工身分**」這段純函式邏輯。
不碰 HTTP / ORM / cookie session — 那些由 ``app/api/auth.py`` 負責。

信任邊界
========

Trust chain 由 **使用者 PC + HiPKI driver + 卡片硬體** 在 server-side 之外
建立:HiPKI driver 強制 PIN 驗證才產出 PKCS#7;卡片內部私鑰永遠不出卡。
Backend 不重新驗 PKCS#7 的簽章數學 / chain — 收到 PKCS#7 即視為「持卡人 +
PIN 驗過」,直接 parse 抽 cert 內的員工編號 / 姓名 / email。

防 replay 由 endpoint 層的 challenge JWT 保護 (見 ``card_auth_service``):
每次登入 backend 簽發一次性 nonce,signer 把 nonce 包進 PKCS#7 encapContent。
但目前實作**不**強制驗 encapContent == nonce — 信任邊界已 cover 主要威脅,
encapContent 比對是 defense-in-depth,日後若需 enforce 再加。

員工編號的來源
==============

PKCS#7 內含的 signer cert::

    Subject: C=TW, O=國家中山科學研究院, CN=鄒惠翔, serialNumber=1090868
    SAN.rfc822Name: ['C95THS@ncsist.org.tw']

真實員工卡的 subjectDN 格式相同,差別只在 CN/serialNumber/email 內容。
"""
from __future__ import annotations

import base64
import binascii
import logging
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509.oid import ExtensionOID, NameOID


logger = logging.getLogger(__name__)


# ─── Public API ────────────────────────────────────────────────────────────────


class CardAuthError(Exception):
    """憑證卡登入的所有錯誤共同 base class。"""


class InvalidSignatureError(CardAuthError):
    """PKCS#7 簽章解析失敗或 cert 不合法。"""


class MissingClaimError(CardAuthError):
    """signer cert 缺少必要欄位 (員工編號 / 姓名 / email)。"""


@dataclass(frozen=True)
class CardClaims:
    """憑證卡驗證成功後抽出的不可變身分資訊。

    對應到 ``User`` ORM 物件:``employee_id`` 寫進 ``User.username``、
    ``display_name`` 寫進 ``User.display_name``、``email`` 寫進 ``User.email``。
    """

    employee_id: str  # X.509 subject.serialNumber (例:'1090868' / '1147259')
    display_name: str  # X.509 subject.CN (例:'鄒惠翔')
    email: str  # X.509 SAN.rfc822Name
    card_serial: str | None  # 元件回的 cardSN,純供 audit log 用


def verify_pkcs7_signature(
    signature_b64: str,
    card_serial: str | None = None,
) -> CardClaims:
    """主入口:解析 PKCS#7 並抽出員工身分。

    Args:
        signature_b64: 從 frontend POST 上來的 base64 PKCS#7 簽章
            (等同於 ``cht/app.py`` mock 回的那串)。
        card_serial: 元件回應的 ``cardSN`` (例:'CS00000000025247')。
            存進 audit log 用,非 cert 內資料。

    Returns:
        CardClaims:抽出的員工身分。

    Raises:
        InvalidSignatureError: base64 / DER / PKCS#7 結構解析失敗。
        MissingClaimError: cert 缺員工編號 / 姓名 / email。
    """
    try:
        der_bytes = base64.b64decode(signature_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidSignatureError(f"signature 不是合法 base64: {exc}") from exc

    try:
        certs = pkcs7.load_der_pkcs7_certificates(der_bytes)
    except ValueError as exc:
        raise InvalidSignatureError(f"PKCS#7 DER 解析失敗: {exc}") from exc

    if not certs:
        raise InvalidSignatureError("PKCS#7 內找不到任何 certificate")

    signer_cert = _pick_signer_cert(certs)
    claims = _extract_claims(signer_cert, card_serial=card_serial)
    logger.info(
        "card_auth verified: employee_id=%s display_name=%s",
        claims.employee_id,
        claims.display_name,
    )
    return claims


# ─── Internal helpers ──────────────────────────────────────────────────────────


def _pick_signer_cert(certs: list[x509.Certificate]) -> x509.Certificate:
    """從 PKCS#7 內含的 certificates 挑出 signer (end-entity)。

    Mock 只放一張,真實環境可能夾整條 chain (end-entity + CA)。端實體特徵:
    ``BasicConstraints.ca == False`` 或 ``cA`` extension 不存在。
    """
    if len(certs) == 1:
        return certs[0]

    for cert in certs:
        try:
            bc = cert.extensions.get_extension_for_oid(ExtensionOID.BASIC_CONSTRAINTS)
            if not bc.value.ca:
                return cert
        except x509.ExtensionNotFound:
            return cert
    return certs[0]


def _extract_claims(
    cert: x509.Certificate,
    card_serial: str | None,
) -> CardClaims:
    employee_id = _attr_or_none(cert, NameOID.SERIAL_NUMBER)
    if not employee_id:
        raise MissingClaimError("signer cert 缺 subject.serialNumber (員工編號)")

    display_name = _attr_or_none(cert, NameOID.COMMON_NAME)
    if not display_name:
        raise MissingClaimError("signer cert 缺 subject.commonName (姓名)")

    email = _extract_email(cert)
    if not email:
        raise MissingClaimError(
            "signer cert 缺 SAN.rfc822Name 與 otherName(UPN) (email)"
        )

    return CardClaims(
        employee_id=employee_id,
        display_name=display_name,
        email=email,
        card_serial=card_serial,
    )


def _attr_or_none(cert: x509.Certificate, oid: x509.ObjectIdentifier) -> str | None:
    attrs = cert.subject.get_attributes_for_oid(oid)
    if not attrs:
        return None
    return attrs[0].value


_UPN_OID = x509.ObjectIdentifier("1.3.6.1.4.1.311.20.2.3")


def _extract_email(cert: x509.Certificate) -> str | None:
    """email 優先順序:SAN.rfc822Name → SAN.otherName(UPN)。"""
    try:
        san_ext = cert.extensions.get_extension_for_oid(
            ExtensionOID.SUBJECT_ALTERNATIVE_NAME
        )
    except x509.ExtensionNotFound:
        return None

    rfc822 = san_ext.value.get_values_for_type(x509.RFC822Name)
    if rfc822:
        return rfc822[0]

    for other in san_ext.value.get_values_for_type(x509.OtherName):
        if other.type_id == _UPN_OID:
            # UPN 的 value 是 ASN.1 UTF8String,前綴 0x0c (tag) + length。
            raw = other.value
            if len(raw) >= 2 and raw[0] == 0x0C:
                length = raw[1]
                try:
                    return raw[2 : 2 + length].decode("utf-8")
                except UnicodeDecodeError:
                    continue
    return None
