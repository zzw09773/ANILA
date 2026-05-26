"""P1-6 CompactingSession 與三個 compactor 策略的單元測試。

覆蓋面:
- ``MicroCompactor``:保留最近 N 則、把舊的 tool 互動細節壓掉並留 placeholder。
- ``SnipCompactor``:對單一長 message 中段挖洞、留頭尾。
- ``LlmSummaryCompactor``:用 mock summarize_fn(sync + async 兩版)確認
  call shape 對、threshold 與 preserve 邊界正確。
- ``CompactingSession``:wrap 一個 in-memory fake Session,chain 多個 compactor。
- 與 ``Tracer``(P0-9)整合:每次 compact 開 span,attribute 對齊
  ``compactor.name`` / ``before_tokens`` / ``after_tokens`` / ``messages_dropped``。
"""

from __future__ import annotations

from typing import Any

import pytest

from anila_agent.memory.compaction import (
    CompactingSession,
    CompactorABC,
    LlmSummaryCompactor,
    Message,
    MicroCompactor,
    SnipCompactor,
    estimate_total_tokens,
)
from anila_agent.tracing import Span, Trace, Tracer

# ---------------------------------------------------------------------------
# 共用 helper
# ---------------------------------------------------------------------------


def _user(text: str) -> Message:
    return {"role": "user", "content": text}


def _assistant(text: str) -> Message:
    return {"role": "assistant", "content": text}


def _tool_call(call_id: str, name: str = "read") -> Message:
    return {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": "{}",
    }


def _tool_result(call_id: str, output: str) -> Message:
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": output,
    }


class _FakeSession:
    """測試用 in-memory Session — 對齊 openai-agents Session Protocol 4 個 method。"""

    def __init__(self, session_id: str = "test-session") -> None:
        self.session_id = session_id
        self._items: list[Message] = []

    async def get_items(self, limit: int | None = None) -> list[Message]:
        if limit is None:
            return list(self._items)
        return list(self._items[-limit:])

    async def add_items(self, items: list[Message]) -> None:
        self._items.extend(items)

    async def pop_item(self) -> Message | None:
        return self._items.pop() if self._items else None

    async def clear_session(self) -> None:
        self._items.clear()


class _RecordingProcessor:
    """tracing test processor — 收所有 span 事件供斷言用。"""

    def __init__(self) -> None:
        self.span_starts: list[Span] = []
        self.span_ends: list[Span] = []

    def on_trace_start(self, trace: Trace) -> None:
        del trace

    def on_trace_end(self, trace: Trace) -> None:
        del trace

    def on_span_start(self, span: Span) -> None:
        self.span_starts.append(span)

    def on_span_end(self, span: Span) -> None:
        self.span_ends.append(span)


# ---------------------------------------------------------------------------
# estimate_total_tokens
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_estimate_total_tokens_string_content() -> None:
    """單純 string content 應以 char/4 估算。"""
    msgs = [_user("a" * 400), _assistant("b" * 200)]
    # 400/4 + 200/4 = 150
    assert estimate_total_tokens(msgs) == 150


@pytest.mark.unit
def test_estimate_total_tokens_multipart_content() -> None:
    """multipart content(list of dict)應加總每個 text part。"""
    msg: Message = {
        "role": "user",
        "content": [
            {"type": "input_text", "text": "a" * 100},
            {"type": "input_text", "text": "b" * 100},
        ],
    }
    # (100 + 100) / 4 = 50
    assert estimate_total_tokens([msg]) == 50


# ---------------------------------------------------------------------------
# MicroCompactor
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_micro_compactor_keeps_recent_n_intact() -> None:
    """retain_last_n 範圍內 message 應原封不動回傳。"""
    msgs = [_user(f"q{i}") for i in range(3)]
    out = MicroCompactor(retain_last_n=10).compact(msgs)
    assert out == msgs
    # 不該回傳同一個 list ref(immutability principle)
    assert out is not msgs


