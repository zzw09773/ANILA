"""WS 握手的 JWT 驗證。

驗證邏輯逐條對齊 `services/anila-studio/app/auth.py`(RS256 + JWKS 本地驗章、
要求 `kid`、拒絕 refresh token、fail-closed 撤銷查核)。**不要在這裡「簡化」
任何一條** —— 每一條都是信任錨的一部分,studio 那份的註解說明了它們為什麼
存在。

⚠ **2026-07-31 修正:本檔原本驗的是一份這棵樹裡不存在的權杖規格。**
原始版本要求 `iss`/`aud`/`jti`/`iat` 與一組 session assurance claim
(`sid`/`amr`/`acr`/`auth_time`/`break_glass`),並到 `__Host-anila_access_token`
這個 cookie 名去取權杖。實際上:

- csp 的 `create_tokens()`(services/csp/app/services/auth_service.py:55)
  只簽 `sub`/`username`/`role`/`tv`,`create_access_token()` 再補 `exp`/`iat`/`type`
  —— **沒有 iss、沒有 aud、沒有 jti、沒有 amr**。
- csp 發的 cookie 叫 `anila_access_token`(services/csp/app/middleware/cookies.py:33),
  `__Host-` 與 `anila_dev_` 兩個名字平台上沒有任何地方發出。
- 卡登入走的是同一個 `create_tokens()`(services/csp/app/api/auth/card.py),
  所以「用憑證卡就會過」是不成立的 —— 卡登入也一樣被擋。

結果是**沒有任何簽發者能滿足的檢查**:握手成功後一律 4401。那不是安全性,
是壞掉的契約剛好 fail-closed。修法是把本檔對齊平台真正在發的權杖,而不是去
改全平台的權杖來遷就這一個服務。**移掉的是「從來沒被簽發過的 claim 的必填
要求」,不是驗證本身** —— 簽章驗證、`kid` 要求、演算法白名單、過期、拒絕
refresh token、fail-closed 撤銷查核全部原封不動。

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

from jose import JWTError, jwt
from jose.exceptions import ExpiredSignatureError
from starlette.websockets import WebSocket

from app.config import settings
from app.services import jwks_client, revocation_cache as revocation_cache_mod

logger = logging.getLogger(__name__)

# csp 只發這一個名字(services/csp/app/middleware/cookies.py:33),studio 也只讀
# 這一個(services/anila-studio/app/auth.py:38)。這裡不做 secure/dev 分軌 ——
# 分軌的前提是有人會發 `__Host-` 前綴的那份,而平台上沒有。
ACCESS_COOKIE_NAME = "anila_access_token"


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
    kid: str = ""


def extract_token(websocket: WebSocket) -> str | None:
    """Authorization header 優先,cookie 次之(對齊 studio 的取用順序)。"""
    authorization = websocket.headers.get("authorization")
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value:
            return value
    return websocket.cookies.get(ACCESS_COOKIE_NAME)


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
        # `verify_aud=False` 與 studio 逐字相同(services/anila-studio/app/auth.py:100):
        # csp 不簽 `aud`,要求它等於要求一個沒有人會發的東西。`iss` 同理。
        # ⚠ 這裡沒有關掉的是:簽章(public_key)、演算法白名單(擋 alg=none /
        # HS256 混淆)、以及 `exp` —— python-jose 的 verify_exp 預設就是 True,
        # 過期權杖會走上面的 ExpiredSignatureError。
        payload = jwt.decode(
            token,
            public_key,
            algorithms=list(settings.JWT_ALGORITHMS),
            options={"verify_aud": False},
        )
    except ExpiredSignatureError as exc:
        raise AuthError("權杖已過期,請重新登入") from exc
    except JWTError as exc:
        raise AuthError("無效的存取權杖") from exc

    if not jwks_client.cached_key_allows(kid, payload):
        raise AuthError("簽章金鑰不在簽發期間內")
    return payload


async def _check_revocation(user_id: int, token_version: int, kid: str = "") -> None:
    """fail-closed:清單不可用時拒絕,不降級成放行(同 studio)。

    ⚠ 簽名要與 `app/services/revocation_cache.py` 的 `is_revoked(user_id,
    token_version)` 一致 —— 那份是 studio 的逐位元組副本。原始版本在這裡多傳
    了 `jti=`/`sid=`,而副本根本不收這兩個 kwarg:就算權杖驗過了,這一行也會
    TypeError。撤銷查核是紅線,壞在這裡不會 fail-closed,會變成 500。
    """
    cache = revocation_cache_mod.get_revocation_cache()
    if not cache.ready:
        logger.warning("revocation cache not ready; denying user_id=%s", user_id)
        raise AuthUnavailable("auth deny-list unhealthy")
    kid_revoked = getattr(cache, "is_kid_revoked", None)
    if kid and kid_revoked is not None and await kid_revoked(kid):
        raise AuthError("簽章金鑰已撤銷,請重新登入")
    if await cache.is_revoked(user_id, token_version):
        # ⚠ 這句會變成 WebSocket close reason,RFC 6455 上限 123 bytes ——
        # 中文一字 3 bytes,所以講得比 studio 短。要點一樣:說出成因(被撤銷
        # 和「已過期」的解法不同),並給出重登仍失敗時的下一步。
        raise AuthError("此工作階段已被撤銷,請重新登入;若仍被拒絕請洽管理員")


async def authenticate(token: str) -> CurrentUserIdentity:
    payload = await _verify_jwt(token)

    # csp 對 access/refresh 都簽同一把 key,只靠 type 區分 → refresh token
    # 不得用在這個介面(同 studio)。
    if payload.get("type") != "access":
        raise AuthError("無效的存取權杖")

    # ⚠ 這裡原本有一段 CSP card-only mode → 要求 `amr` 含 "sc" 的檢查,
    # 已移除。理由不是「放寬」,是那段程式做不到它宣稱的事:
    #
    # - csp 從不簽 `amr`,所以 flag 一開,**連憑證卡登入的人也一律被拒**
    #   (卡登入走同一個 create_tokens)。compose 的預設值正是
    #   舊 compose 預設會讓內網一上線就全員被擋。
    # - 「只准卡登入」這件事的執法點在簽發端,不在這裡:csp 的
    #   `ANILA_AUTH_MODE=card-only` 會把帳密與 OIDC 登入路徑關掉
    #   (services/csp/app/api/auth/password.py:95、oidc.py:115、_common.py:39)。
    #   csp 處於 card-only 時,它發得出來的權杖本來就只可能來自卡登入。
    #
    # ⚠ 已知的語意收窄(不是疏漏,是刻意記在這裡):csp 在 card-only 之下仍
    # 保留 owner 的帳密 break-glass(password.py:267)。所以本服務的實際政策是
    # 「csp 願意發的工作階段都能用語音」,而不是「僅限卡片」。要真的做到後者,
    # 必須先讓 csp 把登入方式簽進權杖(例如 `amr`),再回來把這段檢查加回去 ——
    # 在那之前留著它只是一個擋不住壞人、只擋得住所有人的假控制項。
    sub = payload.get("sub")
    if not sub:
        raise AuthError("無效的存取權杖")
    try:
        user_id = int(sub)
    except (TypeError, ValueError) as exc:
        raise AuthError("無效的存取權杖") from exc

    token_version = int(payload.get("tv", 0))
    try:
        header_kid = jwt.get_unverified_header(token).get("kid")
    except JWTError:
        header_kid = ""
    signing_kid = header_kid if isinstance(header_kid, str) else ""
    await _check_revocation(user_id, token_version, kid=signing_kid)
    return CurrentUserIdentity(
        id=user_id,
        username=str(payload.get("username") or ""),
        role=str(payload.get("role") or "user"),
        token_version=token_version,
        kid=signing_kid,
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
    signing_kid = getattr(identity, "kid", "") or ""
    kid_revoked = getattr(cache, "is_kid_revoked", None)
    if signing_kid and kid_revoked is not None and await kid_revoked(signing_kid):
        return False
    return not await cache.is_revoked(identity.id, identity.token_version)
