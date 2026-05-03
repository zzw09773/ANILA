"""agentic_rag.runtime.framework — provider-agnostic agent runtime primitives.

v0.1.0-alpha. Public surface stabilises in v0.1.0 GA after Sprint 4
lands the Memory primitive (see ``docs/agenticrag-phase1-plan.md``
upstream).

Stage B + Sprint 2 surface (this release):

- ``Action`` / ``ActionKind`` / ``ActionResult`` / ``ActionContext`` —
  the single primitive for "thing an agent does"
- ``Agent`` / ``ModelSettings`` — immutable agent configuration
- ``Runner`` / ``RunResult`` — single-pass run loop with middleware
  composition (run-level + per-Action chains)
- ``Message`` / ``Role`` / ``ToolCall`` / ``ToolResult`` — conversation +
  audit-trail item types
- ``ToolRegistry`` / ``ToolDefinition`` — Action ↔ provider bridge
- ``Usage`` — token / request accounting
- ``providers.LLMProvider`` Protocol +
  ``providers.openai_compat.OpenAICompatProvider`` (Chat Completions)
- ``agentic_rag.runtime.framework.middleware`` — Middleware Protocol + 5 built-ins
  (Trace / Cost / Guardrail / ShellHook / Retry); see module docstring

Coming in later sprints (do NOT depend on these names yet):

- ``agentic_rag.runtime.framework.state_machine`` / ``RunState`` — Sprint 3
- ``agentic_rag.runtime.framework.memory`` (MessageHistory + SemanticMemory) — Sprint 4

Provenance: files inspired by openai-agents-python (MIT) carry a
header note. The runtime is a synthesis design, not a verbatim port —
see ``docs/anila-agent-framework-architecture.md`` for the rationale.
"""

__version__ = "0.1.0a3"

from agentic_rag.runtime.framework.action import (
    Action,
    ActionContext,
    ActionHandler,
    ActionKind,
    ActionResult,
    CostEstimate,
    SideEffectClass,
)
from agentic_rag.runtime.framework.agent import Agent, ModelSettings
from agentic_rag.runtime.framework.exceptions import (
    AgentsException,
    MaxTurnsExceeded,
    ModelBehaviorError,
    ModelRefusalError,
    OutputValidationError,
    RunCancelled,
    ToolTimeoutError,
    UserError,
)
from agentic_rag.runtime.framework.items import (
    ChatCompletionResponse,
    ContentPart,
    ErrorItem,
    FinishReason,
    HandoffItem,
    ImageURLContent,
    Message,
    MessageOutputItem,
    RefusalContent,
    Role,
    RunItem,
    TextContent,
    ToolCall,
    ToolCallItem,
    ToolResult,
    ToolResultItem,
)
from agentic_rag.runtime.framework.bg_task import (
    BgTaskHandle,
    BgTaskRunner,
    BgTaskState,
    FileSink,
    MemorySink,
    OutputSink,
)
from agentic_rag.runtime.framework.bg_task_tools import (
    make_bg_task_actions,
    make_cancel_bg_task_action,
    make_check_bg_task_action,
    make_list_bg_tasks_action,
)
from agentic_rag.runtime.framework.coordinator import (
    Coordinator,
    WorkerState,
    WorkerTask,
)
from agentic_rag.runtime.framework.coordinator_tools import (
    make_check_worker_action,
    make_coordinator_actions,
    make_spawn_worker_action,
    make_wait_for_workers_action,
)
from agentic_rag.runtime.framework.runner import Runner, RunResult
from agentic_rag.runtime.framework.schema_generator import tool
from agentic_rag.runtime.framework.skill_loader import (
    Skill,
    load_skills_from_dir,
    parse_skill_file,
)
from agentic_rag.runtime.framework.skill_registry import (
    RelevanceRanker,
    SkillRegistry,
)
from agentic_rag.runtime.framework.task_notification import (
    KNOWN_STATUSES,
    TaskNotification,
    build_task_notification,
    collect_for_summary,
    parse_all,
    parse_task_notification,
)
from agentic_rag.runtime.framework.stream_events import (
    HandoffEvent,
    MessageDeltaEvent,
    RunCompletedEvent,
    RunErrorEvent,
    StreamEvent,
    ToolCallFinishedEvent,
    ToolCallStartedEvent,
    UsageUpdateEvent,
)
from agentic_rag.runtime.framework.tool import ToolDefinition, ToolRegistry
from agentic_rag.runtime.framework.usage import (
    InputTokensDetails,
    OutputTokensDetails,
    RequestUsage,
    Usage,
)

__all__ = [
    "Action",
    "ActionContext",
    "ActionHandler",
    "ActionKind",
    "ActionResult",
    "Agent",
    "AgentsException",
    "ChatCompletionResponse",
    "ContentPart",
    "CostEstimate",
    "ErrorItem",
    "FinishReason",
    "HandoffItem",
    "ImageURLContent",
    "InputTokensDetails",
    "MaxTurnsExceeded",
    "Message",
    "MessageOutputItem",
    "ModelBehaviorError",
    "ModelRefusalError",
    "ModelSettings",
    "OutputTokensDetails",
    "OutputValidationError",
    "RunCancelled",
    "RefusalContent",
    "RequestUsage",
    "Role",
    "RunItem",
    "RunResult",
    "Runner",
    "SideEffectClass",
    "TextContent",
    "ToolCall",
    "ToolCallItem",
    "ToolDefinition",
    "ToolRegistry",
    "ToolResult",
    "ToolResultItem",
    "ToolTimeoutError",
    "Usage",
    "UserError",
    "__version__",
    "tool",
    # Sprint 5: Coordinator + worker spawn
    "Coordinator",
    "KNOWN_STATUSES",
    "TaskNotification",
    "WorkerState",
    "WorkerTask",
    "build_task_notification",
    "collect_for_summary",
    "make_check_worker_action",
    "make_coordinator_actions",
    "make_spawn_worker_action",
    "make_wait_for_workers_action",
    "parse_all",
    "parse_task_notification",
    # Sprint 6: BG_TASK runtime
    "BgTaskHandle",
    "BgTaskRunner",
    "BgTaskState",
    "FileSink",
    "MemorySink",
    "OutputSink",
    "make_bg_task_actions",
    "make_cancel_bg_task_action",
    "make_check_bg_task_action",
    "make_list_bg_tasks_action",
    # Sprint 7: Skill loader
    "RelevanceRanker",
    "Skill",
    "SkillRegistry",
    "load_skills_from_dir",
    "parse_skill_file",
    "HandoffEvent",
    "MessageDeltaEvent",
    "RunCompletedEvent",
    "RunErrorEvent",
    "StreamEvent",
    "ToolCallFinishedEvent",
    "ToolCallStartedEvent",
    "UsageUpdateEvent",
]
