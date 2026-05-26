"""P1-1 forkSubagent byte-identical prefix 的 unit test。

涵蓋:

* :func:`build_subagent_prefix` deterministic — 同 input → 同 output(同 hash)。
* dict key 插入順序不影響 hash(``sort_keys=True``)。
* SHARE 策略:同 parent messages + 不同 directive → 同 prefix_hash(只有
  尾巴 instruction message 不一樣,prefix 仍 byte-identical)。
* FORK 策略:fork_point 截斷後 prefix bytes 仍與 parent 前 K 條 deterministic;
  改 fork_point → prefix_hash 改變。
* SHARE 與 FORK 在相同 parent + 相同 directive 下,因納入 prefix 長度不同 →
  hash 不同。
* :func:`compute_prefix_hash` 與 :func:`build_subagent_prefix` 對相同 prefix
  訊息算出來的 hash 一致。
* :func:`is_in_fork_child` 對 str / list[dict] 兩種 content 形式都能偵測到
  boilerplate tag。
* fork directive 是空字串 / 非 share/fork 策略 → raise ValueError。
* :class:`AgentTool` dispatch 把 ``prompt_cache.*`` attribute 寫進 trace span。
* 兩次 dispatch 同 parent context + 同 strategy + 同 directive → trace 上
  ``prompt_cache.prefix_hash`` 一致(verify cache slot 共用)。
* FORK 兩次 dispatch 不同 directive → ``prompt_cache.prefix_hash`` 仍相同
  (directive 不入 prefix);但 fork_point 不同 → hash 變。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
from agents import Agent
from agents.tool_context import ToolContext

from anila_agent.core import (
    AnilaToolContext,
    SubagentPrefix,
    build_fork_directive_message,
    build_share_directive_message,
    build_subagent_prefix,
    compute_prefix_hash,
    is_in_fork_child,
    make_agent_tool,
)
from anila_agent.core.prompt_cache import (
    DEFAULT_PREFIX_MESSAGE_COUNT,
    FORK_BOILERPLATE_TAG,
    FORK_DIRECTIVE_PREFIX,
    SHARE_INSTRUCTION_PREFIX,
)
from anila_agent.tracing import Span, Trace, Tracer

# ---------------------------------------------------------------------------
# 測試輔助
# ---------------------------------------------------------------------------


def _sample_messages() -> list[dict[str, Any]]:
    """產一份代表性 parent message stack(system + tool desc + user + assistant)。

    這四條對應 vLLM 上常見的 prefix-cacheable 區段(system prompt + tool
    description 通常在前 1-2 條)。
    """
    return [
        {"role": "system", "content": "You are ANILA platform's orchestrator."},
        {
            "role": "system",
            "content": "Tools: read_file, write_file, search_documents.",
        },
        {"role": "user", "content": "請幫我查 ANILA 的部署架構。"},
        {
            "role": "assistant",
            "content": "好的,我先讀部署文件。",
        },
    ]


def _make_sub_agent(name: str = "researcher") -> Agent[Any]:
    return Agent(name=name, instructions="Be concise.")


def _make_parent_context_with_messages(
    messages: list[dict[str, Any]] | None,
) -> AnilaToolContext:
    """造一份 parent context,把 messages 塞進 metadata['parent_messages']。"""
    metadata: dict[str, Any] = {}
    if messages is not None:
        metadata["parent_messages"] = messages
    return AnilaToolContext(
        session_id="sess-1",
        turn_id=2,
        tool_call_id="call-parent",
        agent_name="orchestrator",
        metadata=metadata,
    )


def _make_tool_context(parent_anila_ctx: AnilaToolContext | None) -> ToolContext[Any]:
    return ToolContext(
        context=parent_anila_ctx,
        usage=None,  # type: ignore[arg-type]
        tool_name="call_researcher",
        tool_call_id="call-parent",
        tool_arguments="",
    )


class _CapturingProcessor:
    """蒐集所有 span end 事件供 assertion 用。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, Span | Trace]] = []

    def on_trace_start(self, trace: Trace) -> None:
        self.events.append(("trace.start", trace))

    def on_trace_end(self, trace: Trace) -> None:
        self.events.append(("trace.end", trace))

    def on_span_start(self, span: Span) -> None:
        self.events.append(("span.start", span))

    def on_span_end(self, span: Span) -> None:
        self.events.append(("span.end", span))

    def dispatch_spans(self) -> list[Span]:
        return [
            obj
            for name, obj in self.events
            if name == "span.end"
            and isinstance(obj, Span)
            and obj.name.startswith("agent_tool.dispatch")
        ]


