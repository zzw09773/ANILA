"""WS 握手的 JWT 驗證。

驗證邏輯逐條移植自 `services/anila-studio/app/auth.py`(RS256 + JWKS 本地驗
章、session envelope 檢查、拒絕 refresh token、fail-closed 撤銷查核)。
**不要在這裡「簡化」任何一條** —— 每一條都是信任錨的一部分,studio 那份的
註解說明了它們為什麼存在。

與 studio 的兩處必要差異(不是隨意偏離):

1. **取權杖的方式**:studio 用 FastAPI 的 `Depends(Cookie/Header)` 注入,WS
   握手沒有那套 —— 改成從 `websocket.cookies` / `websocket.headers` 手動取。
   瀏覽器的 WebSocket API **不能帶 Authorization header**,所以對前端而言
   cookie 是唯一路徑;Bearer 只有測試與服務間會用。
2. **失敗的表達方式**:studio 丟 HTTPException(401/503),WS 沒有 HTTP 狀態
   碼可回 —— 改成丟自訂例外,由呼叫端映射成 close code。刻意分成兩種:
   `AuthError`(權杖有問題 → 4401,叫使用者重新登入)與
   `AuthUnavailable`(驗證基礎設施掛了 → 4503,重新登入也沒用)。把後者也
   回 4401 會叫使用者去做一件解決不了問題的事。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from jose import JWTError, jwt
from jose.exceptions import ExpiredSignatureError
from starlette.websockets import WebSocket

from app.config import settings
from app.services import jwks_client, revocation_cache as revocation_cache_mod

logger = logging.getLogger(__name__)

SECURE_ACCESS_COOKIE_NAME = "__Host-anila_access_token"
DEV_ACCESS_COOKIE_NAME = "anila_dev_access_token"

_ACR_BY_PRIMARY_AMR = {
    "sc": "urn:anila:acr:smart-card",
    "oidc": "urn:anila:acr:federated",
    "pwd": "urn:anila:acr:password",
}
_KNOWN_AMR = frozenset(_ACR_BY_PRIMARY_AMR)


def _access_cookie_name(secure: bool) -> str:
    return SECURE_ACCESS_COOKIE_NAME if secure else DEV_ACCESS_COOKIE_NAME


ACCESS_COOKIE_NAME = _access_cookie_name(settings.COOKIE_SECURE)


class AuthError(Exception):
    """權杖無效/過期/被撤銷 → close 4401。重新登入可解。"""


class AuthUnavailable(Exception):
    """JWKS 拉不到、撤銷清單未就緒 → close 4503。fail-closed,重登無用。"""


@dataclass(frozen=True)
class CurrentUserIdentity:
    id: int
    username: str
    role: str
    token_version: int
    # 長連線重查撤銷要用(studio 是 per-request 所以不必留)。
    jti: str
    sid: str


def extract_token(websocket: WebSocket) -> str | None:
    """Authorization header 優先,cookie 次之(對齊 studio 的取用順序)。"""
    authorization = websocket.headers.get("authorization")
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value:
            return value
    return websocket.cookies.get(ACCESS_COOKIE_NAME)


def _has_valid_session_assurance(payload: dict) -> bool:
    """Mirror CSP's signed session-envelope validation at this boundary.

    逐行移植自 studio,不要改。
    """
    jti = payload.get("jti")
    sid = payload.get("sid")
    amr = payload.get("amr")
    acr = payload.get("acr")
    auth_time = payload.get("auth_time")
    issued_at = payload.get("iat")
    break_glass = payload.get("break_glass")
    if not isinstance(jti, str) or not jti:
        return False
    if not isinstance(sid, str) or not sid:
        return False
    if (
        not isinstance(amr, list)
        or any(
            not isinstance(method, str) or method not in _KNOWN_AMR
            for method in amr
        )
        or len(amr) != len(set(amr))
    ):
        return False
    if not isinstance(acr, str) or not acr:
        return False
    if (
        isinstance(auth_time, bool)
        or not isinstance(auth_time, (int, float))
        or isinstance(issued_at, bool)
        or not isinstance(issued_at, (int, float))
        or auth_time < 0
        or auth_time > issued_at
        or issued_at
        > datetime.now(timezone.utc).timestamp() + settings.JWT_LEEWAY_SECONDS
    ):
        return False
    if not isinstance(break_glass, bool):
        return False
    if break_glass:
        return amr == ["pwd"] and acr == "urn:anila:acr:break-glass"
    expected_acr = "urn:anila:acr:unspecified"
    for method in ("sc", "oidc", "pwd"):
        if method in amr:
            expected_acr = _ACR_BY_PRIMARY_AMR[method]
            break
    return acr == expected_acr


async def _verify_jwt(token: str) -> dict:
    try:
        header = jwt.get_unverified_header(token)
    except JWTError as exc:
        raise AuthError("無效的存取權杖") from exc

    kid = header.get("kid")
    if not kid:
        # algorithm-confusion defence:沒有 kid 一律拒絕(同 studio)
        raise AuthError("無效的存取權杖")

    try:
        public_key = await jwks_client.get_public_key(kid)
    except jwks_client.JwksKeyNotFoundError as exc:
        logger.warning("JWT kid=%s not present in JWKS", kid)
        raise AuthError("無效的存取權杖") from exc
    except jwks_client.JwksFetchError as exc:
        logger.error("JWKS fetch failed during verify: %s", exc)
        raise AuthUnavailable("auth keys unavailable") from exc

    try:
        payload = jwt.decode(
            token,
            public_key,
            algorithms=list(settings.JWT_ALGORITHMS),
            issuer=settings.JWT_ISSUER,
            audience=settings.JWT_AUDIENCE,
            options={
                "require_exp": True,
                "require_iat": True,
                "require_iss": True,
                "require_aud": True,
                "require_jti": True,
            },
        )
    except ExpiredSignatureError as exc:
        raise AuthError("權杖已過期,請重新登入") from exc
    except JWTError as exc:
        raise AuthError("無效的存取權杖") from exc

    if not _has_valid_session_assurance(payload):
        raise AuthError("無效的存取權杖")
    return payload


async def _check_revocation(user_id: int, token_version: int, *, jti: str, sid: str) -> None:
    """fail-closed:清單不可用時拒絕,不降級成放行(同 studio)。"""
    cache = revocation_cache_mod.get_revocation_cache()
    if not cache.ready:
        logger.warning("revocation cache not ready; denying user_id=%s", user_id)
        raise AuthUnavailable("auth deny-list unhealthy")
    if await cache.is_revoked(user_id, token_version, jti=jti, sid=sid):
        raise AuthError("權杖已失效,請重新登入")


async def authenticate(token: str) -> CurrentUserIdentity:
    payload = await _verify_jwt(token)

    # csp 對 access/refresh 都簽同一把 key,只靠 type 區分 → refresh token
    # 不得用在這個介面(同 studio)。
    if payload.get("type") != "access":
        raise AuthError("無效的存取權杖")

    if settings.REQUIRE_CARD_LOGIN_ONLY:
        raw_amr = payload.get("amr")
        methods = (
            set(raw_amr)
            if isinstance(raw_amr, list) and all(isinstance(m, str) for m in raw_amr)
            else set()
        )
        if "sc" not in methods:
            raise AuthError("此服務僅接受憑證卡登入工作階段")

    sub = payload.get("sub")
    if not sub:
        raise AuthError("無效的存取權杖")
    try:
        user_id = int(sub)
    except (TypeError, ValueError) as exc:
        raise AuthError("無效的存取權杖") from exc

    token_version = int(payload.get("tv", 0))
    await _check_revocation(
        user_id, token_version, jti=payload["jti"], sid=payload["sid"]
    )
    return CurrentUserIdentity(
        id=user_id,
        username=str(payload.get("username") or ""),
        role=str(payload.get("role") or "user"),
        token_version=token_version,
        jti=payload["jti"],
        sid=payload["sid"],
    )


async def is_still_valid(identity: CurrentUserIdentity) -> bool:
    """長連線的定期重查(每 REVOCATION_RECHECK_SECONDS 一次)。

    只查撤銷,不重驗 exp —— token 過期是規劃書 §2.2 明列的接受決策(握手驗
    一次,session 上限 300s 遠短於 access token 的 60 分鐘)。**若日後把
    session 上限調大,必須回頭重評這條。**

    cache 不可用時回 False(斷線)—— fail-closed 姿態要一路貫徹到長連線,
    否則「Redis 掛掉時已連上的人可以一直用」就是個繞過撤銷的破口。
    """
    cache = revocation_cache_mod.get_revocation_cache()
    if not cache.ready:
        return False
    return not await cache.is_revoked(
        identity.id, identity.token_version, jti=identity.jti, sid=identity.sid
    )
