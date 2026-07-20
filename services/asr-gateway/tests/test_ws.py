"""WS 端點 + 認證測試。

認證以 monkeypatch 注入(不需要 csp / JWKS / Redis),decode 以假件注入。
重點在驗**規格說了但很容易做不到**的事:close code 送不送得到前端、踢舊留新、
逾時先 flush、fail-closed。
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import auth as auth_mod
from app.auth import AuthError, AuthUnavailable, CurrentUserIdentity
from app.config import Settings
from app.main import (
    CLOSE_AUTH_FAILED,
    CLOSE_AUTH_UNAVAILABLE,
    CLOSE_CONCURRENCY,
    CLOSE_SESSION_TIMEOUT,
    SessionRegistry,
    create_app,
    _validate_settings,
)

from tests.test_session import FakeDecode


IDENTITY = CurrentUserIdentity(
    id=7, username="tester", role="user", token_version=1, jti="j1", sid="s1"
)


class FakeDecodeClient:
    def __init__(self, decode=None):
        self.decode = decode or FakeDecode()

    async def aclose(self):
        pass


def build_app(monkeypatch, *, identity=IDENTITY, auth_exc=None, **overrides):
    async def fake_authenticate(token: str):
        if auth_exc is not None:
            raise auth_exc
        return identity

    async def fake_still_valid(ident):
        return True

    monkeypatch.setattr(auth_mod, "authenticate", fake_authenticate)
    monkeypatch.setattr(auth_mod, "is_still_valid", fake_still_valid)

    s = Settings(
        ASR_DECODE_URL="https://decoder:9000",
        ASR_DECODER_TOKEN="t",
        **overrides,
    )
    app = create_app(app_settings=s, decode_client=FakeDecodeClient(),
                     skip_upstreams=True)
    return app


def connect(client: TestClient, cookie: str = "tok"):
    # cookie 設在 client 實例上,不用 per-request(starlette 已標為語意模糊)。
    client.cookies.set(auth_mod.ACCESS_COOKIE_NAME, cookie)
    return client.websocket_connect("/asr/stream")


# ── 認證 ─────────────────────────────────────────────────────────────────


def test_missing_token_closes_4401(monkeypatch):
    app = build_app(monkeypatch)
    with TestClient(app) as client:
        with client.websocket_connect("/asr/stream") as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
    assert exc.value.code == CLOSE_AUTH_FAILED


def test_invalid_token_closes_4401(monkeypatch):
    app = build_app(monkeypatch, auth_exc=AuthError("無效的存取權杖"))
    with TestClient(app) as client:
        with connect(client) as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
    assert exc.value.code == CLOSE_AUTH_FAILED


def test_auth_infrastructure_down_closes_4503_not_4401(monkeypatch):
    """fail-closed 時回 4401 會叫使用者去重新登入 —— 但問題出在 Redis/csp,
    重登解決不了。必須用不同的 code 才能給出正確的錯誤訊息。"""
    app = build_app(monkeypatch, auth_exc=AuthUnavailable("deny-list unhealthy"))
    with TestClient(app) as client:
        with connect(client) as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
    assert exc.value.code == CLOSE_AUTH_UNAVAILABLE


def test_valid_token_gets_listening_status(monkeypatch):
    app = build_app(monkeypatch)
    with TestClient(app) as client:
        with connect(client) as ws:
            msg = ws.receive_json()
    assert msg["type"] == "status"
    assert msg["state"] == "listening"


def test_bearer_header_also_works(monkeypatch):
    """服務間/測試用 Bearer;瀏覽器只能用 cookie(WS API 不能帶 header)。"""
    app = build_app(monkeypatch)
    with TestClient(app) as client:
        with client.websocket_connect(
            "/asr/stream", headers={"Authorization": "Bearer tok"}
        ) as ws:
            assert ws.receive_json()["state"] == "listening"


def test_cookie_name_follows_cookie_secure():
    """本機 dev(COOKIE_SECURE=false)是 anila_dev_access_token —— 只做
    __Host- 的話 M5 本機測試必 401。"""
    assert auth_mod._access_cookie_name(True) == "__Host-anila_access_token"
    assert auth_mod._access_cookie_name(False) == "anila_dev_access_token"


# ── 併發:踢舊留新 ───────────────────────────────────────────────────────


def test_second_session_kicks_the_first(monkeypatch):
    """不是拒絕新連線 —— 死 TCP 會把使用者鎖在門外。"""
    app = build_app(monkeypatch)
    with TestClient(app) as client:
        with connect(client) as first:
            first.receive_json()                      # listening
            with connect(client) as second:
                assert second.receive_json()["state"] == "listening"
                with pytest.raises(WebSocketDisconnect) as exc:
                    first.receive_json()              # 舊的被踢
    assert exc.value.code == CLOSE_CONCURRENCY


async def test_registry_unregister_does_not_clobber_the_newer_session():
    """被踢掉的舊連線收尾時,登記的已經是新連線 —— 不能把它清掉,
    否則新 session 失去併發保護。"""
    reg = SessionRegistry()
    old, new = object(), object()
    await reg.register(1, old)
    reg._by_user[1] = new          # 模擬新連線已接手(略過真的 close)
    reg.unregister(1, old)         # 舊的收尾
    assert reg._by_user[1] is new


# ── 逾時 ─────────────────────────────────────────────────────────────────


def test_session_timeout_closes_4408(monkeypatch):
    app = build_app(monkeypatch, ASR_MAX_SESSION_SECONDS=1)
    with TestClient(app) as client:
        with connect(client) as ws:
            ws.receive_json()
            with pytest.raises(WebSocketDisconnect) as exc:
                for _ in range(10):
                    ws.receive_json()
    assert exc.value.code == CLOSE_SESSION_TIMEOUT


# ── 撤銷重查 ─────────────────────────────────────────────────────────────


def test_revoked_midsession_closes_4401(monkeypatch):
    """握手驗過就不再看撤銷 = 撤銷對長連線無效。必須定期重查。"""
    app = build_app(monkeypatch, ASR_MAX_SESSION_SECONDS=60,
                    REVOCATION_RECHECK_SECONDS=0.1)

    async def revoked(ident):
        return False

    monkeypatch.setattr(auth_mod, "is_still_valid", revoked)
    with TestClient(app) as client:
        with connect(client) as ws:
            ws.receive_json()
            with pytest.raises(WebSocketDisconnect) as exc:
                for _ in range(10):
                    ws.receive_json()
    assert exc.value.code == CLOSE_AUTH_FAILED


# ── 控制訊息 ─────────────────────────────────────────────────────────────


def test_malformed_control_frame_does_not_kill_the_session(monkeypatch):
    """client 送壞 JSON 是 client 的 bug;斷線只會讓使用者錄音無故中斷。"""
    app = build_app(monkeypatch)
    with TestClient(app) as client:
        with connect(client) as ws:
            ws.receive_json()
            ws.send_text("{not json")
            ws.send_text('{"type":"bogus"}')
            ws.send_bytes(b"\x00\x00" * 100)       # 還能繼續收音
            # 沒有斷線就算過


# ── 設定 fail-loud ──────────────────────────────────────────────────────


def test_missing_decode_url_fails_loud():
    with pytest.raises(RuntimeError, match="ASR_DECODE_URL"):
        _validate_settings(Settings(ASR_DECODE_URL="", ASR_DECODER_TOKEN="t"))


def test_missing_decoder_token_fails_loud():
    with pytest.raises(RuntimeError, match="ASR_DECODER_TOKEN"):
        _validate_settings(Settings(ASR_DECODE_URL="https://d:9000",
                                    ASR_DECODER_TOKEN=""))


def test_internal_service_name_over_http_needs_no_flag():
    """內部版(`http://asr-decoder:9000`,走 anila-models-net)音訊不出主機。

    ⚠ 這是 compose 驗證抓出來的 regression:早期版本對**任何** http 都要求
    放行旗標,導致 platform.yml 的預設設定(內部版)根本起不來。旗標管的是
    「語音會不會明文離開這台主機」,不是「有沒有用 https」。
    """
    _validate_settings(Settings(ASR_DECODE_URL="http://asr-decoder:9000",
                                ASR_DECODER_TOKEN="t"))


@pytest.mark.parametrize("url", [
    "http://gpu-host.ai.ncsist.org.tw:9000",   # FQDN → 跨主機
    "http://10.53.100.12:9000",                # IP → 跨主機
    "http://aiops.ai.ncsist.org.tw:30080",     # MLSteam NodePort
])
def test_external_host_over_http_requires_explicit_opt_in(url):
    """外部版純 http = 語音明文過內網(規劃書 §6 的書面風險接受項)。"""
    with pytest.raises(RuntimeError, match="ASR_ALLOW_HTTP_DECODER"):
        _validate_settings(Settings(ASR_DECODE_URL=url, ASR_DECODER_TOKEN="t"))


def test_external_http_allowed_when_opted_in():
    _validate_settings(Settings(ASR_DECODE_URL="http://gpu-host.ai.ncsist.org.tw:9000",
                                ASR_DECODER_TOKEN="t",
                                ASR_ALLOW_HTTP_DECODER=True))


def test_https_needs_no_flag_even_for_external():
    _validate_settings(Settings(ASR_DECODE_URL="https://gpu.ai.ncsist.org.tw:9000",
                                ASR_DECODER_TOKEN="t"))


@pytest.mark.parametrize("host,internal", [
    ("asr-decoder", True),          # compose 服務名
    ("decoder", True),
    ("gpu.ai.ncsist.org.tw", False),  # FQDN
    ("10.53.100.12", False),          # IPv4
    ("", False),
])
def test_internal_service_name_detection(host, internal):
    from app.main import _is_internal_service_name
    assert _is_internal_service_name(host) is internal


def test_non_http_scheme_is_rejected():
    with pytest.raises(RuntimeError, match="must be http"):
        _validate_settings(Settings(ASR_DECODE_URL="ftp://x/y",
                                    ASR_DECODER_TOKEN="t"))
