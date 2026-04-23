"""Unified auth dependency for data-plane (``/v1/*``) endpoints.

Accepts either a JWT access token (issued by ``/api/auth/login`` / OIDC
callback) or a user API key (``sk-*`` Bearer). Both resolve to the same
``User``; the returned ``Caller`` also carries ``api_key_id`` when the
request presented an API key, so downstream usage writers can still
attribute traffic to a specific named key when one exists.

Why both paths share a dependency:
- The SPA holds a JWT (localStorage today, httpOnly cookie in Wave 2) and
  should not need to also hold an API key just to hit ``/v1/*``.
- SDK / curl users continue to paste ``sk-*`` into ``Authorization: Bearer``
  — that path is unchanged.
- Usage dashboards group by ``api_key_id``; JWT-only calls land in a
  ``api_key_id=NULL`` bucket rendered as "Web UI" in the UI.

Token discrimination is unambiguous: API keys are minted by
``api_key_service.generate_api_key`` with a literal ``sk-`` prefix, while
JWT tokens are base64url segments joined by dots. So
``token.startswith("sk-")`` is a safe splitter.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.services.api_key_service import validate_api_key
from app.services.auth_service import _load_user_from_payload
from app.utils.security import decode_token


@dataclass(frozen=True)
class Caller:
    """Resolved caller identity for ``/v1/*`` endpoints."""

    user: User
    api_key_id: int | None


# Cookie name reserved for Wave 2 (httpOnly cookie-based JWT). Reading it now
# is a no-op when the SPA still sends Authorization headers, but it means
# Wave 2 can ship without touching ``get_caller`` again.
ACCESS_COOKIE_NAME = "anila_access_token"


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    if not authorization.startswith("Bearer "):
        return None
    token = authorization[7:].strip()
    return token or None


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def get_caller(
    request: Request,
    db: Session = Depends(get_db),
) -> Caller:
    """Resolve the caller from either a JWT or an API key.

    Precedence: Authorization header > cookie. The cookie branch exists for
    Wave 2's httpOnly SPA flow; today all SPA traffic still comes in via the
    Authorization header, so this just reserves the wiring.
    """
    token = _extract_bearer(request.headers.get("Authorization"))
    if token is None:
        token = request.cookies.get(ACCESS_COOKIE_NAME)
    if not token:
        raise _unauthorized("缺少認證資訊，請提供 Bearer 權杖或登入後 cookie")

    if token.startswith("sk-"):
        api_key = validate_api_key(db, token)
        if not api_key:
            raise _unauthorized("無效或已過期的 API Key")
        user = api_key.user
        if not user or not user.is_active:
            raise _unauthorized("API Key 對應的使用者已停用")
        return Caller(user=user, api_key_id=api_key.id)

    payload = decode_token(token)
    user = _load_user_from_payload(payload, db, "access")
    return Caller(user=user, api_key_id=None)
