"""P2-13 — visualization tests:DOT / ASCII / cycle / output_path / no-graphviz fallback。"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from agents import Agent
from agents.handoffs import Handoff

from anila_agent.cli.draw_graph_cli import build_stub_agent
from anila_agent.cli.draw_graph_cli import main as cli_main
from anila_agent.core.agent_tool import make_agent_tool
from anila_agent.extensions.visualization import (
    draw_graph,
    draw_graph_ascii,
    get_main_graph,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_plain_tool(name: str, description: str = "") -> Any:
    """組一個最小可用的 FunctionTool — 純測試,on_invoke 不會被呼叫。"""
    from agents import FunctionTool

    async def _noop(_ctx: Any, _input_json: str) -> str:
        return ""

    return FunctionTool(
        name=name,
        description=description or name,
        params_json_schema={"type": "object", "properties": {}, "additionalProperties": False},
        on_invoke_tool=_noop,
        strict_json_schema=True,
    )


# ---------------------------------------------------------------------------
# get_main_graph — simple / multi sub-agent / handoff
# ---------------------------------------------------------------------------


def test_draw_graph_simple_agent_returns_dot_with_node_and_edge() -> None:
    """無 sub-agent / tool 的 agent 應產出含本身 node 與 __start__/__end__ 的 DOT。"""
    agent = Agent[Any](name="solo", instructions="hi")

    dot = draw_graph(agent)

    assert dot.startswith("digraph G {")
    assert '"solo"' in dot
    assert '"__start__"' in dot
    assert '"__end__"' in dot
    assert '"__start__" -> "solo"' in dot
    assert '"solo" -> "__end__"' in dot
    assert dot.rstrip().endswith("}")


def test_draw_graph_with_plain_tool_has_dotted_bidirectional_edges() -> None:
    """plain FunctionTool 應產出 tool node + 雙向虛線 edge,並標 ``uses`` label。"""
    tool = _make_plain_tool("search", "搜尋")
    agent = Agent[Any](name="main", instructions="hi", tools=[tool])

    dot = draw_graph(agent)

    assert '"search"' in dot
    assert 'shape=ellipse' in dot  # plain tool style
    assert '"main" -> "search"' in dot
    assert '"search" -> "main"' in dot
    assert 'label="uses"' in dot
    assert 'style=dotted' in dot


def test_draw_graph_multi_sub_agent_expands_with_double_border() -> None:
    """兩個 AgentTool wrap sub-agent 應有 peripheries=2(double box)且 uses edge 連 root → sub。"""
    sub_a = Agent[Any](name="alpha", instructions="A")
    sub_b = Agent[Any](name="beta", instructions="B")
    tool_a = make_agent_tool(sub_a, name="call_alpha", description="A")
    tool_b = make_agent_tool(sub_b, name="call_beta", description="B")
    assert tool_a.tool is not None and tool_b.tool is not None

    root = Agent[Any](name="root", instructions="hi", tools=[tool_a.tool, tool_b.tool])

    dot = get_main_graph(root)

    # sub-agent 用本名 (alpha/beta) 當 node id,不是 tool name (call_alpha)
    assert '"alpha"' in dot
    assert '"beta"' in dot
    # double border 標記
    assert "peripheries=2" in dot
    # 從 root 出去的 uses edge
    assert '"root" -> "alpha"' in dot
    assert '"root" -> "beta"' in dot


def test_draw_graph_handoff_object_produces_handoff_edge() -> None:
    """Handoff 物件應以 ``agent_name`` 為節點 + ``handoff`` label 的 edge。"""

    async def _on_invoke(_ctx: Any, _args: str) -> Agent[Any]:  # pragma: no cover
        return Agent[Any](name="dummy", instructions="x")

    ho = Handoff(
        tool_name="to_billing",
        tool_description="hand off to billing agent",
        input_json_schema={"type": "object", "properties": {}, "additionalProperties": False},
        on_invoke_handoff=_on_invoke,
        agent_name="billing",
    )
    agent = Agent[Any](name="triage", instructions="hi", handoffs=[ho])

    dot = draw_graph(agent)

    assert '"billing"' in dot
    assert '"triage" -> "billing" [label="handoff"];' in dot
    # 有 handoff 就不會連 __end__
    assert '"triage" -> "__end__"' not in dot


def test_draw_graph_handoff_direct_agent_recurses() -> None:
    """handoffs 直接給 Agent 物件時應遞迴展開 children。"""
    leaf = Agent[Any](name="leaf", instructions="leaf")
    parent = Agent[Any](name="parent", instructions="p", handoffs=[leaf])

    dot = draw_graph(parent)

    assert '"leaf"' in dot
    assert '"parent" -> "leaf" [label="handoff"];' in dot


# ---------------------------------------------------------------------------
# ASCII tree
# ---------------------------------------------------------------------------


def test_draw_graph_ascii_solo_agent() -> None:
    """無 children 的 agent ASCII tree 只有自己一行。"""
    agent = Agent[Any](name="solo", instructions="hi")

    tree = draw_graph_ascii(agent)

    assert tree == "[solo]"


def test_draw_graph_ascii_multi_child_uses_box_drawing() -> None:
    """有多個 children 時應用 ├── / └── box-drawing 字元,最後一筆用 └──。"""
    sub = Agent[Any](name="alpha", instructions="A")
    sub_tool = make_agent_tool(sub, name="call_alpha", description="A")
    assert sub_tool.tool is not None
    plain = _make_plain_tool("plain_tool")
    root = Agent[Any](name="root", instructions="hi", tools=[sub_tool.tool, plain])

    tree = draw_graph_ascii(root)

    lines = tree.splitlines()
    assert lines[0] == "[root]"
    # 第一筆 sub-agent → ├──、最後一筆 plain tool → └──
    assert any(line.startswith("├── sub-agent alpha") for line in lines)
    assert any(line.startswith("└── tool plain_tool") for line in lines)
    # sub-agent 自己沒 children → tree 只標 [[alpha]] 不展開
    assert "[[alpha]]" not in tree  # 因為 sub-agent 本身被當 child label,不重複印


def test_draw_graph_ascii_handoff_recurses_with_continuation_prefix() -> None:
    """handoff 直接給 Agent 物件且該 agent 有 children 時,延續層 prefix 應正確使用。"""
    grandchild_tool = _make_plain_tool("inner_tool")
    child = Agent[Any](name="child", instructions="c", tools=[grandchild_tool])
    root = Agent[Any](name="root", instructions="r", handoffs=[child])

    tree = draw_graph_ascii(root)

    # root → handoff -> child(└── 最後一筆)→ tool inner_tool(子層用 4-space prefix)
    assert "[root]" in tree
    assert "└── handoff -> child" in tree
    # 由於是最後一筆,延續用 4 個空白 (_TREE_BLANK),不是垂直線
    assert "    └── tool inner_tool" in tree


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------


def test_draw_graph_cycle_detection_does_not_infinite_loop() -> None:
    """A → tool_A → A 互引時 draw_graph 應在 visited set 攔住,不爆 RecursionError。"""
    # 先建 agent A、把自己包成 AgentTool 加到自己 tools 上(self-loop)
    agent_a = Agent[Any](name="A", instructions="a")
    self_tool = make_agent_tool(agent_a, name="call_A", description="self")
    assert self_tool.tool is not None
    agent_a.tools.append(self_tool.tool)

    # 不應該 raise RecursionError
    dot = draw_graph(agent_a)

    assert '"A"' in dot
    # visited 攔住 → 出現一次 sub-agent node 即夠
    assert dot.count('"A" [label="A"') == 1


def test_draw_graph_ascii_cycle_marks_cycle_label() -> None:
    """ASCII tree 對 cycle 應加 ``(cycle)`` 標記。"""
    agent_a = Agent[Any](name="A", instructions="a")
    self_tool = make_agent_tool(agent_a, name="call_A", description="self")
    assert self_tool.tool is not None
    agent_a.tools.append(self_tool.tool)

    tree = draw_graph_ascii(agent_a)

    assert "(cycle)" in tree


# ---------------------------------------------------------------------------
# output_path .dot / .svg
# ---------------------------------------------------------------------------


def test_draw_graph_writes_dot_file(tmp_path: Path) -> None:
    """output_path=.dot 應寫純 DOT 文字。"""
    agent = Agent[Any](name="solo", instructions="hi")
    out = tmp_path / "graph.dot"

    dot = draw_graph(agent, output_path=out)

    assert out.exists()
    written = out.read_text(encoding="utf-8")
    assert written == dot
    assert written.startswith("digraph G {")


def test_draw_graph_renders_svg_via_mocked_dot(tmp_path: Path) -> None:
    """output_path=.svg 應呼叫 subprocess.run 走 ``dot -Tsvg``。"""
    agent = Agent[Any](name="solo", instructions="hi")
    out = tmp_path / "graph.svg"

    with patch(
        "anila_agent.extensions.visualization._has_graphviz_cli",
        return_value=True,
    ), patch(
        "anila_agent.extensions.visualization.subprocess.run"
    ) as mock_run:
        mock_run.return_value = type(
            "CR", (), {"returncode": 0, "stderr": "", "stdout": ""}
        )()
        dot = draw_graph(agent, output_path=out)

    mock_run.assert_called_once()
    call_args = mock_run.call_args
    cmd = call_args.args[0]
    assert cmd == ["dot", "-Tsvg", "-o", str(out)]
    # input 傳的應是 dot source
    assert call_args.kwargs["input"] == dot


def test_draw_graph_fallback_when_no_graphviz(tmp_path: Path) -> None:
    """沒 graphviz CLI 時 .svg 應 fallback 為 .dot 並 emit UserWarning。"""
    agent = Agent[Any](name="solo", instructions="hi")
    out = tmp_path / "graph.svg"

    with patch(
        "anila_agent.extensions.visualization._has_graphviz_cli",
        return_value=False,
    ), pytest.warns(UserWarning, match="graphviz `dot` CLI not found"):
        draw_graph(agent, output_path=out)

    # .svg 沒被寫;.dot fallback 寫了
    assert not out.exists()
    fallback = out.with_suffix(".dot")
    assert fallback.exists()
    assert fallback.read_text(encoding="utf-8").startswith("digraph G {")


def test_draw_graph_output_path_without_suffix_raises(tmp_path: Path) -> None:
    """沒副檔名應 raise ValueError。"""
    agent = Agent[Any](name="solo", instructions="hi")

    with pytest.raises(ValueError, match="缺少副檔名"):
        draw_graph(agent, output_path=tmp_path / "noext")


def test_draw_graph_returns_dot_even_without_output_path() -> None:
    """output_path=None 應直接回 DOT 字串(不寫檔)。"""
    agent = Agent[Any](name="solo", instructions="hi")
    dot = draw_graph(agent, output_path=None)
    assert dot.startswith("digraph G {")


# ---------------------------------------------------------------------------
# CLI stub
# ---------------------------------------------------------------------------


def test_build_stub_agent_has_two_sub_agent_tools() -> None:
    """stub agent 應有兩個 sub-agent tool(search / summarize)。"""
    agent = build_stub_agent()
    assert agent.name == "main"
    assert len(agent.tools) == 2
    tool_names = {t.name for t in agent.tools}
    assert tool_names == {"search", "summarize"}


def test_cli_main_prints_dot_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    """無參數 CLI 應印 DOT 到 stdout。"""
    rc = cli_main([])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.startswith("digraph G {")
    assert '"main"' in captured.out
    assert '"retriever"' in captured.out
    assert '"writer"' in captured.out


def test_cli_main_ascii_flag(capsys: pytest.CaptureFixture[str]) -> None:
    """--ascii flag 應印 ASCII tree。"""
    rc = cli_main(["--ascii"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.startswith("[main]")
    assert "sub-agent retriever" in captured.out
    assert "sub-agent writer" in captured.out


def test_cli_main_output_writes_dot(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--output graph.dot 應寫檔並 stderr 報路徑。"""
    out = tmp_path / "g.dot"
    rc = cli_main(["--output", str(out)])
    captured = capsys.readouterr()

    assert rc == 0
    assert out.exists()
    assert "wrote" in captured.err
    # stdout 不該有 DOT 內容(避免 pipe 重複)
    assert "digraph" not in captured.out


def test_cli_main_invalid_output_returns_nonzero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """無副檔名 output 應 exit code 2 + stderr error。"""
    rc = cli_main(["--output", str(tmp_path / "noext")])
    captured = capsys.readouterr()

    assert rc == 2
    assert "error" in captured.err.lower()
