"""anila_agent.extensions — 對齊上游 openai-agents ``agents.extensions`` 的 ANILA 擴充包。

本套件用來放「非 core 必備、但對開發者體驗有加分」的工具,例如 P2-13 的
:mod:`anila_agent.extensions.visualization`(agent graph 視覺化)。對齊上游
`src/agents/extensions/visualization.py` 的設計,但 ANILA 版額外支援:

* AgentTool / FunctionTool wrap sub-agent 的「double-box」展開(P0-8)
* 沒裝 graphviz binary 也能跑(deg-graded 為 ASCII tree)
* 不引入 PyPI dep,graphviz CLI 走 subprocess 自由偵測
"""

from __future__ import annotations

from anila_agent.extensions.visualization import (
    draw_graph,
    draw_graph_ascii,
    get_main_graph,
)

__all__ = [
    "draw_graph",
    "draw_graph_ascii",
    "get_main_graph",
]
