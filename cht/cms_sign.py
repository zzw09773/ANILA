"""誠實的卡片簽章器：把交給它的 tbs 真的簽下去，而不是回一份寫死的簽章。

為什麼需要這個檔案
==================

真實的中華電信 HiPKI 本機元件（``localhost:16888``）拿到 ``tbsPackage.tbs``
之後，會把 **tbs 原文放進 CMS 的 eContent** 並用卡片私鑰簽章。這一點有實證：
``services/csp/tests/test_card_auth.py`` 的 ``MOCK_SIGNATURE_B64`` 是真元件的
實際輸出（2025-05-27 內網擷取），而當初送給它的 ``tbs`` 就是 ``cht/login.html``
``getTbsPackage()`` 裡寫死的字串 ``"TBS"`` —— 那份簽章的 eContent 正好就是
``b"TBS"``，且它能通過釘死的 ``cspki_ca_bundle.pem`` 全套驗證。

也就是說，正式流程（瀏覽器把後端發的 nonce 當 tbs 送進元件）本來就會產生
「eContent == 本次 challenge 的 nonce」的簽章，2026-06-12 加上的 nonce 綁定
對真卡是**可滿足**的。過時的是 mock：它回寫死的簽章，eContent 永遠是 ``b"TBS"``，
所以本機開發永遠測不到 nonce 綁定。

本模組補上那塊：產生一組**只存在於這台機器**的測試 PKI，並用真的私鑰簽出
結構與真元件輸出**逐欄對齊**的 CMS SignedData：

===================  ==========================================
欄位                 值（真元件輸出實測 → 本模組照抄）
===================  ==========================================
contentType          signed_data
eContent             tbs 原文（``tbsEncoding=NONE``，UTF-8 bytes）
digestAlgorithm      sha256
signedAttrs          contentType / signingTime / messageDigest
signatureAlgorithm   rsassa_pkcs1v15
SignerIdentifier     issuerAndSerialNumber
certificates         只夾 leaf（中繼與 root 靠信任 bundle）
===================  ==========================================

金鑰的安全性
============

這裡產生的 root/中繼/卡片私鑰**在執行時才生成**，落在
``CHT_DEV_CA_DIR``（預設 ``/dev-ca``，由 host 的 ``secrets/dev-card-ca/``
掛進來）。repo 是 PUBLIC，``secrets/`` 與 ``*.key``/``*.pem`` 都在
``.gitignore`` 內，任何一把私鑰都不會進版控、也不會被 build 進任何映像。

測試 CA 的識別標記
==================

三張 CA 憑證的 Organization 一律是 ``_DEV_CA_MARKER_ORG``。
``card_auth._load_ca_anchors()`` 認得這個字串，在 production 訊號下會直接
拒收整包 bundle（fail-closed）。改標記就得整組 PKI 重簽，不是改個字串而已。
"""
from __future__ import annotations

import datetime
import hashlib
import os
from pathlib import Path

from asn1crypto import cms as asn1_cms
from asn1crypto import x509 as asn1_x509
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import NameOID


# ⚠ 這個字串是 card_auth 的 fence 依據，兩邊必須一字不差。改這裡要同步改
# ``services/csp/app/services/card_auth.py`` 的 ``_DEV_TEST_CA_MARKER_ORG``。
_DEV_CA_MARKER_ORG = "ANILA DEV TEST CA - DO NOT TRUST"

# ─── 合成卡片持有人（2026-07-31 擁有者裁決：mock 不放真人）──────────────────
#
# 原本 mock 內嵌的是一位同仁的真憑證（姓名 / 工號 / 院內信箱）。憑證本質公開、
# 不算祕密外洩，但這是 PUBLIC repo，沒有理由讓真人的人事資料長住在這裡。
# 既然 mock 現在自己簽章，就順手把身分一起換成合成的。
#
# 兩個要求同時滿足：
#
# 1. **一眼看得出是假的** —— 工號全 9、信箱掛 ``.invalid``（RFC 2606 保留
#    網域，永遠不可能解析），沒有人會把它誤認成人事紀錄。
# 2. **結構跟真卡一模一樣** —— DN 仍是 ``C / O / CN / serialNumber`` 四段，
#    員工編號仍放在 ``subject.serialNumber``、email 仍放在 SAN.rfc822Name。
#    ``card_auth._extract_claims()`` 只讀這三個位置，形狀若不同，parsing bug
#    會從本機測試底下溜過去。
#
# 工號要通過 ``card_auth._EMPLOYEE_ID_RE``（6–9 位數字），所以是 7 個 9 而不是
# 「MOCK」之類的字串。
DEV_CARD_EMPLOYEE_ID = "9999999"
DEV_CARD_DISPLAY_NAME = "測試人員（MOCK 假卡）"
DEV_CARD_EMAIL = "mock-card-9999999@example.invalid"
DEV_CARD_SERIAL = "MOCKCARD00000001"
# 真卡的 O 是「國家中山科學研究院」。這裡保留同一個欄位位置但標明是假卡，
# 免得有人把 mock 的輸出當成真憑證樣本。
DEV_CARD_ORG = "國家中山科學研究院（MOCK 測試卡，非真實憑證）"

