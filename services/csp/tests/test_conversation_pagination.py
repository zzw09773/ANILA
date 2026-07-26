# -*- coding: utf-8 -*-
"""對話清單 cursor 分頁 —— 補救計畫 W3-7e。

缺口
----
`conversation_service.list_conversations` 是 `.order_by(updated_at.desc()).all()`
—— **沒有分頁**。一個累積了五千條對話的使用者,每次側欄載入都把五千列連同
`title` / `updated_at` 全撈回來,而畫面上只看得到前二十條。

分頁最容易錯的兩件事,這裡都釘住:

1. **tie**:`updated_at` 單獨當排序鍵,同秒更新的列之間沒有穩定順序 → 換頁時
   同一列可能出現兩次,或整列被跳過。排序鍵必須是 `(updated_at DESC, id DESC)`。
2. **向後相容**:不給 `limit` 必須是舊行為。而且 JSON 形狀不能變 —— cursor 走
   `X-Next-Cursor` 回應頭,`response_model` 與 OpenAPI 產出物一個字都不動。
   「有參數就換 shape」那種做法會讓契約長出兩個版本。

另外 cursor 只帶 id,錨點的 `updated_at` 用子查詢現查 —— 這樣比較是欄位對欄位、
同型別,不管 `conversations.updated_at` 哪天被 W2-10 轉成 timestamptz 都成立。
把時間字面值編進 cursor 的做法會在那次轉換之後炸(aware 對 naive 比較 →
TypeError,W1-4 修的正是同一個坑)。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.models.conversation import Conversation
from tests.conftest import login, make_user

BASE = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


def _mk(db, user, *, title: str, minutes: int) -> Conversation:
    conv = Conversation(
        user_id=user.id,
        title=title,
        updated_at=BASE + timedelta(minutes=minutes),
    )
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def _auth(client, username="alice"):
    return {"Authorization": f"Bearer {login(client, username=username)}"}


def _page(client, headers, **params):
    resp = client.get("/api/conversations", params=params, headers=headers)
    assert resp.status_code == 200, resp.text
    return [row["id"] for row in resp.json()], resp.headers.get("X-Next-Cursor")


# ── 向後相容 ──────────────────────────────────────────────────────────────────

def test_no_limit_returns_everything_and_no_cursor_header(client, db):
    """舊 client(不帶參數)行為與形狀完全不變。"""
    user = make_user(db, username="alice")
    for i in range(5):
        _mk(db, user, title=f"c{i}", minutes=i)
    h = _auth(client)

    resp = client.get("/api/conversations", headers=h)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body, list), "回應必須仍是陣列,不能變成 {items:…}"
    assert len(body) == 5
    assert "X-Next-Cursor" not in resp.headers
    # 舊的排序契約:最新在前
    assert [r["title"] for r in body] == ["c4", "c3", "c2", "c1", "c0"]


# ── 兩頁不重不漏 ──────────────────────────────────────────────────────────────

def test_two_pages_cover_everything_exactly_once(client, db):
    user = make_user(db, username="alice")
    for i in range(7):
        _mk(db, user, title=f"c{i}", minutes=i)
    h = _auth(client)

    first, cursor = _page(client, h, limit=3)
    assert len(first) == 3
    assert cursor is not None

    second, cursor2 = _page(client, h, limit=3, cursor=cursor)
    assert len(second) == 3
    assert cursor2 is not None

    third, cursor3 = _page(client, h, limit=3, cursor=cursor2)
    assert len(third) == 1
    assert cursor3 is None, "最後一頁不該再給 cursor"

    seen = first + second + third
    assert len(seen) == 7, f"總數不對:{seen}"
    assert len(set(seen)) == 7, f"有重複:{seen}"


def test_pagination_survives_ties_on_updated_at(client, db):
    """**同一個 updated_at** 的列之間也要有穩定順序,否則換頁會重複或漏掉。

    這是 keyset 分頁最典型的 bug:只用時間當 cursor,同秒的列會互相踩。
    """
    user = make_user(db, username="alice")
    # 六列全部同一個 updated_at
    for i in range(6):
        _mk(db, user, title=f"tie{i}", minutes=0)
    h = _auth(client)

    all_ids, _ = _page(client, h)
    page1, c1 = _page(client, h, limit=2)
    page2, c2 = _page(client, h, limit=2, cursor=c1)
    page3, c3 = _page(client, h, limit=2, cursor=c2)

    paged = page1 + page2 + page3
    assert c3 is None
    assert paged == all_ids, (
        "同 updated_at 的列在分頁與全撈之間順序不一致 —— 排序鍵不夠穩定"
    )
    assert len(set(paged)) == 6


def test_cursor_respects_filters(client, db):
    """分頁不得讓 origin/collection 過濾漏掉。"""
    user = make_user(db, username="alice")
    for i in range(4):
        c = _mk(db, user, title=f"lm{i}", minutes=i)
        c.origin = "anilalm"
        c.collection_id = 1
    for i in range(3):
        c = _mk(db, user, title=f"ui{i}", minutes=10 + i)
        c.origin = "anila-ui"
    db.commit()
    h = _auth(client)

    p1, cur = _page(client, h, limit=3, origin="anilalm")
    p2, cur2 = _page(client, h, limit=3, origin="anilalm", cursor=cur)

    ids = p1 + p2
    assert len(ids) == 4 and len(set(ids)) == 4
    assert cur2 is None
    titles = [
        r["title"]
        for r in client.get(
            "/api/conversations", params={"origin": "anilalm"}, headers=h
        ).json()
    ]
    assert all(t.startswith("lm") for t in titles)


# ── cursor 不得變成別人資料的探測工具 ────────────────────────────────────────

def test_cursor_pointing_at_another_users_conversation_leaks_nothing(client, db):
    """傳別人的 conversation id 當 cursor,不得把它的 updated_at 當錨點。

    否則可以靠「回了幾列」二分出那條對話的最後更新時間落在哪個區間。
    """
    victim = make_user(db, username="victim")
    attacker = make_user(db, username="attacker")
    foreign = _mk(db, victim, title="受害者的對話", minutes=5)
    for i in range(4):
        _mk(db, attacker, title=f"mine{i}", minutes=i)

    h = _auth(client, "attacker")
    rows, _ = _page(client, h, limit=10, cursor=foreign.id)

    # 錨點查不到(被 user_id 綁住)→ 子查詢回 NULL → 比較為 UNKNOWN → 零列。
    # 重點是**不得**因為錨點是別人的時間而回傳一個「被那個時間切過」的子集。
    assert all(r != foreign.id for r in rows), "回傳了別人的對話"
    assert rows == [] or len(rows) == 4, (
        f"回了一個被別人時間切過的子集({len(rows)} 列)—— 那就是可探測的"
    )


def test_limit_bounds_are_enforced(client, db):
    make_user(db, username="alice")
    h = _auth(client)
    assert client.get("/api/conversations", params={"limit": 0}, headers=h).status_code == 422
    assert client.get("/api/conversations", params={"limit": 201}, headers=h).status_code == 422
    assert client.get("/api/conversations", params={"cursor": 0}, headers=h).status_code == 422
