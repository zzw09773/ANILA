# -*- coding: utf-8 -*-
"""P4.5 —— ANILALM 的記憶只在同一個對話框內。

擁有者裁定(PLAN.md §4.4/4.5,2026-07-30):

    ANILALM 的「同一 session」= **同一個對話框**;不是關分頁,也不是登出。

SYSTEM-MAP §5 L51 同一條規則:長期記憶 ANILA ✓ /
ANILALM「—(只在同一 session 內,不跨 session)」。

所以規則有**兩邊**,兩邊都要驗:

* 對話框**外**的記憶不得注入 ANILALM —— 這是原本的缺陷。
* 對話框**內**的記憶仍然要注入 —— 全部擋掉不是修好,是另一個 bug。
* ANILA 側完全不變,仍然跨對話召回。

這份檔案在 SQLite 上驗 ``user_facts`` 那個儲存體的完整行為(注入與否
看得到 block 內容);``conversation_memory_chunks`` 的向量召回要
pgvector 的 ``halfvec`` / ``<=>``,SQLite 沒有,驗在
``test_memory_scope_pg.py``。
"""
from __future__ import annotations

import itertools

import pytest

from app.api import proxy
from app.models.conversation import Conversation
from app.models.user_memory import UserFact
from app.services import memory_service

from tests.conftest import make_user


_next_id = itertools.count(1)


@pytest.fixture(autouse=True)
def no_embedding(monkeypatch):
    """記憶檢索的向量那半在 SQLite 上跑不動,讓 ``_embed`` 失敗。

    ``retrieve_relevant_chunks`` 對 embed 失敗是 fail-soft(回 []),
    所以 chunk 那半自然空掉,剩下 ``user_facts`` 這條真路徑受測。
    """

    async def _boom(*a, **kw):
        raise RuntimeError("no embedder in the SQLite suite")

    monkeypatch.setattr(memory_service, "_embed", _boom)


def make_conversation(db, user, origin, title="t"):
    conv = Conversation(user_id=user.id, title=title, origin=origin)
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def add_fact(db, user, conv, marker):
    db.add(
        UserFact(
            id=next(_next_id),
            user_id=user.id,
            key=f"key_{marker}",
            value=f"value_{marker}",
            source_conversation_id=conv.id,
            confidence=0.9,
        )
    )
    db.commit()


async def inject(db, user, conv):
    """跑真正的注入路徑,回傳注入後的 system prompt(沒有就是空字串)。"""
    body = {"messages": [{"role": "user", "content": "今天要談什麼?"}]}
    await proxy._inject_memory(
        db, user.id, body, exclude_conversation_id=conv.id
    )
    first = body["messages"][0]
    if first.get("role") != "system":
        return ""
    return first.get("content") or ""


# ── 規則的兩邊 ────────────────────────────────────────────────────────────────


class TestAnilalmMemoryIsConfinedToItsOwnConversation:
    @pytest.mark.asyncio
    async def test_outside_memory_is_not_injected_but_inside_memory_is(self, db):
        """一次驗兩邊 —— 因為「只擋外面」與「裡面還在」是同一條規則。"""
        user = make_user(db)
        lm_conv = make_conversation(db, user, origin="anilalm", title="lm")
        elsewhere = make_conversation(db, user, origin="anila-ui", title="ui")

        add_fact(db, user, lm_conv, "inside")
        add_fact(db, user, elsewhere, "outside")

        block = await inject(db, user, lm_conv)

        assert "value_inside" in block, (
            "同一個對話框內的記憶仍然要注入 —— 全擋掉是另一個 bug"
        )
        assert "value_outside" not in block, (
            "對話框外的記憶洩進 ANILALM(P4.5 的缺陷本體)"
        )

    @pytest.mark.asyncio
    async def test_anilalm_with_only_outside_memory_gets_no_block_at_all(self, db):
        """外面全部擋掉之後沒東西可注入,就不該硬塞一個空 system 訊息。"""
        user = make_user(db)
        lm_conv = make_conversation(db, user, origin="anilalm", title="lm")
        elsewhere = make_conversation(db, user, origin="anila-ui", title="ui")
        add_fact(db, user, elsewhere, "outside")

        block = await inject(db, user, lm_conv)

        assert block == ""

    @pytest.mark.asyncio
    async def test_other_anilalm_conversation_is_also_outside(self, db):
        """「同一 session」= 同一個對話框,不是「ANILALM 這個應用」。
        另一個 ANILALM 對話框一樣算外面。"""
        user = make_user(db)
        lm_a = make_conversation(db, user, origin="anilalm", title="lm-a")
        lm_b = make_conversation(db, user, origin="anilalm", title="lm-b")
        add_fact(db, user, lm_a, "box_a")
        add_fact(db, user, lm_b, "box_b")

        block = await inject(db, user, lm_b)

        assert "value_box_b" in block
        assert "value_box_a" not in block


