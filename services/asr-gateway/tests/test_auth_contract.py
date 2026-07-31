"""asr-gateway 與 csp 之間的**權杖契約**測試。

⚠ 這個檔案存在的理由,請先讀完再改:

`tests/test_ws.py` 裡每一個認證測試都 monkeypatch 掉 `auth_mod.authenticate`。
所以 2026-07-30 部署時,78 個測試全綠,而使用者按下麥克風**每一次**都拿到
close 4401 —— 因為 `authenticate()` 驗的是一份這棵樹裡沒有任何簽發者實作的
權杖規格(要求 `iss`/`aud`/`jti`/`amr`/`acr`/`sid`/`auth_time`,並到
`__Host-anila_access_token` 這個沒人發的 cookie 名去取權杖)。
**把 authenticate 換掉的測試,測不到 authenticate。**

本檔一律走**真的** `auth.authenticate()`:真的 RS256 簽章、真的 `jwt.decode`、
真的 `RevocationCache`。只有兩個東西被替身:

* `jwks_client.get_public_key` —— 它的職責是「把 kid 換成公鑰」,那是網路 I/O,
  不是本檔要驗的邏輯。替身回傳的是**測試自己簽章用的那把真公鑰**,所以簽章
  驗證本身完全沒有被繞過(見 `test_bad_signature_rejected`:換一把金鑰簽就必須
  被拒)。
* Redis —— 用真的 `RevocationCache` 類別,只是直接填 `_cache` / `_ready` 而不
  去連 Redis。**刻意不寫自己的假 cache**:原始程式的 bug 之一是拿
  `jti=`/`sid=` 去呼叫 `is_revoked()`,而真的 cache 根本不收這兩個 kwarg
  —— 一個手寫的 `**kwargs` 假件會把那個 TypeError 藏起來。
"""

from __future__ import annotations

import json
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt

from app import auth as auth_mod
from app.services import jwks_client
from app.services import revocation_cache as revocation_cache_mod
from app.services.revocation_cache import RevocationCache


KID = "anila-v1"


# ── 金鑰 ────────────────────────────────────────────────────────────────


def _make_keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


# 模組層產一次:RSA-2048 keygen 不便宜,而每個測試要的只是「一把真金鑰」。
CSP_PRIVATE_PEM, CSP_PUBLIC_PEM = _make_keypair()
# 「別人的」金鑰 —— 專門用來偽造簽章。
ATTACKER_PRIVATE_PEM, _ATTACKER_PUBLIC_PEM = _make_keypair()


# ── csp 權杖的忠實複製品 ────────────────────────────────────────────────


def csp_access_token(
    *,
    user_id: int = 7,
    username: str = "tester",
    role: str = "user",
    token_version: int = 1,
    expires_in: int = 3600,
    private_pem: str = CSP_PRIVATE_PEM,
    kid: str | None = KID,
    token_type: str = "access",
) -> str:
    """逐欄複製 csp 真正簽出來的 access token。

    來源(2026-07-31 核對過):

    * `services/csp/app/services/auth_service.py:55` `create_tokens()`
      → `{"sub": str(user.id), "username": ..., "role": ..., "tv": ...}`
    * `services/csp/app/utils/security.py:208` `create_access_token()`
      → 再 `update({"exp": ..., "type": "access"})`,RS256 + `kid` header

    **這個 claim 集合就是全部** —— 沒有 `iss`、沒有 `aud`、沒有 `jti`、沒有
    `iat`、沒有 `amr`/`acr`/`sid`/`auth_time`。要新增欄位請先確認 csp 真的簽了
    它,否則這個測試就會退化成「驗證我剛剛寫的那段程式」而測不到契約。

    ⚠ 卡登入(`services/csp/app/api/auth/card.py:166`)呼叫的是同一個
    `create_tokens()`,所以這也就是擁有者的憑證卡會拿到的權杖形狀。
    """
    claims = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "tv": token_version,
        "exp": int(time.time()) + expires_in,
        "type": token_type,
    }
    headers = {"kid": kid} if kid is not None else None
    return jwt.encode(claims, private_pem, algorithm="RS256", headers=headers)


# ── 替身接線 ────────────────────────────────────────────────────────────


@pytest.fixture
def cache(monkeypatch) -> RevocationCache:
    """真的 RevocationCache,只是不連 Redis。預設 ready 且空(= 沒人被撤銷)。"""
    c = RevocationCache()
    c._ready = True
    monkeypatch.setattr(revocation_cache_mod, "get_revocation_cache", lambda: c)
    return c


