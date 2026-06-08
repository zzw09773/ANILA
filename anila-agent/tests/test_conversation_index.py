"""P2-5 ConversationIndex 單元測試。

覆蓋面:
- ``TurnRecord`` / ``CompactionRecord`` dataclass field、duration、to/from_dict roundtrip。
- ``ConversationIndex`` record helpers:start_turn / finalize_turn / bump_tool_call /
  record_compaction、找不到 turn 的錯誤、duplicate turn_index 防呆。
- find_turn / find_turn_by_id / compactions_in_turn / latest_turn 查詢。
- summary() / 統計 property。
- to_dict / from_dict 完整 roundtrip。
- 與 P1-3 ``SessionContext`` 整合 — attach index 後 start_turn 自動 record + finalize。
- 與 P1-6 ``CompactingSession`` 整合 — index_callback 收到 CompactionStats。
"""

from __future__ import annotations

from typing import Any

import pytest

from anila_agent.core import SessionContext
from anila_agent.memory.compaction import (
    CompactingSession,
    CompactionStats,
    MicroCompactor,
)
from anila_agent.memory.conversation_index import (
    CompactionRecord,
    ConversationIndex,
    TurnRecord,
)

# ---------------------------------------------------------------------------
# TurnRecord
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_turn_record_defaults() -> None:
    """TurnRecord 預設欄位:ended_at None、count 0、metadata 空 dict。"""
    r = TurnRecord(turn_id="t1", turn_index=0, started_at=100.0)
    assert r.turn_id == "t1"
    assert r.turn_index == 0
    assert r.started_at == 100.0
    assert r.ended_at is None
    assert r.message_indices is None
    assert r.tool_call_count == 0
    assert r.compaction_count == 0
    assert r.metadata == {}
    assert r.duration is None


@pytest.mark.unit
def test_turn_record_duration_after_ended() -> None:
    """ended_at 有值時 duration 才有值。"""
    r = TurnRecord(turn_id="t", turn_index=0, started_at=100.0, ended_at=105.5)
    assert r.duration == pytest.approx(5.5)


@pytest.mark.unit
def test_turn_record_to_from_dict_roundtrip() -> None:
    """to_dict/from_dict roundtrip 還原所有欄位。"""
    original = TurnRecord(
        turn_id="t-x",
        turn_index=3,
        started_at=1000.0,
        ended_at=1005.0,
        message_indices=(2, 7),
        tool_call_count=2,
        compaction_count=1,
        metadata={"foo": "bar"},
    )
    data = original.to_dict()
    assert isinstance(data["message_indices"], list)
    restored = TurnRecord.from_dict(data)
    assert restored == original


@pytest.mark.unit
def test_turn_record_from_dict_handles_none_message_indices() -> None:
    """message_indices 為 None 時 from_dict 不要炸。"""
    data: dict[str, Any] = {
        "turn_id": "x",
        "turn_index": 0,
        "started_at": 1.0,
        "ended_at": None,
        "message_indices": None,
        "tool_call_count": 0,
        "compaction_count": 0,
        "metadata": {},
    }
    r = TurnRecord.from_dict(data)
    assert r.message_indices is None
    assert r.ended_at is None


# ---------------------------------------------------------------------------
# CompactionRecord
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_compaction_record_tokens_saved() -> None:
    """tokens_saved = before - after,after > before 時為 0。"""
    r = CompactionRecord(
        compaction_id="c",
        at_turn_index=1,
        compactor_name="micro",
        messages_dropped=3,
        before_tokens=1000,
        after_tokens=400,
        triggered_at=100.0,
    )
    assert r.tokens_saved == 600

    grow = CompactionRecord(
        compaction_id="c2",
        at_turn_index=1,
        compactor_name="micro",
        messages_dropped=0,
        before_tokens=100,
        after_tokens=120,
        triggered_at=100.0,
    )
    assert grow.tokens_saved == 0


@pytest.mark.unit
def test_compaction_record_to_from_dict_roundtrip() -> None:
    """to_dict/from_dict roundtrip。"""
    original = CompactionRecord(
        compaction_id="cid-1",
        at_turn_index=2,
        compactor_name="llm_summary",
        messages_dropped=10,
        before_tokens=5000,
        after_tokens=2000,
        triggered_at=12345.6,
    )
    restored = CompactionRecord.from_dict(original.to_dict())
    assert restored == original


