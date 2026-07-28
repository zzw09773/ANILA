# -*- coding: utf-8 -*-
"""`ui_settings` 逐鍵合併 + auth 熱路徑不拉大欄 —— 補救計畫 W3-7f。

兩個缺陷
--------
1. **整包 last-write-wins 造成資料遺失。** `PUT /me/ui-settings` 取代整個 blob,
   而 client 送的是它自己那份可能過時的完整內容。兩個分頁同時開著:A 改 folders、
   B 改 convMeta,誰後寫誰全贏 → 另一邊的改動整個消失,而且沒有任何錯誤訊息。
   使用者的體驗是「我剛建的資料夾不見了」。
2. **auth 熱路徑每個請求都拉那個大 JSON 欄。** `_load_user_from_payload` 是全平台
   每一個帶 token 的請求都會走的地方(`get_current_user` 與 `middleware/caller.py`
   都呼叫它),而它一個欄位都用不到 `ui_settings`(上限 256KB)。

⚠ **per-key merge 只在 client 送部分 payload 時才救得到 (1)。** 前端還沒切過去
之前,資料遺失仍然存在。這條限制在 endpoint docstring 與這裡都寫明,免得有人以為
後端加了就修好了。
"""
from __future__ import annotations

from tests.conftest import login, make_user


def _auth(client, username="alice"):
    return {"Authorization": f"Bearer {login(client, username=username)}"}


# ── PATCH 逐鍵合併 ────────────────────────────────────────────────────────────

def test_patch_merges_instead_of_replacing(client, db):
    make_user(db, username="alice")
    h = _auth(client)
    client.put("/api/users/me/ui-settings",
               json={"folders": ["a"], "convMeta": {"1": "x"}}, headers=h)

    resp = client.patch("/api/users/me/ui-settings",
                        json={"folders": ["a", "b"]}, headers=h)

    assert resp.status_code == 200, resp.text
    got = resp.json()["ui_settings"]
    assert got["folders"] == ["a", "b"]
    # 沒送的鍵必須留著 —— 這就是整包取代會弄丟的東西
    assert got["convMeta"] == {"1": "x"}


def test_two_partial_patches_to_different_keys_both_survive(client, db):
    """模擬兩個分頁各改不同的鍵。整包 PUT 會讓其中一邊消失。"""
    make_user(db, username="alice")
    h = _auth(client)
    client.put("/api/users/me/ui-settings",
               json={"folders": ["orig"], "convMeta": {"1": "orig"}}, headers=h)

    client.patch("/api/users/me/ui-settings",
                 json={"folders": ["A 改的"]}, headers=h)
    client.patch("/api/users/me/ui-settings",
                 json={"convMeta": {"1": "B 改的"}}, headers=h)

    got = client.get("/api/users/me/ui-settings", headers=h).json()["ui_settings"]
    assert got["folders"] == ["A 改的"], "A 的改動被 B 蓋掉了"
    assert got["convMeta"] == {"1": "B 改的"}


def test_null_deletes_a_key(client, db):
    """合併語意下必須有辦法刪鍵,否則東西只能越積越多。"""
    make_user(db, username="alice")
    h = _auth(client)
    client.put("/api/users/me/ui-settings",
               json={"folders": ["a"], "stars": [1, 2]}, headers=h)

    got = client.patch("/api/users/me/ui-settings",
                       json={"stars": None}, headers=h).json()["ui_settings"]

    assert "stars" not in got
    assert got["folders"] == ["a"]


def test_merged_size_is_capped(client, db):
    """不能用一連串小 PATCH 疊出超大 blob。"""
    make_user(db, username="alice")
    h = _auth(client)
    client.put("/api/users/me/ui-settings", json={"a": "x" * 200_000}, headers=h)

    resp = client.patch("/api/users/me/ui-settings",
                        json={"b": "y" * 100_000}, headers=h)

    assert resp.status_code == 413, resp.text


def test_put_still_replaces(client, db):
    """PUT 的語意不變 —— 整包取代才是 PUT 該做的事,舊 client 不受影響。"""
    make_user(db, username="alice")
    h = _auth(client)
    client.put("/api/users/me/ui-settings",
               json={"folders": ["a"], "convMeta": {"1": "x"}}, headers=h)

    got = client.put("/api/users/me/ui-settings",
                     json={"folders": ["b"]}, headers=h).json()["ui_settings"]

    assert got == {"folders": ["b"]}, "PUT 不該變成合併"


# ── auth 熱路徑不拉 ui_settings ───────────────────────────────────────────────

def test_auth_hot_path_does_not_select_ui_settings(db):
    """用 SQL echo 斷言,不是看程式碼 —— 那才證明得了 defer 真的生效。"""
    from sqlalchemy import event

    from app.services.auth_service import _load_user_from_payload

    user = make_user(db, username="alice")
    statements: list[str] = []

    def capture(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        payload = {"type": "access", "sub": str(user.id), "tv": user.token_version}
        # 這裡只在意 SELECT 的欄位清單;其餘 claim 檢查失敗與否不影響斷言。
        try:
            _load_user_from_payload(payload, db, "access")
        except Exception:
            pass
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    user_selects = [s for s in statements if "FROM users" in s and s.strip().upper().startswith("SELECT")]
    assert user_selects, f"沒有抓到對 users 的 SELECT:{statements}"
    assert not any("ui_settings" in s for s in user_selects), (
        "auth 熱路徑仍然在拉 ui_settings(每個帶 token 的請求都會付這個成本):\n"
        + "\n".join(user_selects)
    )


def test_ui_settings_still_readable_after_defer(client, db):
    """defer 之後那條真的需要它的路徑仍然讀得到(lazy load)。"""
    make_user(db, username="alice")
    h = _auth(client)
    client.put("/api/users/me/ui-settings", json={"folders": ["a"]}, headers=h)

    resp = client.get("/api/users/me/ui-settings", headers=h)

    assert resp.status_code == 200, resp.text
    assert resp.json()["ui_settings"] == {"folders": ["a"]}
