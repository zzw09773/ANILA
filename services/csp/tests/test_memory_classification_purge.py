# -*- coding: utf-8 -*-
"""P4.4 —— 對話升密即刪除先前萃取的記憶。

擁有者裁定(PLAN.md §4.4/4.5,2026-07-30):

    對話**升密之後,先前萃取的記憶直接刪除**(不是標記不可用)。

SYSTEM-MAP §5 L189 同一條規則的另一半:「對話中途升密 → 之前萃取的記憶
要撤回 → 每則記憶要記得來源對話,才找得到」。

**記憶落在哪些儲存體**(這份清單就是覆蓋率;漏一個就等於沒修):

1. ``conversation_memory_chunks`` —— RAG 召回列,以 ``conversation_id``
   指回來源對話。
2. ``conversation_memory_chunks.embedding`` —— 向量是**同一列上的欄位**,
   不是獨立的儲存體或索引表,所以刪列即刪向量;下面的測試明確斷言列數,
   涵蓋向量。
3. ``user_facts`` —— 萃取出的 key/value,以 ``source_conversation_id``
   指回來源對話。

沒有第四個儲存體:記憶檢索每次都直接查這兩張表(``memory_service``
沒有任何 in-process 快取或 Redis 層),所以「刪表列」= 「使用者再也讀不到」。

閂鎖是單向的,這裡不新增任何降級路徑。
"""
from __future__ import annotations

import itertools

import pytest

from app.models.classification import ClassificationEvent
from app.models.conversation import Conversation
from app.models.user_memory import ConversationMemoryChunk, UserFact
from app.modules.policy import apply_classification

from tests.conftest import make_user


# ── fixtures ─────────────────────────────────────────────────────────────────


def make_conversation(db, user, origin="anila-ui", title="t"):
    conv = Conversation(user_id=user.id, title=title, origin=origin)
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


_next_id = itertools.count(1)


def seed_memory(db, user, conv, marker):
    """Write one row into EVERY store memory lands in, tagged with ``marker``.

    Primary keys are assigned by hand: both tables use ``BigInteger``
    autoincrement PKs, which SQLite (the suite's backend) does not
    populate — only ``INTEGER PRIMARY KEY`` gets rowid aliasing.
    """
    db.add(
        ConversationMemoryChunk(
            id=next(_next_id),
            user_id=user.id,
            conversation_id=conv.id,
            message_id=None,
            role="user",
            content=f"chunk-{marker}",
            # halfvec at the SQL layer, declared Text on the ORM — the vector
            # is a column on this row, so the row DELETE takes it with it.
            embedding="[0.1,0.2]",
            embedding_source_model="nvidia/nv-embed-v2",
            embedding_native_dim=4096,
            is_encrypted=False,
        )
    )
    db.add(
        UserFact(
            id=next(_next_id),
            user_id=user.id,
            key=f"key-{marker}",
            value=f"value-{marker}",
            source_conversation_id=conv.id,
            source_message_id=None,
            confidence=0.9,
        )
    )
    db.commit()


def stores_for(db, conv):
    """Row counts per store, keyed by store name (the enumeration above)."""
    return {
        "conversation_memory_chunks": db.query(ConversationMemoryChunk)
        .filter(ConversationMemoryChunk.conversation_id == conv.id)
        .count(),
        "user_facts": db.query(UserFact)
        .filter(UserFact.source_conversation_id == conv.id)
        .count(),
    }


def upgrade(db, conv, actor, level="機密", reason="manual_admin"):
    return apply_classification(
        db,
        resource_type="conversation",
        resource_id=str(conv.id),
        new_level=level,
        actor_type="user",
        actor_id=str(actor.id),
        reason=reason,
    )


# ── 主張:升密 → 每一個儲存體都不再持有舊內容 ─────────────────────────────


