"""Local JWT verification for anila-studio.

csp signs JWTs with RS256 (private key). anila-studio fetches the public
key from csp's JWKS endpoint and verifies tokens locally — no DB lookup
required for the common case.

Revocation:
- csp publishes ``anila:auth:token-revoke`` Redis events when a user's
  ``token_version`` bumps (logout / password change / admin revoke).
- ``revocation_cache`` subscribes + maintains a 30-day deny-list.
- If ``revocation_cache`` is not ready (Redis disconnected, cold-start
  not yet completed), authenticated endpoints **fail-closed with 503** —
  this is the v2 policy fix for plan v1's R14 (do NOT degrade to
  TTL-only mode that would allow banned users access during outage).

Public surface:
- ``CurrentUserIdentity`` dataclass — what handlers receive
- ``get_current_user_identity`` FastAPI dependency — drop-in replacement
  for csp's ``Depends(get_current_user)``
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import Cookie, Depends, Header, HTTPException, status
from jose import JWTError, jwt
from jose.exceptions import ExpiredSignatureError

from app.config import settings
from app.services import jwks_client, revocation_cache as revocation_cache_mod


logger = logging.getLogger(__name__)


ACCESS_COOKIE_NAME = "anila_access_token"


@dataclass(frozen=True)
class CurrentUserIdentity:
    """The thin identity object anila-studio handlers carry.

    Mirrors what csp's ``User`` row provides for studio's needs, but is
    a frozen dataclass — no DB-row mutation hazards. Populate from JWT
    claims; do not hit DB.
    """

    id: int
    username: str
    role: str
    token_version: int


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _service_unavailable(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=detail,
    )


async def _verify_jwt(token: str) -> dict:
    """RS256 + JWKS verify; raise 401 on any failure."""
    try:
        header = jwt.get_unverified_header(token)
    except JWTError as exc:
        logger.debug("invalid JWT header: %s", exc)
        raise _unauthorized("無效的存取權杖") from exc

    kid = header.get("kid")
    if not kid:
        # algorithm-confusion defence: reject tokens without kid
        logger.debug("JWT missing kid header")
        raise _unauthorized("無效的存取權杖")

    try:
        public_key = await jwks_client.get_public_key(kid)
    except jwks_client.JwksKeyNotFoundError as exc:
        logger.warning("JWT kid=%s not present in JWKS", kid)
        raise _unauthorized("無效的存取權杖") from exc
    except jwks_client.JwksFetchError as exc:
        # csp JWKS unreachable — service degraded, fail-closed
        logger.error("JWKS fetch failed during verify: %s", exc)
        raise _service_unavailable("auth keys unavailable") from exc

    try:
        payload = jwt.decode(
            token,
            public_key,
            algorithms=list(settings.JWT_ALGORITHMS),
            options={"verify_aud": False},
        )
    except ExpiredSignatureError as exc:
        raise _unauthorized("權杖已過期，請重新登入") from exc
    except JWTError as exc:
        logger.debug("JWT verify failed: %s", exc)
        raise _unauthorized("無效的存取權杖") from exc

    if not jwks_client.cached_key_allows(kid, payload):
        raise _unauthorized("簽章金鑰不在簽發期間內")
    return payload


async def _check_revocation(user_id: int, token_version: int, kid: str = "") -> None:
    """Consult the cross-service revocation cache.

    Fail-closed: if the cache itself is not ready (Redis down / cold-start
    not completed) we return 503 instead of letting potentially-revoked
    tokens through. This is the v2 plan policy.
    """
    cache = revocation_cache_mod.get_revocation_cache()
    if not cache.ready:
        # Don't allow auth while we can't enforce revocation deny-list.
        # k8s readiness probe will already be 503-ing /health; this guards
        # any in-flight requests during the window.
        logger.warning(
            "revocation cache not ready; denying request for user_id=%s", user_id
        )
        raise _service_unavailable("auth deny-list unhealthy")
    kid_revoked = getattr(cache, "is_kid_revoked", None)
    if kid and kid_revoked is not None and await kid_revoked(kid):
        raise _unauthorized("簽章金鑰已撤銷，請重新登入")
    if await cache.is_revoked(user_id, token_version):
        logger.info(
            "rejecting revoked token: user_id=%s tv=%s", user_id, token_version
        )
        # 「權杖已失效」對使用者沒有可行動的資訊,而且和「權杖已過期」的解法
        # 不同:過期只要重新登入,被撤銷則要先知道自己是被誰、為什麼撤的。
        # 講清楚成因,並給出「重登仍失敗 → 找管理員」這條出路。
        raise _unauthorized(
            "此工作階段已被撤銷（變更密碼、登出，或管理員強制登出）。"
            "請重新登入；若重新登入後仍被拒絕，請聯絡管理員。"
        )


async def get_current_user_identity(
    authorization: str | None = Header(default=None),
    anila_access_token: str | None = Cookie(default=None, alias=ACCESS_COOKIE_NAME),
) -> CurrentUserIdentity:
    """FastAPI dependency: resolve identity from Bearer header OR cookie.

    Mirrors csp's ``get_current_user`` token sourcing precedence
    (Authorization header wins, anila_access_token cookie is SPA fallback)
    but uses local RS256 + JWKS verify instead of csp's HS256 + DB.

    Returns ``CurrentUserIdentity`` (frozen dataclass). Raises 401 for
    bad/missing token, 503 if revocation cache is unhealthy.
    """
    token: str | None = None
    if authorization:
        # "Bearer <token>" — accept case-insensitive scheme name
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value:
            token = value
    if token is None and anila_access_token:
        token = anila_access_token

    if not token:
        raise _unauthorized("未登入或權杖已過期")

    payload = await _verify_jwt(token)

    # csp's create_access_token sets type="access" on access tokens
    # and type="refresh" on refresh tokens. Refuse refresh tokens at
    # the studio API surface.
    if payload.get("type") != "access":
        raise _unauthorized("無效的存取權杖")

    sub = payload.get("sub")
    if not sub:
        raise _unauthorized("無效的存取權杖")
    try:
        user_id = int(sub)
    except (TypeError, ValueError) as exc:
        raise _unauthorized("無效的存取權杖") from exc

    token_version = int(payload.get("tv", 0))
    try:
        header_kid = jwt.get_unverified_header(token).get("kid")
    except JWTError:
        header_kid = ""

    await _check_revocation(
        user_id,
        token_version,
        kid=header_kid if isinstance(header_kid, str) else "",
    )

    return CurrentUserIdentity(
        id=user_id,
        username=str(payload.get("username") or ""),
        role=str(payload.get("role") or "user"),
        token_version=token_version,
    )


def _extract_bearer_for_csp_proxy(authorization: str | None) -> str:
    """Extract the raw bearer token (still RS256-signed) for forwarding to
    csp on behalf of the user.

    Studio handlers will call this to grab the token they should pass to
    ``csp_client.*`` functions — csp re-verifies it via its own RS256 +
    JWKS path so user-on-behalf-of semantics are preserved.
    """
    if not authorization:
        raise _unauthorized("未登入或權杖已過期")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value:
        raise _unauthorized("未登入或權杖已過期")
    return value


async def get_bearer_token(
    authorization: str | None = Header(default=None),
    anila_access_token: str | None = Cookie(default=None, alias=ACCESS_COOKIE_NAME),
) -> str:
    """FastAPI dependency: return the raw bearer token string for csp passthrough.

    Studio handlers will declare ``Depends(get_bearer_token)`` alongside
    ``Depends(get_current_user_identity)`` so they can pass the token to
    ``csp_client.search_chunks(..., bearer=token)`` etc. The verify step
    has already happened in ``get_current_user_identity``; this dependency
    is just transport.
    """
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value:
            return value
    if anila_access_token:
        return anila_access_token
    raise _unauthorized("未登入或權杖已過期")
