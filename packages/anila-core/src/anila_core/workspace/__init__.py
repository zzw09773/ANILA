"""Workspace primitive — capability-scoped temp directory for sandboxed agents.

The Sprint 12 "soft sandbox" — a per-session ``temp dir + cap dict``
that file / shell tools resolve paths against. Designed for the
roadmap agents:

- 資料分析 agent (CSV upload → Python script → result)
- 程式碼審查 agent (clone repo → linter / pytest)
- 檔案編輯 agent (Claude-Code-style file edits)

Explicitly **not** a hard sandbox — escaping it requires only the
agent's tool implementations to bypass ``Workspace.safe_path()``.
The defence-in-depth boundary remains the per-agent Docker container
(at the CSP layer). Workspace's job is to:

1. Catch LLM mistakes (bad path, write outside workspace).
2. Enforce per-call capability dict (network on/off, exec on/off,
   command allowlist).
3. Provide a stable temp-dir lifetime tied to the chat session.
4. Auto-cleanup on close (opt-out via ``cleanup_after=False``).

Why no manifest / snapshot abstraction (per Sprint 12 design): we're
single-process single-host; pause-resume happens at run-loop level
(Sprint 9 Approvals). Workspace is ephemeral by design.
"""

from .caps import WorkspaceCaps
from .errors import (
    CapDeniedError,
    PathEscapeError,
    WorkspaceError,
)
from .workspace import (
    DEFAULT_WORKSPACE_ROOT,
    Workspace,
    make_workspace,
)

__all__ = [
    "Workspace",
    "WorkspaceCaps",
    "WorkspaceError",
    "PathEscapeError",
    "CapDeniedError",
    "make_workspace",
    "DEFAULT_WORKSPACE_ROOT",
]
