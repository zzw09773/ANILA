"""Read-only filesystem tools. Sandbox to a configured working directory.

Coworkers can extend this module with write/edit tools for their use case.
The defaults are read-only on purpose: write semantics interact with hook
permissions and confirmation flows that depend on the host environment.
"""

from __future__ import annotations

import os
from pathlib import Path

from anila_agent.tools.base import anila_tool

_workdir: Path = Path.cwd()


def set_workdir(path: str | Path) -> None:
    global _workdir
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise ValueError(f"workdir must be an existing directory: {resolved}")
    _workdir = resolved


def get_workdir() -> Path:
    return _workdir


def _resolve_within_workdir(path: str) -> Path:
    candidate = (_workdir / path).resolve() if not os.path.isabs(path) else Path(path).resolve()
    try:
        candidate.relative_to(_workdir)
    except ValueError as e:
        raise PermissionError(f"path escapes workdir {_workdir}: {candidate}") from e
    return candidate


@anila_tool(is_read_only=True, category="filesystem")
def read_file(path: str, max_bytes: int = 32_000) -> str:
    """Read a UTF-8 text file relative to the configured workdir.

    Args:
        path: File path; relative paths resolve against the workdir.
        max_bytes: Truncate after this many bytes. Default 32KB.

    Returns:
        The file content as text. Truncation is marked at the end.
    """
    target = _resolve_within_workdir(path)
    raw = target.read_bytes()
    if len(raw) > max_bytes:
        return raw[:max_bytes].decode("utf-8", errors="replace") + "\n... [truncated]"
    return raw.decode("utf-8", errors="replace")


@anila_tool(is_read_only=True, category="filesystem")
def list_dir(path: str = ".", max_entries: int = 200) -> list[dict[str, str]]:
    """List a directory relative to the workdir.

    Returns up to `max_entries` items as {name, kind} dicts where kind is
    'file' or 'dir'. Symlinks are reported as the kind they point at.
    """
    target = _resolve_within_workdir(path)
    if not target.is_dir():
        raise NotADirectoryError(str(target))
    out: list[dict[str, str]] = []
    for child in sorted(target.iterdir()):
        kind = "dir" if child.is_dir() else "file"
        out.append({"name": child.name, "kind": kind})
        if len(out) >= max_entries:
            break
    return out
