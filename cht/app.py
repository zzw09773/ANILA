"""中華電信 HiPKI 本機元件（``localhost:16888``）的 dev mock。

真元件跑在**使用者自己的 PC** 上，接讀卡機、要 PIN、在卡片晶片內做簽章。
本機開發沒有讀卡機，所以用這支 Flask app 假裝它，協定與埠號一致
（``/popupForm`` / ``/cht_api/sign`` / ``/cht_api/pkcs11info``）。

2026-07-31 之前這支 mock 是假的：``/cht_api/sign`` 只拿 ``tbsPackage`` 檢查
PIN 是不是 ``123456``，然後回一份**寫死的** PKCS#7，eContent 永遠是 ``b"TBS"``。
2026-06-12 卡登加上 nonce 綁定（eContent 必須等於本次 challenge 的 nonce）之後，
這份寫死的簽章**在結構上不可能**通過驗證，於是本機只好把 ``ENABLE_CARD_LOGIN``
關掉，整條登入路徑在開發機上走不完。

現在 mock 誠實了：它有自己的測試 PKI（執行時生成，見 ``cms_sign.py``），
拿到什麼 tbs 就簽什麼 tbs。因此本機流程會**真的**行使 nonce 綁定、CMS 簽章
驗證與憑證鏈驗證，唯一跟內網不同的是信任錨換成測試 root（由 csp 端的
``CARD_CA_BUNDLE_PATH`` 指定，且 csp 有 fence 擋住 production 誤用）。

身分也換了（2026-07-31 擁有者裁決）：原本 mock 內嵌的是一位同仁的真憑證
（姓名 / 工號 / 院內信箱），現在一律是 ``cms_sign.py`` 現生的合成假身分。
真元件輸出的保真樣本仍留在 ``services/csp/tests/test_card_auth.py``
（``MOCK_SIGNATURE_B64``），本檔不再含任何真人資料。
"""
import base64
import datetime
import json

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import NameOID
from flask import Flask, jsonify, render_template, request

from cms_sign import (
    DEV_CARD_DISPLAY_NAME,
    DEV_CARD_EMAIL,
    DEV_CARD_EMPLOYEE_ID,
    DEV_CARD_SERIAL,
    build_pkcs7,
    dev_ca_dir,
    ensure_dev_pki,
    load_dev_card,
)

app = Flask(__name__)


@app.route("/popupForm")
def popup_form():
    return render_template("popupForm.html")


def _b64_der(cert: x509.Certificate) -> str:
    return base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()


@app.route("/cht_api/sign", methods=["POST"])
def sign():
    """對 ``tbsPackage.tbs`` 真的做一次卡片簽章。

    ``tbsEncoding`` 目前只支援 ``NONE``（前端 ``caAuth.js`` 與 ``login.html``
    都固定送 ``NONE``），tbs 原文即為 eContent —— 這正是真元件的行為，
    佐證在 ``real_component_capture.json``。
    """
    tbs_package = json.loads(request.form.get("tbsPackage"))

    # PIN 檢查維持原行為：錯 PIN → ret_code=1、不簽章（前端據此丟
    # CardSignFailedError）。真元件是卡片自己鎖，mock 只能比字串。
    if tbs_package.get("pin") != "123456":
        return jsonify({"func": "sign", "last_error": 0, "ret_code": 1, "version": "2.3.2"})

    key, cert = load_dev_card()
    tbs = (tbs_package.get("tbs") or "").encode("utf-8")
    der = build_pkcs7(tbs, key, cert)

    return jsonify(
        {
            "cardSN": DEV_CARD_SERIAL,
            "certb64": _b64_der(cert),
            "func": "sign",
            "last_error": 0,
            "ret_code": 0,
            "signature": base64.b64encode(der).decode(),
            "version": "2.3.2",
        }
    )


def _subject_dn(cert: x509.Certificate) -> str:
    """組真元件那種 ``C=TW,O=...,CN=...,serialNumber=...`` 寫法。

    ``caAuth.js`` 的 ``parseSubjectDN()`` 是照這個格式切的，順序與 key 名稱
    不能改成 rfc4514 的樣子。
    """
    order = (
        ("C", NameOID.COUNTRY_NAME),
        ("O", NameOID.ORGANIZATION_NAME),
        ("CN", NameOID.COMMON_NAME),
        ("serialNumber", NameOID.SERIAL_NUMBER),
    )
    parts = []
    for label, oid in order:
        attrs = cert.subject.get_attributes_for_oid(oid)
        if attrs:
            parts.append(f"{label}={attrs[0].value}")
    return ",".join(parts)


