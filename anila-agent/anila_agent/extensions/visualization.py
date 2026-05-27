"""P2-13 — draw_graph visualization extension(graphviz DOT + ASCII tree)。

本模組對應 enhancement roadmap §4.13(原文標 P3 nice-to-have,實作 ticket 列為
P2-13)。上游參考為 ``agents.extensions.visualization``(openai-agents 0.17.x
``src/agents/extensions/visualization.py``)。

差異與 ANILA 加分:

* **不強制依賴 graphviz Python binding**:上游 `import graphviz` 是硬依賴,沒裝就
  整個 module import 不起來;ANILA 版只用 std lib 產 DOT 字串,**沒 binary 也能跑**。
* **subprocess 偵測 dot CLI**:有 ``dot`` 命令時才 render ``.svg`` / ``.png``,沒
  裝就 fallback 只寫 ``.dot``(plus 給 caller warn 訊息)。
* **AgentTool 展開 sub-agent**:P0-8 包出來的 :class:`AgentTool` 會在 graph 顯示
  為 double-border box,並遞迴展開 sub-agent 內部的 tool / handoff,讓多層派工拓
  樸一眼看見。
* **ASCII fallback**(:func:`draw_graph_ascii`):純文字 box-drawing tree,方便在
  CLI / log 直接 dump 不靠 graphviz。
* **cycle detection**:visited set 以 ``id(agent)`` 為 key,避免循環 handoff /
  agent-as-tool 互引造成無限遞迴。

設計刻意維持「pure function + 純 std lib」,所有 I/O(寫檔、render)集中在
:func:`draw_graph` 的尾段,便於測試 mock。
"""

from __future__ import annotations

import shutil
import subprocess  # nosec B404 — 僅用於呼叫本機 graphviz CLI,參數固定 hard-coded
import warnings
from pathlib import Path
from typing import Any

from agents import Agent
from agents.handoffs import Handoff

from anila_agent.core.agent_tool import get_agent_tool_spec

# ---------------------------------------------------------------------------
# DOT header / footer 樣板
# ---------------------------------------------------------------------------

_DOT_HEADER = (
    "digraph G {\n"
    '    graph [splines=true, rankdir=LR];\n'
    '    node [fontname="Arial"];\n'
    "    edge [penwidth=1.5];\n"
)
_DOT_FOOTER = "}\n"

# 三種節點樣式 — 用 dict-of-dict 方便擴充。
_NODE_STYLES: dict[str, str] = {
    # 一般 agent → 黃底 box
    "agent": 'shape=box, style=filled, fillcolor=lightyellow, width=1.6, height=0.6',
    # sub-agent(AgentTool wrap)→ 黃底 double box,跟一般 agent 區分
    "sub_agent": (
        'shape=box, style="filled,bold", fillcolor=lightyellow, '
        "peripheries=2, width=1.6, height=0.6"
    ),
    # 一般 tool → 綠底 ellipse
    "tool": 'shape=ellipse, style=filled, fillcolor=lightgreen, width=1.2, height=0.4',
}

# ASCII tree 用的 box-drawing 字元;集中常數方便置換。
_TREE_BRANCH = "├── "
_TREE_LAST = "└── "
_TREE_VERT = "│   "
_TREE_BLANK = "    "


# ---------------------------------------------------------------------------
# 內部 helper — visited set 用 id(agent) 防 cycle
# ---------------------------------------------------------------------------


def _is_agent_tool(tool: Any) -> Agent[Any] | None:
    """若 ``tool`` 是 P0-8 :class:`AgentTool` wrap 出來的 FunctionTool,回 sub-agent。

    用 :func:`anila_agent.core.agent_tool.get_agent_tool_spec` 反查 spec;
    非 AgentTool wrap 的 FunctionTool / 一般 tool 回 None。
    """
    spec = get_agent_tool_spec(tool)
    if spec is None:
        return None
    return spec.sub_agent


