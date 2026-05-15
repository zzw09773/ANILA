"""一次性產出 default CSPKI CA bundle（dev / staging 用）。

中科院憑證卡 STRICT mode 需要 trust anchor (Root CA) + Intermediate CA 才能
驗 chain。本 script 把 ``cht/`` mock 容器內 pkcs11info response 提供的兩張
**公開** CSPKI CA cert 抽出來輸出成 PEM bundle，方便 dev / staging 直接使用，
不必等中科院 IT 配發。

Production 應該以 ``NCSIST_CA_BUNDLE_PATH`` env var 指向 IT 配發的官方
PEM bundle（內容應該相同，但分開保管能讓信任邊界明確）。

Usage:
    python -m scripts.extract_ncsist_ca_bundle
    python -m scripts.extract_ncsist_ca_bundle --output /tmp/foo.pem

Output 預設位置：``myCSPPlatform/backend/data/ncsist-ca-bundle.pem``。
"""
from __future__ import annotations

import argparse
import base64
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization


# ── 來源 ────────────────────────────────────────────────────────────────────
# 兩段 base64 取自 ``cht/app.py:26`` 的 ``pkcs11info`` response：
#   - id="Uk9PVENBQ2VydA==" (b64 of "ROOTCACert"), label="ROOT CA Cert"
#   - id="Q0FDZXJ0"          (b64 of "CACert"),     label="CA Cert"
# 這兩張是 CSPKI 體系的公開 CA cert，非機密。

ROOT_CA_B64 = (
    "MIIC0DCCAjKgAwIBAgIRAJ27l/amC0BYIKvgh67WQbEwCgYIKoZIzj0EAwIwgYMx"
    "CzAJBgNVBAYTAlRXMUIwQAYDVQQKDDlOYXRpb25hbCBDaHVuZy1TaGFuIEluc3Rp"
    "dHV0aW9uIG9mIFNjaWVuY2UgYW5kIFRlY2hub2xvZ3kxMDAuBgNVBAMMJ0NTUEtJ"
    "IFJvb3QgQ2VydGlmaWNhdGlvbiBBdXRob3JpdHkgLSBHMTAeFw0xOTExMjgwODA3"
    "MjVaFw00OTExMjgwODA3MjVaMIGDMQswCQYDVQQGEwJUVzFCMEAGA1UECgw5TmF0"
    "aW9uYWwgQ2h1bmctU2hhbiBJbnN0aXR1dGlvbiBvZiBTY2llbmNlIGFuZCBUZWNo"
    "bm9sb2d5MTAwLgYDVQQDDCdDU1BLSSBSb290IENlcnRpZmljYXRpb24gQXV0aG9y"
    "aXR5IC0gRzEwgZswEAYHKoZIzj0CAQYFK4EEACMDgYYABAG5Sm4veEmURBEMChGp"
    "hXg74wzwV4VpIrDOr+lId2hHhxpIW6jHm7KJdBLgKDDmNhY9owHs+vJsYqh16rZa"
    "kepaIQC/GEP5rc5OOTMzSZpS4rsMctx9YD1XuKaN4saLZtxZYU4BYaA5in+FaHTT"
    "ZsyrJqxj6Lo+pey3BiIPpeNTBawRf6NCMEAwHQYDVR0OBBYEFBRMMX58xj9MMEve"
    "J9sIAs6M/y2HMA8GA1UdEwEB/wQFMAMBAf8wDgYDVR0PAQH/BAQDAgGGMAoGCCqG"
    "SM49BAMCA4GLADCBhwJBa+PLmn85qBlHfMLHgWj9VVxbkYslmPaxUjNnr0lvbgQr"
    "iyLcK9ztlScAVdOwV8qNPV3TIEHG3lFDWb+9HbwhV+0CQgDJCEnZrPRU1IhXEX1S"
    "yqJHoZl9XGcDWvCjn4KLDRG4NhJ0PDqm9m3jDNnQwxo+W+AI1RON/vgecW3kt6Z7"
    "iJByCA=="
)

