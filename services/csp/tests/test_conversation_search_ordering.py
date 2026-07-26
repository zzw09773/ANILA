# -*- coding: utf-8 -*-
"""對話搜尋的排序與截斷 —— 補救計畫 T2-7a。

修的缺陷
--------
`GET /api/conversations/search` 先前的實作是:

    matched_ids = (db.query(Conversation.id)
                     .outerjoin(Message, ...)
                     .filter(...)
                     .distinct()
                     .limit(limit)      # ← 沒有 order_by 就先截斷
                     .subquery())
    convs = (db.query(Conversation)
               .filter(Conversation.id.in_(matched_ids))
               .order_by(Conversation.updated_at.desc())   # ← 才排序
               .all())

`LIMIT` 沒有 `ORDER BY` 時回哪幾列在 SQL 語意上是**未定義**的(PostgreSQL 通常
給 scan 順序,約等於偏舊),而排序只發生在**已經被截斷的子集**上。

使用者面的後果:用高頻詞搜三個月前的對話,結果宣稱只有 30 筆而且很可能不含
目標 → 使用者的結論是「找不到」,但資料就在庫裡。而且失敗是**靜默**的:沒有
錯誤、沒有「還有更多」的提示。這是「三個月後想找回一則對話」最典型的失敗。

為什麼修法不是「把 order_by 加進原本的 subquery」
------------------------------------------------
那在 PostgreSQL 會直接報錯 —— `SELECT DISTINCT` 的 ORDER BY 運算式必須出現在
select list 裡,而 select list 只有 `id`。改用 `EXISTS` 之後 outerjoin 與
DISTINCT 都不需要了(join 產生重複 id 才是 DISTINCT 存在的唯一理由)。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

from app.models.conversation import Conversation
from app.models.message import Message

from .conftest import login, make_user


TOTAL = 50
LIMIT = 30
TERM = "季度報告"  # 高頻詞:命中數遠大於 limit,這才會觸發截斷


def _seed(db, user, *, in_title: bool) -> list[int]:
    """建 TOTAL 筆命中的對話,updated_at 遞增(索引越大越新)。

    回傳依「最新優先」排序的 conversation id 清單。
    """
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    convs = []
    for i in range(TOTAL):
        conv = Conversation(
            user_id=user.id,
            title=(f"{TERM} 第 {i} 期" if in_title else f"無關標題 {i}"),
            created_at=base + timedelta(days=i),
            updated_at=base + timedelta(days=i),
        )
        db.add(conv)
        convs.append(conv)
    db.commit()
    for conv in convs:
        db.refresh(conv)
        if not in_title:
            # 標題不含關鍵字 → 只能靠訊息內容命中,走 EXISTS 那一支
            db.add(
                Message(
                    conversation_id=conv.id,
                    role="user",
                    content=f"請幫我看 {TERM} 的數字",
                )
            )
    db.commit()
    # updated_at 越大越新 → 反轉
    return [c.id for c in reversed(convs)]


def _search(client, token, *, limit=LIMIT):
    resp = client.get(
        f"/api/conversations/search?q={TERM}&limit={limit}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_search_returns_the_newest_matches_not_arbitrary_ones(db, client):
    """核心斷言:命中數 > limit 時,回傳的必須是**最新的** limit 筆。

    這就是先紅後綠的那條 —— 舊實作因為 LIMIT 先於 ORDER BY,回的是 scan 順序
    (偏舊),所以會少掉最新的那些。
    """
    user = make_user(db, username="search-order-owner")
    newest_first = _seed(db, user, in_title=True)
    token = login(client, "search-order-owner")

    hits = _search(client, token)
    assert len(hits) == LIMIT
    assert [h["id"] for h in hits] == newest_first[:LIMIT]


def test_search_orders_by_updated_at_desc(db, client):
    """回傳順序本身必須是最新優先(不只是「集合對」)。"""
    user = make_user(db, username="search-order-owner2")
    _seed(db, user, in_title=True)
    token = login(client, "search-order-owner2")

    hits = _search(client, token)
    updated = [h["updated_at"] for h in hits]
    assert updated == sorted(updated, reverse=True)


def test_search_matches_message_content_via_exists(db, client):
    """標題不含關鍵字、只有訊息內容命中時也要找得到,且同樣是最新優先。

    這條走的是改寫後的 EXISTS 分支 —— 也就是原本 outerjoin + DISTINCT 想做的事。
    """
    user = make_user(db, username="search-body-owner")
    newest_first = _seed(db, user, in_title=False)
    token = login(client, "search-body-owner")

    hits = _search(client, token)
    assert len(hits) == LIMIT
    assert [h["id"] for h in hits] == newest_first[:LIMIT]


def test_search_returns_each_conversation_once(db, client):
    """一則對話有多筆命中訊息時不可重複出現。

    原本靠 `DISTINCT` 消除 outerjoin 產生的重複;改用 EXISTS 之後不會產生重複,
    這支測試把該性質釘住,避免日後有人「順手」把 EXISTS 改回 join。
    """
    user = make_user(db, username="search-dedup-owner")
    conv = Conversation(user_id=user.id, title="無關標題")
    db.add(conv)
    db.commit()
    db.refresh(conv)
    for i in range(5):
        db.add(
            Message(
                conversation_id=conv.id,
                role="user",
                content=f"{TERM} 的第 {i} 個問題",
            )
        )
    db.commit()
    token = login(client, "search-dedup-owner")

    hits = _search(client, token)
    assert [h["id"] for h in hits] == [conv.id]


def test_search_does_not_leak_other_users_conversations(db, client):
    """搜尋只能看到自己的 —— 改寫掉 join 之後這條 filter 仍必須在。"""
    owner = make_user(db, username="search-owner")
    other = make_user(db, username="search-other")
    mine = Conversation(user_id=owner.id, title=f"{TERM} 我的")
    theirs = Conversation(user_id=other.id, title=f"{TERM} 別人的")
    db.add_all([mine, theirs])
    db.commit()
    db.refresh(mine)
    token = login(client, "search-owner")

    hits = _search(client, token)
    assert [h["id"] for h in hits] == [mine.id]