class TestAnilaSideKeepsCrossConversationMemory:
    @pytest.mark.asyncio
    async def test_anila_ui_still_sees_memory_from_other_conversations(self, db):
        """ANILA 的長期記憶是規格給的功能(SYSTEM-MAP §5 L51 ✓),不能被這包收掉。"""
        user = make_user(db)
        ui_conv = make_conversation(db, user, origin="anila-ui", title="ui")
        elsewhere = make_conversation(db, user, origin="anila-ui", title="other")
        add_fact(db, user, ui_conv, "inside")
        add_fact(db, user, elsewhere, "outside")

        block = await inject(db, user, ui_conv)

        assert "value_inside" in block
        assert "value_outside" in block, "ANILA 側必須維持跨對話召回"

    @pytest.mark.asyncio
    async def test_legacy_null_origin_is_treated_as_anila(self, db):
        """``origin`` NULL = 舊資料 = ANILA UI(見 models/conversation.py L23)。"""
        user = make_user(db)
        legacy = make_conversation(db, user, origin=None, title="legacy")
        elsewhere = make_conversation(db, user, origin="anila-ui", title="other")
        add_fact(db, user, legacy, "inside")
        add_fact(db, user, elsewhere, "outside")

        block = await inject(db, user, legacy)

        assert "value_outside" in block


class TestScopeResolution:
    """路由本身:哪個對話框要被關進去。"""

    def test_anilalm_conversation_confines_to_itself(self, db):
        user = make_user(db)
        conv = make_conversation(db, user, origin="anilalm")
        assert proxy._memory_confined_to_conversation(db, conv.id) == conv.id

    def test_anila_ui_conversation_is_not_confined(self, db):
        user = make_user(db)
        conv = make_conversation(db, user, origin="anila-ui")
        assert proxy._memory_confined_to_conversation(db, conv.id) is None

    def test_no_conversation_id_is_not_confined(self, db):
        assert proxy._memory_confined_to_conversation(db, None) is None


class TestFactStoreScoping:
    """``get_user_facts`` 是 block 的兩個來源之一,單獨驗它的範圍參數。"""

    def test_only_conversation_id_filters_to_that_source(self, db):
        user = make_user(db)
        a = make_conversation(db, user, origin="anilalm", title="a")
        b = make_conversation(db, user, origin="anilalm", title="b")
        add_fact(db, user, a, "a")
        add_fact(db, user, b, "b")

        scoped = memory_service.get_user_facts(
            db, user.id, only_conversation_id=a.id
        )
        assert [f.value for f in scoped] == ["value_a"]

    def test_default_returns_everything(self, db):
        user = make_user(db)
        a = make_conversation(db, user, origin="anila-ui", title="a")
        b = make_conversation(db, user, origin="anila-ui", title="b")
        add_fact(db, user, a, "a")
        add_fact(db, user, b, "b")

        assert len(memory_service.get_user_facts(db, user.id)) == 2