@pytest.mark.unit
def test_micro_compactor_drops_old_tool_details_keeps_user_assistant() -> None:
    """舊的 tool_call / tool_result 應被砍掉,user / assistant 主軸保留。"""
    history: list[Message] = [
        _user("讀 readme"),
        _tool_call("c1"),
        _tool_result("c1", "x" * 2000),
        _assistant("讀完了"),
        _user("再讀一次"),
        _tool_call("c2"),
        _tool_result("c2", "y" * 2000),
        _assistant("OK"),
        # 最近 3 則:
        _user("第三輪"),
        _assistant("ack"),
        _user("第四輪"),
    ]
    out = MicroCompactor(retain_last_n=3).compact(history)

    # 結構:user, assistant, user, assistant + 1 placeholder + 最近 3 則
    user_msgs = [m for m in out if m.get("role") == "user"]
    assert [m["content"] for m in user_msgs] == [
        "讀 readme",
        "再讀一次",
        "第三輪",
        "第四輪",
    ]
    assistant_msgs = [m for m in out if m.get("role") == "assistant"]
    assert [m["content"] for m in assistant_msgs] == ["讀完了", "OK", "ack"]
    # tool_call / tool_result 應全部不在 output(都在舊範圍 + 都是 detail)
    assert not any(m.get("type") in {"function_call", "function_call_output"} for m in out)
    # 應出現一個壓縮 placeholder(4 個 tool detail 被收掉)
    placeholders = [
        m for m in out if m.get("role") == "system" and "已壓縮" in str(m.get("content", ""))
    ]
    assert len(placeholders) == 1
    assert "4" in placeholders[0]["content"]


@pytest.mark.unit
def test_micro_compactor_no_drop_when_no_tool_details() -> None:
    """舊範圍內全是 user/assistant 時不應產生 placeholder。"""
    history = [_user(f"q{i}") for i in range(5)] + [_assistant("done")]
    out = MicroCompactor(retain_last_n=2).compact(history)
    # 應與輸入等長(沒人被壓掉)
    assert len(out) == len(history)
    # 沒有 placeholder
    assert not any(m.get("role") == "system" and "已壓縮" in str(m.get("content", "")) for m in out)


@pytest.mark.unit
def test_micro_compactor_does_not_mutate_input() -> None:
    """compact 不應 mutate 輸入 list 或內部 message dict。"""
    history: list[Message] = [_tool_call("c1"), _tool_result("c1", "x"), _user("end")]
    snapshot = [dict(m) for m in history]
    MicroCompactor(retain_last_n=1).compact(history)
    assert history == snapshot


@pytest.mark.unit
def test_micro_compactor_invalid_n_rejected() -> None:
    """負數 retain_last_n 應 raise ValueError。"""
    with pytest.raises(ValueError):
        MicroCompactor(retain_last_n=-1)


# ---------------------------------------------------------------------------
# SnipCompactor
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_snip_compactor_snips_long_message_keeps_head_tail() -> None:
    """長 content 應被切中間,留頭尾各約一半。"""
    long_text = "HEAD" + ("x" * 10000) + "TAIL"
    msg = _user(long_text)
    out = SnipCompactor(max_chars_per_message=200).compact([msg])
    assert len(out) == 1
    content = out[0]["content"]
    assert isinstance(content, str)
    # 頭尾保留
    assert content.startswith("HEAD")
    assert content.endswith("TAIL")
    # 中間 marker
    assert "snipped" in content
    assert "chars" in content
    # 切後長度應遠小於原本
    assert len(content) < len(long_text)


@pytest.mark.unit
def test_snip_compactor_short_message_passthrough() -> None:
    """短於上限的 message 不應被改。"""
    msg = _user("short")
    out = SnipCompactor(max_chars_per_message=4000).compact([msg])
    assert out[0]["content"] == "short"
    # 是 copy 不是同物件
    assert out[0] is not msg


@pytest.mark.unit
def test_snip_compactor_multipart_content_untouched() -> None:
    """content 是 list (multipart) 時應不動。"""
    msg: Message = {
        "role": "user",
        "content": [{"type": "input_text", "text": "x" * 10000}],
    }
    out = SnipCompactor(max_chars_per_message=100).compact([msg])
    assert out[0]["content"] == msg["content"]


@pytest.mark.unit
def test_snip_compactor_invalid_max_rejected() -> None:
    """max_chars_per_message < 100 應 raise(留太少沒 context 意義)。"""
    with pytest.raises(ValueError):
        SnipCompactor(max_chars_per_message=50)


# ---------------------------------------------------------------------------
# LlmSummaryCompactor
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_llm_summary_compactor_below_threshold_no_call() -> None:
    """token 估計低於 threshold 時不應 call summarize_fn。"""
    calls: list[list[Message]] = []

    def fake_summarize(msgs: list[Message]) -> str:
        calls.append(msgs)
        return "should not be called"

    msgs = [_user("hi"), _assistant("hello")]  # 只有 ~3 token
    compactor = LlmSummaryCompactor(fake_summarize, threshold_tokens=1000)
    out = compactor.compact(msgs)

    assert calls == []
    assert out == msgs


