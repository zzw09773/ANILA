"""Local CHT card-component emulator backed by an ephemeral synthetic PKI."""
from __future__ import annotations

import base64
import json
import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import NameOID
from flask import Flask, Response, jsonify, render_template, request

try:  # package import in tests
    from .synthetic_pki import (
        SYNTHETIC_CARD,
        SYNTHETIC_CARD_SERIAL,
        SYNTHETIC_DISPLAY_NAME,
        SYNTHETIC_EMAIL,
        SYNTHETIC_EMPLOYEE_ID,
    )
except ImportError:  # direct ``python app.py`` / container entrypoint
    from synthetic_pki import (
        SYNTHETIC_CARD,
        SYNTHETIC_CARD_SERIAL,
        SYNTHETIC_DISPLAY_NAME,
        SYNTHETIC_EMAIL,
        SYNTHETIC_EMPLOYEE_ID,
    )


app = Flask(__name__)
SYNTHETIC_PIN = os.environ.get("CHT_SYNTHETIC_PIN", "654321")


def _certificate_record(certificate, *, label: str, usage: str) -> dict:
    der = certificate.public_bytes(serialization.Encoding.DER)
    thumbprint = certificate.fingerprint(hashes.SHA256()).hex().upper()
    return {
        "certb64": base64.b64encode(der).decode("ascii"),
        "id": base64.b64encode(label.encode("ascii")).decode("ascii"),
        "issuerDN": certificate.issuer.rfc4514_string(),
        "label": label,
        "notAfter": certificate.not_valid_after_utc.isoformat(),
        "notBefore": certificate.not_valid_before_utc.isoformat(),
        "signatureAlgorithm": certificate.signature_algorithm_oid.dotted_string,
        "sn": f"{certificate.serial_number:X}",
        "subjectCN": certificate.subject.get_attributes_for_oid(
            NameOID.COMMON_NAME
        )[0].value,
        "subjectDN": certificate.subject.rfc4514_string(),
        "thumbprint": thumbprint,
        "usage": usage,
    }


@app.get("/popupForm")
def popup_form():
    return render_template("popupForm.html")


@app.post("/cht_api/sign")
def sign():
    try:
        tbs_package = request.form.get("tbsPackage", type=str)
        payload = json.loads(tbs_package or "{}")
    except (TypeError, ValueError):
        return jsonify({"func": "sign", "last_error": 2, "ret_code": 2}), 400

    if payload.get("pin") != SYNTHETIC_PIN:
        return jsonify({"func": "sign", "last_error": 1, "ret_code": 1})
    tbs = payload.get("tbs")
    if not isinstance(tbs, str) or not tbs:
        return jsonify({"func": "sign", "last_error": 3, "ret_code": 3}), 400

    return jsonify(
        {
            "cardSN": SYNTHETIC_CARD_SERIAL,
            "certb64": SYNTHETIC_CARD.signer_certificate_b64,
            "func": "sign",
            "last_error": 0,
            "ret_code": 0,
            "signature": SYNTHETIC_CARD.sign(tbs),
            "version": "synthetic-1",
        }
    )


@app.post("/cht_api/pkcs11info")
def pkcs11info():
    certificates = [
        _certificate_record(
            SYNTHETIC_CARD.root_certificate,
            label="SYNTHETIC ROOT CA",
            usage="keyCertSign|cRLSign",
        ),
        _certificate_record(
            SYNTHETIC_CARD.intermediate_certificate,
            label="SYNTHETIC ISSUING CA",
            usage="keyCertSign|cRLSign",
        ),
        {
            **_certificate_record(
                SYNTHETIC_CARD.signer_certificate,
                label="SYNTHETIC SIGNING CERT",
                usage="digitalSignature",
            ),
            "email": SYNTHETIC_EMAIL,
            "subjectID": SYNTHETIC_EMPLOYEE_ID,
        },
    ]
    public_key = SYNTHETIC_CARD.signer_certificate.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return jsonify(
        {
            "cryptokiVersion": "synthetic-1",
            "func": "pkcs11info",
            "last_error": 0,
            "ret_code": 0,
            "slots": [
                {
                    "manufacturerID": "ANILA Synthetic Test Lab",
                    "slotDescription": "Ephemeral in-memory synthetic card",
                    "slotID": 0,
                    "token": {
                        "certs": certificates,
                        "keys": [
                            {
                                "id": "SYNTHETIC-PUBLIC-KEY",
                                "keyb64": base64.b64encode(public_key).decode("ascii"),
                                "label": "SYNTHETIC SIGNING PUBLIC KEY",
                                "type": 0,
                            }
                        ],
                        "label": "ANILA SYNTHETIC CARD",
                        "manufacturerID": "ANILA Synthetic Test Lab",
                        "model": "TEST-ONLY",
                        "serialNumber": SYNTHETIC_CARD_SERIAL,
                        "subjectCN": SYNTHETIC_DISPLAY_NAME,
                    },
                }
            ],
        }
    )


@app.get("/cht_api/synthetic-ca.pem")
def synthetic_ca_bundle():
    """Public test trust bundle for the currently running emulator instance."""
    return Response(SYNTHETIC_CARD.ca_bundle_pem, mimetype="application/x-pem-file")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=16888)
