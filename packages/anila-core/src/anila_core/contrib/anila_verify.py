#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ANILA 派工身分驗章（單一檔、給第三方 agent 直接複製）。

用途
----
CSP 派工時會帶 ``Authorization: Bearer <JWT>``（RS256、約 5 分鐘有效）。
本檔用平台公開的 JWKS 驗簽，回傳已驗證的 claims；失敗就丟例外（fail-closed）。

相依
----
僅 **stdlib + cryptography**（內網氣隙可離線安裝 cryptography wheel）。
不要用 PyJWT / python-jose。

使用範例
--------
::

    from anila_verify import verify_authorization

    claims = verify_authorization(
        authorization_header,                 # 請求的 Authorization 標頭值
        jwks_url="https://anila.example/.well-known/jwks.json",
        ca_file="/path/to/cspki_ca_bundle.pem",  # CSPKI 鏈；勿設 SSL_CERT_FILE
    )
    user_id = claims["user_id"]
    department = claims["department"]         # 可能是 None
    agent_id = claims["agent_id"]

也可一次抓 JWKS、之後重用（避免每次派工都打網路）::

    jwks = fetch_jwks(jwks_url, ca_file=ca_file)
    claims = verify_authorization(authorization_header, jwks=jwks)

契約（與 CSP P2.1 簽發端對齊）
------------------------------
* ``iss`` = ``anila-csp``
* ``aud`` = ``anila-agent``
* claims：``user_id`` / ``department`` / ``agent_id``（另有 ``iat``/``exp``/``jti``/``sub``）
* JWT header 含 ``kid``；演算法固定 ``RS256``

