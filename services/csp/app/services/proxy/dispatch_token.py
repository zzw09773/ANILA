# -*- coding: utf-8 -*-
"""Per-dispatch short-lived identity JWT (P2.1 / SYSTEM-MAP §身分).

CSP signs one 5-minute RS256 token on every agent dispatch. Claims are the
spec's three identity fields (``user_id``, ``department``, ``agent_id``)
plus standard registered claims. Same signing path as launch / access
tokens (``get_private_key`` + ``kid`` header) so ``/.well-known/jwks.json``
verifies these tokens.

TTL is a module constant — not a config knob (owner iron rule).
``issue_dispatch_token`` does not accept time overrides: 5 minutes is an
invariant for production minting. Tests that need fixed clocks / expired
fixtures go through ``build_dispatch_claims`` instead.

W2 (this module's verify half): in-task agent→CSP callbacks present the
same token; CSP verifies it with the local kid→public-key path already
used by ``decode_token`` (not a copied JWKS client).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from jose import JWTError, jwt

from app.config import settings
from app.utils.security import ALGORITHM, _public_key_for_kid, get_private_key

DISPATCH_TOKEN_ISSUER = "anila-csp"
DISPATCH_TOKEN_AUDIENCE = "anila-agent"
DISPATCH_TOKEN_TTL_MINUTES = 5


def build_dispatch_claims(
    *,
    user_id: int,
    department: int | None,
    agent_id: int,
    issued_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> dict:
    """Build the dispatch-token claims (spec minimalism).

    Identity claims: ``user_id``, ``department``, ``agent_id``.
    Registered: ``iss`` / ``aud`` / ``sub`` / ``iat`` / ``exp`` / ``jti``.

    Time overrides are for test fixtures only (e.g. expired tokens).
    Production minting goes through ``issue_dispatch_token``, which never
    passes them.
    """
    now = issued_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    exp = expires_at or (now + timedelta(minutes=DISPATCH_TOKEN_TTL_MINUTES))
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return {
        "iss": DISPATCH_TOKEN_ISSUER,
        "aud": DISPATCH_TOKEN_AUDIENCE,
        "sub": str(user_id),
        "user_id": user_id,
        "department": department,
        "agent_id": agent_id,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "jti": uuid.uuid4().hex,
    }


def issue_dispatch_token(
    *,
    user_id: int,
    department: int | None,
    agent_id: int,
) -> str:
    """Sign a 5-minute dispatch identity token with CSP's RS256 key + kid.

    TTL is fixed by ``DISPATCH_TOKEN_TTL_MINUTES`` — callers cannot widen it.
    """
    claims = build_dispatch_claims(
        user_id=user_id,
        department=department,
        agent_id=agent_id,
    )
    return jwt.encode(
        claims,
        get_private_key(),
        algorithm=ALGORITHM,
        headers={"kid": settings.JWT_KID, "typ": "JWT"},
    )


def verify_dispatch_token(token: str) -> dict[str, Any] | None:
    """Verify a CSP-signed dispatch JWT; return claims or None.

    Uses ``security._public_key_for_kid`` (same kid rule as access-token
    verify / JWKS). ``aud`` / ``iss`` are enforced here (python-jose
    requires ``audience=`` whenever the token carries ``aud``, so the bare
    ``decode_token`` helper cannot be reused for this shape). ``exp`` must
    be an ``int`` — jose skips the check when absent, and non-int values
    would otherwise surface as TypeError → HTTP 500.
    Non-dispatch JWTs (user access, launch, …) return None.
    """
    if not token or not isinstance(token, str):
        return None
    # Fast reject for the retired agent static credential prefix — never
    # feed ``csk-…`` into the JWT decoder (and never dual-accept it).
    if token.startswith("csk-"):
        return None
    try:
        header = jwt.get_unverified_header(token)
    except JWTError:
        return None
    public_key = _public_key_for_kid(header.get("kid"))
    if public_key is None:
        return None
    try:
        claims = jwt.decode(
            token,
            public_key,
            algorithms=[ALGORITHM],
            audience=DISPATCH_TOKEN_AUDIENCE,
            issuer=DISPATCH_TOKEN_ISSUER,
        )
    except (JWTError, TypeError):
        # TypeError: jose int()-coerces exp; None/[]/{} must not become HTTP 500.
        return None
    # python-jose silently skips exp validation when the claim is absent;
    # the 5-minute TTL is the entire security model — reject missing/non-int.
    if not isinstance(claims.get("exp"), int):
        return None
    if "user_id" not in claims or "agent_id" not in claims or "department" not in claims:
        return None
    try:
        int(claims["user_id"])
        int(claims["agent_id"])
    except (TypeError, ValueError):
        return None
    return claims


def extract_bearer_token(authorization: str | None) -> str | None:
    """Return the raw Bearer credential, or None if the header is absent/malformed."""
    if not authorization or not isinstance(authorization, str):
        return None
    parts = authorization.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        return None
    return parts[1].strip()


__all__ = [
    "DISPATCH_TOKEN_AUDIENCE",
    "DISPATCH_TOKEN_ISSUER",
    "DISPATCH_TOKEN_TTL_MINUTES",
    "build_dispatch_claims",
    "extract_bearer_token",
    "issue_dispatch_token",
    "verify_dispatch_token",
]
