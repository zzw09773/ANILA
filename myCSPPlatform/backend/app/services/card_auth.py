"""中科院憑證卡登入：PKCS#7/CMS 簽章「驗證」+ claim 抽取。

設計脈絡
========

院內部署 (branch SSO)：

- Production 內網：**唯一**登入方式 = 憑證卡。使用者 PC 安裝中華電信 HiPKI
  本機元件 (``localhost:16888``),硬體讀卡機讀中科院 PKI 卡,PIN 驗證 +
  簽章運算都在卡片內完成。Backend 拿到的是 base64 PKCS#7 (CMS SignedData)。
- Dev：用 ``cht/`` mock 容器假裝 localhost:16888,回鄒惠翔測試卡的「固定」簽章
  (eContent 永遠是 ``b"TBS"``,mock 無私鑰故無法簽新 nonce)。

信任邊界（2026-06-12 重做）
===========================

**先前版本只 `load_der_pkcs7_certificates` 解析、不驗證**,等於「POST 一張自簽
憑證、serialNumber 填成某 owner 工號就拿到 owner session」= 完全認證繞過。本版
改為伺服器端**真正驗證**,信任錨是釘死的中科院 CSPKI CA bundle:

1. **驗簽章**:CMS SignerInfo 的簽章必須由卡片私鑰對 signedAttrs 簽出,且
   signedAttrs 的 messageDigest == hash(eContent)。
2. **驗憑證鏈**:signer cert 必須一路鏈到釘死的 ``CSPKI Root Certification
   Authority - G1``(經 ``中科院憑證管理中心 - G1`` 中繼),且都在效期內。攻擊者
   自簽的憑證鏈不到我們的 root → 拒絕。
3. **綁 nonce(反 replay)**:eContent 必須 == 本次 challenge 發出的 nonce
   (見 ``card_auth_service``),除非 ``CARD_DEV_SKIP_NONCE_BINDING`` 開啟
   (僅供 dev 用固定 mock 測試;**prod 一律不可開**)。

撤銷檢查(CRL/OCSP)**刻意不做**:院內離職流程實體回收銷毀卡片,加上系統端
``User.is_active``/核准狀態把關,air-gap 下接受「不查線上撤銷」為已知設計。

員工編號的來源
==============

驗證通過後,從 signer cert 的 Subject 取::

    Subject: C=TW, O=國家中山科學研究院, CN=鄒惠翔, serialNumber=1090868
    SAN.rfc822Name: ['C95THS@ncsist.org.tw']

員工編號 = ``serialNumber`` 屬性(非憑證序號)。
"""
from __future__ import annotations

import base64
import binascii
import datetime
import hashlib
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from asn1crypto import cms as asn1_cms
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.x509.oid import ExtensionOID, NameOID


# 中科院員工編號:純數字,目前觀察到 7 digits (例:1147259、1090868);
# 6/8/9 留邊界給歷史與未來 ID schema 變化。用 ``\A...\Z`` 嚴格頭尾。
_EMPLOYEE_ID_RE = re.compile(r"\A\d{6,9}\Z")

# 釘死的信任錨 bundle(CSPKI Root + 中科院憑證管理中心 中繼)。隨碼附帶;
# 可用 CARD_CA_BUNDLE_PATH 覆寫(內網若換 CA 換檔即可,不必改碼)。
_DEFAULT_CA_BUNDLE = Path(__file__).resolve().parent / "cspki_ca_bundle.pem"

# Dev-only:用固定 mock(eContent 永遠 b"TBS",無法簽新 nonce)測試時跳過
# nonce 綁定。**prod 一律不可開**;簽章 + 憑證鏈驗證照常執行。
_SKIP_NONCE_BINDING = os.environ.get("CARD_DEV_SKIP_NONCE_BINDING", "").lower() in (
    "1",
    "true",
    "yes",
)

_HASH_BY_NAME = {
    "sha256": hashes.SHA256,
    "sha384": hashes.SHA384,
    "sha512": hashes.SHA512,
    "sha1": hashes.SHA1,
}

logger = logging.getLogger(__name__)


# ─── Public API ────────────────────────────────────────────────────────────────


class CardAuthError(Exception):
    """憑證卡登入的所有錯誤共同 base class。"""


class InvalidSignatureError(CardAuthError):
    """PKCS#7 簽章/憑證鏈驗證失敗或 cert 不合法。"""


class MissingClaimError(CardAuthError):
    """signer cert 缺少必要欄位 (員工編號 / 姓名 / email)。"""


class CardConfigError(CardAuthError):
    """伺服器端設定錯誤 (CA bundle 缺失/不合法)。fail-closed。"""


