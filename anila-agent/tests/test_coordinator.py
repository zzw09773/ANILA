"""P1-13 coordinatorMode XML notification 機制的 unit / integration test。

涵蓋:

* :class:`CoordinatorMessage` to_xml / from_xml roundtrip,attribute 與空 body
  邊界都過。
* :func:`parse_coordinator_messages` 抽多段 top-level tag、處理 nested 同名
  tag、attribute parsing、malformed open tag(無 close)只 warn 不 raise。
* :func:`format_coordinator_messages` 多訊息渲染為 ``\\n``-分隔字串、空 list
  → 空字串、結尾 newline。
* :class:`CoordinatorNotification` enum 成員可被 ``in`` 比對。
* integration:用 :func:`make_agent_tool` mock runner,parent 把 ``<task>`` XML
  附在 prompt 後傳給 sub-agent,sub-agent 把 ``<summary>`` 回給 parent → parent
  用 :func:`parse_coordinator_messages` 抽出 summary。
"""

from __future__ import annotations

import json
import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest
from agents import Agent
from agents.tool_context import ToolContext

from anila_agent.core import (
    AnilaToolContext,
    CoordinatorMessage,
    CoordinatorNotification,
    format_coordinator_messages,
    make_agent_tool,
    parse_coordinator_messages,
)


# ---------------------------------------------------------------------------
# CoordinatorMessage.to_xml / from_xml roundtrip
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_coordinator_message_to_xml_basic() -> None:
    """to_xml 應渲染 ``<tag>body</tag>``,attribute 順序依 dict insertion order。"""
    msg = CoordinatorMessage(
        tag="task",
        body="Investigate auth bug",
        attributes={"id": "task_1", "priority": "high"},
    )
    xml = msg.to_xml()
    assert xml == '<task id="task_1" priority="high">Investigate auth bug</task>'


@pytest.mark.unit
def test_coordinator_message_to_xml_empty_body() -> None:
    """空 body 仍會 emit 完整 open + close tag(非 short empty form)。"""
    msg = CoordinatorMessage(tag="status", attributes={"value": "running"})
    assert msg.to_xml() == '<status value="running"></status>'


@pytest.mark.unit
def test_coordinator_message_to_xml_escapes_special_chars() -> None:
    """body 含 ``<`` 應被 escape 為 ``&lt;``;讓 from_xml 反解仍能拿到原值。"""
    msg = CoordinatorMessage(tag="error", body="x < y")
    assert "&lt;" in msg.to_xml()


@pytest.mark.unit
def test_coordinator_message_from_xml_basic() -> None:
    """from_xml 應抽出 tag / body / attributes。"""
    parsed = CoordinatorMessage.from_xml('<task id="t1">do thing</task>')
    assert parsed.tag == "task"
    assert parsed.body == "do thing"
    assert parsed.attributes == {"id": "t1"}


@pytest.mark.unit
def test_coordinator_message_roundtrip_simple() -> None:
    """to_xml → from_xml 應拿回 equal 物件(tag / body / attributes 三項都對)。"""
    original = CoordinatorMessage(
        tag="summary",
        body="agent completed",
        attributes={"task-id": "agent-a1b", "status": "completed"},
    )
    roundtripped = CoordinatorMessage.from_xml(original.to_xml())
    assert roundtripped == original


@pytest.mark.unit
def test_coordinator_message_roundtrip_with_escaped_body() -> None:
    """body 含特殊字元 → escape → unescape 後內容一致。"""
    original = CoordinatorMessage(tag="error", body="x < y & z > w")
    roundtripped = CoordinatorMessage.from_xml(original.to_xml())
    assert roundtripped.body == "x < y & z > w"


@pytest.mark.unit
def test_coordinator_message_from_xml_malformed_raises() -> None:
    """from_xml 對 malformed input 應 raise ValueError(單一物件解析走嚴格路徑)。"""
    with pytest.raises(ValueError):
        CoordinatorMessage.from_xml("<task>no close")


@pytest.mark.unit
def test_coordinator_message_invalid_tag_raises() -> None:
    """tag 包含空白 / 特殊字元應 raise ValueError(在 __post_init__ 擋掉)。"""
    with pytest.raises(ValueError):
        CoordinatorMessage(tag="bad tag")
    with pytest.raises(ValueError):
        CoordinatorMessage(tag="")


# ---------------------------------------------------------------------------
# parse_coordinator_messages — multiple tags / nested / attributes / malformed
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_parse_multiple_top_level_messages() -> None:
    """text 內含多段 ``<task>`` / ``<summary>``,應全部抽出且順序保留。"""
    text = (
        "Coordinator note:\n"
        '<task id="task_1">Investigate auth bug</task>\n'
        "Some chitchat.\n"
        '<summary task-id="task_1">Found null pointer in src/auth/validate.ts:42</summary>\n'
    )
    msgs = parse_coordinator_messages(text)

    assert len(msgs) == 2
    assert msgs[0].tag == "task"
    assert msgs[0].attributes == {"id": "task_1"}
    assert msgs[0].body == "Investigate auth bug"
    assert msgs[1].tag == "summary"
    assert msgs[1].attributes == {"task-id": "task_1"}
    assert "null pointer" in msgs[1].body