INTERMEDIATE_CA_B64 = (
    "MIIDpzCCAwmgAwIBAgIQPHfuE3kA8LHIz9fVUdAeEzAKBggqhkjOPQQDAjCBgzEL"
    "MAkGA1UEBhMCVFcxQjBABgNVBAoMOU5hdGlvbmFsIENodW5nLVNoYW4gSW5zdGl0"
    "dXRpb24gb2YgU2NpZW5jZSBhbmQgVGVjaG5vbG9neTEwMC4GA1UEAwwnQ1NQS0kg"
    "Um9vdCBDZXJ0aWZpY2F0aW9uIEF1dGhvcml0eSAtIEcxMB4XDTE5MTEyODA4MDky"
    "OFoXDTM5MTEyODA4MDkyOFowXjELMAkGA1UEBhMCVFcxJDAiBgNVBAoMG+Wci+Wu"
    "tuS4reWxseenkeWtuOeglOeptumZojEpMCcGA1UEAwwg5Lit56eR6Zmi5oaR6K2J"
    "566h55CG5Lit5b+DIC0gRzEwdjAQBgcqhkjOPQIBBgUrgQQAIgNiAAT3jvbNVpBv"
    "Ne7dfErcPF8vA0b5MMJSwlieEpVr+iV0KByOoMeiNz64AbPJ6zow8IA4Zy9WOZav"
    "xiBrKLABPM8YgDrvg8o4wi1M8NUi80d/DHSO7LEi1IOcI+tUoFy5hLijggFkMIIB"
    "YDAfBgNVHSMEGDAWgBQUTDF+fMY/TDBL3ifbCALOjP8thzAdBgNVHQ4EFgQUHZyJ"
    "wv7uNVXVRo9VuPwDcVlMK+AwRAYDVR0fBD0wOzA5oDegNYYzaHR0cDovL3JlcG9z"
    "aXRvcnkubmNzaXN0Lm9yZy50dy9yZXBvc2l0b3J5L0NBUkwuY3JsMH8GCCsGAQUF"
    "BwEBBHMwcTBABggrBgEFBQcwAoY0aHR0cDovL3JlcG9zaXRvcnkubmNzaXN0Lm9y"
    "Zy50dy9jZXJ0cy9OQ1NJU1RSb290LmNlcjAtBggrBgEFBQcwAYYhaHR0cDovL29j"
    "c3AubmNzaXN0Lm9yZy50dy9SQ0FPQ1NQMDMGA1UdIAQsMCowDAYKYIZ2aYaNIwAD"
    "ATAMBgpghnZpho0jAAMCMAwGCmCGdmmGjSMAAwMwEgYDVR0TAQH/BAgwBgEB/wIB"
    "ADAOBgNVHQ8BAf8EBAMCAQYwCgYIKoZIzj0EAwIDgYsAMIGHAkF8wIm7VTQVstOV"
    "JUlo1HjvMyeXRmBe3kaZMiuxE8DLMpTdg8ncHo2tdgskezwPD+Ug9uLu/zpssSpN"
    "YvVgxsajmAJCAe0+IgkiqIBWyK7EXjeaVzqg5D19C4ShKNP/wBnUmuduck2ohm/w"
    "bd2UuJ4nzMtSmlNC9Ng3tjONTUsWQwloCp/l"
)


DEFAULT_OUTPUT = (
    Path(__file__).resolve().parent.parent / "data" / "ncsist-ca-bundle.pem"
)


def extract() -> list[x509.Certificate]:
    """Decode the two base64 strings into cryptography Certificate objects."""
    return [
        x509.load_der_x509_certificate(base64.b64decode(b64))
        for b64 in (ROOT_CA_B64, INTERMEDIATE_CA_B64)
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output PEM file path (default: {DEFAULT_OUTPUT})",
    )
    args = parser.parse_args()

    certs = extract()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    pem = b"".join(c.public_bytes(serialization.Encoding.PEM) for c in certs)
    args.output.write_bytes(pem)

    print(f"Wrote {len(certs)} CSPKI CA cert(s) to {args.output}")
    for cert in certs:
        print(f"  • {cert.subject.rfc4514_string()}")
        print(
            f"    valid: {cert.not_valid_before_utc.isoformat()} → "
            f"{cert.not_valid_after_utc.isoformat()}"
        )

    # Sanity: chain self-consistency (Intermediate 應由 Root 簽發)
    root, intermediate = certs
    try:
        intermediate.verify_directly_issued_by(root)
        print("  ✓ chain self-check: intermediate 由 root 直接簽發")
    except Exception as exc:
        raise SystemExit(
            f"chain self-check failed: {type(exc).__name__}: {exc}"
        )


if __name__ == "__main__":
    main()