@dataclass(frozen=True)
class CardClaims:
    """憑證卡驗證成功後抽出的不可變身分資訊。"""

    employee_id: str  # X.509 subject.serialNumber (例:'1090868' / '1147259')
    display_name: str  # X.509 subject.CN (例:'鄒惠翔')
    email: str  # X.509 SAN.rfc822Name
    card_serial: str | None  # 元件回的 cardSN,純供 audit log 用


def verify_pkcs7_signature(
    signature_b64: str,
    expected_nonce: str | bytes,
    card_serial: str | None = None,
) -> CardClaims:
    """主入口:**驗證** PKCS#7/CMS 簽章 + 憑證鏈 + nonce,通過才抽員工身分。

    Args:
        signature_b64: frontend POST 上來的 base64 PKCS#7 (CMS SignedData)。
        expected_nonce: 本次 challenge 發出的 nonce;必須等於卡片簽的 eContent
            (反 replay)。``CARD_DEV_SKIP_NONCE_BINDING`` 開啟時不比對(dev only)。
        card_serial: 元件回的 ``cardSN``,純 audit log 用。

    Raises:
        InvalidSignatureError: base64/DER/CMS 解析失敗、簽章不對、鏈驗不過、
            nonce 不符、憑證過期。
        MissingClaimError: cert 缺員工編號 / 姓名 / email。
        CardConfigError: CA bundle 設定錯誤。
    """
    try:
        der_bytes = base64.b64decode(signature_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidSignatureError(f"signature 不是合法 base64: {exc}") from exc

    if isinstance(expected_nonce, str):
        expected_nonce = expected_nonce.encode("utf-8")

    try:
        signer_cert = _verify_cms_and_get_signer(der_bytes, expected_nonce)
    except CardAuthError:
        raise  # 我們自己的錯誤(含 CardConfigError)照原樣往上
    except Exception as exc:  # noqa: BLE001 - 不可信 DER:任何非預期解析錯誤都當驗證失敗(401),不可洩成 500
        raise InvalidSignatureError(
            f"CMS 解析/驗證發生非預期錯誤: {type(exc).__name__}"
        ) from exc
    claims = _extract_claims(signer_cert, card_serial=card_serial)
    logger.info(
        "card_auth verified: employee_id=%s display_name=%s",
        claims.employee_id,
        claims.display_name,
    )
    return claims


# ─── CMS verification ───────────────────────────────────────────────────────────


def _verify_cms_and_get_signer(
    der_bytes: bytes, expected_nonce: bytes
) -> x509.Certificate:
    """驗 CMS SignedData(簽章 + 鏈 + nonce),回傳已驗證的 signer 憑證。"""
    anchors, roots = _load_ca_anchors()

    try:
        content_info = asn1_cms.ContentInfo.load(der_bytes)
    except ValueError as exc:
        raise InvalidSignatureError(f"CMS DER 解析失敗: {exc}") from exc

    if content_info["content_type"].native != "signed_data":
        raise InvalidSignatureError("CMS 不是 signed_data")
    signed_data = content_info["content"]

    # eContent (卡片簽的內容) — 反 replay 綁 nonce。
    econtent = signed_data["encap_content_info"]["content"].native
    if econtent is None:
        raise InvalidSignatureError("CMS 缺 eContent(detached 簽章不支援)")
    if isinstance(econtent, str):
        econtent = econtent.encode("utf-8")
    if not _SKIP_NONCE_BINDING and econtent != expected_nonce:
        raise InvalidSignatureError("eContent 不等於本次 challenge 的 nonce(疑似 replay)")

    signer_infos = signed_data["signer_infos"]
    if len(signer_infos) < 1:
        raise InvalidSignatureError("CMS 無 signer_infos")
    signer_info = signer_infos[0]

    # CMS 內含的憑證 → cryptography 物件。
    embedded = []
    for choice in signed_data["certificates"]:
        if choice.name != "certificate":
            continue
        try:
            embedded.append(x509.load_der_x509_certificate(choice.chosen.dump()))
        except ValueError:
            continue
    if not embedded:
        raise InvalidSignatureError("CMS 內找不到 signer 憑證")

    signer_cert = _match_signer_cert(signer_info, embedded)

    # 簽章驗證(對 signedAttrs,並確認 messageDigest == hash(eContent))。
    _verify_signer_info(signer_info, signer_cert, econtent)

    # 憑證鏈:signer → … → 釘死的 CSPKI root,逐層驗簽 + 效期。
    _verify_chain(signer_cert, anchors, roots)

    return signer_cert


def _match_signer_cert(signer_info, embedded: list[x509.Certificate]) -> x509.Certificate:
    """用 SignerInfo.sid 在 CMS 內含憑證中找出 signer。"""
    sid = signer_info["sid"]
    if sid.name == "issuer_and_serial_number":
        want_serial = sid.chosen["serial_number"].native
        for cert in embedded:
            if cert.serial_number == want_serial:
                return cert
        raise InvalidSignatureError("找不到 SignerInfo.sid 對應的 signer 憑證")
    if sid.name == "subject_key_identifier":
        want_ski = sid.chosen.native
        for cert in embedded:
            try:
                ski = cert.extensions.get_extension_for_oid(
                    ExtensionOID.SUBJECT_KEY_IDENTIFIER
                ).value.digest
                if ski == want_ski:
                    return cert
            except x509.ExtensionNotFound:
                continue
        raise InvalidSignatureError("找不到 SKI 對應的 signer 憑證")
    raise InvalidSignatureError(f"不支援的 SignerIdentifier: {sid.name}")


def _verify_signer_info(signer_info, signer_cert: x509.Certificate, econtent: bytes) -> None:
    """驗 SignerInfo 的簽章。有 signedAttrs 時簽章是對 signedAttrs 的 DER(SET OF),
    且 signedAttrs 必含 messageDigest == hash(eContent)。"""
    digest_name = signer_info["digest_algorithm"]["algorithm"].native
    hash_cls = _HASH_BY_NAME.get(digest_name)
    if hash_cls is None:
        raise InvalidSignatureError(f"不支援的 digest 演算法: {digest_name}")

    signed_attrs = signer_info["signed_attrs"]
    if signed_attrs.native is not None:
        message_digest = None
        for attr in signed_attrs:
            if attr["type"].native == "message_digest":
                message_digest = attr["values"][0].native
                break
        if message_digest is None:
            raise InvalidSignatureError("signedAttrs 缺 messageDigest")
        if message_digest != hashlib.new(digest_name, econtent).digest():
            raise InvalidSignatureError("messageDigest != hash(eContent)")
        # 簽章覆蓋的是 signedAttrs 的 SET OF 重新編碼(非 [0] implicit)。
        signed_bytes = signed_attrs.untag().dump()
    else:
        signed_bytes = econtent

    sig_algo = signer_info["signature_algorithm"]["algorithm"].native
    signature = signer_info["signature"].native
    _verify_signature(signer_cert, signature, signed_bytes, hash_cls(), sig_algo)


def _verify_signature(
    cert: x509.Certificate,
    signature: bytes,
    data: bytes,
    hash_algo,
    sig_algo: str,
) -> None:
    """用 signer 公鑰驗 data 的簽章,依公鑰型別 + sig_algo 選 padding。"""
    pub = cert.public_key()
    try:
        if isinstance(pub, rsa.RSAPublicKey):
            if "pss" in (sig_algo or ""):
                pad = padding.PSS(
                    mgf=padding.MGF1(hash_algo), salt_length=padding.PSS.DIGEST_LENGTH
                )
            else:
                pad = padding.PKCS1v15()
            pub.verify(signature, data, pad, hash_algo)
        elif isinstance(pub, ec.EllipticCurvePublicKey):
            pub.verify(signature, data, ec.ECDSA(hash_algo))
        else:
            raise InvalidSignatureError("不支援的 signer 公鑰型別")
    except InvalidSignature as exc:
        raise InvalidSignatureError("CMS 簽章驗證失敗") from exc


# ─── Certificate chain ──────────────────────────────────────────────────────────


def _verify_chain(
    signer: x509.Certificate,
    anchors: dict[bytes, x509.Certificate],
    roots: list[x509.Certificate],
) -> None:
    """從 signer 沿 issuer 連到釘死 bundle 內的自簽 root,逐層驗簽 + 效期。"""
    root_fps = {r.fingerprint(hashes.SHA256()) for r in roots}
    cur = signer
    for _ in range(8):  # 深度上限,防迴圈
        _check_validity(cur)
        if cur.fingerprint(hashes.SHA256()) in root_fps:
            return  # 抵達釘死的 root
        parent = anchors.get(cur.issuer.public_bytes())
        if parent is None:
            raise InvalidSignatureError(
                "憑證鏈無法連到釘死的 CSPKI CA(issuer 不在信任 bundle 內)"
            )
        _verify_cert_signed_by(cur, parent)
        cur = parent
    raise InvalidSignatureError("憑證鏈過深")


def _verify_cert_signed_by(cert: x509.Certificate, issuer: x509.Certificate) -> None:
    pub = issuer.public_key()
    try:
        if isinstance(pub, rsa.RSAPublicKey):
            pub.verify(
                cert.signature,
                cert.tbs_certificate_bytes,
                padding.PKCS1v15(),
                cert.signature_hash_algorithm,
            )
        elif isinstance(pub, ec.EllipticCurvePublicKey):
            pub.verify(
                cert.signature,
                cert.tbs_certificate_bytes,
                ec.ECDSA(cert.signature_hash_algorithm),
            )
        else:
            raise InvalidSignatureError("不支援的 issuer 公鑰型別")
    except InvalidSignature as exc:
        raise InvalidSignatureError("憑證鏈簽章驗證失敗") from exc


def _check_validity(cert: x509.Certificate) -> None:
    now = datetime.datetime.now(datetime.timezone.utc)
    not_before = getattr(cert, "not_valid_before_utc", None) or (
        cert.not_valid_before.replace(tzinfo=datetime.timezone.utc)
    )
    not_after = getattr(cert, "not_valid_after_utc", None) or (
        cert.not_valid_after.replace(tzinfo=datetime.timezone.utc)
    )
    if not (not_before <= now <= not_after):
        raise InvalidSignatureError("憑證不在效期內")


_ca_anchor_cache: tuple[dict[bytes, x509.Certificate], list[x509.Certificate]] | None = None


def _load_ca_anchors() -> tuple[dict[bytes, x509.Certificate], list[x509.Certificate]]:
    """載入並快取釘死的 CA bundle。回 (subject_DER → cert, [self-signed roots])。"""
    global _ca_anchor_cache
    if _ca_anchor_cache is not None:
        return _ca_anchor_cache

    path = Path(os.environ.get("CARD_CA_BUNDLE_PATH", str(_DEFAULT_CA_BUNDLE)))
    try:
        pem_bytes = path.read_bytes()
    except OSError as exc:
        raise CardConfigError(f"無法讀取卡片 CA bundle: {path} ({exc})") from exc

    try:
        certs = x509.load_pem_x509_certificates(pem_bytes)
    except (ValueError, AttributeError):
        certs = _load_pem_certs_fallback(pem_bytes)
    if not certs:
        raise CardConfigError(f"CA bundle 不含任何憑證: {path}")

    anchors: dict[bytes, x509.Certificate] = {}
    roots: list[x509.Certificate] = []
    for cert in certs:
        anchors[cert.subject.public_bytes()] = cert
        if cert.subject == cert.issuer:
            try:
                _verify_cert_signed_by(cert, cert)  # 確認 root 自簽有效
            except InvalidSignatureError as exc:
                raise CardConfigError(f"CA bundle root 自簽無效: {exc}") from exc
            roots.append(cert)
    if not roots:
        raise CardConfigError("CA bundle 內沒有自簽 root CA")

    _ca_anchor_cache = (anchors, roots)
    return _ca_anchor_cache


def _load_pem_certs_fallback(pem_bytes: bytes) -> list[x509.Certificate]:
    """cryptography <42 沒有 load_pem_x509_certificates 時逐塊解析。"""
    out: list[x509.Certificate] = []
    marker = b"-----BEGIN CERTIFICATE-----"
    end = b"-----END CERTIFICATE-----"
    idx = 0
    while True:
        start = pem_bytes.find(marker, idx)
        if start == -1:
            break
        stop = pem_bytes.find(end, start)
        if stop == -1:
            break
        block = pem_bytes[start : stop + len(end)] + b"\n"
        out.append(x509.load_pem_x509_certificate(block))
        idx = stop + len(end)
    return out


# ─── Claim extraction (驗證通過後) ───────────────────────────────────────────────


def _extract_claims(cert: x509.Certificate, card_serial: str | None) -> CardClaims:
    employee_id = _attr_or_none(cert, NameOID.SERIAL_NUMBER)
    if not employee_id:
        raise MissingClaimError("signer cert 缺 subject.serialNumber (員工編號)")
    if not _EMPLOYEE_ID_RE.match(employee_id):
        raise MissingClaimError(
            f"signer cert.subject.serialNumber 格式不符員工編號規格: {employee_id!r}"
        )

    display_name = _attr_or_none(cert, NameOID.COMMON_NAME)
    if not display_name:
        raise MissingClaimError("signer cert 缺 subject.commonName (姓名)")

    email = _extract_email(cert)
    if not email:
        raise MissingClaimError("signer cert 缺 SAN.rfc822Name 與 otherName(UPN) (email)")

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
            raw = other.value
            if len(raw) >= 2 and raw[0] == 0x0C:
                length = raw[1]
                try:
                    return raw[2 : 2 + length].decode("utf-8")
                except UnicodeDecodeError:
                    continue
    return None
