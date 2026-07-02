"""Workspace capability set — what the agent is allowed to do inside it.

Kept as a simple frozen dataclass (not pydantic Manifest / EnvEntry /
Mount / etc.) — see Sprint 12 design notes for why we deliberately
skip the openai-agents manifest abstraction.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(frozen=True)
class WorkspaceCaps:
    """Per-workspace capability dict.

    Defaults are **safe** — read/write inside the workspace is on, but
    network and exec are off. Agents that need exec must request it
    explicitly when calling :func:`make_workspace`.

    Attributes:
        fs_read: Tool may read files inside the workspace.
        fs_write: Tool may create / modify / delete files inside the
            workspace. Path escape is always blocked regardless.
        network: Subprocesses may make outbound network calls.
            Implementation: when False, exec env scrubs proxy vars and
            relies on the caller (Docker container etc.) for actual
            enforcement. Soft enforcement only.
        exec_bash: ``exec_bash`` tool may run shell commands.
        exec_python: ``exec_python`` tool may run python scripts.
        command_allowlist: When non-empty AND ``exec_bash`` is True,
            only commands whose first token is in this list may run.
            Empty list = unrestricted (when exec_bash is True). Useful
            for read-only mount inspection (``ls``, ``cat``, ``grep``).
        max_exec_seconds: Wall-clock timeout per shell / python call.
        max_workspace_size_mb: Soft cap on total workspace bytes; tool
            implementations check before write. Default 100 MB.
    """

    fs_read: bool = True
    fs_write: bool = True
    network: bool = False
    exec_bash: bool = False
    exec_python: bool = False
    command_allowlist: tuple[str, ...] = field(default_factory=tuple)
    max_exec_seconds: int = 30
    max_workspace_size_mb: int = 100

    def with_overrides(self, **changes: Any) -> "WorkspaceCaps":
        """Return a copy with the given fields replaced (immutable update)."""
        return replace(self, **changes)


# A convenient pre-set for the "code review" agent shape: read-only
# inspection of a cloned repo, allow common Unix inspection commands,
# allow network only for the initial git clone (caller must turn it
# off after).
READ_ONLY_INSPECT_ALLOWLIST = (
    "ls", "find", "stat", "cat", "less", "head", "tail",
    "du", "wc", "sort", "cut", "grep", "rg", "echo",
)
