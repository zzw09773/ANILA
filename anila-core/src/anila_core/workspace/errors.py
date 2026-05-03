"""Workspace-layer exceptions."""

from __future__ import annotations


class WorkspaceError(Exception):
    """Base class for workspace-layer errors."""


class PathEscapeError(WorkspaceError):
    """Raised when a tool tries to access a path outside the workspace.

    Carries both the requested path (as the agent supplied it) and the
    resolved absolute path so the audit trail / trace span can capture
    what was actually attempted.
    """

    def __init__(
        self, *, requested: str, resolved: str, workspace_root: str
    ) -> None:
        super().__init__(
            f"path escape attempt: {requested!r} resolved to {resolved!r}, "
            f"which is outside workspace root {workspace_root!r}"
        )
        self.requested = requested
        self.resolved = resolved
        self.workspace_root = workspace_root


class CapDeniedError(WorkspaceError):
    """Raised when an operation is blocked by the workspace's capability set.

    Examples: shell exec when ``exec_bash`` is False; outbound HTTP when
    ``network`` is False; ``rm -rf`` when ``"rm"`` is not in the
    command allowlist.
    """

    def __init__(self, *, cap: str, detail: str = "") -> None:
        msg = f"workspace capability denied: {cap}"
        if detail:
            msg = f"{msg} ({detail})"
        super().__init__(msg)
        self.cap = cap
        self.detail = detail
