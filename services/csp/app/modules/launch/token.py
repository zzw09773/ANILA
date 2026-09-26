# -*- coding: utf-8 -*-
"""Launch token issuance — short-lived CSP-signed RS256 JWT (doc 07 §6).

The launch token rides the same RS256 keypair / ``kid`` as CSP's access
tokens, so registered services verify it LOCALLY via the existing
``GET /.well-known/jwks.json`` (checking ``aud`` / ``iss`` / ``exp`` /
signature — doc §6, §15.5). There is no server-side verify/consume endpoint in
this slice; ``service_launches.consumed_at`` is reserved for future one-time
semantics.

Hard rules (doc §6): TTL 5–10 min; NEVER embed a model API key or a long-lived
user JWT. The 14 claims are taken verbatim from doc §6.
"""

from __future__ import annotations

from datetime import datetime, timezone

from jose import jwt

from app.utils.security import ALGORITHM

LAUNCH_TOKEN_ISSUER = "anila-csp"
# doc §6 recommends 5–10 min; we mint at the 10-min upper bound.
LAUNCH_TOKEN_TTL_MINUTES = 10


def build_launch_claims(
    *,
    aud: str,
    launch_id: str,
    service_id: str,
    user_id: int | None,
    employee_id: str | None,
    department_id: int | None,
    roles: list[str],
    task_id: str | None,
    trace_id: str | None,
    classification_level: str,
    source_snapshot_id: str | None,
    issued_at: datetime,
    expires_at: datetime,
) -> dict:
    """The 14 launch-token claims (doc §6, verbatim order).

    No model API key, no long-lived user JWT — this function only accepts the
    identity/context fields listed in the doc, so neither can leak in.
    """
    return {
        "iss": LAUNCH_TOKEN_ISSUER,
        "aud": aud,
        "launch_id": launch_id,
        "service_id": service_id,
        "user_id": user_id,
        "employee_id": employee_id,
        "department_id": department_id,
        "roles": list(roles),
        "task_id": task_id,
        "trace_id": trace_id,
        "classification_level": classification_level,
        "source_snapshot_id": source_snapshot_id,
        "iat": int(issued_at.replace(tzinfo=timezone.utc).timestamp())
        if issued_at.tzinfo is None
        else int(issued_at.timestamp()),
        "exp": int(expires_at.replace(tzinfo=timezone.utc).timestamp())
        if expires_at.tzinfo is None
        else int(expires_at.timestamp()),
    }


def issue_launch_token(claims: dict, *, db=None) -> str:
    """Sign the launch claims with CSP's RS256 private key + active ``kid``.

    Same signing path as ``app.utils.security.create_access_token`` so the
    published JWKS verifies both.
    """
    from app.services.jwt_keyring import active_signing_material

    material = active_signing_material(db)
    return jwt.encode(
        claims,
        material.private_pem,
        algorithm=ALGORITHM,
        headers={"kid": material.kid, "typ": "JWT"},
    )