def _escape_label(text: str) -> str:
    """把節點 label 內的雙引號 escape,避免 DOT parser 炸掉。"""
    return text.replace('"', '\\"')


def _node_line(node_id: str, label: str, style_key: str) -> str:
    """產出一行 DOT node 宣告。"""
    style = _NODE_STYLES[style_key]
    return f'    "{_escape_label(node_id)}" [label="{_escape_label(label)}", {style}];\n'


# ---------------------------------------------------------------------------
# get_all_nodes / get_all_edges — 上游 visualization.py 風格的兩段式 DOT 產生
# ---------------------------------------------------------------------------


def _collect_nodes(
    agent: Agent[Any],
    *,
    visited: set[int],
    is_root: bool = True,
) -> list[str]:
    """遞迴收集 agent / tool / sub-agent / handoff 的 DOT node 宣告。

    visited 以 ``id(agent)`` 為 key,避免循環 handoff / agent-as-tool 無限遞迴。
    """
    if id(agent) in visited:
        return []
    visited.add(id(agent))

    parts: list[str] = []

    # root agent 才畫 __start__ / __end__ 兩個錨點
    if is_root:
        parts.append(
            '    "__start__" [label="__start__", shape=ellipse, style=filled, '
            "fillcolor=lightblue, width=0.5, height=0.3];\n"
        )
        parts.append(
            '    "__end__" [label="__end__", shape=ellipse, style=filled, '
            "fillcolor=lightblue, width=0.5, height=0.3];\n"
        )
        parts.append(_node_line(agent.name, agent.name, "agent"))

    # 每個 tool 都畫 node — 若是 AgentTool wrap 就用 sub_agent style + 遞迴展開
    for tool in agent.tools:
        sub_agent = _is_agent_tool(tool)
        if sub_agent is not None:
            # 若 sub-agent 已在 visited(self-loop 或 diamond),不重複印 node。
            if id(sub_agent) not in visited:
                # double-box 表示 sub-agent
                parts.append(_node_line(sub_agent.name, sub_agent.name, "sub_agent"))
                # 遞迴展開 sub-agent 內部(非 root,不再畫 __start__/__end__)
                parts.extend(_collect_nodes(sub_agent, visited=visited, is_root=False))
        else:
            parts.append(_node_line(tool.name, tool.name, "tool"))

    # handoff target — 兩種 case:Handoff 物件或直接給 Agent
    for handoff in agent.handoffs:
        if isinstance(handoff, Handoff):
            parts.append(_node_line(handoff.agent_name, handoff.agent_name, "agent"))
        elif isinstance(handoff, Agent) and id(handoff) not in visited:
            parts.append(_node_line(handoff.name, handoff.name, "agent"))
            parts.extend(_collect_nodes(handoff, visited=visited, is_root=False))

    return parts


def _collect_edges(
    agent: Agent[Any],
    *,
    visited: set[int],
    is_root: bool = True,
) -> list[str]:
    """遞迴收集 agent ↔ tool / sub-agent / handoff 的 DOT edge 宣告。"""
    if id(agent) in visited:
        return []
    visited.add(id(agent))

    parts: list[str] = []

    if is_root:
        parts.append(f'    "__start__" -> "{_escape_label(agent.name)}";\n')

    for tool in agent.tools:
        sub_agent = _is_agent_tool(tool)
        if sub_agent is not None:
            # sub-agent 派工 — 雙向實線 + 標 "uses"
            parts.append(
                f'    "{_escape_label(agent.name)}" -> '
                f'"{_escape_label(sub_agent.name)}" [label="uses", penwidth=1.5];\n'
            )
            parts.extend(_collect_edges(sub_agent, visited=visited, is_root=False))
        else:
            # 一般 tool — 雙向虛線 + 標 "uses"
            parts.append(
                f'    "{_escape_label(agent.name)}" -> '
                f'"{_escape_label(tool.name)}" [label="uses", style=dotted, penwidth=1.5];\n'
            )
            parts.append(
                f'    "{_escape_label(tool.name)}" -> '
                f'"{_escape_label(agent.name)}" [style=dotted, penwidth=1.5];\n'
            )

    for handoff in agent.handoffs:
        if isinstance(handoff, Handoff):
            parts.append(
                f'    "{_escape_label(agent.name)}" -> '
                f'"{_escape_label(handoff.agent_name)}" [label="handoff"];\n'
            )
        elif isinstance(handoff, Agent):
            parts.append(
                f'    "{_escape_label(agent.name)}" -> '
                f'"{_escape_label(handoff.name)}" [label="handoff"];\n'
            )
            parts.extend(_collect_edges(handoff, visited=visited, is_root=False))

    # 末端 agent(無 handoff)接 __end__,只有 root 鏈尾才畫。
    if not agent.handoffs and is_root:
        parts.append(f'    "{_escape_label(agent.name)}" -> "__end__";\n')

    return parts


