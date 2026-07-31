# -*- coding: utf-8 -*-
"""``users.updated_at`` 是 NULL 的列不得讓端點 500。

``models/user.py:59`` 的 ``updated_at`` 只有 python 端的 ``default`` /
``onupdate``,沒有 ``server_default`` 也沒有 ``nullable=False`` —— 任何不是
經由 ORM 插入的列(舊 migration、匯入腳本、raw SQL)都可能是 NULL。
``UserResponse`` 卻宣告成非選填,序列化時直接炸成 500。

500 的壞處不只是壞掉:管理員看到的是「使用者清單整個掛了」,而不是
「有一列資料缺欄位」,排查方向會完全走偏。
"""

from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from sqlalchemy import text

from tests.conftest import login, make_user

pytestmark = pytest.mark.filterwarnings("ignore")


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _null_out_updated_at(db, user_id: int) -> None:
    # 走 raw SQL:ORM flush 會被 onupdate 補回時間,測不到 NULL 這個狀態。
    db.execute(text("UPDATE users SET updated_at = NULL WHERE id = :id"),
               {"id": user_id})
    db.commit()
    assert db.execute(
        text("SELECT updated_at FROM users WHERE id = :id"), {"id": user_id}
    ).scalar() is None


def test_get_user_with_null_updated_at_returns_200(client, db):
    admin = make_user(db, username="ua_admin", role="admin")
    target = make_user(db, username="ua_target", role="user")
    _null_out_updated_at(db, target.id)

    token = login(client, "ua_admin")
    resp = client.get(f"/api/users/{target.id}", headers=_bearer(token))

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["username"] == "ua_target"
    assert body["updated_at"] is None
    assert admin.id != target.id


def test_user_list_with_null_updated_at_returns_200(client, db):
    make_user(db, username="ul_admin", role="admin")
    target = make_user(db, username="ul_target", role="user")
    _null_out_updated_at(db, target.id)

    token = login(client, "ul_admin")
    resp = client.get("/api/users", headers=_bearer(token))

    assert resp.status_code == 200, resp.text
    row = next(u for u in resp.json() if u["username"] == "ul_target")
    assert row["updated_at"] is None


def test_normal_user_still_reports_updated_at(client, db):
    """把欄位改成可為 None 不能順手把正常值弄丟。"""
    make_user(db, username="uk_admin", role="admin")
    target = make_user(db, username="uk_target", role="user")

    token = login(client, "uk_admin")
    resp = client.get(f"/api/users/{target.id}", headers=_bearer(token))
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated_at"] is not None