_BUNDLE_NAME = "dev_ca_bundle.pem"


def dev_ca_dir() -> Path:
    return Path(os.environ.get("CHT_DEV_CA_DIR", "/dev-ca"))


# ─── 測試 PKI 生成 ──────────────────────────────────────────────────────────


def _name(org: str, cn: str, employee_id: str | None = None) -> x509.Name:
    attrs = [
        x509.NameAttribute(NameOID.COUNTRY_NAME, "TW"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, org),
        x509.NameAttribute(NameOID.COMMON_NAME, cn),
    ]
    if employee_id is not None:
        attrs.append(x509.NameAttribute(NameOID.SERIAL_NUMBER, employee_id))
    return x509.Name(attrs)


def _write_private(path: Path, key) -> None:
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)


def _write_cert(path: Path, cert: x509.Certificate) -> None:
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def generate_dev_pki(target: Path) -> None:
    """在 ``target`` 產生 root → 中繼 → 卡片 leaf 三層測試 PKI。

    對齊真實 CSPKI 的層數（root 自簽 → 中繼 → 卡片），所以本機流程走到的
    ``_verify_chain`` 迴圈跟內網一樣要走兩層，而不是一層就到底。
    """
    target.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc)
    not_before = now - datetime.timedelta(days=1)
    # 效期刻意只有 30 天:萬一這份 bundle 真的被誰帶上內網,它會自己過期。
    # ``ensure_dev_pki()`` 過期自動重生,開發者不會感覺到摩擦。
    not_after = now + datetime.timedelta(days=30)

    # Root：EC P-384 自簽（真 CSPKI root 也是 EC）。
    root_key = ec.generate_private_key(ec.SECP384R1())
    root_name = _name(_DEV_CA_MARKER_ORG, "ANILA Dev Card Test Root CA")
    root_cert = (
        x509.CertificateBuilder()
        .subject_name(root_name)
        .issuer_name(root_name)
        .public_key(root_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(root_key, hashes.SHA256())
    )

    # 中繼。
    issuing_key = ec.generate_private_key(ec.SECP384R1())
    issuing_name = _name(_DEV_CA_MARKER_ORG, "ANILA Dev Card Test Issuing CA")
    issuing_cert = (
        x509.CertificateBuilder()
        .subject_name(issuing_name)
        .issuer_name(root_name)
        .public_key(issuing_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(root_key, hashes.SHA256())
    )

    # 卡片 leaf：RSA-2048，對齊真卡的 RSACert1。
    card_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    card_cert = (
        x509.CertificateBuilder()
        .subject_name(_name(DEV_CARD_ORG, DEV_CARD_DISPLAY_NAME, DEV_CARD_EMPLOYEE_ID))
        .issuer_name(issuing_name)
        .public_key(card_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.RFC822Name(DEV_CARD_EMAIL)]),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(issuing_key, hashes.SHA256())
    )

    _write_private(target / "dev_root_ca.key", root_key)
    _write_cert(target / "dev_root_ca.pem", root_cert)
    _write_private(target / "dev_issuing_ca.key", issuing_key)
    _write_cert(target / "dev_issuing_ca.pem", issuing_cert)
    _write_private(target / "dev_card.key", card_key)
    _write_cert(target / "dev_card.pem", card_cert)

    # 信任 bundle 只放 root + 中繼，跟釘死的 ``cspki_ca_bundle.pem`` 一樣：
    # leaf 由 CMS 夾帶，不進 bundle。
    (target / _BUNDLE_NAME).write_bytes(
        root_cert.public_bytes(serialization.Encoding.PEM)
        + issuing_cert.public_bytes(serialization.Encoding.PEM)
    )


def ensure_dev_pki(target: Path | None = None) -> Path:
    """確保測試 PKI 存在（缺件或過期就重生），回傳目錄。"""
    target = target or dev_ca_dir()
    bundle = target / _BUNDLE_NAME
    needed = ["dev_card.key", "dev_card.pem", "dev_issuing_ca.pem", _BUNDLE_NAME]
    if all((target / n).exists() for n in needed):
        try:
            cert = x509.load_pem_x509_certificate((target / "dev_card.pem").read_bytes())
            not_after = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after.replace(
                tzinfo=datetime.timezone.utc
            )
            if not_after > datetime.datetime.now(datetime.timezone.utc):
                return target
        except ValueError:
            pass  # 壞檔 → 重生
    generate_dev_pki(target)
    assert bundle.exists()
    return target


def load_dev_card(target: Path | None = None):
    """回傳 (private_key, cert)；缺件時先生成。"""
    target = ensure_dev_pki(target)
    key = serialization.load_pem_private_key((target / "dev_card.key").read_bytes(), password=None)
    cert = x509.load_pem_x509_certificate((target / "dev_card.pem").read_bytes())
    return key, cert


# ─── CMS SignedData 產生 ────────────────────────────────────────────────────


def build_pkcs7(tbs: bytes, signer_key, signer_cert: x509.Certificate) -> bytes:
    """用 ``signer_key`` 對 ``tbs`` 簽出 CMS SignedData（DER）。

    結構逐欄對齊真元件實測輸出（見模組 docstring 的表）。

    這個函式是「卡片內部那顆晶片」的等價物：mock 用它、csp 的端點測試也用它
    （見 ``services/csp/tests/test_card_endpoints.py``），兩邊對「一份合法簽章
    長什麼樣」只有一個答案，不會各自為政。金鑰素材不共用 —— 測試自己現生
    一組拋棄式的，不依賴這台機器上的 ``/dev-ca``。
    """
    digest = "sha256"
    cert_asn1 = asn1_x509.Certificate.load(signer_cert.public_bytes(serialization.Encoding.DER))

    signed_attrs = asn1_cms.CMSAttributes(
        [
            asn1_cms.CMSAttribute({"type": "content_type", "values": ["data"]}),
            asn1_cms.CMSAttribute(
                {
                    "type": "signing_time",
                    "values": [
                        asn1_cms.Time(
                            {"utc_time": datetime.datetime.now(datetime.timezone.utc)}
                        )
                    ],
                }
            ),
            asn1_cms.CMSAttribute(
                {"type": "message_digest", "values": [hashlib.new(digest, tbs).digest()]}
            ),
        ]
    )

    # 簽的是 signedAttrs 的 SET OF 重新編碼（RFC 5652 §5.4），不是 [0] implicit
    # 那顆 —— card_auth 驗的時候也是 ``signed_attrs.untag().dump()``。
    to_be_signed = signed_attrs.dump()

    if isinstance(signer_key, rsa.RSAPrivateKey):
        from cryptography.hazmat.primitives.asymmetric import padding

        signature = signer_key.sign(to_be_signed, padding.PKCS1v15(), hashes.SHA256())
        sig_algo = "rsassa_pkcs1v15"
    else:
        signature = signer_key.sign(to_be_signed, ec.ECDSA(hashes.SHA256()))
        sig_algo = "ecdsa"

    signer_info = asn1_cms.SignerInfo(
        {
            "version": "v1",
            "sid": asn1_cms.SignerIdentifier(
                {
                    "issuer_and_serial_number": asn1_cms.IssuerAndSerialNumber(
                        {
                            "issuer": cert_asn1.issuer,
                            "serial_number": cert_asn1["tbs_certificate"]["serial_number"],
                        }
                    )
                }
            ),
            "digest_algorithm": {"algorithm": digest},
            "signed_attrs": signed_attrs,
            "signature_algorithm": {"algorithm": sig_algo},
            "signature": signature,
        }
    )

    signed_data = asn1_cms.SignedData(
        {
            "version": "v1",
            "digest_algorithms": [{"algorithm": digest}],
            "encap_content_info": {"content_type": "data", "content": tbs},
            "certificates": [asn1_cms.CertificateChoices({"certificate": cert_asn1})],
            "signer_infos": [signer_info],
        }
    )

    return asn1_cms.ContentInfo(
        {"content_type": "signed_data", "content": signed_data}
    ).dump()
