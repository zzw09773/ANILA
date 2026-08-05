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


IDENTITY = CurrentUserIdentity(id=7, username="tester", role="user", token_version=1)


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


def test_cookie_name_matches_what_csp_actually_issues():
    """cookie 名必須逐字等於 csp 發的那個。

    這個測試取代了原本的 `test_cookie_name_follows_cookie_secure` —— 那個測試
    斷言的是 `__Host-anila_access_token` / `anila_dev_access_token`,兩個平台上
    沒有任何地方會發的名字。它綠燈綠了一整輪,而使用者按麥克風一律 4401。
    寫死常數是刻意的:要對齊的是 csp 的
    `middleware/cookies.py:ACCESS_COOKIE_NAME`,不是本服務的某個旗標。
    """
    assert auth_mod.ACCESS_COOKIE_NAME == "anila_access_token"


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


def test_internal_service_name_over_http_accepted(intranet_guard_env):
    """內部版(`http://asr-decoder:9000`)與外部版純 http 同一道門:都接受。

    治理中心在 ANILA_ALLOW_HTTP_ENDPOINT=1 時已接受 http;環境變數門若再拒絕
    會讓「同一位址、兩扇門、兩種結果」。內網前例(P0.2)以接受為準。

    ⚠ 2026-08-05:這道門現在**就是** anila_core 的 `validate_outbound_url`
    (以前是本檔自己的 startswith 字串檢查)。同一位址要通過的條件因此變成
    「旗標 + ANILA_TRUSTED_HOSTS 點名」—— 那正是 guard 為 docker 服務名準備
    的 operator 機制,platform.yml 已把 `asr-decoder` 併進 asr-gateway 自己的
    trusted hosts。fixture 只是把部署時的環境在測試裡明說出來,**不是**放寬
    檢查:反向那一半在下面兩條。
    """
    _validate_settings(Settings(ASR_DECODE_URL="http://asr-decoder:9000",
                                ASR_DECODER_TOKEN="t"))


def test_internal_service_name_refused_when_not_trusted(monkeypatch):
    """`asr-decoder` 沒被 ANILA_TRUSTED_HOSTS 點名時必須被擋。

    PROVE RED:把 main.py `_validate_settings` 裡的 `guard_decode_url(url)`
    拿掉 → 這條變綠(而且遠端 ASR 就成了唯一不過 guard 的模型呼叫)。
    """
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "")
    with pytest.raises(RuntimeError, match="未通過出向檢查"):
        _validate_settings(Settings(ASR_DECODE_URL="http://asr-decoder:9000",
                                    ASR_DECODER_TOKEN="t"))


@pytest.mark.parametrize("url", [
    "http://gpu-host.example.test:9000",
    "http://decoder.example.test:9000",
    "http://asr-external.example.test:30080",
])
def test_external_host_over_http_accepted_without_flag(url, intranet_guard_env):
    """環境變數門與治理中心門一致:純 http 由 ANILA_ALLOW_HTTP_ENDPOINT 一個
    旗標決定,不再有 ASR 專屬的第二個(ASR_ALLOW_HTTP_DECODER 已退役)。"""
    _validate_settings(Settings(ASR_DECODE_URL=url, ASR_DECODER_TOKEN="t"))


def test_external_host_over_http_refused_without_flag(monkeypatch):
    """沒開 ANILA_ALLOW_HTTP_ENDPOINT 時,http 解碼端要被擋。

    這正是「http 旗標分域」的語意:ASR 跟其他 model endpoint 用同一個旗標,
    行為也必須一樣。
    """
    monkeypatch.delenv("ANILA_ALLOW_HTTP_ENDPOINT", raising=False)
    with pytest.raises(RuntimeError, match="未通過出向檢查"):
        _validate_settings(
            Settings(ASR_DECODE_URL="http://gpu-host.example.test:9000",
                     ASR_DECODER_TOKEN="t")
        )


@pytest.mark.parametrize("url", [
    "https://127.0.0.1:9000",
    "https://localhost:9000",
    "https://169.254.169.254/latest/meta-data",
    "https://10.53.100.15:9000",
])
def test_loopback_metadata_and_private_ip_decoders_are_refused(url, monkeypatch):
    """迴環 / cloud metadata / RFC1918:解碼端不該是這些位址。

    前三條無論旗標怎麼設都擋;私網那一條靠 ANILA_ALLOW_PRIVATE_ENDPOINT
    (此處刻意不開)。
    """
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.delenv("ANILA_ALLOW_PRIVATE_ENDPOINT", raising=False)
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "")
    with pytest.raises(RuntimeError, match="未通過出向檢查"):
        _validate_settings(Settings(ASR_DECODE_URL=url, ASR_DECODER_TOKEN="t"))


def test_https_accepted_for_external():
    _validate_settings(Settings(ASR_DECODE_URL="https://gpu.example.test:9000",
                                ASR_DECODER_TOKEN="t"))


def test_non_http_scheme_is_rejected():
    with pytest.raises(RuntimeError, match="must be http"):
        _validate_settings(Settings(ASR_DECODE_URL="ftp://x/y",
                                    ASR_DECODER_TOKEN="t"))