def _revoke(cache: RevocationCache, *, user_id: int, revoked_at_version: int) -> None:
    """把一次撤銷灌進 cache —— 走真的事件處理路徑,不去戳 `_cache`。

    餵進去的字串就是 csp 發到 Redis channel 的 payload
    (`services/csp/app/services/token_revocation_publisher.py::_revocation_message`)。
    `revoked_at_version` 是 csp 把 `users.token_version` **撞完之後**的值。
    """
    cache._handle_message(
        json.dumps(
            {
                "user_id": user_id,
                "revoked_at_version": revoked_at_version,
                "ts": "2026-07-31T00:00:00Z",
                "schema_version": 1,
            }
        )
    )


@pytest.fixture
def jwks(monkeypatch):
    """把 kid 換成公鑰。回傳的是測試簽章用的那把真公鑰。"""

    async def get_public_key(kid: str) -> str:
        if kid != KID:
            raise jwks_client.JwksKeyNotFoundError(kid)
        return CSP_PUBLIC_PEM

    monkeypatch.setattr(jwks_client, "get_public_key", get_public_key)


# ── 1. 接受 ─────────────────────────────────────────────────────────────


async def test_token_exactly_as_csp_issues_is_accepted(jwks, cache):
    """驗收條件 1:csp 真的會發的那種權杖,必須被接受。

    這是整個修復的核心。修復前這一條會因為 `missing required key "aud"` 而
    炸掉 —— 那正是使用者按麥克風時 gateway 日誌裡的那行。
    """
    identity = await auth_mod.authenticate(csp_access_token())

    assert identity.id == 7
    assert identity.username == "tester"
    assert identity.role == "user"
    assert identity.token_version == 1


async def test_accepted_identity_carries_role_for_session(jwks, cache):
    """驗證後的身分要真的帶著 csp 給的 role/username,不是預設值 ——
    session 記錄與併發控制都靠它。"""
    identity = await auth_mod.authenticate(
        csp_access_token(user_id=42, username="owner-user", role="owner")
    )
    assert (identity.id, identity.username, identity.role) == (42, "owner-user", "owner")


# ── 2. 拒絕(不變式)────────────────────────────────────────────────────


async def test_bad_signature_rejected(jwks, cache):
    """驗收條件 2a:別把金鑰換掉還能過 —— 這條掛掉代表信任錨沒了。"""
    forged = csp_access_token(private_pem=ATTACKER_PRIVATE_PEM)
    with pytest.raises(auth_mod.AuthError):
        await auth_mod.authenticate(forged)


async def test_expired_token_rejected(jwks, cache):
    """驗收條件 2b:過期。`expires_in` 為負 = `exp` 已經在過去。"""
    with pytest.raises(auth_mod.AuthError):
        await auth_mod.authenticate(csp_access_token(expires_in=-60))


async def test_revoked_token_rejected(jwks, cache):
    """驗收條件 2c:被撤銷。

    csp 把 user 7 的 token_version 從 1 撞到 2,並發布
    `revoked_at_version=2`(撞**完之後**的值)。依 `is_revoked` 的
    `token_version < revoked_at_version` 規則,撞之前簽出的 tv=1 即失效。
    """
    _revoke(cache, user_id=7, revoked_at_version=2)
    with pytest.raises(auth_mod.AuthError):
        await auth_mod.authenticate(csp_access_token(user_id=7, token_version=1))


async def test_revocation_is_scoped_to_the_revoked_user(jwks, cache):
    """撤銷不能變成「撤一個等於撤全部」,也不能撤了等於沒撤。"""
    _revoke(cache, user_id=7, revoked_at_version=2)
    # 別人不受影響
    assert (await auth_mod.authenticate(csp_access_token(user_id=8))).id == 8
    # 同一個 user 換發的新權杖(tv 更高)必須能用
    identity = await auth_mod.authenticate(
        csp_access_token(user_id=7, token_version=2)
    )
    assert identity.token_version == 2


# ── 2.5 撤銷語意的邊界(2026-07-31 永久鎖死案的迴歸防線)────────────────
#
# 這一節每一條都走真的 `authenticate()` + 真的 `RevocationCache.is_revoked`,
# 灌資料一律用 `_revoke()`(真的 `_handle_message`)。不准用替身 cache:
# 那樣測到的是測試自己寫的那行比較,不是服務真的用的那行。


async def test_revocation_kills_old_token_and_spares_the_replacement(jwks, cache):
    """驗收條件 1:兩半必須同時成立,少一半都不算修好。

    使用者改密碼:csp token_version 4 → 5,寫下 revoked_at_version=5,並用
    tv=5 簽發新權杖。舊的必須死,新的必須活。

    修好之前,後半會失敗 —— 那正是使用者改完密碼後語音再也不能用的原因。
    """
    uid = 500
    _revoke(cache, user_id=uid, revoked_at_version=5)

    with pytest.raises(auth_mod.AuthError):
        await auth_mod.authenticate(
            csp_access_token(user_id=uid, token_version=4)
        )

    identity = await auth_mod.authenticate(
        csp_access_token(user_id=uid, token_version=5)
    )
    assert identity.token_version == 5, "撤銷後重新登入拿到的權杖必須被接受"


