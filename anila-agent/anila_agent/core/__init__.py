"""anila_agent.core — agent 組裝、執行、事件、hooks、tool context 的核心模組。"""

from __future__ import annotations

from anila_agent.core.context import (
    AnilaToolContext,
    FileStateCache,
    FileStateEntry,
    WorkspaceEscapeError,
)
from anila_agent.core.guardrails import (
    GuardrailResult,
    GuardrailTripwireTriggered,
    InputGuardrail,
    InputGuardrailProtocol,
    OutputGuardrail,
    OutputGuardrailProtocol,
    input_guardrail,
    output_guardrail,
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
    # P0-3 context
    "AnilaToolContext",
    "FileStateCache",
    "FileStateEntry",
    "WorkspaceEscapeError",
    # P0-4 hook flavors
    "CommandHook",
    "HookABC",
    "HookFlavor",
    "HttpHook",
    "PromptHook",
    "PythonHook",
    # P0-5 hook taxonomy
    "DecideHook",
    "Decision",
    "DecisionVerdict",
    "HookExecutor",
    "InspectHook",
    "PipelineResult",
    "TransformHook",
    # P0-6 guardrails
    "GuardrailResult",
    "GuardrailTripwireTriggered",
    "InputGuardrail",
    "InputGuardrailProtocol",
    "OutputGuardrail",
    "OutputGuardrailProtocol",
    "input_guardrail",
    "output_guardrail",
]
