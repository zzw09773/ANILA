"""Workspace-scoped file tools (Sprint 12 PR 2).

Five factories — :func:`file_read_tool` / :func:`file_write_tool` /
:func:`file_edit_tool` / :func:`glob_tool` / :func:`grep_tool` — each
takes a :class:`Workspace` and returns a :class:`ToolDefinition` whose
implementation routes every path operation through
:func:`Workspace.safe_path`. Path escapes are caught by the workspace
layer; cap denials surface as ``ToolResult(is_error=True)``.

Output sizing: tool replies are short — the model often cycles through
many file calls, so we cap result text and surface a "...truncated"
suffix rather than firing the entire file at the LLM.

Wire-up::

    ws = make_workspace(caps=WorkspaceCaps(fs_read=True, fs_write=True))
    registry = ToolRegistry()
    registry.register(file_read_tool(ws))
    registry.register(file_write_tool(ws))
    # ...
"""

from __future__ import annotations

import re
from typing import Any

from ..models.tool import ToolDefinition, ToolPermission, ToolSafety
from ..workspace import PathEscapeError, Workspace


_DEFAULT_LIMIT = 2000  # default line limit for file_read
_MAX_GLOB_RESULTS = 250
_MAX_GREP_RESULTS = 250
_MAX_GREP_LINE_PREVIEW = 240
_TRUNCATED_SUFFIX = "\n\n[…truncated]"


def _check_size(workspace: Workspace, new_bytes: int) -> str | None:
    """Return an error message if writing ``new_bytes`` would breach the
    workspace size cap; return None if OK.
    """
    cap_bytes = workspace.caps.max_workspace_size_mb * 1024 * 1024
    if cap_bytes <= 0:
        return None
    used = sum(
        f.stat().st_size for f in workspace.path.rglob("*") if f.is_file()
    )
    if used + new_bytes > cap_bytes:
        return (
            f"workspace size cap exceeded "
            f"({(used + new_bytes) / 1024 / 1024:.1f} MB > "
            f"{workspace.caps.max_workspace_size_mb} MB)"
        )
    return None


# ---------------------------------------------------------------------------
# file_read
# ---------------------------------------------------------------------------


def file_read_tool(workspace: Workspace) -> ToolDefinition:
    """Read a file inside ``workspace``. Line-numbered output."""

    async def _impl(input: dict[str, Any], **_: Any) -> str:
        if not workspace.caps.fs_read:
            return "file_read error: capability fs_read denied"
        rel = str(input.get("path", "")).strip()
        if not rel:
            return "file_read error: 'path' is required"
        offset = int(input.get("offset", 0) or 0)
        limit = int(input.get("limit", _DEFAULT_LIMIT) or _DEFAULT_LIMIT)
        try:
            target = workspace.safe_path(rel)
        except PathEscapeError as exc:
            return f"file_read error: {exc}"
        if not target.exists():
            return f"file_read error: {workspace.relative(target)} does not exist"
        if not target.is_file():
            return (
                f"file_read error: {workspace.relative(target)} is not a regular file"
            )
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"file_read error: {exc}"
        lines = text.splitlines()
        sliced = lines[offset : offset + limit]
        # Render with cat -n style numbering (1-indexed line numbers).
        out_lines = [
            f"{(offset + i + 1):>6}\t{line}"
            for i, line in enumerate(sliced)
        ]
        if offset + limit < len(lines):
            out_lines.append(f"\n[…{len(lines) - (offset + limit)} more lines]")
        return "\n".join(out_lines) if out_lines else "(empty file)"

    return ToolDefinition(
        name="file_read",
        description=(
            "Read a file inside the agent's workspace. Output is "
            "line-numbered (1-indexed) for easy referencing in edits. "
            "Use offset + limit for files larger than 2000 lines."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative path.",
                },
                "offset": {
                    "type": "number",
                    "description": "Skip this many lines (default 0).",
                },
                "limit": {
                    "type": "number",
                    "description": (
                        f"Max lines to return (default {_DEFAULT_LIMIT})."
                    ),
                },
            },
            "required": ["path"],
        },
        safety=ToolSafety.READ_ONLY,
        permission=ToolPermission.ALLOW,
        implementation=_impl,
    )


# ---------------------------------------------------------------------------
# file_write
# ---------------------------------------------------------------------------


