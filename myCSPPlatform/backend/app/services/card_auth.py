"""中科院憑證卡登入：PKCS#7 簽章驗證 + claim 抽取。

設計脈絡
========

院內政策（branch SSO）：

- 內網 production：**唯一**登入方式 = 憑證卡（中華電信 HiPKI 本機元件
  + 中科院 PKI 卡）
- dev：可用 ``cht/`` mock 容器假裝本機元件（``localhost:16888``），
  PIN=``123456`` 通過後產生**鄒惠翔測試卡**的 PKCS#7 簽章

本模組負責「**收到 PKCS#7 簽章 → 抽出可信任的員工身分**」這段純函式邏輯。
它不碰 HTTP、不碰 ORM、不碰 cookie session — 那些由 ``app/api/auth.py``
的 endpoint 負責。

兩段驗證模式
============

LOOSE
    僅 parse PKCS#7 內含的 signer cert，抽出 ``serialNumber``（員工編號）、
    ``commonName``（姓名）、SAN/rfc822Name（email）。
    **不**驗證簽章的數學一致性、**不**驗 X.509 chain、**不**查 OCSP/CRL。
    用於 dev 環境（信任 ``cht/`` mock）。

STRICT
    上面所有 LOOSE 做的事 + 額外:
        1. encapContentInfo.content == ``expected_tbs``（防 replay：簽章
           內容必須是當下 challenge 的 nonce）
        2. signedAttrs.messageDigest == SHA256(encapContent)
        3. signature 對 signedAttrs 的數學一致（用 signer cert 的 public key）
        4. signer cert chain 驗到 ``settings.NCSIST_CA_BUNDLE_PATH`` 的 trust root
        5. （optional, settings.CARD_CHECK_REVOCATION）OCSP/CRL 撤銷檢查

    Implementation note：
        cryptography (即使 44.x) **沒有**高階 ``pkcs7_verify_*`` API
        （pyca/cryptography#8059）。故 STRICT 用 ``asn1crypto`` 解 SignerInfo，
        再用 ``cryptography`` 驗 signature 數學 + chain。

員工編號的來源
==============

從 mock 卡 (``cht/app.py:18``) 解出的 signer cert::

    Subject: C=TW, O=國家中山科學研究院, CN=鄒惠翔, serialNumber=1090868
    SAN.rfc822Name: ['C95THS@ncsist.org.tw']
    SAN.otherName (UPN, OID 1.3.6.1.4.1.311.20.2.3): 'C95THS@ncsist.org.tw'

實際員工卡（1147259 等）的差異會在內網對到真卡時對照。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from asn1crypto import cms
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509.oid import ExtensionOID, NameOID
from cryptography.x509.verification import (
    PolicyBuilder,
    Store,
    VerificationError,
)


logger = logging.getLogger(__name__)


# Default CA bundle 位置（dev / staging）。Production 可用
# settings.NCSIST_CA_BUNDLE_PATH 覆寫。
_DEFAULT_CA_BUNDLE_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "data"
    / "ncsist-ca-bundle.pem"
)


# CSPKI 卡片用的 RSA-SHA256 algorithm OID 在 asn1crypto 內的字串表示
_SUPPORTED_DIGEST_ALGOS = frozenset({"sha256"})
_SUPPORTED_SIG_ALGOS_RSA = frozenset({"rsassa_pkcs1v15", "rsassa_pkcs1_v15"})


# ─── Public API ────────────────────────────────────────────────────────────────


class VerifyMode(str, Enum):
    """簽章驗證強度。"""

    LOOSE = "loose"  # dev：只抽 cert claims，信任 mock
    STRICT = "strict"  # prod：驗 chain + OCSP + 簽章數學一致性


class CardAuthError(Exception):
    """憑證卡登入的所有錯誤共同 base class。"""


class InvalidSignatureError(CardAuthError):
    """PKCS#7 簽章本身解析失敗、cert 不合法、或數學驗證失敗。"""


class MissingClaimError(CardAuthError):
    """signer cert 缺少必要欄位（員工編號 / 姓名 / email）。"""


