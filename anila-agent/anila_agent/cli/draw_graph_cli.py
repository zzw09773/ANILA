"""P2-13 — ``draw_graph`` CLI(stub):把 agent graph 印到 stdout / 寫檔。

用法:

.. code-block:: bash

    # 印 DOT 到 stdout(stub 範例 agent)
    python -m anila_agent.cli.draw_graph_cli

    # 印 ASCII tree
    python -m anila_agent.cli.draw_graph_cli --ascii

    # 寫 .dot / .svg(.svg 需本機有 dot CLI)
    python -m anila_agent.cli.draw_graph_cli --output /tmp/graph.dot
    python -m anila_agent.cli.draw_graph_cli --output /tmp/graph.svg

設計刻意維持「不跑 demo agent / 不打 LLM」,只示範 :func:`anila_agent.extensions.visualization`
的 API 與輸出格式。實際應用會由 caller 自行 build agent 後傳進來。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from agents import Agent

from anila_agent.core.agent_tool import make_agent_tool
from anila_agent.extensions.visualization import draw_graph, draw_graph_ascii


def build_stub_agent() -> Agent[object]:
    """建一份 stub 範例 agent(有兩個 sub-agent + 一個 plain tool)。

    純測試 / 文件用途,不打 LLM。可被測試 import 來驗 CLI 行為。
    """
    retriever = Agent[object](
        name="retriever",
        instructions="搜尋向量資料庫,回 top-K chunks。",
    )
    writer = Agent[object](
        name="writer",
        instructions="把 chunks 變成中文摘要。",
    )

    retriever_tool = make_agent_tool(retriever, name="search", description="搜尋")
    writer_tool = make_agent_tool(writer, name="summarize", description="摘要")

    assert retriever_tool.tool is not None  # make_agent_tool 必設;讓 type checker 過
    assert writer_tool.tool is not None

    return Agent[object](
        name="main",
        instructions="先 search 再 summarize。",
        tools=[retriever_tool.tool, writer_tool.tool],
    )


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="anila-draw-graph",
        description="P2-13 — 把 agent graph 印成 DOT 或 ASCII tree。",
    )
    parser.add_argument(
        "--ascii",
        action="store_true",
        help="改印 ASCII tree(沒 graphviz 也能看)。",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="輸出檔路徑(副檔名決定格式:.dot / .svg / .png)。",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point。

    Returns:
        exit code(0 成功;非 0 例如 output_path 副檔名錯)。
    """
    args = _parse_args(argv)
    agent = build_stub_agent()

    if args.ascii:
        sys.stdout.write(draw_graph_ascii(agent) + "\n")
        return 0

    try:
        dot_source = draw_graph(agent, output_path=args.output)
    except ValueError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2

    # 若沒指定 output,就把 DOT 印到 stdout;有指定就只回報路徑
    if args.output is None:
        sys.stdout.write(dot_source)
    else:
        sys.stderr.write(f"wrote {args.output}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover — 由 -m 啟動才進來
    sys.exit(main())