def get_main_graph(agent: Agent[Any]) -> str:
    """產生 root agent 的 DOT 字串(可餵給 ``dot`` CLI 或 graphviz Python binding)。

    Args:
        agent: 要視覺化的 root agent。

    Returns:
        DOT format 字串(含 header / nodes / edges / footer)。
    """
    visited_nodes: set[int] = set()
    visited_edges: set[int] = set()
    body_nodes = "".join(_collect_nodes(agent, visited=visited_nodes, is_root=True))
    body_edges = "".join(_collect_edges(agent, visited=visited_edges, is_root=True))
    return _DOT_HEADER + body_nodes + body_edges + _DOT_FOOTER


# ---------------------------------------------------------------------------
# draw_graph — 主 entry,支援 output_path .dot / .svg / .png
# ---------------------------------------------------------------------------


def _has_graphviz_cli() -> bool:
    """偵測本機是否有 ``dot`` 執行檔(用 shutil.which,跨平台)。"""
    return shutil.which("dot") is not None


def _render_with_dot(dot_source: str, output_path: Path, output_format: str) -> None:
    """呼叫本機 ``dot`` CLI 把 DOT 渲染為 svg / png 等格式。

    用 ``subprocess.run`` 走 stdin → stdout 的 pipe;不寫暫存檔避免污染檔案系統。
    raise :class:`RuntimeError` 若 dot 失敗(由 caller 接住 wrap warning)。
    """
    cmd = ["dot", f"-T{output_format}", "-o", str(output_path)]
    try:
        subprocess.run(  # nosec B603 — 參數 hard-coded,output_path 已型別檢查
            cmd,
            input=dot_source,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"graphviz dot CLI failed (returncode={exc.returncode}): {exc.stderr}"
        ) from exc


def draw_graph(
    agent: Agent[Any],
    output_path: Path | str | None = None,
) -> str:
    """產生 agent graph 的 DOT 字串,選填寫檔 / render。

    Args:
        agent: 要視覺化的 root agent。
        output_path: 選填輸出檔路徑。副檔名決定行為:

            * ``.dot`` — 寫 DOT 文字檔。
            * ``.svg`` / ``.png`` / 其他 graphviz 支援格式 — 若本機有 ``dot``
              CLI 則 render;沒裝 graphviz binary 時 fallback 為寫 ``.dot``
              並 emit :class:`UserWarning`。
            * ``None`` — 不寫檔。

    Returns:
        DOT format 字串。永遠回傳,即便寫檔失敗也保證 caller 拿得到 source。

    Raises:
        ValueError: ``output_path`` 副檔名空白或不支援(例如 ``.txt``)時。
    """
    dot_source = get_main_graph(agent)

    if output_path is None:
        return dot_source

    path = Path(output_path)
    suffix = path.suffix.lower().lstrip(".")
    if not suffix:
        raise ValueError(
            f"output_path '{path}' 缺少副檔名;請給 .dot / .svg / .png 等。"
        )

    if suffix == "dot":
        path.write_text(dot_source, encoding="utf-8")
        return dot_source

    # 非 .dot — 需 graphviz CLI render
    if not _has_graphviz_cli():
        # fallback - 寫一份 .dot 在同位置(改副檔名)+ warn
        fallback = path.with_suffix(".dot")
        fallback.write_text(dot_source, encoding="utf-8")
        warnings.warn(
            f"graphviz `dot` CLI not found; wrote DOT source to {fallback} instead "
            f"of rendering {path}.",
            UserWarning,
            stacklevel=2,
        )
        return dot_source

    _render_with_dot(dot_source, path, suffix)
    return dot_source