def _cert_entry(
    cert: x509.Certificate,
    label: str,
    usage: str,
    email: str | None = None,
) -> dict:
    """組一筆 pkcs11info 的 cert 記錄。

    ``caAuth.js.extractCardClaims()`` 挑 signer 的條件是「有 email + usage 含
    digitalSignature + subjectDN 帶 serialNumber=」，這三個欄位的格式要對。
    """
    not_before = getattr(cert, "not_valid_before_utc", None) or cert.not_valid_before
    not_after = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after
    cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    entry = {
        "certb64": _b64_der(cert),
        "id": base64.b64encode(label.encode()).decode(),
        "issuerDN": _subject_dn_of_name(cert.issuer),
        "label": label,
        "notAfter": not_after.strftime("%y%m%d%H%M%SZ"),
        "notAfterT": int(not_after.replace(tzinfo=datetime.timezone.utc).timestamp()),
        "notBefore": not_before.strftime("%y%m%d%H%M%SZ"),
        "notBeforeT": int(not_before.replace(tzinfo=datetime.timezone.utc).timestamp()),
        "sn": format(cert.serial_number, "X"),
        "subjectCN": cn[0].value if cn else "",
        "subjectDN": _subject_dn(cert),
        "thumbprint": cert.fingerprint(hashes.SHA1()).hex().upper(),
        "usage": usage,
    }
    if email:
        entry["email"] = email
    return entry


def _subject_dn_of_name(name: x509.Name) -> str:
    order = (
        ("C", NameOID.COUNTRY_NAME),
        ("O", NameOID.ORGANIZATION_NAME),
        ("CN", NameOID.COMMON_NAME),
    )
    parts = []
    for label, oid in order:
        attrs = name.get_attributes_for_oid(oid)
        if attrs:
            parts.append(f"{label}={attrs[0].value}")
    return ",".join(parts)


@app.route("/cht_api/pkcs11info", methods=["POST"])
def pkcs11info():
    """回卡片/讀卡機資訊。結構照真元件，內容換成合成測試身分。"""
    target = ensure_dev_pki()
    root = x509.load_pem_x509_certificate((target / "dev_root_ca.pem").read_bytes())
    issuing = x509.load_pem_x509_certificate((target / "dev_issuing_ca.pem").read_bytes())
    _, card = load_dev_card()

    return jsonify(
        {
            "cryptokiVersion": 2.04,
            "flags": 0,
            "func": "pkcs11info",
            "last_error": 0,
            "libraryDescription": "ANILA DEV MOCK (not a real HiPKI component)",
            "libraryVersion": 1.001,
            "manufacturerID": "ANILA dev mock",
            "ret_code": 0,
            "slots": [
                {
                    "firmwareVersion": 0,
                    "flags": 7,
                    "hardwareVersion": 0,
                    "manufacturerID": "unKnow",
                    "slotDescription": "ANILA DEV MOCK READER 0",
                    "slotID": 0,
                    "token": {
                        "certs": [
                            _cert_entry(root, "ROOT CA Cert", "keyCertSign|cRLSign"),
                            _cert_entry(issuing, "CA Cert", "keyCertSign|cRLSign"),
                            _cert_entry(
                                card,
                                "RSACert1",
                                "digitalSignature",
                                email=DEV_CARD_EMAIL,
                            ),
                        ],
                        "label": "ANILA-DEV-MOCK-CARD",
                        "manufacturerID": "ANILA dev mock",
                        "model": "dev mock        <",
                        "serialNumber": DEV_CARD_SERIAL,
                        "ulMaxPinLen": 6,
                        "ulMinPinLen": 6,
                    },
                }
            ],
            "version": "2.2.6",
            "serverVersion": "1.3.8",
        }
    )


@app.route("/dev/ca-bundle.pem")
def ca_bundle():
    """把測試信任 bundle 露出來，方便確認 csp 端掛到的是同一份。

    只有公開憑證，沒有任何私鑰。
    """
    return (
        (ensure_dev_pki() / "dev_ca_bundle.pem").read_bytes(),
        200,
        {"Content-Type": "application/x-pem-file"},
    )


if __name__ == "__main__":
    ensure_dev_pki()
    print(
        f"[cht mock] 測試 PKI 就緒於 {dev_ca_dir()}；"
        f"卡片身分 {DEV_CARD_EMPLOYEE_ID} / {DEV_CARD_DISPLAY_NAME} / {DEV_CARD_EMAIL}",
        flush=True,
    )
    app.run(host="0.0.0.0", port=16888)
