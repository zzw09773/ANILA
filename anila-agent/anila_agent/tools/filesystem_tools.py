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


@anila_tool(
    is_read_only=True,
    category="filesystem",
    cost_estimate="free",
)
def read_file(path: str, max_bytes: int = 32_000) -> str:
    """讀取 workdir 內的 UTF-8 文字檔。

    Args:
        path: 檔案路徑;相對路徑會解析至 workdir 底下。
        max_bytes: 截斷上限,預設 32KB。

    Returns:
        檔案內容;若超過 max_bytes 會在尾端標註 truncated。
    """
    target = _resolve_within_workdir(path)
    raw = target.read_bytes()
    if len(raw) > max_bytes:
        return raw[:max_bytes].decode("utf-8", errors="replace") + "\n... [truncated]"
    return raw.decode("utf-8", errors="replace")


@anila_tool(
    is_read_only=True,
    category="filesystem",
    cost_estimate="free",
)
def list_dir(path: str = ".", max_entries: int = 200) -> list[dict[str, str]]:
    """列出 workdir 內的目錄。

    最多回傳 `max_entries` 筆 {name, kind} dict;kind 為 'file' 或 'dir'。
    Symlink 以其指向目標的 kind 標示。
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