@pytest.mark.unit
def test_parse_nested_tag_keeps_inner_xml_in_body() -> None:
    """``<task><summary>...</summary></task>`` parse 應拿到外層 ``task``,
    body 保留 inner XML 原文,呼叫端可再 parse 一層。"""
    text = (
        '<task id="task_1">'
        '<summary status="completed">agent done</summary>'
        '<result>Found bug at line 42</result>'
        '</task>'
    )
    msgs = parse_coordinator_messages(text)
    assert len(msgs) == 1
    assert msgs[0].tag == "task"
    assert msgs[0].attributes == {"id": "task_1"}
    # 外層 body 包含 inner XML 原始字串
    assert "<summary" in msgs[0].body
    assert "<result>" in msgs[0].body

    # 對 outer body 再 parse 一次應拿到兩個子 message
    inner = parse_coordinator_messages(msgs[0].body)
    assert len(inner) == 2
    assert inner[0].tag == "summary"
    assert inner[0].body == "agent done"
    assert inner[1].tag == "result"


@pytest.mark.unit
def test_parse_nested_same_name_tag_matches_outermost() -> None:
    """``<task>...<task>inner</task>...</task>`` 應只回一條 top-level ``task``,
    inner ``<task>`` 留在 body 內(depth 計算正確)。"""
    text = "<task>outer-pre<task>inner</task>outer-post</task>"
    msgs = parse_coordinator_messages(text)
    assert len(msgs) == 1
    assert msgs[0].tag == "task"
    assert msgs[0].body == "outer-pre<task>inner</task>outer-post"


@pytest.mark.unit
def test_parse_attributes_quoted() -> None:
    """attribute 用單引號或雙引號 ElementTree 都能解(雙引號是 canonical 寫法)。"""
    text = '<task id="t1" priority="high">x</task>'
    msgs = parse_coordinator_messages(text)
    assert msgs[0].attributes == {"id": "t1", "priority": "high"}


