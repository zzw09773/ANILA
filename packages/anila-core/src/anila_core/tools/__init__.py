"""anila-core tools package.

Sprint 1 boundary cleanup (docs/archive/anila-core/anila-core-boundary.md §2.3) removed the
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

# 2026-09-02: lazy (PEP 562). The Router imports ``tools.dispatch_tool`` and
# nothing else from here; eager re-exports used to drag shell / files /
# apply_patch / plan_mode / todo_write / ask_user (~1,800 lines, incl. a
# subprocess tool) into the Router process. Names resolve on first access.
from __future__ import annotations

import importlib

_LAZY: dict[str, str] = {
    "make_agent_tool": ".agent_as_tool",
    "PatchApplyError": ".apply_patch",
    "PatchParseError": ".apply_patch",
    "apply_patch_fn": ".apply_patch",
    "apply_patch_tool": ".apply_patch",
    "parse_patch": ".apply_patch",
    "ask_user_tool": ".ask_user",
    "all_file_tools": ".files",
    "file_edit_tool": ".files",
    "file_read_tool": ".files",
    "file_write_tool": ".files",
    "glob_tool": ".files",
    "grep_tool": ".files",
    "enter_plan_mode_tool": ".plan_mode",
    "exit_plan_mode_tool": ".plan_mode",
    "is_plan_mode_active": ".plan_mode",
    "all_shell_tools": ".shell",
    "exec_bash_tool": ".shell",
    "exec_python_tool": ".shell",
    "TodoValidationError": ".todo_write",
    "todo_write_tool": ".todo_write",
}
# ``apply_patch`` the function was re-exported under the alias below.
_ALIASES: dict[str, str] = {"apply_patch_fn": "apply_patch"}

__all__ = sorted(_LAZY)


def __getattr__(name: str):
    try:
        modname = _LAZY[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    module = importlib.import_module(modname, __name__)
    value = getattr(module, _ALIASES.get(name, name))
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY))
