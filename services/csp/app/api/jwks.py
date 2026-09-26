"""JWKS（RFC 7517）。

不需登入。公布金鑰圈裡的 next、active、retiring，不公布 retired。
``Cache-Control`` 的 max-age 與輪替提前公布的最短間隔是同一個常數。
簽名只用 active。next 先公布，但升成 active 之前不能拿來驗權杖。
每把鑰匙帶 ``anila_key_state``；retiring 另帶 ``anila_iat_not_after``。
"""
from __future__ import annotations

import base64
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.jwt_keyring import JWKS_CACHE_MAX_AGE_SECONDS, PublishedJwk, jwks_material


router = APIRouter(prefix="/.well-known", tags=["auth"])


_CACHE_HEADER_VALUE = f"max-age={JWKS_CACHE_MAX_AGE_SECONDS}, public"


def _int_to_base64url(value: int) -> str:
    """Encode a non-negative integer as base64url (no padding) per
    RFC 7518 §6.3.1.

    JWKS ``n`` / ``e`` use big-endian byte order with the minimum
    number of bytes that fit the value. ``int.to_bytes(byte_length,
    "big")`` paired with ``(bit_length + 7) // 8`` is the canonical
    incantation — Python rejects negative lengths so we handle the
    zero case explicitly.
    """
    if value == 0:
        return "AA"  # single zero byte, base64url("\x00")
    byte_length = (value.bit_length() + 7) // 8
    raw = value.to_bytes(byte_length, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _jwk_from_pem(kid: str, public_pem: str) -> dict[str, Any]:
    key = serialization.load_pem_public_key(public_pem.encode("utf-8"))
    if not isinstance(key, RSAPublicKey):
        raise RuntimeError("JWKS 只公布 RSA 公鑰")
    numbers = key.public_numbers()
    return {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": _int_to_base64url(numbers.n),
        "e": _int_to_base64url(numbers.e),
    }


def _jwk_from_published(item: PublishedJwk) -> dict[str, Any]:
    body = _jwk_from_pem(item.kid, item.public_pem)
    body["anila_key_state"] = item.state
    body["anila_accept_missing_iat"] = item.accept_missing_iat
    if item.iat_not_after is not None:
        body["anila_iat_not_after"] = item.iat_not_after
    return body


def _serialize_jwks(db: Session | None = None) -> dict[str, Any]:
    """組出 JWKS。``keys`` 含 next、active、retiring。"""
    material, _inserted = jwks_material(db)
    if not material:
        raise RuntimeError("沒有可公布的簽章公鑰")
    return {"keys": [_jwk_from_published(item) for item in material]}


@router.get(
    "/jwks.json",
    summary="JWKS for RS256 JWT verification",
    response_description="RFC 7517 JSON Web Key Set",
)
async def jwks(
    response: Response,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """公布中的驗簽公鑰。快取週期內，下一把必須已經在這份文件裡。"""
    material, inserted = jwks_material(db)
    if inserted:
        db.commit()
    if not material:
        raise RuntimeError("沒有可公布的簽章公鑰")
    response.headers["Cache-Control"] = _CACHE_HEADER_VALUE
    return {"keys": [_jwk_from_published(item) for item in material]}


__all__ = ["router"]
