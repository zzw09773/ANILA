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

from .agent_as_tool import make_agent_tool
from .apply_patch import (
    PatchApplyError,
    PatchParseError,
    apply_patch as apply_patch_fn,
    apply_patch_tool,
    parse_patch,
)
from .ask_user import ask_user_tool
from .files import (
    all_file_tools,
    file_edit_tool,
    file_read_tool,
    file_write_tool,
    glob_tool,
    grep_tool,
)
from .plan_mode import (
    enter_plan_mode_tool,
    exit_plan_mode_tool,
    is_plan_mode_active,
)
from .shell import all_shell_tools, exec_bash_tool, exec_python_tool
from .todo_write import TodoValidationError, todo_write_tool

__all__ = [
    "ask_user_tool",
    "enter_plan_mode_tool",
    "exit_plan_mode_tool",
    "is_plan_mode_active",
    "make_agent_tool",
    "todo_write_tool",
    "TodoValidationError",
    # Sprint 12 PR 2 — file tools (workspace-scoped)
    "file_read_tool",
    "file_write_tool",
    "file_edit_tool",
    "glob_tool",
    "grep_tool",
    "all_file_tools",
    # Sprint 12 PR 3 — shell tools (workspace-scoped)
    "exec_bash_tool",
    "exec_python_tool",
    "all_shell_tools",
    # Sprint 12 PR 4 — apply_patch (workspace-scoped, V4A envelope)
    "apply_patch_tool",
    "apply_patch_fn",
    "parse_patch",
    "PatchParseError",
    "PatchApplyError",
]