# ---------------------------------------------------------------------------
# ConversationIndex — record helpers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_start_turn_appends_record_and_returns_it() -> None:
    """start_turn append 進 turns,回傳同一個 record。"""
    idx = ConversationIndex()
    r = idx.start_turn(turn_id="t1", turn_index=0, started_at=100.0)
    assert idx.turns == [r]
    assert r.turn_index == 0
    assert r.ended_at is None


@pytest.mark.unit
def test_start_turn_duplicate_index_raises() -> None:
    """同 turn_index record 兩次應丟 ValueError。"""
    idx = ConversationIndex()
    idx.start_turn(turn_id="t1", turn_index=0)
    with pytest.raises(ValueError, match="turn_index 0 already recorded"):
        idx.start_turn(turn_id="t2", turn_index=0)


@pytest.mark.unit
def test_finalize_turn_replaces_record_with_ended_at() -> None:
    """finalize_turn 後 record 拿到 ended_at。"""
    idx = ConversationIndex()
    idx.start_turn(turn_id="t1", turn_index=0, started_at=100.0)
    finalized = idx.finalize_turn(turn_index=0, ended_at=110.0)
    assert finalized.ended_at == 110.0
    assert finalized.duration == 10.0
    assert idx.find_turn(0) == finalized


@pytest.mark.unit
def test_finalize_turn_with_message_indices() -> None:
    """start_turn 帶 message_index_start + finalize 帶 end → message_indices 自動填。"""
    idx = ConversationIndex()
    idx.start_turn(turn_id="t1", turn_index=0, message_index_start=5)
    finalized = idx.finalize_turn(turn_index=0, message_index_end=12)
    assert finalized.message_indices == (5, 12)
    # 內部標記不應外漏
    assert "_message_index_start" not in finalized.metadata


@pytest.mark.unit
def test_finalize_turn_unknown_raises_keyerror() -> None:
    """finalize 沒 start 過的 turn 要丟 KeyError。"""
    idx = ConversationIndex()
    with pytest.raises(KeyError):
        idx.finalize_turn(turn_index=99)


@pytest.mark.unit
def test_bump_tool_call_increments_count() -> None:
    """bump_tool_call 把 tool_call_count +1。"""
    idx = ConversationIndex()
    idx.start_turn(turn_id="t1", turn_index=0)
    r1 = idx.bump_tool_call(turn_index=0)
    assert r1.tool_call_count == 1
    r2 = idx.bump_tool_call(turn_index=0, delta=2)
    assert r2.tool_call_count == 3


@pytest.mark.unit
def test_bump_tool_call_unknown_raises_keyerror() -> None:
    idx = ConversationIndex()
    with pytest.raises(KeyError):
        idx.bump_tool_call(turn_index=0)


@pytest.mark.unit
def test_record_compaction_appends_and_updates_turn() -> None:
    """record_compaction 把該 turn 的 compaction_count +1。"""
    idx = ConversationIndex()
    idx.start_turn(turn_id="t1", turn_index=0)
    record = idx.record_compaction(
        compactor_name="micro",
        messages_dropped=2,
        before_tokens=800,
        after_tokens=300,
        at_turn_index=0,
    )
    assert record.compactor_name == "micro"
    assert record.tokens_saved == 500
    assert idx.find_turn(0) is not None
    turn = idx.find_turn(0)
    assert turn is not None
    assert turn.compaction_count == 1


@pytest.mark.unit
def test_record_compaction_no_active_turn_is_ok() -> None:
    """at_turn_index=-1 不會丟例外,只是不更新任何 turn。"""
    idx = ConversationIndex()
    idx.record_compaction(
        compactor_name="micro",
        messages_dropped=0,
        before_tokens=10,
        after_tokens=5,
    )
    assert len(idx.compactions) == 1
    assert idx.compactions[0].at_turn_index == -1


@pytest.mark.unit
def test_record_compaction_unknown_turn_silently_skips_turn_update() -> None:
    """at_turn_index 指向不存在的 turn,不要丟 KeyError(caller 不應被罰)。"""
    idx = ConversationIndex()
    idx.record_compaction(
        compactor_name="snip",
        messages_dropped=1,
        before_tokens=100,
        after_tokens=50,
        at_turn_index=99,
    )
    assert len(idx.compactions) == 1


# ---------------------------------------------------------------------------
# 查詢 helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_find_turn_by_id_and_index() -> None:
    idx = ConversationIndex()
    idx.start_turn(turn_id="alpha", turn_index=0)
    idx.start_turn(turn_id="beta", turn_index=1)
    assert idx.find_turn(0) is not None
    assert idx.find_turn(99) is None
    assert idx.find_turn_by_id("beta") is not None
    assert idx.find_turn_by_id("zeta") is None


