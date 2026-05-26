"""Prompts package — P1-10 systemContext / userContext 兩段 prompt builder。

Re-export :mod:`prompt_builder` 的公開 API,讓呼叫端可直接::

    from anila_agent.prompts import AnilaPromptBuilder, SystemContextBuilder

既有 markdown(``system.md`` / ``agent.md`` / ``tool_policy.md``)維持不動,
仍可透過 :meth:`SystemContextBuilder.from_markdown` 載入當 base role。
"""

from __future__ import annotations

from anila_agent.prompts.prompt_builder import (
    AnilaPromptBuilder,
    SystemContextBuilder,
    ToolDescriptor,
    UserContextBuilder,
)

__all__ = [
    "AnilaPromptBuilder",
    "SystemContextBuilder",
    "ToolDescriptor",
    "UserContextBuilder",
]