class TestUpgradePurgesEveryStore:
    def test_upgrade_empties_every_memory_store_for_that_conversation(self, db):
        """核心驗收:升密後,上面列舉的每一個儲存體都查不到那則記憶。"""
        user = make_user(db)
        conv = make_conversation(db, user)
        seed_memory(db, user, conv, "target")

        # 前置條件:每個儲存體都真的有東西可以刪(否則測試會空跑成假綠燈)。
        before = stores_for(db, conv)
        assert before == {
            "conversation_memory_chunks": 1,
            "user_facts": 1,
        }, before

        upgrade(db, conv, user)

        after = stores_for(db, conv)
        assert after == {
            "conversation_memory_chunks": 0,
            "user_facts": 0,
        }, f"升密後仍有記憶存活:{after}"

    def test_purge_does_not_touch_other_conversations(self, db):
        """全刪不是修好,是另一個 bug —— 別的對話的記憶必須毫髮無傷。"""
        user = make_user(db)
        target = make_conversation(db, user, title="target")
        bystander = make_conversation(db, user, title="bystander")
        seed_memory(db, user, target, "target")
        seed_memory(db, user, bystander, "bystander")

        upgrade(db, target, user)

        assert stores_for(db, target) == {
            "conversation_memory_chunks": 0,
            "user_facts": 0,
        }
        assert stores_for(db, bystander) == {
            "conversation_memory_chunks": 1,
            "user_facts": 1,
        }, "升密只該撤回該對話的記憶"

    def test_purge_lands_in_the_same_transaction_as_the_upgrade(self, db):
        """升密與撤回必須同生共死,不能出現「已升密但記憶還在」。"""
        user = make_user(db)
        conv = make_conversation(db, user)
        seed_memory(db, user, conv, "atomic")

        event = upgrade(db, conv, user)

        db.refresh(conv)
        assert conv.classification_level == "機密"
        assert event is not None
        assert db.query(ClassificationEvent).count() == 1
        assert stores_for(db, conv) == {
            "conversation_memory_chunks": 0,
            "user_facts": 0,
        }


class TestEveryUpgradePathPurges:
    """撤回掛在 ``apply_classification``(四級閂鎖的唯一入口),
    所以每一條升密路徑都會撤回,而不是只有手動標記那條。"""

    @pytest.mark.parametrize(
        "reason,level",
        [
            ("manual_admin", "機密"),
            ("memory_inherited", "密"),
            ("source_selected", "營業秘密"),
            ("agent_policy", "密"),
            ("service_policy", "密"),
            ("content_detection", "密"),
        ],
    )
    def test_each_reason_purges(self, db, reason, level):
        user = make_user(db)
        conv = make_conversation(db, user)
        seed_memory(db, user, conv, reason)

        upgrade(db, conv, user, level=level, reason=reason)

        assert stores_for(db, conv) == {
            "conversation_memory_chunks": 0,
            "user_facts": 0,
        }, f"reason={reason} 這條升密路徑沒有撤回記憶"

    def test_first_latch_from_無機密_purges(self, db):
        """最低一級的首次升密(無機密 → 營業秘密)也算升密。"""
        user = make_user(db)
        conv = make_conversation(db, user)
        seed_memory(db, user, conv, "lowest")

        upgrade(db, conv, user, level="營業秘密", reason="source_selected")

        assert stores_for(db, conv)["user_facts"] == 0
        assert stores_for(db, conv)["conversation_memory_chunks"] == 0


class TestNonUpgradesDoNotPurge:
    """不是升密就不該刪。維持同級 / 嘗試降級都是 no-op,
    否則任何一次重複 latch 都會把使用者的記憶清掉。"""

    def test_same_level_noop_keeps_memory(self, db):
        user = make_user(db)
        conv = make_conversation(db, user)
        upgrade(db, conv, user, level="密", reason="manual_admin")
        # 升密後重新種一份記憶,模擬升密後才產生的新記憶。
        seed_memory(db, user, conv, "after")

        assert upgrade(db, conv, user, level="密", reason="manual_admin") is None

        assert stores_for(db, conv) == {
            "conversation_memory_chunks": 1,
            "user_facts": 1,
        }, "同級 no-op 不該刪記憶"

    def test_downgrade_attempt_noop_keeps_memory_and_stays_latched(self, db):
        user = make_user(db)
        conv = make_conversation(db, user)
        upgrade(db, conv, user, level="機密", reason="manual_admin")
        seed_memory(db, user, conv, "after")

        assert upgrade(db, conv, user, level="密", reason="manual_admin") is None

        db.refresh(conv)
        # 閂鎖仍是單向的 —— 本包不新增任何降級路徑。
        assert conv.classification_level == "機密"
        assert stores_for(db, conv) == {
            "conversation_memory_chunks": 1,
            "user_facts": 1,
        }

    def test_non_conversation_resource_upgrade_keeps_conversation_memory(self, db):
        """升的是別的資源型別時,對話記憶不受影響。"""
        from app.models.ingestion import IngestionCollection

        user = make_user(db)
        conv = make_conversation(db, user)
        seed_memory(db, user, conv, "unrelated")

        coll = IngestionCollection(
            name="c",
            chunking_config={},
            embedding_model="nvidia/nv-embed-v2",
            embedding_dim=4000,
            created_by=user.id,
        )
        db.add(coll)
        db.commit()
        db.refresh(coll)

        apply_classification(
            db,
            resource_type="collection",
            resource_id=str(coll.id),
            new_level="機密",
            actor_type="user",
            actor_id=str(user.id),
            reason="manual_admin",
        )

        assert stores_for(db, conv) == {
            "conversation_memory_chunks": 1,
            "user_facts": 1,
        }