@pytest.mark.unit
def test_compactions_in_turn_filters_by_turn_index() -> None:
    idx = ConversationIndex()
    idx.start_turn(turn_id="t0", turn_index=0)
    idx.start_turn(turn_id="t1", turn_index=1)
    idx.record_compaction(
        compactor_name="m", messages_dropped=1, before_tokens=10, after_tokens=5, at_turn_index=0
    )
    idx.record_compaction(
        compactor_name="m", messages_dropped=1, before_tokens=10, after_tokens=5, at_turn_index=1
    )
    idx.record_compaction(
        compactor_name="m", messages_dropped=1, before_tokens=10, after_tokens=5, at_turn_index=0
    )
    assert len(idx.compactions_in_turn(0)) == 2
    assert len(idx.compactions_in_turn(1)) == 1
    assert len(idx.compactions_in_turn(9)) == 0


@pytest.mark.unit
def test_latest_turn_returns_last() -> None:
    idx = ConversationIndex()
    assert idx.latest_turn() is None
    idx.start_turn(turn_id="a", turn_index=0)
    idx.start_turn(turn_id="b", turn_index=1)
    latest = idx.latest_turn()
    assert latest is not None
    assert latest.turn_id == "b"


# ---------------------------------------------------------------------------
# 統計 / summary
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_summary_human_readable() -> None:
    idx = ConversationIndex()
    idx.start_turn(turn_id="t0", turn_index=0)
    idx.bump_tool_call(turn_index=0)
    idx.bump_tool_call(turn_index=0)
    idx.record_compaction(
        compactor_name="micro",
        messages_dropped=3,
        before_tokens=1000,
        after_tokens=400,
        at_turn_index=0,
    )
    s = idx.summary()
    assert "1 turns" in s
    assert "1 compactions" in s
    assert "2 tool calls" in s
    assert "600 tokens saved" in s


@pytest.mark.unit
def test_totals_zero_on_empty_index() -> None:
    idx = ConversationIndex()
    assert idx.total_turns == 0
    assert idx.total_compactions == 0
    assert idx.total_tool_calls == 0
    assert idx.total_tokens_saved == 0
    assert "0 turns" in idx.summary()


# ---------------------------------------------------------------------------
# Serialization roundtrip
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_conversation_index_to_from_dict_roundtrip() -> None:
    """整個 index 序列化還原後內容相等。"""
    idx = ConversationIndex()
    idx.start_turn(turn_id="t0", turn_index=0, started_at=100.0)
    idx.bump_tool_call(turn_index=0)
    idx.finalize_turn(turn_index=0, ended_at=110.0, message_index_end=5)
    idx.record_compaction(
        compactor_name="snip",
        messages_dropped=2,
        before_tokens=900,
        after_tokens=300,
        at_turn_index=0,
        compaction_id="cid-stable",
        triggered_at=105.0,
    )

    data = idx.to_dict()
    restored = ConversationIndex.from_dict(data)

    assert restored.total_turns == idx.total_turns
    assert restored.total_compactions == idx.total_compactions
    assert restored.turns == idx.turns
    assert restored.compactions == idx.compactions


# ---------------------------------------------------------------------------
# 整合:SessionContext (P1-3)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_session_context_default_conversation_index_is_none() -> None:
    """不傳 conversation_index 時為 None — P1-3 既有 caller 完全不感知。"""
    sess = SessionContext(session_id="s")
    assert sess.conversation_index is None


@pytest.mark.unit
def test_session_start_turn_with_index_auto_records() -> None:
    """SessionContext 掛 index 後,start_turn 進出自動 record + finalize。"""
    idx = ConversationIndex()
    sess = SessionContext(session_id="s", conversation_index=idx)

    with sess.start_turn() as turn:
        # 進入 with 後 turn 應該已 record 進 index
        rec = idx.find_turn_by_id(turn.turn_id)
        assert rec is not None
        assert rec.ended_at is None  # 還沒結束

    # 離開 with 後 finalize 過了
    finalized = idx.find_turn_by_id(turn.turn_id)
    assert finalized is not None
    assert finalized.ended_at is not None