@pytest.mark.unit
def test_llm_summary_compactor_above_threshold_summarizes_middle() -> None:
    """超過 threshold 時應 call summarize_fn 並把中段換成 system summary。"""
    captured: dict[str, Any] = {}

    def fake_summarize(msgs: list[Message]) -> str:
        captured["received"] = msgs
        return "MIDDLE_SUMMARY"

    # 10 則 message,每則 100 char = 25 token,總 ~250
    history: list[Message] = []
    for i in range(10):
        history.append(_user(f"q{i} " + "x" * 100))

    compactor = LlmSummaryCompactor(
        fake_summarize,
        threshold_tokens=100,  # 很低,確保觸發
        preserve_first_n=2,
        preserve_last_n=3,
    )
    out = compactor.compact(history)

    # 結構:前 2 + summary + 後 3 = 6
    assert len(out) == 6
    assert out[0] == history[0]
    assert out[1] == history[1]
    assert out[2] == {"role": "system", "content": "[對話摘要] MIDDLE_SUMMARY"}
    assert out[3:] == history[-3:]

    # summarize_fn 收到的是中段(index 2..7,共 5 則)
    assert "received" in captured
    assert len(captured["received"]) == 5
    assert captured["received"][0] == history[2]
    assert captured["received"][-1] == history[6]


@pytest.mark.unit
async def test_llm_summary_compactor_acompact_with_async_fn() -> None:
    """async summarize_fn 應透過 acompact 正常運作。"""

    async def async_summarize(msgs: list[Message]) -> str:
        return f"ASYNC_SUMMARY_{len(msgs)}"

    history = [_user("x" * 1000) for _ in range(10)]  # token 估 ~2500,觸發
    compactor = LlmSummaryCompactor(
        async_summarize,
        threshold_tokens=100,
        preserve_first_n=1,
        preserve_last_n=1,
    )
    out = await compactor.acompact(history)
    assert len(out) == 3  # head 1 + summary + tail 1
    assert out[1]["content"] == "[對話摘要] ASYNC_SUMMARY_8"


@pytest.mark.unit
def test_llm_summary_compactor_no_middle_to_summarize() -> None:
    """preserve 範圍涵蓋所有 message 時應不觸發。"""
    calls: list[list[Message]] = []

    def fake(msgs: list[Message]) -> str:
        calls.append(msgs)
        return "x"

    history = [_user("x" * 1000) for _ in range(4)]  # token ~1000
    compactor = LlmSummaryCompactor(
        fake,
        threshold_tokens=100,
        preserve_first_n=2,
        preserve_last_n=2,
    )
    out = compactor.compact(history)
    assert calls == []
    assert out == history


@pytest.mark.unit
def test_llm_summary_compactor_invalid_threshold_rejected() -> None:
    """threshold <= 0 應拒絕。"""
    with pytest.raises(ValueError):
        LlmSummaryCompactor(lambda m: "x", threshold_tokens=0)


# ---------------------------------------------------------------------------
# CompactingSession
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_compacting_session_chain_applies_in_order() -> None:
    """多個 compactor 應依序套用,後者吃前者 output。"""

    class _AppendMarker(CompactorABC):
        name = "marker"

        def __init__(self, marker: str) -> None:
            self.marker = marker

        def compact(self, messages: list[Message]) -> list[Message]:
            return [*messages, {"role": "system", "content": self.marker}]

    session = _FakeSession()
    await session.add_items([_user("hi")])
    wrapped = CompactingSession(
        underlying=session,
        compactors=[_AppendMarker("A"), _AppendMarker("B")],
    )
    items = await wrapped.get_items()
    # 順序:user(原始) → +A → +B
    assert items[0] == _user("hi")
    assert items[1]["content"] == "A"
    assert items[2]["content"] == "B"


@pytest.mark.unit
async def test_compacting_session_write_passthrough_to_underlying() -> None:
    """add_items 應 passthrough,raw 歷史完整存在底層。"""
    session = _FakeSession()
    wrapped = CompactingSession(
        underlying=session,
        compactors=[MicroCompactor(retain_last_n=1)],
    )
    history = [
        _user("q1"),
        _tool_call("c1"),
        _tool_result("c1", "raw_data_should_persist"),
        _assistant("done"),
    ]
    await wrapped.add_items(history)
    # 底層保留 raw(不被 compact 影響)
    raw = await session.get_items()
    assert len(raw) == 4
    assert raw[2]["output"] == "raw_data_should_persist"

    # 但讀取(get_items)會經 compact
    seen = await wrapped.get_items()
    # tool detail 被收掉 + 1 placeholder + 最後 1 則 assistant
    assert any(m.get("role") == "system" and "已壓縮" in str(m["content"]) for m in seen)


