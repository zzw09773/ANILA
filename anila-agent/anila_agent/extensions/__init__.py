"""anila_agent.extensions — 對齊上游 openai-agents ``agents.extensions`` 的 ANILA 擴充包。

本套件收納「非 core 必備、但對開發者體驗有加分」的工具,對應上游 ``src/agents/extensions/``。

目前模組:

* :mod:`anila_agent.extensions.handoff_prompt`(P2-3)— :func:`prompt_with_handoff_instructions`
  與 :class:`HandoffInstructionsBuilder`,把 sub-agent handoff 清單自動塞進 system prompt。
* :mod:`anila_agent.extensions.visualization`(P2-13)— :func:`draw_graph` /
  :func:`draw_graph_ascii`,用 graphviz DOT 或 ASCII tree 把 agent + tool + handoff
  關係圖印出來;沒裝 graphviz binary 時自動 fallback 為 ASCII。
"""

from __future__ import annotations

from anila_agent.extensions.handoff_prompt import (
    DEFAULT_HANDOFF_INSTRUCTIONS_LANGUAGE,
    HANDOFF_SECTION_HEADER_EN,
    HANDOFF_SECTION_HEADER_ZH,
    HandoffInstructionsBuilder,
    HandoffLanguage,
    prompt_with_handoff_instructions,
)
from anila_agent.extensions.visualization import (
    draw_graph,
    draw_graph_ascii,
    get_main_graph,
)

__all__ = [
    # P2-3 handoff prompt
    "DEFAULT_HANDOFF_INSTRUCTIONS_LANGUAGE",
    "HANDOFF_SECTION_HEADER_EN",
    "HANDOFF_SECTION_HEADER_ZH",
    "HandoffInstructionsBuilder",
    "HandoffLanguage",
    "prompt_with_handoff_instructions",
    # P2-13 visualization
    "draw_graph",
    "draw_graph_ascii",
    "get_main_graph",
]