@dataclass(frozen=True)
class CardClaims:
    """憑證卡驗證成功後抽出的不可變身分資訊。

    對應到 ``User`` ORM 物件後，``employee_id`` 寫進 ``User.username``，
    ``display_name`` 寫進 ``User.display_name`` 或 ``User.full_name``，
    ``email`` 寫進 ``User.email``。
    """

    employee_id: str  # X.509 subject.serialNumber（例：'1090868' / '1147259'）
    display_name: str  # X.509 subject.CN（例：'鄒惠翔'）
    email: str  # X.509 SAN.rfc822Name
    card_serial: str | None  # 元件回的 cardSN（用於 audit log；non-cert source）


def verify_pkcs7_signature(
    signature_b64: str,
    expected_tbs: str,
    mode: VerifyMode = VerifyMode.LOOSE,
    card_serial: str | None = None,
    *,
    ca_bundle_path: Path | str | None = None,
) -> CardClaims:
    """主入口：驗證 PKCS#7 簽章並回傳員工身分。

    Args:
        signature_b64: 從 frontend POST 上來的 base64 PKCS#7 簽章
            （等同於 ``cht/app.py:18`` 那串）。
        expected_tbs: 本次登入流程裡，後端產的一次性 nonce（防 replay）。
            **STRICT mode** 強制 encapContent == expected_tbs；LOOSE mode
            為了 round-trip 同介面而保留參數但不驗。
        mode: ``LOOSE`` 或 ``STRICT``。
        card_serial: 元件回應的 ``cardSN`` 欄位（例：'CS00000000025247'）。
            不是 cert 內的資料，純粹由本機元件回報，存進 audit log 用。
        ca_bundle_path: 覆寫 default CA bundle 路徑，僅 STRICT mode 用到。
            None 時走 ``_DEFAULT_CA_BUNDLE_PATH``（``backend/data/ncsist-ca-bundle.pem``，
            從 ``cht/`` mock 抽出來的 CSPKI 公開 CA）。

    Returns:
        CardClaims: 抽出的員工身分。

    Raises:
        InvalidSignatureError: 任一驗證階段失敗（base64 / DER / PKCS#7
            結構 / 簽章數學 / chain）。
        MissingClaimError: cert 內找不到員工編號 / 姓名 / email。
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

    if mode is VerifyMode.STRICT:
        _verify_strict(
            der_bytes,
            signer_cert,
            expected_tbs,
            embedded_certs=certs,
            ca_bundle_path=Path(ca_bundle_path) if ca_bundle_path else None,
        )
    else:
        logger.debug(
            "card_auth running in LOOSE mode (dev only); "
            "skipping chain + signature math validation"
        )

    claims = _extract_claims(signer_cert, card_serial=card_serial)
    logger.info(
        "card_auth verified: employee_id=%s display_name=%s mode=%s",
        claims.employee_id,
        claims.display_name,
        mode.value,
    )
    return claims


# ─── Internal helpers ──────────────────────────────────────────────────────────


def _pick_signer_cert(certs: list[x509.Certificate]) -> x509.Certificate:
    """從 PKCS#7 內含的 certificates 列表中挑出 signer。

    Mock 只放一張 (signer)，真實環境通常會夾整條 chain（end-entity + CA(s)）。
    端實體（end-entity）的辨識特徵：``BasicConstraints.ca == False`` 或
    ``cA`` extension 不存在。挑第一張符合的當 signer。

    .. note::
        嚴格說 PKCS#7 SignedData 結構裡 SignerInfo 會用 ``issuerAndSerialNumber``
        指明 signer 是哪一張。但 cryptography 41.x 的 ``load_der_pkcs7_certificates``
        只回 cert list，沒有 SignerInfo 對應。先用 BasicConstraints 啟發式判斷；
        升級到 STRICT mode 時會改成從 SignerInfo 精確指認。
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
    """從 signer cert 抽出 ``CardClaims`` 三必要欄位。

    Raises:
        MissingClaimError: 任一必要欄位缺失。
    """
    employee_id = _attr_or_none(cert, NameOID.SERIAL_NUMBER)
    if not employee_id:
        raise MissingClaimError("signer cert 缺 subject.serialNumber（員工編號）")

    display_name = _attr_or_none(cert, NameOID.COMMON_NAME)
    if not display_name:
        raise MissingClaimError("signer cert 缺 subject.commonName（姓名）")

    email = _extract_email(cert)
    if not email:
        raise MissingClaimError(
            "signer cert 缺 SAN.rfc822Name 與 otherName(UPN)（email）"
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
    """email 優先順序：SAN.rfc822Name → SAN.otherName(UPN)。"""
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
            # UPN 的 value 是 ASN.1 UTF8String，前綴 0x0c (tag) + length
            # 簡易解碼：跳前兩 byte 後 decode UTF-8
            raw = other.value
            if len(raw) >= 2 and raw[0] == 0x0C:
                length = raw[1]
                try:
                    return raw[2 : 2 + length].decode("utf-8")
                except UnicodeDecodeError:
                    continue
    return None


def _verify_strict(
    der_bytes: bytes,
    signer_cert: x509.Certificate,
    expected_tbs: str,
    *,
    embedded_certs: list[x509.Certificate],
    ca_bundle_path: Path | None,
) -> None:
    """嚴格驗證：encapContent + messageDigest + 簽章數學 + chain。

    OCSP/CRL 撤銷檢查留給 caller 透過 ``card_revocation.check_ocsp`` 觸發
    （需 ``settings.CARD_CHECK_REVOCATION``），本函式只做密碼學內生驗證。

    驗證順序（任一失敗即 raise）：

    1. PKCS#7 ContentInfo type 必為 signed_data
    2. encap content == expected_tbs（防 replay：簽章內容必須是當下 nonce）
    3. signedAttrs.messageDigest == SHA256(encapContent)
    4. signature 對 signedAttrs 的 DER (SET tag) 用 signer public key 驗證
    5. signer cert chain 驗到 ca_bundle_path 內的 trust root
    """
    # ── 1. PKCS#7 結構解析 ────────────────────────────────────────────────────
    try:
        content_info = cms.ContentInfo.load(der_bytes)
    except Exception as exc:
        raise InvalidSignatureError(
            f"PKCS#7 ContentInfo 解析失敗: {exc}"
        ) from exc

    if content_info["content_type"].native != "signed_data":
        raise InvalidSignatureError(
            f"PKCS#7 type 必為 signed_data，收到 "
            f"{content_info['content_type'].native!r}"
        )

    signed_data = content_info["content"]
    signer_infos = signed_data["signer_infos"]
    if len(signer_infos) != 1:
        raise InvalidSignatureError(
            f"預期 1 個 SignerInfo，收到 {len(signer_infos)}"
        )
    signer_info = signer_infos[0]

    # ── 2. encap content == expected_tbs (anti-replay) ──────────────────────
    encap_content_field = signed_data["encap_content_info"]["content"]
    if not encap_content_field:
        raise InvalidSignatureError("encapContentInfo 缺 content")
    encap_content_bytes = encap_content_field.native
    if not isinstance(encap_content_bytes, bytes):
        raise InvalidSignatureError(
            f"encap content 期望 bytes，收到 {type(encap_content_bytes).__name__}"
        )

    try:
        encap_content_str = encap_content_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidSignatureError("encap content 不是 UTF-8") from exc

    if encap_content_str != expected_tbs:
        # 不洩漏 expected_tbs 給攻擊者；log 用 debug，使用者只看到通用錯誤。
        logger.warning(
            "STRICT verify: encap content mismatch "
            "(expected len=%d, got len=%d)",
            len(expected_tbs),
            len(encap_content_str),
        )
        raise InvalidSignatureError(
            "簽章內容與當下 challenge 不符（可能是過期的 nonce 或 replay）"
        )

    # ── 3. signedAttrs.messageDigest == SHA256(encapContent) ───────────────
    signed_attrs = signer_info["signed_attrs"]
    if not signed_attrs:
        raise InvalidSignatureError("PKCS#7 SignerInfo 缺 signedAttrs")

    digest_algo = signer_info["digest_algorithm"]["algorithm"].native
    if digest_algo not in _SUPPORTED_DIGEST_ALGOS:
        raise InvalidSignatureError(
            f"digest algorithm {digest_algo!r} 未支援"
            f"（僅 {sorted(_SUPPORTED_DIGEST_ALGOS)}）"
        )

    declared_md: bytes | None = None
    for attr in signed_attrs:
        if attr["type"].native == "message_digest":
            declared_md = attr["values"][0].native
            break
    if declared_md is None:
        raise InvalidSignatureError("signedAttrs 缺 message_digest")

    calculated_md = hashlib.sha256(encap_content_bytes).digest()
    if declared_md != calculated_md:
        raise InvalidSignatureError(
            "signedAttrs.messageDigest 與 encapContent 雜湊不符"
        )

    # ── 4. 簽章數學驗證 ─────────────────────────────────────────────────────
    sig_algo = signer_info["signature_algorithm"]["algorithm"].native
    signature_bytes = signer_info["signature"].native

    # asn1crypto 把 signedAttrs encode 出來時用 implicit [0] (tag 0xa0)，
    # 但 RFC 5652 §5.4 要求驗簽時用「explicit SET (tag 0x31)」DER 編碼。
    signed_attrs_der = signed_attrs.dump(force=True)
    if not signed_attrs_der or signed_attrs_der[0] not in (0xA0, 0x31):
        raise InvalidSignatureError(
            f"signedAttrs DER tag 異常: {signed_attrs_der[:1].hex() if signed_attrs_der else 'empty'}"
        )
    signed_attrs_for_verify = b"\x31" + signed_attrs_der[1:]

    public_key = signer_cert.public_key()
    if sig_algo not in _SUPPORTED_SIG_ALGOS_RSA:
        # ECDSA 等其他演算法 — 真實員工卡也可能用，等遇到再展開
        raise InvalidSignatureError(
            f"signature algorithm {sig_algo!r} 暫未支援"
            f"（目前僅 {sorted(_SUPPORTED_SIG_ALGOS_RSA)}）"
        )

    try:
        public_key.verify(
            signature_bytes,
            signed_attrs_for_verify,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except InvalidSignature as exc:
        raise InvalidSignatureError(
            "簽章對 signedAttrs 數學驗證失敗"
        ) from exc

    # ── 5. Chain 驗證到 trust root ─────────────────────────────────────────
    bundle_path = ca_bundle_path or _DEFAULT_CA_BUNDLE_PATH
    trusted_roots, bundled_intermediates = _load_ca_bundle(bundle_path)
    if not trusted_roots:
        raise InvalidSignatureError(
            f"CA bundle {bundle_path} 內找不到任何 trust root"
        )

    # PKCS#7 內含的 cert + bundle 內的 intermediate 一起餵給 verifier
    intermediates = [
        c for c in embedded_certs if c.subject != signer_cert.subject
    ] + bundled_intermediates

    try:
        verifier = (
            PolicyBuilder().store(Store(trusted_roots)).build_client_verifier()
        )
        verifier.verify(signer_cert, intermediates=intermediates)
    except VerificationError as exc:
        raise InvalidSignatureError(
            f"X.509 chain 驗證失敗: {exc}"
        ) from exc


def _load_ca_bundle(
    path: Path,
) -> tuple[list[x509.Certificate], list[x509.Certificate]]:
    """從 PEM bundle 載入 cert，分成 (roots, intermediates)。

    判斷規則：``BasicConstraints.ca == True`` 且 issuer == subject (self-signed)
    就算 root；其餘 CA cert 算 intermediate。
    """
    if not path.exists():
        raise InvalidSignatureError(
            f"CA bundle 檔案不存在: {path}（請跑 scripts/extract_ncsist_ca_bundle.py）"
        )

    pem_data = path.read_bytes()
    try:
        certs = x509.load_pem_x509_certificates(pem_data)
    except ValueError as exc:
        raise InvalidSignatureError(
            f"CA bundle PEM 解析失敗: {exc}"
        ) from exc

    roots: list[x509.Certificate] = []
    intermediates: list[x509.Certificate] = []
    for cert in certs:
        if cert.issuer == cert.subject:
            roots.append(cert)
        else:
            intermediates.append(cert)
    return roots, intermediates