def file_write_tool(workspace: Workspace) -> ToolDefinition:
    """Write (or create) a file inside ``workspace`` — overwrites on conflict."""

    async def _impl(input: dict[str, Any], **_: Any) -> str:
        if not workspace.caps.fs_write:
            return "file_write error: capability fs_write denied"
        rel = str(input.get("path", "")).strip()
        if not rel:
            return "file_write error: 'path' is required"
        content = input.get("content", "")
        if not isinstance(content, str):
            return "file_write error: 'content' must be a string"
        try:
            target = workspace.safe_path(rel)
        except PathEscapeError as exc:
            return f"file_write error: {exc}"
        size_err = _check_size(workspace, len(content.encode("utf-8")))
        if size_err is not None:
            return f"file_write error: {size_err}"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            return f"file_write error: {exc}"
        return f"wrote {workspace.relative(target)} ({len(content)} chars)"

    return ToolDefinition(
        name="file_write",
        description=(
            "Write content to a file inside the workspace. Overwrites if "
            "the file exists; creates parent directories as needed. Use "
            "file_edit for in-place modifications instead of rewriting."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
        safety=ToolSafety.DESTRUCTIVE,
        permission=ToolPermission.ALLOW,
        implementation=_impl,
    )


# ---------------------------------------------------------------------------
# file_edit — string replacement
# ---------------------------------------------------------------------------


def file_edit_tool(workspace: Workspace) -> ToolDefinition:
    """In-place string replacement; refuses to silently double-edit."""

    async def _impl(input: dict[str, Any], **_: Any) -> str:
        if not workspace.caps.fs_write:
            return "file_edit error: capability fs_write denied"
        rel = str(input.get("path", "")).strip()
        if not rel:
            return "file_edit error: 'path' is required"
        old = input.get("old_string", "")
        new = input.get("new_string", "")
        replace_all = bool(input.get("replace_all", False))
        if not isinstance(old, str) or not isinstance(new, str):
            return "file_edit error: old_string / new_string must be strings"
        if old == "":
            return "file_edit error: old_string must be non-empty"
        if old == new:
            return "file_edit error: old_string and new_string are identical"
        try:
            target = workspace.safe_path(rel)
        except PathEscapeError as exc:
            return f"file_edit error: {exc}"
        if not target.is_file():
            return f"file_edit error: {workspace.relative(target)} is not a regular file"
        try:
            current = target.read_text(encoding="utf-8")
        except OSError as exc:
            return f"file_edit error: {exc}"
        occurrences = current.count(old)
        if occurrences == 0:
            return (
                f"file_edit error: old_string not found in "
                f"{workspace.relative(target)}"
            )
        if occurrences > 1 and not replace_all:
            return (
                f"file_edit error: old_string occurs {occurrences} times in "
                f"{workspace.relative(target)}; pass replace_all=true or use "
                "a more specific old_string"
            )
        updated = current.replace(old, new) if replace_all else current.replace(
            old, new, 1
        )
        size_delta = len(updated.encode("utf-8")) - len(current.encode("utf-8"))
        if size_delta > 0:
            err = _check_size(workspace, size_delta)
            if err is not None:
                return f"file_edit error: {err}"
        try:
            target.write_text(updated, encoding="utf-8")
        except OSError as exc:
            return f"file_edit error: {exc}"
        replaced = occurrences if replace_all else 1
        return (
            f"edited {workspace.relative(target)} "
            f"({replaced} replacement{'s' if replaced != 1 else ''})"
        )

    return ToolDefinition(
        name="file_edit",
        description=(
            "Replace exact string occurrences in a workspace file. By "
            "default refuses if old_string occurs more than once — pass "
            "replace_all=true to override, or supply a more specific "
            "old_string. Indentation must match the file exactly."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {
                    "type": "string",
                    "description": "Exact text to replace (must be unique).",
                },
                "new_string": {"type": "string"},
                "replace_all": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Replace every occurrence rather than refusing on "
                        "duplicate matches."
                    ),
                },
            },
            "required": ["path", "old_string", "new_string"],
        },
        safety=ToolSafety.DESTRUCTIVE,
        permission=ToolPermission.ALLOW,
        implementation=_impl,
    )


# ---------------------------------------------------------------------------
# glob
# ---------------------------------------------------------------------------


def glob_tool(workspace: Workspace) -> ToolDefinition:
    """Glob-pattern file lookup inside ``workspace``."""

    async def _impl(input: dict[str, Any], **_: Any) -> str:
        if not workspace.caps.fs_read:
            return "glob error: capability fs_read denied"
        pattern = str(input.get("pattern", "")).strip()
        if not pattern:
            return "glob error: 'pattern' is required"
        try:
            matches = sorted(workspace.path.glob(pattern))
        except OSError as exc:
            return f"glob error: {exc}"
        results: list[str] = []
        for match in matches:
            try:
                results.append(workspace.relative(match))
            except PathEscapeError:
                # Defensive — glob shouldn't produce escapes but skip if it did.
                continue
            if len(results) >= _MAX_GLOB_RESULTS:
                break
        if not results:
            return f"(no matches for {pattern!r})"
        body = "\n".join(results)
        if len(matches) > _MAX_GLOB_RESULTS:
            body += f"\n\n[…{len(matches) - _MAX_GLOB_RESULTS} more matches]"
        return body

    return ToolDefinition(
        name="glob",
        description=(
            "Find files inside the workspace matching a glob pattern "
            "(e.g. '**/*.py', 'src/**/*.{ts,tsx}'). Returns paths "
            "sorted lexicographically, capped at 250 results."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern relative to workspace root.",
                },
            },
            "required": ["pattern"],
        },
        safety=ToolSafety.CONCURRENCY_SAFE,
        permission=ToolPermission.ALLOW,
        implementation=_impl,
    )