⚠ 信任錨請用 ``ca_file`` 指到 PEM 檔。**不要**設 ``SSL_CERT_FILE``——
那個變數會整份取代系統信任庫，設錯會讓所有 HTTPS 全滅。
"""

from __future__ import annotations

import base64
import json
import math
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Mapping, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import (
    RSAPublicKey,
    RSAPublicNumbers,
)

DISPATCH_TOKEN_ISSUER = "anila-csp"
DISPATCH_TOKEN_AUDIENCE = "anila-agent"
DISPATCH_TOKEN_ALG = "RS256"
JWKS_PATH = "/.well-known/jwks.json"


class AnilaVerifyError(Exception):
    """驗章失敗（缺標頭、簽章錯、過期、JWKS 抓不到…）。"""


# ---------------------------------------------------------------------------
# base64url / JWK
# ---------------------------------------------------------------------------


def _b64url_decode(data: str) -> bytes:
    if not isinstance(data, str) or not data:
        raise AnilaVerifyError("空的 base64url 欄位")
    pad = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + pad)
    except (ValueError, TypeError) as exc:
        raise AnilaVerifyError(f"無效的 base64url: {exc}") from exc


def _b64url_decode_int(value: str) -> int:
    return int.from_bytes(_b64url_decode(value), "big")


def jwk_to_public_key(jwk: Mapping[str, Any]) -> RSAPublicKey:
    if not isinstance(jwk, Mapping):
        raise AnilaVerifyError("JWK 不是物件")
    if jwk.get("kty") != "RSA":
        raise AnilaVerifyError(f"不支援的 kty={jwk.get('kty')!r}（僅 RSA）")
    n_b64, e_b64 = jwk.get("n"), jwk.get("e")
    if not n_b64 or not e_b64:
        raise AnilaVerifyError("JWK 缺少 n 或 e")
    return RSAPublicNumbers(
        e=_b64url_decode_int(str(e_b64)),
        n=_b64url_decode_int(str(n_b64)),
    ).public_key()


def parse_jwks(document: Any) -> dict[str, RSAPublicKey]:
    if not isinstance(document, dict):
        raise AnilaVerifyError("JWKS 不是 JSON 物件")
    keys = document.get("keys")
    if not isinstance(keys, list) or not keys:
        raise AnilaVerifyError("JWKS 缺少 keys")
    out: dict[str, RSAPublicKey] = {}
    for entry in keys:
        if not isinstance(entry, dict):
            raise AnilaVerifyError("JWK 條目不是物件")
        kid = entry.get("kid")
        if not kid or not isinstance(kid, str):
            raise AnilaVerifyError("JWK 缺少 kid")
        out[kid] = jwk_to_public_key(entry)
    return out


def derive_jwks_url(csp_base_url: str) -> str:
    base = (csp_base_url or "").strip().rstrip("/")
    if not base:
        raise AnilaVerifyError("csp_base_url 不可為空")
    return f"{base}{JWKS_PATH}"


# ---------------------------------------------------------------------------
# HTTPS fetch (stdlib only; CA via ca_file — never SSL_CERT_FILE)
# ---------------------------------------------------------------------------


def _ssl_context(ca_file: Optional[str]) -> ssl.SSLContext:
    """Build a trust context from an optional CA bundle file.

    Deliberately does **not** read ``SSL_CERT_FILE``.
    """
    if ca_file:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.check_hostname = True
        ctx.load_verify_locations(cafile=ca_file)
        return ctx
    return ssl.create_default_context()


def fetch_jwks(
    jwks_url: str,
    *,
    ca_file: Optional[str] = None,
    timeout: float = 5.0,
) -> dict[str, RSAPublicKey]:
    """GET JWKS and return ``kid -> RSAPublicKey``."""
    url = (jwks_url or "").strip()
    if not url:
        raise AnilaVerifyError("jwks_url 不可為空")
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(
            req, context=_ssl_context(ca_file), timeout=timeout
        ) as resp:
            body = resp.read()
            status = getattr(resp, "status", None) or resp.getcode()
    except urllib.error.HTTPError as exc:
        raise AnilaVerifyError(f"JWKS HTTP {exc.code}") from exc
    except Exception as exc:  # noqa: BLE001 — surface as verify error
        raise AnilaVerifyError(f"無法抓取 JWKS: {exc}") from exc
    if status != 200:
        raise AnilaVerifyError(f"JWKS HTTP {status}")
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnilaVerifyError("JWKS 不是合法 JSON") from exc
    return parse_jwks(document)


# ---------------------------------------------------------------------------
# JWT verify
# ---------------------------------------------------------------------------


def _extract_bearer(authorization: Optional[str]) -> str:
    if not authorization or not isinstance(authorization, str):
        raise AnilaVerifyError("缺少 Authorization 標頭")
    parts = authorization.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise AnilaVerifyError("Authorization 必須是 Bearer <jwt>")
    return parts[1].strip()


def verify_jwt_rs256(
    token: str,
    public_key: RSAPublicKey,
    *,
    issuer: str = DISPATCH_TOKEN_ISSUER,
    audience: str = DISPATCH_TOKEN_AUDIENCE,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """驗 RS256 簽章與 iss/aud/exp，回傳 payload。"""
    if not token or not isinstance(token, str):
        raise AnilaVerifyError("缺少 token")
    parts = token.split(".")
    if len(parts) != 3:
        raise AnilaVerifyError("JWT 格式錯誤")
    header_b64, payload_b64, sig_b64 = parts
    try:
        header = json.loads(_b64url_decode(header_b64))
        payload = json.loads(_b64url_decode(payload_b64))
        signature = _b64url_decode(sig_b64)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnilaVerifyError("JWT 無法解碼") from exc
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise AnilaVerifyError("JWT 結構錯誤")
    if header.get("alg") != DISPATCH_TOKEN_ALG:
        raise AnilaVerifyError(f"不支援的 alg={header.get('alg')!r}")

    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    try:
        public_key.verify(
            signature,
            signing_input,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except InvalidSignature as exc:
        raise AnilaVerifyError("簽章無效") from exc

    if payload.get("iss") != issuer:
        raise AnilaVerifyError("iss 不符")
    aud = payload.get("aud")
    if isinstance(aud, list):
        if audience not in aud:
            raise AnilaVerifyError("aud 不符")
    elif aud != audience:
        raise AnilaVerifyError("aud 不符")

    clock = time.time() if now is None else float(now)
    exp = payload.get("exp")
    if exp is None:
        raise AnilaVerifyError("缺少 exp")
    try:
        exp_ts = float(exp)
    except (TypeError, ValueError) as exc:
        raise AnilaVerifyError("exp 無效") from exc
    if not math.isfinite(exp_ts):
        raise AnilaVerifyError("exp 無效")
    if clock >= exp_ts:
        raise AnilaVerifyError("權杖已過期")

    for claim in ("user_id", "department", "agent_id"):
        if claim not in payload:
            raise AnilaVerifyError(f"缺少 claim: {claim}")
    return payload


def verify_authorization(
    authorization: Optional[str],
    *,
    jwks_url: Optional[str] = None,
    csp_base_url: Optional[str] = None,
    ca_file: Optional[str] = None,
    jwks: Optional[Mapping[str, RSAPublicKey]] = None,
    issuer: str = DISPATCH_TOKEN_ISSUER,
    audience: str = DISPATCH_TOKEN_AUDIENCE,
    now: Optional[float] = None,
) -> dict[str, Any]:
    """驗 ``Authorization: Bearer <jwt>``，回傳已驗證 claims。

    提供 ``jwks``（已快取的 kid→金鑰）或 ``jwks_url`` / ``csp_base_url``
    其一即可。``ca_file`` 僅在需要現場抓 JWKS 時使用。
    """
    token = _extract_bearer(authorization)
    parts = token.split(".")
    if len(parts) != 3:
        raise AnilaVerifyError("JWT 格式錯誤")
    try:
        header = json.loads(_b64url_decode(parts[0]))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnilaVerifyError("JWT header 無法解碼") from exc
    if not isinstance(header, dict):
        raise AnilaVerifyError("JWT header 不是物件")
    kid = header.get("kid")
    if not kid or not isinstance(kid, str):
        raise AnilaVerifyError("JWT 缺少 kid")

    key_map: Mapping[str, RSAPublicKey]
    if jwks is not None:
        key_map = jwks
    else:
        url = (jwks_url or "").strip()
        if not url:
            if not csp_base_url:
                raise AnilaVerifyError("必須提供 jwks、jwks_url 或 csp_base_url")
            url = derive_jwks_url(csp_base_url)
        key_map = fetch_jwks(url, ca_file=ca_file)

    public_key = key_map.get(kid)
    if public_key is None:
        raise AnilaVerifyError(f"JWKS 沒有 kid={kid!r}")

    return verify_jwt_rs256(
        token,
        public_key,
        issuer=issuer,
        audience=audience,
        now=now,
    )


__all__ = [
    "AnilaVerifyError",
    "DISPATCH_TOKEN_AUDIENCE",
    "DISPATCH_TOKEN_ISSUER",
    "derive_jwks_url",
    "fetch_jwks",
    "parse_jwks",
    "verify_authorization",
    "verify_jwt_rs256",
]


if __name__ == "__main__":  # pragma: no cover
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="驗 ANILA 派工 Bearer JWT")
    parser.add_argument("authorization", help="完整 Authorization 標頭值或純 JWT")
    parser.add_argument("--jwks-url", default="", help="JWKS URL")
    parser.add_argument("--csp-base-url", default="", help="平台根 URL（推導 JWKS）")
    parser.add_argument("--ca-file", default="", help="CSPKI CA bundle PEM 路徑")
    args = parser.parse_args()
    auth = args.authorization
    if not auth.lower().startswith("bearer "):
        auth = f"Bearer {auth}"
    try:
        claims = verify_authorization(
            auth,
            jwks_url=args.jwks_url or None,
            csp_base_url=args.csp_base_url or None,
            ca_file=args.ca_file or None,
        )
    except AnilaVerifyError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(claims, ensure_ascii=False, indent=2))
