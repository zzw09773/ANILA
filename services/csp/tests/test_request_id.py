# -*- coding: utf-8 -*-
"""`X-Request-ID` 貫穿回應頭、錯誤信封、access log —— 補救計畫 W3-3⑤。

W2-12 的錯誤信封有 `request_id` 欄,但它先前**永遠是 None**(唯一來源是 inbound
header,而沒有人送)。使用者回報「我剛剛操作失敗」時,支援端沒有任何東西可以拿去
grep log。

本包補上入站生成。三個地方必須是**同一個值**,否則「使用者念出畫面上的 id」與
「支援端 grep log」對不上,那這個功能就等於沒有:
  ① 回應頭 `X-Request-ID`
  ② 錯誤信封的 `error.request_id`
  ③ access log 那一行

另外清洗 inbound header 是安全需求,不是潔癖:那個值是**攻擊者可控字串**,而它會
進 log 檔(→ log injection,塞換行就能偽造一整行稽核紀錄)與回應頭。
"""
from __future__ import annotations

import logging
import re

from tests.conftest import login, make_user

HEADER = "X-Request-ID"


# ── ① 回應頭 ──────────────────────────────────────────────────────────────────

def test_every_response_carries_a_request_id(client):
    resp = client.get("/health")
    assert resp.status_code == 200, resp.text
    rid = resp.headers.get(HEADER)
    assert rid, "回應頭沒有 X-Request-ID"
    assert re.fullmatch(r"[0-9a-f]{32}", rid), f"自動生成的 id 形狀不對:{rid!r}"


def test_inbound_request_id_is_reused(client):
    resp = client.get("/health", headers={HEADER: "trace-abc_123.4"})
    assert resp.headers.get(HEADER) == "trace-abc_123.4"


def test_two_requests_get_different_ids(client):
    a = client.get("/health").headers[HEADER]
    b = client.get("/health").headers[HEADER]
    assert a != b


# ── 清洗:攻擊者可控字串不得原封不動進 log 與 header ──────────────────────────

def test_sanitizer_rejects_hostile_inputs():
    """直接測清洗函式,不透過 HTTP client。

    httpx 這個 test client 自己就拒送含換行或非 ASCII 的 header value(那是正確
    的 client 行為),所以那些輸入**在 HTTP 層測不到**。但清洗仍然必要:curl、
    raw socket、以及中間的 proxy 都可能送進來,而這個值會進 log 檔。
    這是縱深防禦,不是潔癖 —— 換行是 log injection 的載具,一行 log 一筆紀錄的
    語意會被偽造(例如假造一筆「admin 授權成功」),而 log 是稽核證據。
    """
    from app.middleware.request_id import _sanitize

    hostile = [
        "ok\nfake-log-line admin 授權成功",   # log injection
        "ok\r\nX-Injected: 1",                # header injection
        "x" * 65,                             # 超長
        "has space",
        "semi;colon",
        "中文",
        "<script>alert(1)</script>",
        "",
        "   ",
        None,
    ]
    for bad in hostile:
        assert _sanitize(bad) is None, f"{bad!r} 竟然通過清洗"

    # 合法的要放行(否則跨服務追蹤就斷了)
    for ok in ("trace-abc_123.4", "a" * 64, "A1", "x.y-z_0"):
        assert _sanitize(ok) == ok, f"{ok!r} 被誤擋"


def test_unsafe_inbound_id_falls_back_to_generated(client):
    """HTTP 層測 httpx 送得出去的那些不合格值:當作沒送、自己生一個。

    **不是報錯** —— 一個格式不對的追蹤 id 不值得讓請求失敗。
    """
    for bad in ("has space", "semi;colon", "x" * 65, "<script>"):
        rid = client.get("/health", headers={HEADER: bad}).headers[HEADER]
        assert re.fullmatch(r"[0-9a-f]{32}", rid), f"{bad!r} 竟然被沿用:{rid!r}"


def test_maximum_length_is_accepted(client):
    """邊界:剛好 64 字元的合法 id 要接受(不要把邊界寫成 off-by-one)。"""
    ok = "a" * 64
    assert client.get("/health", headers={HEADER: ok}).headers[HEADER] == ok


# ── ② 錯誤信封 ────────────────────────────────────────────────────────────────

def test_error_envelope_carries_the_same_id(client):
    resp = client.get("/api/conversations/999999")
    assert resp.status_code in (401, 403, 404), resp.text
    body = resp.json()
    assert "error" in body, body
    assert body["error"]["request_id"] == resp.headers[HEADER], (
        "信封裡的 request_id 與回應頭不同 —— 使用者念出畫面上的 id 會 grep 不到"
    )


def test_error_envelope_request_id_is_no_longer_always_null(client, db):
    """W2-12 當時這個欄位永遠是 null。這條就是防它退回去。"""
    make_user(db, username="alice")
    token = login(client, username="alice")
    resp = client.get(
        "/api/conversations/999999",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.json()["error"]["request_id"] is not None


# ── ③ access log ──────────────────────────────────────────────────────────────

def test_access_log_line_contains_the_id(client, caplog):
    with caplog.at_level(logging.INFO, logger="csp.access"):
        resp = client.get("/health")
    rid = resp.headers[HEADER]
    assert any(rid in rec.getMessage() for rec in caplog.records), (
        f"access log 沒有這個 id({rid}),支援端 grep 不到"
    )


def test_failed_request_is_also_logged(client, caplog):
    """失敗的請求正是最需要被 grep 的那些 —— log 用 finally 寫,不是只在成功時。"""
    with caplog.at_level(logging.INFO, logger="csp.access"):
        resp = client.get("/api/conversations/999999")
    rid = resp.headers[HEADER]
    lines = [r.getMessage() for r in caplog.records if rid in r.getMessage()]
    assert lines, "失敗的請求沒有留下 access log"
    assert "/api/conversations/999999" in lines[0]


# ── contextvar:深層程式碼拿得到 ──────────────────────────────────────────────

def test_current_request_id_is_none_outside_a_request():
    from app.middleware.request_id import current_request_id

    assert current_request_id() is None