# ---------------------------------------------------------------------------
# build_subagent_prefix — deterministic / key order independence
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_subagent_prefix_is_deterministic_for_same_input() -> None:
    """同樣 parent_messages + 同 strategy + 同 directive → 同 prefix_hash + bytes。"""
    msgs = _sample_messages()

    p1 = build_subagent_prefix(msgs, strategy="share", directive="task A")
    p2 = build_subagent_prefix(msgs, strategy="share", directive="task A")

    assert isinstance(p1, SubagentPrefix)
    assert p1.prefix_hash == p2.prefix_hash
    assert p1.prefix_bytes == p2.prefix_bytes
    assert p1.messages == p2.messages


@pytest.mark.unit
def test_build_subagent_prefix_ignores_dict_key_order() -> None:
    """同樣 message 但 dict key 寫入順序不同 → 仍同 hash(sort_keys=True 保證)。"""
    base = _sample_messages()
    # 把 base 內所有 message 的 key 反序重建,模擬不同呼叫端塞入順序
    reordered = [{k: m[k] for k in sorted(m.keys(), reverse=True)} for m in base]

    h1 = compute_prefix_hash(base)
    h2 = compute_prefix_hash(reordered)
    assert h1 == h2


@pytest.mark.unit
def test_compute_prefix_hash_matches_build_subagent_prefix_share() -> None:
    """SHARE 策略下,prefix 涵蓋全部 parent_messages → 與 compute_prefix_hash 一致。"""
    msgs = _sample_messages()
    prefix = build_subagent_prefix(msgs, strategy="share", directive="do x")
    standalone_hash = compute_prefix_hash(msgs)
    assert prefix.prefix_hash == standalone_hash
    assert prefix.prefix_message_count == len(msgs)


@pytest.mark.unit
def test_compute_prefix_hash_matches_build_subagent_prefix_fork() -> None:
    """FORK 策略下,prefix 只涵蓋前 fork_point 條 → 對應 compute_prefix_hash。"""
    msgs = _sample_messages()
    fork_point = 2
    prefix = build_subagent_prefix(
        msgs, strategy="fork", directive="do x", fork_point=fork_point
    )
    standalone_hash = compute_prefix_hash(msgs[:fork_point])
    assert prefix.prefix_hash == standalone_hash
    assert prefix.prefix_message_count == fork_point


# ---------------------------------------------------------------------------
# SHARE vs FORK — hash 差異性
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_share_strategy_same_prefix_hash_across_different_directives() -> None:
    """SHARE:同 parent + 不同 directive → prefix_hash 不變(directive 不入 prefix)。

    驗 cache 友善性:這正是 vLLM 上「同 parent → 同 cache slot」的關鍵 invariant。
    """
    msgs = _sample_messages()
    p1 = build_subagent_prefix(msgs, strategy="share", directive="task A")
    p2 = build_subagent_prefix(msgs, strategy="share", directive="task B different")

    assert p1.prefix_hash == p2.prefix_hash
    # 但完整 messages 應該不同(尾巴 sub instruction 不一樣)
    assert p1.messages != p2.messages
    # 且 directive 字串應該真的不同
    assert p1.fork_directive != p2.fork_directive


@pytest.mark.unit
def test_fork_strategy_same_prefix_hash_across_different_directives() -> None:
    """FORK:同 parent + 同 fork_point + 不同 directive → prefix_hash 不變。

    fork directive 本身不進 prefix(它被組成尾巴的 sub instruction message),
    所以多個 fork child 共用同 cache slot。
    """
    msgs = _sample_messages()
    p1 = build_subagent_prefix(msgs, strategy="fork", directive="fork A", fork_point=2)
    p2 = build_subagent_prefix(
        msgs, strategy="fork", directive="fork B different", fork_point=2
    )

    assert p1.prefix_hash == p2.prefix_hash
    assert p1.messages[:-1] == p2.messages[:-1]  # 前綴一樣
    assert p1.messages[-1] != p2.messages[-1]  # 尾巴 directive 不同