# ---------------------------------------------------------------------------
# grep
# ---------------------------------------------------------------------------


def grep_tool(workspace: Workspace) -> ToolDefinition:
    """Regex search across workspace files."""

    async def _impl(input: dict[str, Any], **_: Any) -> str:
        if not workspace.caps.fs_read:
            return "grep error: capability fs_read denied"
        pattern_str = str(input.get("pattern", "")).strip()
        if not pattern_str:
            return "grep error: 'pattern' is required"
        path_arg = str(input.get("path", "") or "").strip()
        glob_arg = str(input.get("glob", "") or "").strip()
        output_mode = str(
            input.get("output_mode", "files_with_matches")
        ).strip()
        ignore_case = bool(input.get("-i", False))

        flags = re.IGNORECASE if ignore_case else 0
        try:
            regex = re.compile(pattern_str, flags)
        except re.error as exc:
            return f"grep error: invalid regex: {exc}"

        if path_arg:
            try:
                base = workspace.safe_path(path_arg)
            except PathEscapeError as exc:
                return f"grep error: {exc}"
        else:
            base = workspace.path
        if not base.exists():
            return f"grep error: {workspace.relative(base)} does not exist"

        if base.is_file():
            candidates = [base]
        else:
            candidates = sorted(
                p for p in base.rglob(glob_arg or "*")
                if p.is_file()
            )

        files_with_match: list[str] = []
        content_hits: list[str] = []
        for f in candidates:
            try:
                rel = workspace.relative(f)
            except PathEscapeError:
                continue
            try:
                # Read in text mode with replace so binary files don't crash.
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            matched_lines: list[tuple[int, str]] = []
            for i, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matched_lines.append((i, line))
                    if len(content_hits) + len(matched_lines) >= _MAX_GREP_RESULTS:
                        break
            if matched_lines:
                files_with_match.append(rel)
                if output_mode == "content":
                    for ln, txt in matched_lines:
                        preview = txt
                        if len(preview) > _MAX_GREP_LINE_PREVIEW:
                            preview = (
                                preview[:_MAX_GREP_LINE_PREVIEW] + "…"
                            )
                        content_hits.append(f"{rel}:{ln}:{preview}")
            if (
                output_mode == "files_with_matches"
                and len(files_with_match) >= _MAX_GREP_RESULTS
            ):
                break

        if output_mode == "content":
            if not content_hits:
                return f"(no content matches for /{pattern_str}/)"
            body = "\n".join(content_hits)
            if len(content_hits) >= _MAX_GREP_RESULTS:
                body += _TRUNCATED_SUFFIX
            return body
        # files_with_matches (default)
        if not files_with_match:
            return f"(no files match /{pattern_str}/)"
        body = "\n".join(files_with_match)
        if len(files_with_match) >= _MAX_GREP_RESULTS:
            body += _TRUNCATED_SUFFIX
        return body

    return ToolDefinition(
        name="grep",
        description=(
            "Regex search inside the workspace. Default returns "
            "matching file paths; pass output_mode='content' to get "
            "file:line:text hits. Restrict with 'path' (file or "
            "subdir) and/or 'glob' (e.g. '*.py'). Capped at 250 hits."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Python regex pattern.",
                },
                "path": {
                    "type": "string",
                    "description": "Optional file or subdir to scope.",
                },
                "glob": {
                    "type": "string",
                    "description": "Optional glob filter (e.g. '*.py').",
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["files_with_matches", "content"],
                    "default": "files_with_matches",
                },
                "-i": {
                    "type": "boolean",
                    "description": "Case-insensitive match.",
                    "default": False,
                },
            },
            "required": ["pattern"],
        },
        safety=ToolSafety.CONCURRENCY_SAFE,
        permission=ToolPermission.ALLOW,
        implementation=_impl,
    )


def all_file_tools(workspace: Workspace) -> list[ToolDefinition]:
    """Convenience: all five file tools for a workspace.

    Pass directly into the agent factory's ToolRegistry::

        for t in all_file_tools(ws):
            registry.register(t)
    """
    return [
        file_read_tool(workspace),
        file_write_tool(workspace),
        file_edit_tool(workspace),
        glob_tool(workspace),
        grep_tool(workspace),
    ]


__all__ = [
    "file_read_tool",
    "file_write_tool",
    "file_edit_tool",
    "glob_tool",
    "grep_tool",
    "all_file_tools",
]
