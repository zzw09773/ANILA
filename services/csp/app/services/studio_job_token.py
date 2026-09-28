# -*- coding: utf-8 -*-
"""Job-scoped delegation JWT for Studio.

When Studio accepts a job it exchanges the user's short-lived access token
for one of these. Later Studio→CSP calls of that job present it instead of
the cookie/bearer that arrived with the create request, so a long job
survives access-token expiry.

Same signing path as agent dispatch tokens (RS256, active keyring kid).
Audience is ``studio``. Claims carry ``user_id``, ``job_id`` and the
collection's ``classification``. TTL is a module constant — not a config
knob. ``issue_studio_job_token`` does not accept time overrides.

The token is not a renewable session: password changes, API-key mutations
and reminting reject it. Revocation follows ``users.token_version`` and the
``token_revocations`` list (reject ``tv < revoked_at_version``).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, Request, status
from jose import JWTError, jwt
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.token_revocation import TokenRevocation
from app.models.user import User
from app.utils.security import ALGORITHM

STUDIO_JOB_TOKEN_ISSUER = "anila-csp"
STUDIO_JOB_TOKEN_AUDIENCE = "studio"
STUDIO_JOB_TOKEN_TTL_MINUTES = 60
STUDIO_JOB_TOKEN_TYPE = "studio_job"

_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_CLASSIFICATIONS = frozenset({"無機密", "營業秘密", "密", "機密"})


def _non_bool_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def build_studio_job_claims(
    *,
    user_id: int,
    job_id: str,
    classification: str,
    token_version: int,
    issued_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> dict:
    """Build job-token claims.

    Time overrides are for fixtures only. Production minting goes through
    ``issue_studio_job_token``, which never passes them.
    """
    uid = _non_bool_int(user_id)
    tv = _non_bool_int(token_version)
    if uid is None or uid <= 0 or tv is None or tv < 0:
        raise ValueError("user_id 與 token_version 必須是非負整數")
    if not isinstance(job_id, str) or _JOB_ID_RE.fullmatch(job_id) is None:
        raise ValueError("job_id 格式不合法")
    if classification not in _CLASSIFICATIONS:
        raise ValueError("classification 不是四級分類之一")
    now = issued_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    iat = int(now.timestamp())
    if expires_at is None:
        exp = iat + STUDIO_JOB_TOKEN_TTL_MINUTES * 60
    else:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        exp = int(expires_at.timestamp())
    return {
        "iss": STUDIO_JOB_TOKEN_ISSUER,
        "aud": STUDIO_JOB_TOKEN_AUDIENCE,
        "sub": str(uid),
        "user_id": uid,
        "job_id": job_id,
        "classification": classification,
        "tv": tv,
        "type": STUDIO_JOB_TOKEN_TYPE,
        "iat": iat,
        "exp": exp,
        "jti": uuid.uuid4().hex,
    }


def issue_studio_job_token(
    *,
    user_id: int,
    job_id: str,
    classification: str,
    token_version: int,
    db=None,
) -> str:
    """Sign a 60-minute job token. Callers cannot widen the TTL."""
    claims = build_studio_job_claims(
        user_id=user_id,
        job_id=job_id,
        classification=classification,
        token_version=token_version,
    )
    from app.services.jwt_keyring import active_signing_material

    material = active_signing_material(db)
    return jwt.encode(
        claims,
        material.private_pem,
        algorithm=ALGORITHM,
        headers={"kid": material.kid, "typ": "JWT"},
    )


def presented_studio_job_token(token: str | None) -> bool:
    """True when the unverified claims say this is a studio job token.

    A forged or expired token still returns True so callers 401 it instead
    of falling through to the access-token decoder.
    """
    if not token or not isinstance(token, str):
        return False
    if token.startswith("sk-") or token.startswith("csk-"):
        return False
    try:
        claims = jwt.get_unverified_claims(token)
    except JWTError:
        return False
    if not isinstance(claims, dict):
        return False
    return (
        claims.get("aud") == STUDIO_JOB_TOKEN_AUDIENCE
        or claims.get("type") == STUDIO_JOB_TOKEN_TYPE
    )


def verify_studio_job_token(token: str, db=None) -> dict[str, Any] | None:
    """Verify a CSP-signed studio job JWT; return claims or None."""
    if not presented_studio_job_token(token):
        return None
    from app.utils.security import _claims_if_issuance_allowed

    claims = _claims_if_issuance_allowed(
        token,
        db,
        audience=STUDIO_JOB_TOKEN_AUDIENCE,
        issuer=STUDIO_JOB_TOKEN_ISSUER,
    )
    if claims is None:
        return None
    if claims.get("type") != STUDIO_JOB_TOKEN_TYPE:
        return None
    if not isinstance(claims.get("exp"), int) or isinstance(claims.get("exp"), bool):
        return None
    user_id = _non_bool_int(claims.get("user_id"))
    tv = _non_bool_int(claims.get("tv"))
    if user_id is None or user_id <= 0 or tv is None or tv < 0:
        return None
    if str(claims.get("sub")) != str(user_id):
        return None
    job_id = claims.get("job_id")
    if not isinstance(job_id, str) or _JOB_ID_RE.fullmatch(job_id) is None:
        return None
    if claims.get("classification") not in _CLASSIFICATIONS:
        return None
    return claims


def load_studio_job_user(claims: dict[str, Any], db: Session) -> User:
    """Resolve the user a verified job token acts as.

    ``tv`` must equal ``users.token_version``, and must not sit below the
    revocation list's post-bump floor.
    """
    user = db.query(User).filter(User.id == int(claims["user_id"])).first()
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="使用者不存在或已停用",
        )
    tv = int(claims["tv"])
    if tv != int(user.token_version or 0):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="權杖已失效，請重新登入",
        )
    floor = (
        db.query(func.max(TokenRevocation.revoked_at_version))
        .filter(TokenRevocation.user_id == user.id)
        .scalar()
    )
    if floor is not None and tv < int(floor):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="權杖已失效，請重新登入",
        )
    return user


def bearer_from_request(request: Request) -> str | None:
    """Authorization header wins over the access cookie, matching get_current_user."""
    authorization = request.headers.get("authorization")
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        if token:
            return token
    from app.middleware.cookies import ACCESS_COOKIE_NAME

    cookie = request.cookies.get(ACCESS_COOKIE_NAME)
    return cookie or None


def reject_studio_job_credential(request: Request) -> None:
    """403 when the presented credential is a studio job token.

    Account mutations (password, API keys, reminting) are not part of the
    job. Data-plane calls keep using the user's own grants.
    """
    token = bearer_from_request(request)
    if token and presented_studio_job_token(token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="工作委託權杖不能用於這個操作",
        )


__all__ = [
    "STUDIO_JOB_TOKEN_AUDIENCE",
    "STUDIO_JOB_TOKEN_ISSUER",
    "STUDIO_JOB_TOKEN_TTL_MINUTES",
    "STUDIO_JOB_TOKEN_TYPE",
    "bearer_from_request",
    "build_studio_job_claims",
    "issue_studio_job_token",
    "load_studio_job_user",
    "presented_studio_job_token",
    "reject_studio_job_credential",
    "verify_studio_job_token",
]