# ---------------------------------------------------------------------------
# draw_graph_ascii — 純文字 fallback
# ---------------------------------------------------------------------------


def _ascii_label(agent: Agent[Any], *, is_sub: bool = False) -> str:
    """ASCII tree 用的 agent label。

    sub-agent 用 ``[[name]]`` 強調 double-box 語意;一般 agent 用 ``[name]``。
    """
    if is_sub:
        return f"[[{agent.name}]]"
    return f"[{agent.name}]"


def _walk_ascii(
    agent: Agent[Any],
    *,
    prefix: str,
    visited: set[int],
    is_sub: bool,
    lines: list[str],
) -> None:
    """遞迴展開 agent 為 ASCII tree。

    ``prefix`` 包含父層垂直線(``│   ``)與留白(``    ``),由 caller 組好傳進來。
    children 依序為 tools(含 sub-agent)→ handoffs;每筆判斷是否最後一筆來選
    ``├──`` 或 ``└──``,並把對應的延續 prefix(``│   `` / ``    ``)傳給子層。
    """
    if id(agent) in visited:
        lines.append(f"{prefix}{_TREE_LAST}{_ascii_label(agent, is_sub=is_sub)} (cycle)")
        return
    visited.add(id(agent))

    # 收集 children entry: (label, kind, sub_agent_or_None)
    children: list[tuple[str, str, Agent[Any] | None]] = []
    for tool in agent.tools:
        sub_agent = _is_agent_tool(tool)
        if sub_agent is not None:
            children.append((f"sub-agent {sub_agent.name}", "sub_agent", sub_agent))
        else:
            children.append((f"tool {tool.name}", "tool", None))
    for handoff in agent.handoffs:
        if isinstance(handoff, Handoff):
            children.append((f"handoff -> {handoff.agent_name}", "handoff", None))
        elif isinstance(handoff, Agent):
            children.append((f"handoff -> {handoff.name}", "handoff", handoff))

    total = len(children)
    for idx, (label, kind, child_agent) in enumerate(children):
        is_last = idx == total - 1
        branch = _TREE_LAST if is_last else _TREE_BRANCH
        lines.append(f"{prefix}{branch}{label}")
        if child_agent is not None:
            next_prefix = prefix + (_TREE_BLANK if is_last else _TREE_VERT)
            _walk_ascii(
                child_agent,
                prefix=next_prefix,
                visited=visited,
                is_sub=(kind == "sub_agent"),
                lines=lines,
            )


def draw_graph_ascii(agent: Agent[Any]) -> str:
    """產生 agent graph 的 ASCII tree 字串(沒 graphviz 也能用)。

    Args:
        agent: 要視覺化的 root agent。

    Returns:
        多行字串,首行為 ``[agent_name]``,後續行用 box-drawing 字元展開 tool /
        sub-agent / handoff;cycle 偵測到時加 ``(cycle)`` 標記。

    Example:
        >>> from agents import Agent
        >>> root = Agent(name="main", instructions="hi")
        >>> print(draw_graph_ascii(root))
        [main]
    """
    visited: set[int] = set()
    lines: list[str] = [_ascii_label(agent, is_sub=False)]
    _walk_ascii(agent, prefix="", visited=visited, is_sub=False, lines=lines)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 公開 API
# ---------------------------------------------------------------------------

__all__ = [
    "draw_graph",
    "draw_graph_ascii",
    "get_main_graph",
]