async def test_boundary_token_at_exactly_the_revocation_version_is_accepted(
    jwks, cache
):
    """驗收條件 2:`tv == revoked_at_version` 落在「活」的那一側。

    因為 `revoked_at_version` 依定義就是 csp 撞完 `users.token_version` 之後
    的值,而 `create_tokens` 拿同一個數字當新權杖的 `tv` —— 帶著這個 tv 的
    權杖,是這次撤銷**發出來的**那張,不是它要殺的那張。
    """
    uid = 501
    _revoke(cache, user_id=uid, revoked_at_version=5)

    with pytest.raises(auth_mod.AuthError):     # 更舊 → 死
        await auth_mod.authenticate(
            csp_access_token(user_id=uid, token_version=4)
        )
    assert (                                     # 邊界 → 活
        await auth_mod.authenticate(
            csp_access_token(user_id=uid, token_version=5)
        )
    ).token_version == 5
    assert (                                     # 更新 → 活
        await auth_mod.authenticate(
            csp_access_token(user_id=uid, token_version=6)
        )
    ).token_version == 6


async def test_second_revocation_kills_the_token_issued_between_them(jwks, cache):
    """驗收條件 3:安全那一半沒被放寬。

    改密碼(→5)之後又被管理員強制登出(→6):
    * 第一次之前的 tv=4:兩次之後仍然死,不會因為又撤一次就復活。
    * 兩次**之間**的 tv=5:必須被第二次殺掉,否則「再撤一次」等於沒用。
    * 第二次之後的 tv=6:活。
    """
    uid = 502
    _revoke(cache, user_id=uid, revoked_at_version=5)
    _revoke(cache, user_id=uid, revoked_at_version=6)

    for dead_tv in (4, 5):
        with pytest.raises(auth_mod.AuthError):
            await auth_mod.authenticate(
                csp_access_token(user_id=uid, token_version=dead_tv)
            )

    assert (
        await auth_mod.authenticate(
            csp_access_token(user_id=uid, token_version=6)
        )
    ).token_version == 6


async def test_revoked_message_is_actionable_and_distinct_from_expired(jwks, cache):
    """被撤銷 ≠ 已過期:解法不同,訊息就不能長一樣。

    這句同時是 WebSocket close reason,RFC 6455 限 123 bytes —— 一併驗長度,
    否則 close frame 送不出去,使用者只會看到連線莫名斷掉。
    """
    uid = 503
    _revoke(cache, user_id=uid, revoked_at_version=3)

    with pytest.raises(auth_mod.AuthError) as revoked:
        await auth_mod.authenticate(
            csp_access_token(user_id=uid, token_version=2)
        )
    with pytest.raises(auth_mod.AuthError) as expired:
        await auth_mod.authenticate(
            csp_access_token(user_id=uid, token_version=3, expires_in=-60)
        )

    revoked_msg, expired_msg = str(revoked.value), str(expired.value)
    assert revoked_msg != expired_msg
    assert "撤銷" in revoked_msg
    assert "管理員" in revoked_msg          # 重登仍失敗時的下一步
    assert "過期" in expired_msg
    assert len(revoked_msg.encode("utf-8")) <= 123, "close reason 會被截斷"


async def test_long_lived_session_recheck_follows_the_same_boundary(jwks, cache):
    """`is_still_valid` 是長連線每 N 秒重查用的 —— 它必須和握手同一條線。

    否則會出現「握手放行、下一次重查踢掉」的鬼打牆:麥克風按得下去,講兩句
    就斷。
    """
    uid = 504
    _revoke(cache, user_id=uid, revoked_at_version=5)

    stale = auth_mod.CurrentUserIdentity(
        id=uid, username="tester", role="user", token_version=4
    )
    fresh = auth_mod.CurrentUserIdentity(
        id=uid, username="tester", role="user", token_version=5
    )
    assert await auth_mod.is_still_valid(stale) is False
    assert await auth_mod.is_still_valid(fresh) is True


async def test_no_token_at_all_is_rejected():
    """驗收條件 2d:沒有權杖。

    `extract_token` 對「沒 cookie、沒 header」必須回 None,main.py 才會關 4401。
    WS 層的對應行為在 test_ws.py:test_missing_token_closes_4401。
    """

    class _NoCredsWebSocket:
        headers: dict[str, str] = {}
        cookies: dict[str, str] = {}

    assert auth_mod.extract_token(_NoCredsWebSocket()) is None


