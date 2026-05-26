"""anila_agent.core — agent 組裝、執行、事件、hooks、tool context 的核心模組。"""

from __future__ import annotations

from anila_agent.core.context import (
    AnilaToolContext,
    FileStateCache,
    FileStateEntry,
    WorkspaceEscapeError,
)
from anila_agent.core.hook_flavors import (
    CommandHook,
    HookABC,
    HookFlavor,
    HttpHook,
    PromptHook,
    PythonHook,
)
from anila_agent.core.hook_taxonomy import (
    DecideHook,
    Decision,
    DecisionVerdict,
    HookExecutor,
    InspectHook,
    PipelineResult,
    TransformHook,
)

__all__ = [
    "AnilaToolContext",
    "CommandHook",
    "FileStateCache",
    "FileStateEntry",
    "HookABC",
    "HookFlavor",
    "HttpHook",
    "PromptHook",
    "PythonHook",
    "WorkspaceEscapeError",
    # P0-5 Hook 三類強型別分類
    "Decision",
    "DecisionVerdict",
    "InspectHook",
    "DecideHook",
    "TransformHook",
    "HookExecutor",
    "PipelineResult",
]