@pytest.mark.unit
def test_fork_strategy_changing_fork_point_changes_hash() -> None:
    """FORK:fork_point 移動 → prefix 內容變 → hash 變。"""
    msgs = _sample_messages()
    p1 = build_subagent_prefix(msgs, strategy="fork", directive="x", fork_point=1)
    p2 = build_subagent_prefix(msgs, strategy="fork", directive="x", fork_point=3)
    assert p1.prefix_hash != p2.prefix_hash
    assert p1.prefix_message_count == 1
    assert p2.prefix_message_count == 3


@pytest.mark.unit
def test_share_and_fork_have_different_hashes_for_same_parent() -> None:
    """SHARE 納入全部 parent vs FORK 只納入前 K 條 → hash 不同。"""
    msgs = _sample_messages()
    share = build_subagent_prefix(msgs, strategy="share", directive="x")
    fork = build_subagent_prefix(msgs, strategy="fork", directive="x", fork_point=2)
    assert share.prefix_hash != fork.prefix_hash
    assert share.prefix_message_count == len(msgs)
    assert fork.prefix_message_count == 2


@pytest.mark.unit
def test_fork_point_defaults_to_constant() -> None:
    """FORK 未指定 fork_point → 用 DEFAULT_PREFIX_MESSAGE_COUNT。"""
    msgs = _sample_messages()
    p = build_subagent_prefix(msgs, strategy="fork", directive="x")
    assert p.prefix_message_count == DEFAULT_PREFIX_MESSAGE_COUNT


@pytest.mark.unit
def test_fork_point_clamps_to_parent_length() -> None:
    """fork_point 超過 parent 長度 → clamp 到 parent 長度,不爆 IndexError。"""
    msgs = _sample_messages()
    p = build_subagent_prefix(msgs, strategy="fork", directive="x", fork_point=999)
    assert p.prefix_message_count == len(msgs)


# ---------------------------------------------------------------------------
# Sub instruction message — fork boilerplate / share lightweight
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_fork_directive_message_contains_boilerplate_tag_and_directive() -> None:
    """fork directive 必須包含 boilerplate XML tag 與 Directive: 前綴。"""
    msg = build_fork_directive_message("refactor module X")
    assert f"<{FORK_BOILERPLATE_TAG}>" in msg
    assert f"</{FORK_BOILERPLATE_TAG}>" in msg
    assert f"{FORK_DIRECTIVE_PREFIX}refactor module X" in msg


@pytest.mark.unit
def test_share_directive_message_uses_lightweight_prefix() -> None:
    """share directive 只是輕量提示;不含 fork boilerplate(避免誤判遞迴)。"""
    msg = build_share_directive_message("continue research")
    assert SHARE_INSTRUCTION_PREFIX in msg
    assert "continue research" in msg
    assert f"<{FORK_BOILERPLATE_TAG}>" not in msg


@pytest.mark.unit
def test_is_in_fork_child_detects_str_content() -> None:
    """content 為 str 的 user message 含 boilerplate tag → True。"""
    msgs = [
        {"role": "user", "content": f"hello <{FORK_BOILERPLATE_TAG}> nested ..."},
    ]
    assert is_in_fork_child(msgs) is True


@pytest.mark.unit
def test_is_in_fork_child_detects_list_content() -> None:
    """content 為 list[dict] 的 user message 含 boilerplate tag → True。"""
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"前綴 <{FORK_BOILERPLATE_TAG}> 後綴"},
            ],
        },
    ]
    assert is_in_fork_child(msgs) is True


@pytest.mark.unit
def test_is_in_fork_child_returns_false_for_clean_messages() -> None:
    """無 boilerplate tag → False。"""
    assert is_in_fork_child(_sample_messages()) is False


@pytest.mark.unit
def test_is_in_fork_child_ignores_assistant_messages_with_tag() -> None:
    """assistant message 內含 tag 不算 fork child(只看 user)。"""
    msgs = [
        {"role": "assistant", "content": f"<{FORK_BOILERPLATE_TAG}>"},
    ]
    assert is_in_fork_child(msgs) is False


