"""Workspace — capability-scoped temp directory for sandboxed agents."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .caps import WorkspaceCaps
from .errors import PathEscapeError

logger = logging.getLogger(__name__)


# Single root under which all anila workspaces live. Override via env
# var ``ANILA_WORKSPACE_ROOT`` (e.g. point at a tmpfs / ramdisk in
# production for ephemeral state).
DEFAULT_WORKSPACE_ROOT = os.environ.get(
    "ANILA_WORKSPACE_ROOT",
    str(Path(tempfile.gettempdir()) / "anila-workspaces"),
)


def _new_workspace_id() -> str:
    return f"ws-{uuid.uuid4().hex[:16]}"


class Workspace:
    """A capability-scoped temp directory.

    Lifecycle:
        ws = make_workspace(caps=...)
        try:
            target = ws.safe_path("subdir/file.txt")
            target.write_text("hi")
        finally:
            ws.cleanup()

    Or as a (sync) context manager::

        with make_workspace() as ws:
            ...

    Tied to chat session: pass ``session_id`` to make the workspace
    name traceable in logs and to support the future "resume the
    workspace alongside the session" flow.

    The ``cleanup_after`` flag controls whether ``cleanup()`` actually
    deletes the dir — set False to inspect contents post-mortem.
    """

    def __init__(
        self,
        *,
        path: Path,
        workspace_id: str,
        caps: WorkspaceCaps,
        session_id: str = "",
        cleanup_after: bool = True,
    ) -> None:
        self._path = path.resolve()
        self._id = workspace_id
        self._caps = caps
        self._session_id = session_id
        self._cleanup_after = cleanup_after
        self._created_at = datetime.utcnow()
        self._closed = False

    # ---- introspection ----

    @property
    def path(self) -> Path:
        """Absolute path to the workspace root."""
        return self._path

    @property
    def id(self) -> str:
        return self._id

    @property
    def caps(self) -> WorkspaceCaps:
        return self._caps

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def created_at(self) -> datetime:
        return self._created_at

    @property
    def is_closed(self) -> bool:
        return self._closed

    # ---- safe path resolution ----

    def safe_path(self, requested: str | Path) -> Path:
        """Resolve ``requested`` inside the workspace, raise on escape.

        Accepts:
        - Relative paths (``"subdir/file.txt"``) — joined to workspace root.
        - Absolute paths that already point inside the workspace.

        Rejects:
        - Absolute paths outside the workspace.
        - Relative paths whose resolved form escapes the root
          (``"../../etc/passwd"``).
        - Symlinks pointing outside the workspace (resolved via realpath).

        Returns: an absolute :class:`pathlib.Path` guaranteed to be
            inside the workspace root (the file itself need not exist).

        Raises:
            PathEscapeError: when the resolved path is outside the workspace.
        """
        if self._closed:
            raise RuntimeError(
                f"workspace {self._id!r} is closed; cannot resolve paths"
            )
        req = str(requested)
        candidate = Path(req)
        if not candidate.is_absolute():
            candidate = self._path / candidate
        # ``resolve(strict=False)`` follows symlinks where the target
        # exists and normalises ``..`` even when it doesn't. Combined
        # with the ``commonpath`` check below it catches both
        # traversal and symlink escapes.
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise PathEscapeError(
                requested=req,
                resolved=str(candidate),
                workspace_root=str(self._path),
            ) from exc

        try:
            common = Path(os.path.commonpath([str(self._path), str(resolved)]))
        except ValueError:
            # commonpath raises when paths are on different drives (Win)
            raise PathEscapeError(
                requested=req,
                resolved=str(resolved),
                workspace_root=str(self._path),
            ) from None
        if common != self._path:
            raise PathEscapeError(
                requested=req,
                resolved=str(resolved),
                workspace_root=str(self._path),
            )
        return resolved

    def relative(self, abs_path: Path) -> str:
        """Render an absolute path as a workspace-rooted POSIX string.

        Used by tools that surface paths back to the LLM — agents
        should never see host paths, only workspace-relative ones.
        """
        try:
            return abs_path.resolve().relative_to(self._path).as_posix()
        except ValueError:
            raise PathEscapeError(
                requested=str(abs_path),
                resolved=str(abs_path),
                workspace_root=str(self._path),
            ) from None

    # ---- lifecycle ----

    def cleanup(self) -> None:
        """Delete the workspace dir (no-op if ``cleanup_after`` is False).

        Idempotent — calling twice is safe.
        """
        if self._closed:
            return
        self._closed = True
        if not self._cleanup_after:
            logger.info(
                "Workspace %s closed without cleanup (path=%s)",
                self._id, self._path,
            )
            return
        try:
            shutil.rmtree(self._path, ignore_errors=False)
        except OSError as exc:
            # On Windows long-path / locked-file scenarios, retry with
            # ignore_errors so we don't leak the workspace forever.
            logger.warning(
                "Workspace %s cleanup failed (%s); retrying with ignore_errors",
                self._id, exc,
            )
            shutil.rmtree(self._path, ignore_errors=True)

    def __enter__(self) -> "Workspace":
        return self

    def __exit__(
        self,
        exc_type: Optional[type],
        exc: Optional[BaseException],
        tb: Optional[Any],
    ) -> None:
        self.cleanup()

    def __repr__(self) -> str:
        return (
            f"Workspace(id={self._id!r}, path={str(self._path)!r}, "
            f"closed={self._closed})"
        )


def make_workspace(
    *,
    caps: Optional[WorkspaceCaps] = None,
    session_id: str = "",
    root: Optional[str] = None,
    cleanup_after: bool = True,
) -> Workspace:
    """Build a fresh :class:`Workspace`.

    Creates ``<root>/<workspace_id>/`` (and its parents if needed),
    returns a Workspace bound to it.

    Args:
        caps: Capability set. Defaults to safe (fs RW only, no
            network / exec).
        session_id: Optional chat-session id for traceability — does
            not affect path layout.
        root: Override the workspace root dir. Defaults to
            ``ANILA_WORKSPACE_ROOT`` env var, or platform tempdir +
            ``anila-workspaces``.
        cleanup_after: When True (default) ``Workspace.cleanup()``
            deletes the dir. Set False for post-mortem inspection.
    """
    workspace_id = _new_workspace_id()
    base = Path(root or DEFAULT_WORKSPACE_ROOT).resolve()
    base.mkdir(parents=True, exist_ok=True)
    ws_path = base / workspace_id
    ws_path.mkdir(parents=True, exist_ok=False)
    return Workspace(
        path=ws_path,
        workspace_id=workspace_id,
        caps=caps or WorkspaceCaps(),
        session_id=session_id,
        cleanup_after=cleanup_after,
    )
