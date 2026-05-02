"""anila-core tools package.

Sprint 1 boundary cleanup (docs/anila-core-boundary.md §2.3) removed the
RAG-specific tool factories that used to live here:

    - create_vector_search_tool
    - create_keyword_search_tool
    - create_read_document_tool

RAG agents now use the AgenticRAG template's own ``agentic_rag.tools``
module, which carries the canonical implementations. anila-core stays
RAG-agnostic — callers building agents wire whatever ToolDefinitions
they need into the ToolRegistry passed to the runtime, and core has no
opinion on whether those tools talk to pgvector, an HTTP service, or
neither.

What still lives in this package:

- ``dispatch_tool``: Router → CSP → Agent dispatch (used by
  ``api/router_server.py``); not RAG-specific.
- ``ask_user``: Sprint 9 — pause for a multiple-choice user question.
- ``plan_mode``: Sprint 9 — propose-then-execute (enter / exit).
"""

from __future__ import annotations

from .ask_user import ask_user_tool
from .plan_mode import (
    enter_plan_mode_tool,
    exit_plan_mode_tool,
    is_plan_mode_active,
)
from .todo_write import TodoValidationError, todo_write_tool

__all__ = [
    "ask_user_tool",
    "enter_plan_mode_tool",
    "exit_plan_mode_tool",
    "is_plan_mode_active",
    "todo_write_tool",
    "TodoValidationError",
]