# ---------------------------------------------------------------------------
# 例外路徑
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_subagent_prefix_rejects_unknown_strategy() -> None:
    with pytest.raises(ValueError, match="unknown prefix strategy"):
        build_subagent_prefix(
            _sample_messages(),
            strategy="bogus",  # type: ignore[arg-type]
            directive="x",
        )


@pytest.mark.unit
def test_build_subagent_prefix_rejects_empty_directive() -> None:
    with pytest.raises(ValueError, match="directive"):
        build_subagent_prefix(_sample_messages(), strategy="share", directive="   ")


@pytest.mark.unit
def test_build_subagent_prefix_rejects_negative_fork_point() -> None:
    with pytest.raises(ValueError, match="fork_point"):
        build_subagent_prefix(
            _sample_messages(),
            strategy="fork",
            directive="x",
            fork_point=-1,
        )


@pytest.mark.unit
def test_build_subagent_prefix_empty_parent_messages_still_hashes() -> None:
    """parent_messages 為空 → 仍能算出 deterministic hash(代表 baseline slot)。"""
    p1 = build_subagent_prefix([], strategy="share", directive="x")
    p2 = build_subagent_prefix([], strategy="share", directive="y")
    assert p1.prefix_hash == p2.prefix_hash
    assert p1.prefix_message_count == 0


# ---------------------------------------------------------------------------
# AgentTool dispatch — span attribute 接 prompt_cache.* 進來
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dispatch_writes_prompt_cache_attributes_to_span() -> None:
    """dispatch 後 span attribute 必含 ``prompt_cache.*`` 四個欄位。"""
    sub = _make_sub_agent(name="echoer")
    tracer = Tracer()
    cap = _CapturingProcessor()
    tracer.register_processor(cap)

    spec = make_agent_tool(sub, runner=AsyncMock(return_value="ok"), tracer=tracer)
    parent_ctx = _make_parent_context_with_messages(_sample_messages())

    with tracer.start_trace("t"):
        asyncio.run(
            spec.tool.on_invoke_tool(
                _make_tool_context(parent_ctx),
                json.dumps({"prompt": "do x", "context_summary": ""}),
            )
        )

    spans = cap.dispatch_spans()
    assert len(spans) == 1
    attrs = spans[0].attributes
    assert attrs["prompt_cache.strategy"] == "share"
    assert isinstance(attrs["prompt_cache.prefix_hash"], str)
    assert len(attrs["prompt_cache.prefix_hash"]) == 64  # SHA-256 hex
    assert attrs["prompt_cache.prefix_messages"] == len(_sample_messages())
    assert attrs["prompt_cache.prefix_bytes"] > 0


@pytest.mark.unit
def test_dispatch_share_strategy_two_calls_same_prefix_hash() -> None:
    """SHARE:同 parent context + 同 strategy → 兩次 dispatch 共享同 prefix_hash。

    這是 prompt-cache 命中的核心 invariant — 同 hash 即代表 vLLM cache slot
    同一個 → 第二次 dispatch 從 KV cache 接 forward。
    """
    sub = _make_sub_agent(name="worker")
    tracer = Tracer()
    cap = _CapturingProcessor()
    tracer.register_processor(cap)

    spec = make_agent_tool(
        sub, runner=AsyncMock(return_value="ok"), tracer=tracer, prefix_strategy="share"
    )
    msgs = _sample_messages()

    with tracer.start_trace("t"):
        asyncio.run(
            spec.tool.on_invoke_tool(
                _make_tool_context(_make_parent_context_with_messages(msgs)),
                json.dumps({"prompt": "task A", "context_summary": ""}),
            )
        )
        asyncio.run(
            spec.tool.on_invoke_tool(
                _make_tool_context(_make_parent_context_with_messages(msgs)),
                json.dumps({"prompt": "task B 完全不同的內容", "context_summary": ""}),
            )
        )

    spans = cap.dispatch_spans()
    assert len(spans) == 2
    assert spans[0].attributes["prompt_cache.prefix_hash"] == spans[1].attributes["prompt_cache.prefix_hash"]