async def test_refresh_token_rejected(jwks, cache):
    """csp 用同一把金鑰簽 refresh token,只靠 `type` 區分 —— 語音介面不收。"""
    with pytest.raises(auth_mod.AuthError):
        await auth_mod.authenticate(csp_access_token(token_type="refresh"))


async def test_token_without_kid_rejected(jwks, cache):
    """algorithm-confusion 防線:沒有 kid 一律拒絕。"""
    with pytest.raises(auth_mod.AuthError):
        await auth_mod.authenticate(csp_access_token(kid=None))


async def test_unknown_kid_rejected(jwks, cache):
    with pytest.raises(auth_mod.AuthError):
        await auth_mod.authenticate(csp_access_token(kid="not-our-key"))


async def test_hs256_token_rejected(jwks, cache):
    """演算法混淆防線:`algorithms` 白名單只有 RS256,對稱簽章一律不收。

    經典手法是拿 RS256 的**公鑰**當 HMAC 秘密去簽 —— python-jose 在
    `jwt.encode` 就擋住不讓人造出那種權杖(JWSError: asymmetric key ...
    should not be used as an HMAC secret),所以這裡改用一般字串秘密。
    要驗的不變式是同一個:decode 端看到 `alg=HS256` 就必須拒絕,不管簽它的
    是什麼秘密。
    """
    forged = jwt.encode(
        {
            "sub": "7",
            "username": "tester",
            "role": "user",
            "tv": 1,
            "exp": int(time.time()) + 3600,
            "type": "access",
        },
        "not-the-signing-key",
        algorithm="HS256",
        headers={"kid": KID},
    )
    with pytest.raises(auth_mod.AuthError):
        await auth_mod.authenticate(forged)


async def test_revocation_cache_not_ready_is_fail_closed(jwks, cache):
    """fail-closed:撤銷清單不可用時拒絕,而且要能和「權杖壞了」分辨開來
    (AuthUnavailable → 4503,不是 4401)—— 叫使用者去重新登入解決不了
    Redis 掛掉。"""
    cache._ready = False
    with pytest.raises(auth_mod.AuthUnavailable):
        await auth_mod.authenticate(csp_access_token())


# ── 長連線期間的撤銷重查 ────────────────────────────────────────────────


async def test_is_still_valid_tracks_revocation_midsession(jwks, cache):
    """握手過了之後,`_guard` 每 30 秒用這條重查。它也必須用真 cache 的
    簽名呼叫得動 —— 原始程式在這裡一樣多傳了 jti=/sid=。"""
    identity = await auth_mod.authenticate(csp_access_token())
    assert await auth_mod.is_still_valid(identity) is True

    # 講話講到一半被管理員撤銷:csp 把 token_version 撞到 identity.tv + 1。
    _revoke(cache, user_id=identity.id, revoked_at_version=identity.token_version + 1)
    assert await auth_mod.is_still_valid(identity) is False


async def test_is_still_valid_fail_closed_when_cache_down(jwks, cache):
    identity = await auth_mod.authenticate(csp_access_token())
    cache._ready = False
    assert await auth_mod.is_still_valid(identity) is False


# ── 端到端:瀏覽器形狀的 cookie 走完整個 WS 握手 ────────────────────────


def test_browser_shaped_cookie_reaches_listening(jwks, cache):
    """**這是本次故障真正的回歸測試。**

    前面每一條都直接呼叫 `authenticate()`,所以驗得到 claim 契約,卻驗不到
    「gateway 到底去哪個 cookie 名拿權杖」。使用者的瀏覽器只會送 csp 發的那個
    `anila_access_token` —— 名字對不上時,`extract_token` 回 None,連
    `authenticate()` 都不會被呼叫到,症狀是握手成功後立刻 4401。

    這條測試刻意**不 monkeypatch `authenticate`**(test_ws.py 全部都有 patch,
    所以整組綠燈也擋不住這個故障),而是設一個真的 cookie、跑真的驗證,一路
    看到 `listening`。
    """
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import create_app
    from tests.test_ws import FakeDecodeClient

    app = create_app(
        app_settings=Settings(ASR_DECODE_URL="https://decoder:9000",
                              ASR_DECODER_TOKEN="t"),
        decode_client=FakeDecodeClient(),
        skip_upstreams=True,
    )
    with TestClient(app) as client:
        # 逐字用 csp 發的 cookie 名。寫死字面值是刻意的:引用
        # auth_mod.ACCESS_COOKIE_NAME 的話,常數改錯這條測試會跟著改錯。
        client.cookies.set("anila_access_token", csp_access_token())
        with client.websocket_connect("/asr/stream") as ws:
            assert ws.receive_json() == {
                "type": "status",
                "state": "listening",
                "msg": "聆聽中…",
            }
