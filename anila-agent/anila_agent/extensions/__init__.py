"""anila_agent.extensions — 對應 openai-agents 上游 ``src/agents/extensions/`` 的 ANILA 等價物。

收納非 core runtime、但仍屬於 SDK 級擴充能力(helper / prompt prefix / 視覺化等)的小套件。

P2-3 起首批模組:

* :mod:`anila_agent.extensions.handoff_prompt` — :func:`prompt_with_handoff_instructions`
  與 :class:`HandoffInstructionsBuilder`,把 sub-agent handoff 清單自動塞進 system prompt。
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

__all__ = [
    "DEFAULT_HANDOFF_INSTRUCTIONS_LANGUAGE",
    "HANDOFF_SECTION_HEADER_EN",
    "HANDOFF_SECTION_HEADER_ZH",
    "HandoffInstructionsBuilder",
    "HandoffLanguage",
    "prompt_with_handoff_instructions",
]