@pytest.mark.unit
def test_dispatch_fork_strategy_directive_change_keeps_prefix_hash() -> None:
    """FORK:同 parent context + 同 fork_point + 不同 directive → prefix_hash 同。"""
    sub = _make_sub_agent(name="worker")
    tracer = Tracer()
    cap = _CapturingProcessor()
    tracer.register_processor(cap)

    spec = make_agent_tool(
        sub,
        runner=AsyncMock(return_value="ok"),
        tracer=tracer,
        prefix_strategy="fork",
        fork_point=2,
    )
    msgs = _sample_messages()

    with tracer.start_trace("t"):
        asyncio.run(
            spec.tool.on_invoke_tool(
                _make_tool_context(_make_parent_context_with_messages(msgs)),
                json.dumps({"prompt": "fork directive 1", "context_summary": ""}),
            )
        )
        asyncio.run(
            spec.tool.on_invoke_tool(
                _make_tool_context(_make_parent_context_with_messages(msgs)),
                json.dumps({"prompt": "fork directive 2 截然不同", "context_summary": ""}),
            )
        )

    spans = cap.dispatch_spans()
    assert spans[0].attributes["prompt_cache.prefix_hash"] == spans[1].attributes["prompt_cache.prefix_hash"]
    assert spans[0].attributes["prompt_cache.strategy"] == "fork"
    assert spans[0].attributes["prompt_cache.prefix_messages"] == 2


@pytest.mark.unit
def test_dispatch_fork_vs_share_have_different_prefix_hashes() -> None:
    """同 parent context 下,strategy=share vs strategy=fork → prefix_hash 不同。"""
    msgs = _sample_messages()
    sub = _make_sub_agent(name="worker")
    tracer = Tracer()
    cap = _CapturingProcessor()
    tracer.register_processor(cap)

    spec_share = make_agent_tool(
        sub, runner=AsyncMock(return_value="ok"), tracer=tracer, prefix_strategy="share"
    )
    spec_fork = make_agent_tool(
        sub,
        runner=AsyncMock(return_value="ok"),
        tracer=tracer,
        prefix_strategy="fork",
        fork_point=2,
    )

    with tracer.start_trace("t"):
        asyncio.run(
            spec_share.tool.on_invoke_tool(
                _make_tool_context(_make_parent_context_with_messages(msgs)),
                json.dumps({"prompt": "x", "context_summary": ""}),
            )
        )
        asyncio.run(
            spec_fork.tool.on_invoke_tool(
                _make_tool_context(_make_parent_context_with_messages(msgs)),
                json.dumps({"prompt": "x", "context_summary": ""}),
            )
        )

    spans = cap.dispatch_spans()
    assert spans[0].attributes["prompt_cache.prefix_hash"] != spans[1].attributes["prompt_cache.prefix_hash"]


@pytest.mark.unit
def test_dispatch_without_parent_messages_still_writes_cache_attrs() -> None:
    """parent context 沒帶 parent_messages → 用空 list,仍寫出有效 cache 屬性。"""
    sub = _make_sub_agent(name="worker")
    tracer = Tracer()
    cap = _CapturingProcessor()
    tracer.register_processor(cap)

    spec = make_agent_tool(sub, runner=AsyncMock(return_value="ok"), tracer=tracer)
    parent_ctx = _make_parent_context_with_messages(None)  # 沒帶 parent_messages

    with tracer.start_trace("t"):
        asyncio.run(
            spec.tool.on_invoke_tool(
                _make_tool_context(parent_ctx),
                json.dumps({"prompt": "x", "context_summary": ""}),
            )
        )

    spans = cap.dispatch_spans()
    assert len(spans) == 1
    attrs = spans[0].attributes
    assert attrs["prompt_cache.prefix_messages"] == 0
    assert isinstance(attrs["prompt_cache.prefix_hash"], str)


@pytest.mark.unit
def test_make_agent_tool_stores_fork_point_in_spec() -> None:
    """``make_agent_tool(fork_point=K)`` → spec.fork_point == K 並可從 dispatch 取用。"""
    sub = _make_sub_agent()
    spec = make_agent_tool(sub, prefix_strategy="fork", fork_point=3)
    assert spec.fork_point == 3
    assert spec.prefix_strategy == "fork"
