"""JSON Web Key Set (JWKS) endpoint — RFC 7517.

Sprint 9 / anila-studio extraction: CSP signs JWTs with an RSA private
key (see ``app.utils.security``) and exposes the matching public key
here so downstream services can verify locally without sharing a
symmetric secret.

The endpoint is intentionally:

* Unauthenticated — public keys are public by definition.
* Cached for one hour via ``Cache-Control: max-age=3600`` — JWKS
  consumers honour this header to avoid hammering CSP on every verify.
* Mounted under ``/.well-known/jwks.json`` — the IETF-reserved path for
  JWKS discovery (RFC 8615 + draft-ietf-jose-json-web-key-discovery).

Today we publish exactly one key (``kid = settings.JWT_KID``). When
rotation lands, the loader returns a list of (kid, public_pem) tuples
and ``_serialize_jwks`` iterates — no change to the response shape.
"""
from __future__ import annotations

import base64
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from fastapi import APIRouter, Response

from app.config import settings
from app.utils.security import get_public_key


router = APIRouter(prefix="/.well-known", tags=["auth"])


_CACHE_HEADER_VALUE = "max-age=3600, public"


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


def _load_public_key() -> RSAPublicKey:
    """Parse the SPKI PEM CSP signs with into a cryptography object."""
    pem = get_public_key()
    key = serialization.load_pem_public_key(pem)
    if not isinstance(key, RSAPublicKey):
        raise RuntimeError(
            "configured JWT public key is not an RSA key — "
            "JWKS only supports RS256 today"
        )
    return key


def _serialize_jwks() -> dict[str, Any]:
    """Build the JWKS document the endpoint returns.

    Shape follows RFC 7517 §4 / §5:

    * ``kty`` — always ``"RSA"`` because we use RS256.
    * ``use`` — ``"sig"`` (signature verification, not encryption).
    * ``alg`` — ``"RS256"`` so consumers can short-circuit before
      decoding the key material.
    * ``kid`` — matches the ``kid`` header CSP stamps on every JWT.
    * ``n`` / ``e`` — RSA modulus and public exponent, base64url
      without padding.
    """
    public_key = _load_public_key()
    numbers = public_key.public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": settings.JWT_KID,
                "n": _int_to_base64url(numbers.n),
                "e": _int_to_base64url(numbers.e),
            }
        ]
    }


@router.get(
    "/jwks.json",
    summary="JWKS for RS256 JWT verification",
    response_description="RFC 7517 JSON Web Key Set",
)
async def jwks(response: Response) -> dict[str, Any]:
    """Public verification keys for tokens issued by this CSP.

    ``Cache-Control`` is set so clients (anila-studio's verifier in
    particular) cache the JWKS for an hour; key rotation flows must
    publish the new key BEFORE retiring the old one so existing tokens
    keep verifying through the cache window.
    """
    response.headers["Cache-Control"] = _CACHE_HEADER_VALUE
    return _serialize_jwks()


__all__ = ["router"]