@pytest.mark.unit
def test_parse_malformed_open_tag_logs_warning_and_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``<task>`` 沒 close、後面跟著合法 ``<summary>``:應抽出 summary,task 略過、log warn。"""
    text = (
        "<task>dangling open without close\n"
        "<summary>this one is fine</summary>"
    )
    with caplog.at_level(logging.WARNING, logger="anila_agent.core.coordinator"):
        msgs = parse_coordinator_messages(text)

    assert len(msgs) == 1
    assert msgs[0].tag == "summary"
    assert msgs[0].body == "this one is fine"
    # 應有對 dangling task 的 warning
    assert any("dangling" in rec.message or "task" in rec.message for rec in caplog.records)


@pytest.mark.unit
def test_parse_empty_text_returns_empty_list() -> None:
    """空字串 → 空 list,不 raise。"""
    assert parse_coordinator_messages("") == []


@pytest.mark.unit
def test_parse_no_xml_text_returns_empty_list() -> None:
    """純自然語言、無 XML → 空 list。"""
    assert parse_coordinator_messages("Just some normal LLM reply, no tags.") == []


@pytest.mark.unit
def test_parse_ignores_non_letter_tag_starts() -> None:
    """``<3`` / ``< `` 這種不是合法 tag 起點的不應被誤識別為 open tag。"""
    text = "score is <3 happy. <summary>ok</summary>"
    msgs = parse_coordinator_messages(text)
    assert len(msgs) == 1
    assert msgs[0].tag == "summary"


# ---------------------------------------------------------------------------
# format_coordinator_messages
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_format_empty_list_returns_empty_string() -> None:
    """空 list → 空字串(不要加雜訊 newline)。"""
    assert format_coordinator_messages([]) == ""


@pytest.mark.unit
def test_format_single_message_ends_with_newline() -> None:
    """單一訊息結尾應有 ``\\n``,方便後續 append。"""
    msg = CoordinatorMessage(tag="task", body="x")
    out = format_coordinator_messages([msg])
    assert out.endswith("\n")
    assert out == "<task>x</task>\n"


@pytest.mark.unit
def test_format_multiple_messages_joined_by_newline() -> None:
    """多訊息以 ``\\n`` 分隔,結尾 ``\\n`` 收尾。"""
    msgs = [
        CoordinatorMessage(tag="task", body="t1", attributes={"id": "1"}),
        CoordinatorMessage(tag="summary", body="done"),
    ]
    out = format_coordinator_messages(msgs)
    assert out == '<task id="1">t1</task>\n<summary>done</summary>\n'


# ---------------------------------------------------------------------------
# CoordinatorNotification enum
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_coordinator_notification_str_value() -> None:
    """enum 成員直接是 tag string,做為 :class:`CoordinatorMessage.tag` 可用。"""
    msg = CoordinatorMessage(
        tag=CoordinatorNotification.TASK.value,
        body="x",
    )
    assert msg.tag == "task"


@pytest.mark.unit
def test_coordinator_notification_reverse_lookup() -> None:
    """``CoordinatorNotification("task")`` 應反查出 TASK 成員。"""
    assert CoordinatorNotification("task") is CoordinatorNotification.TASK
    assert CoordinatorNotification("plan_check") is CoordinatorNotification.PLAN_CHECK


# ---------------------------------------------------------------------------
# Integration — sub-agent dispatch 跟 XML notification 串接
# ---------------------------------------------------------------------------


def _make_sub_agent(name: str = "researcher") -> Agent[Any]:
    """最小 sub-agent,只給 dispatch test 用。"""
    return Agent(name=name, instructions="answer briefly")


def _make_parent_context() -> AnilaToolContext:
    """parent AnilaToolContext。"""
    return AnilaToolContext(
        session_id="sess-parent",
        turn_id=1,
        tool_call_id="call-parent",
        agent_name="orchestrator",
    )


def _make_tool_context(parent_ctx: AnilaToolContext) -> ToolContext[Any]:
    """組 openai-agents ToolContext。"""
    return ToolContext(
        context=parent_ctx,
        usage=None,  # type: ignore[arg-type]
        tool_name="call_researcher",
        tool_call_id="call-parent",
        tool_arguments="",
    )


@pytest.mark.unit
def test_integration_sub_agent_receives_task_xml_in_prompt() -> None:
    """parent 把 ``<task>`` XML 附加在 prompt 後面 → mock runner 收到時 prompt 內含該 XML。"""
    sub = _make_sub_agent()
    captured: dict[str, Any] = {}

    async def _capturing_runner(sub_agent: Agent[Any], prompt: str) -> Any:
        captured["prompt"] = prompt
        return "ok"

    spec = make_agent_tool(sub, runner=_capturing_runner)

    # parent 想塞 <task> notification 給 sub-agent。把 XML append 到 prompt 後面。
    task_msg = CoordinatorMessage(
        tag=CoordinatorNotification.TASK.value,
        body="Investigate auth bug",
        attributes={"id": "task_1"},
    )
    composed_prompt = (
        "Research the recent auth changes.\n\n"
        + format_coordinator_messages([task_msg])
    )

    tool_input = json.dumps({"prompt": composed_prompt})
    parent_ctx = _make_parent_context()
    tool_ctx = _make_tool_context(parent_ctx)

    import asyncio

    result = asyncio.run(spec.tool.on_invoke_tool(tool_ctx, tool_input))

    assert result == "ok"
    # sub-agent 看到的 prompt 內必須含 <task> XML 段
    assert "<task" in captured["prompt"]
    assert 'id="task_1"' in captured["prompt"]
    assert "Investigate auth bug" in captured["prompt"]


@pytest.mark.unit
def test_integration_parent_parses_summary_xml_from_sub_agent_output() -> None:
    """sub-agent 回應內含 ``<summary>`` → parent 用 parse_coordinator_messages 抽得到。"""
    sub = _make_sub_agent(name="impl_worker")

    async def _summary_emitting_runner(sub_agent: Agent[Any], prompt: str) -> Any:
        return (
            "I'm done.\n"
            '<summary task-id="task_1" status="completed">'
            "Patched validate.ts line 42 with null check"
            "</summary>"
        )

    spec = make_agent_tool(sub, runner=_summary_emitting_runner)

    tool_input = json.dumps({"prompt": "Implement the fix"})
    parent_ctx = _make_parent_context()
    tool_ctx = _make_tool_context(parent_ctx)

    import asyncio

    sub_output = asyncio.run(spec.tool.on_invoke_tool(tool_ctx, tool_input))

    # parent 端 parse sub-agent 回應
    msgs = parse_coordinator_messages(sub_output)
    assert len(msgs) == 1
    assert msgs[0].tag == "summary"
    assert msgs[0].attributes == {"task-id": "task_1", "status": "completed"}
    assert "validate.ts" in msgs[0].body


@pytest.mark.unit
def test_integration_task_notification_full_shape() -> None:
    """模擬上游 ``<task-notification>`` 完整形狀(含 nested ``<usage>``)— 兩階段 parse 應能取到所有欄位。"""
    raw = (
        '<task-notification>'
        '<task-id>agent-a1b</task-id>'
        '<status>completed</status>'
        '<summary>Agent "Investigate auth bug" completed</summary>'
        '<result>Found null pointer in src/auth/validate.ts:42</result>'
        '<usage>'
        '<total_tokens>1234</total_tokens>'
        '<tool_uses>5</tool_uses>'
        '</usage>'
        '</task-notification>'
    )
    outer = parse_coordinator_messages(raw)
    assert len(outer) == 1
    assert outer[0].tag == "task-notification"

    inner = parse_coordinator_messages(outer[0].body)
    tags = [m.tag for m in inner]
    assert tags == ["task-id", "status", "summary", "result", "usage"]

    usage_msg = inner[4]
    usage_inner = parse_coordinator_messages(usage_msg.body)
    assert [m.tag for m in usage_inner] == ["total_tokens", "tool_uses"]
    assert usage_inner[0].body == "1234"
    assert usage_inner[1].body == "5"
