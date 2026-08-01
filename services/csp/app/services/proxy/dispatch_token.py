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
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from jose import jwt

from app.config import settings
from app.utils.security import ALGORITHM, get_private_key

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