@pytest.mark.unit
async def test_compacting_session_passthrough_session_id_and_lifecycle() -> None:
    """session_id / pop_item / clear_session 都應正確透給底層。"""
    session = _FakeSession(session_id="abc")
    wrapped = CompactingSession(underlying=session)
    assert wrapped.session_id == "abc"

    await wrapped.add_items([_user("a"), _user("b")])
    popped = await wrapped.pop_item()
    assert popped == _user("b")

    await wrapped.clear_session()
    assert await session.get_items() == []
    assert wrapped.last_stats == []


@pytest.mark.unit
def test_compacting_session_rejects_unknown_strategy() -> None:
    """未知 strategy 應立刻 raise。"""
    with pytest.raises(ValueError):
        CompactingSession(underlying=_FakeSession(), strategy="bogus")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# tracing 整合(P0-9)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_compacting_session_emits_tracing_span_per_compactor() -> None:
    """每個 compactor 跑一次應產生對應 span,attribute 對齊規格。"""
    tracer = Tracer()
    rec = _RecordingProcessor()
    tracer.register_processor(rec)

    session = _FakeSession()
    # 注入 12 則 message,其中 4 個 tool detail 在「舊」範圍內 → 預期被壓掉。
    await session.add_items(
        [
            _user("q1"),
            _tool_call("c1"),
            _tool_result("c1", "x" * 500),
            _assistant("a1"),
            _user("q2"),
            _tool_call("c2"),
            _tool_result("c2", "y" * 500),
            _assistant("a2"),
            _user("q3"),
            _assistant("a3"),
            _user("q4"),
            _assistant("a4"),
        ]
    )
    wrapped = CompactingSession(
        underlying=session,
        compactors=[
            MicroCompactor(retain_last_n=4),
            SnipCompactor(max_chars_per_message=200),
        ],
        tracer=tracer,
    )

    with tracer.start_trace("memory.read"):
        await wrapped.get_items()

    # 兩個 compactor → 兩個 span
    span_names = [s.name for s in rec.span_ends]
    assert span_names == ["memory.compact.micro", "memory.compact.snip"]

    micro_span = rec.span_ends[0]
    # before_messages = 12,後續見 messages_dropped > 0(tool details 被收 + 多 1 placeholder)
    assert micro_span.attributes["compactor.name"] == "micro"
    assert micro_span.attributes["compactor.before_messages"] == 12
    assert micro_span.attributes["compactor.before_tokens"] > 0
    assert micro_span.attributes["compactor.after_tokens"] >= 0
    # tool details dropped (4 - placeholder added back) net = 3
    assert micro_span.attributes["compactor.messages_dropped"] >= 1
    assert micro_span.status == "ok"


@pytest.mark.unit
async def test_compacting_session_no_tracer_still_works() -> None:
    """無 tracer 時應正常運作,last_stats 仍記錄。"""
    session = _FakeSession()
    await session.add_items([_user("q"), _assistant("a")])
    wrapped = CompactingSession(
        underlying=session,
        compactors=[MicroCompactor(retain_last_n=10)],
        tracer=None,
    )
    await wrapped.get_items()
    assert len(wrapped.last_stats) == 1
    assert wrapped.last_stats[0].compactor_name == "micro"
    assert wrapped.last_stats[0].before_messages == 2


@pytest.mark.unit
async def test_compacting_session_clear_resets_stats() -> None:
    """clear_session 後 last_stats 應清空。"""
    session = _FakeSession()
    await session.add_items([_user("x")])
    wrapped = CompactingSession(
        underlying=session,
        compactors=[MicroCompactor(retain_last_n=1)],
    )
    await wrapped.get_items()
    assert wrapped.last_stats  # 有東西
    await wrapped.clear_session()
    assert wrapped.last_stats == []


# ---------------------------------------------------------------------------
# CompactionStats serialization
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_compaction_stats_to_attributes_keys() -> None:
    """``CompactionStats.to_attributes`` 鍵命名應對齊 OTel-ish ``compactor.*``。"""
    from anila_agent.memory.compaction import CompactionStats

    stats = CompactionStats(
        compactor_name="micro",
        before_messages=10,
        after_messages=4,
        before_tokens=500,
        after_tokens=100,
        messages_dropped=6,
    )
    attrs = stats.to_attributes()
    assert attrs == {
        "compactor.name": "micro",
        "compactor.before_messages": 10,
        "compactor.after_messages": 4,
        "compactor.before_tokens": 500,
        "compactor.after_tokens": 100,
        "compactor.messages_dropped": 6,
    }
