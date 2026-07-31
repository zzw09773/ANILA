# -*- coding: utf-8 -*-
"""``GET /api/banners/public`` —— 登入頁公告的唯一對外投影。

為什麼會有這支端點
==================
等待核准的人**還沒有帳號**,更沒有 token。在這之前登入頁呼叫的是
``/api/banners/active``(``Depends(get_current_user)``),對這群人一律 401 ——
擁有者在治理中心貼的公告,只有「已經進得來的人」看得到,正好漏掉需要看的
那一群。

風險反過來也很清楚:登入頁是**任何連得到這台機器的人**都看得到的畫面。
所以公開不是預設,是逐則勾選(``show_on_login``),而且回應只帶登入頁真正
用得到的兩個欄位。

本檔守四件事(每一條都對應一個真的會發生的退化):
1. 沒有 token 的人拿得到勾選過的那一則,而且**只有**那一則。
2. 回應的 key 集合被釘死 —— 未來有人往公開 schema 加欄位,這裡會紅。
3. 沒勾就是不公開(預設值)。管理員不會因為這個功能上線就被動洩漏舊公告。
4. 停用(``is_active=False``)的公告即使勾過也不出現。

⚠ 這個 repo 是 PUBLIC:以下文案一律是明顯的假資料。
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.services.auth_service import create_tokens

from tests.conftest import make_user


PUBLIC_URL = "/api/banners/public"
ADMIN_URL = "/api/banners"

# 登入頁要顯示的那一則(勾了 show_on_login)。刻意用假姓名假分機。
LOGIN_TEXT = "系統負責人:測試單位 王小明 分機 000000"
# 只給院內已登入使用者看的那一則(沒勾)。這串**不該**出現在公開回應的任何角落。
INTERNAL_TEXT = "內部維運通知:今晚 22:00 重啟批次作業節點 TEST-NODE-000"


def _bearer(user) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_tokens(user)['access_token']}"}


def _post_banner(client: TestClient, admin, **payload) -> dict:
    body = {"level": "info", "content": "x", **payload}
    resp = client.post(ADMIN_URL, json=body, headers=_bearer(admin))
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── ① 未認證的人拿得到勾選過的,而且只有勾選過的 ─────────────────────────────


def test_unauthenticated_caller_gets_only_the_opted_in_banner(
    client: TestClient, db: Session
):
    admin = make_user(db, username="banner-admin-1", role="admin")
    _post_banner(client, admin, content=LOGIN_TEXT, show_on_login=True)
    _post_banner(client, admin, content=INTERNAL_TEXT, show_on_login=False)

    # 完全不帶 Authorization / cookie —— 這就是還沒有帳號的人的處境。
    resp = client.get(PUBLIC_URL)

    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert [row["content"] for row in rows] == [LOGIN_TEXT]
    # 不只是「不在 list 裡」:整個 response body 的原始位元組裡都不該有它。
    assert INTERNAL_TEXT not in resp.text


# ── ② 公開投影的欄位集合釘死 ────────────────────────────────────────────────


def test_public_payload_exposes_exactly_the_fields_the_login_page_needs(
    client: TestClient, db: Session
):
    """登入頁只需要「顯示什麼字」跟「用什麼顏色」。

    管理面的 ``id`` / ``is_active`` / ``sort_order`` / ``created_at`` /
    ``created_by_user_id`` 對未登入的人沒有用途,只是免費送出去的內部狀態。
    這裡斷言完整 key 集合而不是「不含某幾個欄位」,所以往公開 schema 加任何
    一個新欄位都會**故意**弄紅這支測試,逼加欄位的人重新想一次。
    """
    admin = make_user(db, username="banner-admin-2", role="admin")
    _post_banner(client, admin, content=LOGIN_TEXT, level="warning", show_on_login=True)

    rows = client.get(PUBLIC_URL).json()

    assert len(rows) == 1
    assert set(rows[0].keys()) == {"level", "content"}
    assert rows[0]["level"] == "warning"


# ── ③ 預設不公開 ────────────────────────────────────────────────────────────


def test_banner_created_without_the_flag_is_not_public(
    client: TestClient, db: Session
):
    """這一支擋的是「未來某個管理員被嚇一跳」。

    ``show_on_login`` 沒傳 → 必須是 false。既有公告(migration 補的
    ``server_default false``)與所有沒勾的新公告,都不會因為這個功能上線
    就變成任何人讀得到。
    """
    admin = make_user(db, username="banner-admin-3", role="admin")
    created = _post_banner(client, admin, content=INTERNAL_TEXT)

    assert created["show_on_login"] is False
    resp = client.get(PUBLIC_URL)
    assert resp.json() == []
    assert INTERNAL_TEXT not in resp.text


# ── ④ 停用的公告即使勾過也不出現 ────────────────────────────────────────────


def test_deactivated_banner_stays_off_the_login_page(
    client: TestClient, db: Session
):
    admin = make_user(db, username="banner-admin-4", role="admin")
    created = _post_banner(
        client, admin, content=LOGIN_TEXT, show_on_login=True, is_active=False
    )
    assert created["show_on_login"] is True
    assert created["is_active"] is False

    resp = client.get(PUBLIC_URL)
    assert resp.json() == []
    assert LOGIN_TEXT not in resp.text


# ── 管理面本身沒有被這次改動弄鬆 ────────────────────────────────────────────


def test_admin_listing_still_requires_admin(client: TestClient, db: Session):
    """公開的是 ``/public``,不是整個 banners 資源。"""
    assert client.get(ADMIN_URL).status_code == 401
    user = make_user(db, username="banner-plain-user", role="user")
    assert client.get(ADMIN_URL, headers=_bearer(user)).status_code == 403