@pytest.mark.unit
def test_session_multiple_turns_index_in_order() -> None:
    """多個 turn 進出後,index 內 turn 順序 == turn_index 順序。"""
    idx = ConversationIndex()
    sess = SessionContext(session_id="s", conversation_index=idx)

    with sess.start_turn():
        pass
    with sess.start_turn():
        pass
    with sess.start_turn():
        pass

    assert idx.total_turns == 3
    assert [t.turn_index for t in idx.turns] == [0, 1, 2]
    for t in idx.turns:
        assert t.ended_at is not None


# ---------------------------------------------------------------------------
# 整合:CompactingSession (P1-6)
# ---------------------------------------------------------------------------


class _FakeSession:
    """In-memory fake — 對齊 P1-6 既有 test 的 _FakeSession 介面。"""

    def __init__(self, session_id: str = "test-session") -> None:
        self.session_id = session_id
        self._items: list[dict[str, Any]] = []

    async def get_items(self, limit: int | None = None) -> list[dict[str, Any]]:
        if limit is None:
            return list(self._items)
        return list(self._items[-limit:])

    async def add_items(self, items: list[dict[str, Any]]) -> None:
        self._items.extend(items)

    async def pop_item(self) -> dict[str, Any] | None:
        return self._items.pop() if self._items else None

    async def clear_session(self) -> None:
        self._items.clear()


@pytest.mark.asyncio
async def test_compacting_session_index_callback_receives_stats() -> None:
    """掛 index_callback 後,每跑完一個 compactor 都回呼一次。"""
    fake = _FakeSession()
    # 塞超過 retain_last_n,確保 MicroCompactor 真的會丟東西
    items: list[dict[str, Any]] = [
        {"type": "function_call", "call_id": "c1"},
        {"type": "function_call_output", "call_id": "c1", "output": "x"},
        {"type": "function_call", "call_id": "c2"},
        {"type": "function_call_output", "call_id": "c2", "output": "y"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    await fake.add_items(items)

    captured: list[CompactionStats] = []

    def _cb(stats: CompactionStats) -> None:
        captured.append(stats)

    cs = CompactingSession(
        underlying=fake,
        compactors=[MicroCompactor(retain_last_n=2)],
        index_callback=_cb,
    )
    await cs.get_items()

    assert len(captured) == 1
    assert captured[0].compactor_name == "micro"
    assert captured[0].before_messages == 6


@pytest.mark.asyncio
async def test_compacting_session_default_no_callback_does_not_crash() -> None:
    """index_callback 預設 None,行為不變(向後相容)。"""
    fake = _FakeSession()
    await fake.add_items([{"role": "user", "content": "hi"}])
    cs = CompactingSession(
        underlying=fake,
        compactors=[MicroCompactor(retain_last_n=10)],
    )
    items = await cs.get_items()
    assert len(items) == 1


@pytest.mark.asyncio
async def test_compacting_session_callback_exception_swallowed() -> None:
    """callback 拋例外不應炸 compact loop。"""
    fake = _FakeSession()
    await fake.add_items([{"role": "user", "content": "hi"}])

    def _bad(_stats: CompactionStats) -> None:
        raise RuntimeError("boom")

    cs = CompactingSession(
        underlying=fake,
        compactors=[MicroCompactor(retain_last_n=10)],
        index_callback=_bad,
    )
    items = await cs.get_items()  # 不應炸
    assert len(items) == 1


@pytest.mark.asyncio
async def test_compacting_session_callback_wires_into_conversation_index() -> None:
    """用 ConversationIndex.record_compaction 當 callback,實際串起來。"""
    fake = _FakeSession()
    items: list[dict[str, Any]] = [
        {"type": "function_call", "call_id": "c1"},
        {"type": "function_call_output", "call_id": "c1", "output": "x"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    await fake.add_items(items)

    idx = ConversationIndex()
    # 模擬一個 turn 正在跑
    idx.start_turn(turn_id="t0", turn_index=0)

    def _callback(stats: CompactionStats) -> None:
        idx.record_compaction(
            compactor_name=stats.compactor_name,
            messages_dropped=stats.messages_dropped,
            before_tokens=stats.before_tokens,
            after_tokens=stats.after_tokens,
            at_turn_index=0,
        )

    cs = CompactingSession(
        underlying=fake,
        compactors=[MicroCompactor(retain_last_n=2)],
        index_callback=_callback,
    )
    await cs.get_items()

    assert idx.total_compactions == 1
    turn = idx.find_turn(0)
    assert turn is not None
    assert turn.compaction_count == 1
