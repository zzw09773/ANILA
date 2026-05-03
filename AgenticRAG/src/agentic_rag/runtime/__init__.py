"""AgenticRAG agent runtime — framework primitives + AgenticRAG bridge.

Layout:

- ``runtime.framework`` — Action / Agent / Runner / Middleware / Provider
  primitives, vendored inside AgenticRAG. NOT a separate package, NOT
  on PyPI; AgenticRAG owns this code so a fork is self-contained
  (``git clone`` → ``pip install -e .`` → ``docker compose up``).
  The synthesis design is informed by ``D:\\ANILA\\runtime_logic\\``
  (openai-agents-python MIT, claude-code-src reference) but does not
  import from them at runtime.
- ``runtime.bridge`` — adapters that connect framework primitives to
  AgenticRAG's existing surfaces (stream-based ``Provider``,
  dict-output ``ToolDefinition``, SSE ``ServerEvent``).

Most app code can import from ``agentic_rag.runtime`` directly — this
module re-exports the common framework + bridge names. Sub-module
imports are still available for code that wants to be explicit about
where a name came from.
"""

# ── Framework re-exports (most-used surface) ──────────────────────────

from agentic_rag.runtime.framework import (
    Action,
    ActionContext,
    ActionKind,
    ActionResult,
    Agent,
    AgentsException,
    ChatCompletionResponse,
    CostEstimate,
    FinishReason,
    HandoffItem,
    MaxTurnsExceeded,
    Message,
    MessageOutputItem,
    ModelBehaviorError,
    ModelRefusalError,
    ModelSettings,
    OutputValidationError,
    Role,
    RunCancelled,
    RunItem,
    RunResult,
    Runner,
    SideEffectClass,
    ToolCall,
    ToolCallItem,
    ToolDefinition,
    ToolRegistry,
    ToolResult,
    ToolResultItem,
    ToolTimeoutError,
    Usage,
    UserError,
    tool,
)

# ── Bridge re-exports (AgenticRAG-specific glue) ──────────────────────

from agentic_rag.runtime.bridge import (
    FrameworkProviderAdapter,
    build_keyword_search_action,
    build_rag_agent,
    build_read_document_action,
    build_vector_search_action,
    run_agentic_chat_sse,
    wrap_tool_definition,
)

__all__ = [
    # Framework primitives
    "Action",
    "ActionContext",
    "ActionKind",
    "ActionResult",
    "Agent",
    "AgentsException",
    "ChatCompletionResponse",
    "CostEstimate",
    "FinishReason",
    "FrameworkProviderAdapter",
    "HandoffItem",
    "MaxTurnsExceeded",
    "Message",
    "MessageOutputItem",
    "ModelBehaviorError",
    "ModelRefusalError",
    "ModelSettings",
    "OutputValidationError",
    "Role",
    "RunCancelled",
    "RunItem",
    "RunResult",
    "Runner",
    "SideEffectClass",
    "ToolCall",
    "ToolCallItem",
    "ToolDefinition",
    "ToolRegistry",
    "ToolResult",
    "ToolResultItem",
    "ToolTimeoutError",
    "Usage",
    "UserError",
    "tool",
    # Bridge
    "build_keyword_search_action",
    "build_rag_agent",
    "build_read_document_action",
    "build_vector_search_action",
    "run_agentic_chat_sse",
    "wrap_tool_definition",
]
