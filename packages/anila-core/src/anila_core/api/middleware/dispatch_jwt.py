"""RS256 JWT verify for CSP dispatch tokens (stdlib + cryptography only).

Mirrors W1 contract (``services/csp/.../dispatch_token.py``):

* ``iss`` = ``anila-csp``
* ``aud`` = ``anila-agent``
* identity claims: ``user_id``, ``department``, ``agent_id``
* header ``kid`` selects the JWKS key; ``alg`` must be ``RS256``
"""

from __future__ import annotations

import base64
import json
import math
import time
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey


DISPATCH_TOKEN_ISSUER = "anila-csp"
DISPATCH_TOKEN_AUDIENCE = "anila-agent"
DISPATCH_TOKEN_ALG = "RS256"


class DispatchTokenError(Exception):
    """Dispatch JWT failed verification (any reason)."""


def _b64url_decode(data: str) -> bytes:
    if not isinstance(data, str) or not data:
        raise DispatchTokenError("empty JWT segment")
    pad = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data + pad)
    except (ValueError, TypeError) as exc:
        raise DispatchTokenError(f"invalid base64url: {exc}") from exc


def parse_unverified_header(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise DispatchTokenError("malformed JWT")
    try:
        header = json.loads(_b64url_decode(parts[0]))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DispatchTokenError("invalid JWT header") from exc
    if not isinstance(header, dict):
        raise DispatchTokenError("JWT header is not an object")
    return header


def verify_dispatch_jwt(
    token: str,
    public_key: RSAPublicKey,
    *,
    issuer: str = DISPATCH_TOKEN_ISSUER,
    audience: str = DISPATCH_TOKEN_AUDIENCE,
    now: float | None = None,
) -> dict[str, Any]:
    """Verify RS256 signature + registered claims; return payload dict.

    Raises :class:`DispatchTokenError` on any failure (fail-closed).
    """
    if not token or not isinstance(token, str):
        raise DispatchTokenError("missing token")

    parts = token.split(".")
    if len(parts) != 3:
        raise DispatchTokenError("malformed JWT")
    header_b64, payload_b64, sig_b64 = parts

    try:
        header = json.loads(_b64url_decode(header_b64))
        payload = json.loads(_b64url_decode(payload_b64))
        signature = _b64url_decode(sig_b64)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DispatchTokenError("invalid JWT encoding") from exc

    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise DispatchTokenError("invalid JWT structure")

    if header.get("alg") != DISPATCH_TOKEN_ALG:
        raise DispatchTokenError(
            f"unsupported alg {header.get('alg')!r}; only RS256 accepted"
        )

    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    try:
        public_key.verify(
            signature,
            signing_input,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except InvalidSignature as exc:
        raise DispatchTokenError("invalid signature") from exc

    if payload.get("iss") != issuer:
        raise DispatchTokenError("invalid issuer")

    aud = payload.get("aud")
    if isinstance(aud, list):
        if audience not in aud:
            raise DispatchTokenError("invalid audience")
    elif aud != audience:
        raise DispatchTokenError("invalid audience")

    clock = time.time() if now is None else float(now)
    exp = payload.get("exp")
    if exp is None:
        raise DispatchTokenError("missing exp")
    try:
        exp_ts = float(exp)
    except (TypeError, ValueError) as exc:
        raise DispatchTokenError("invalid exp") from exc
    if not math.isfinite(exp_ts):
        raise DispatchTokenError("invalid exp")
    if clock >= exp_ts:
        raise DispatchTokenError("token expired")

    for claim in ("user_id", "agent_id"):
        if claim not in payload:
            raise DispatchTokenError(f"missing claim {claim}")

    # ``department`` may be null (W1 allows Optional[int]).
    if "department" not in payload:
        raise DispatchTokenError("missing claim department")

    return payload


def extract_bearer(authorization: str | None) -> str:
    """Parse ``Authorization: Bearer <jwt>``; raise on missing/malformed."""
    if not authorization or not isinstance(authorization, str):
        raise DispatchTokenError("missing Authorization header")
    parts = authorization.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise DispatchTokenError("Authorization must be Bearer <jwt>")
    return parts[1].strip()


__all__ = [
    "DISPATCH_TOKEN_ALG",
    "DISPATCH_TOKEN_AUDIENCE",
    "DISPATCH_TOKEN_ISSUER",
    "DispatchTokenError",
    "extract_bearer",
    "parse_unverified_header",
    "verify_dispatch_jwt",
]
