"""``apply_patch`` tool — V4A-style multi-file patch applier.

Why this format and not standard unified diff: LLMs are bad at
producing accurate ``@@ -123,4 +123,5 @@`` line numbers but very good
at producing context blocks. The V4A envelope (used by Claude Code
and openai-agents' apply_patch) drops line numbers entirely — each
hunk just shows the unique surrounding context plus ``-``/``+`` lines,
and the applier finds the unique match in the file.

Format::

    *** Begin Patch
    *** Update File: src/foo.py
    @@
     unchanged context line
    -old line to remove
    +new line to add
     more context
    *** End Patch

Operations:

- ``*** Add File: <path>`` — body is ``+`` lines containing the new
  file content. File must not already exist.
- ``*** Delete File: <path>`` — no body. File must exist.
- ``*** Update File: <path>`` — body is one or more ``@@``-delimited
  hunks. Each hunk's "before" (context + ``-`` lines) is replaced
  with "after" (context + ``+`` lines). The before block must occur
  exactly once in the file; otherwise the hunk is rejected with a
  helpful error so the LLM can re-emit with more context.

All paths resolve through :meth:`Workspace.safe_path` — escapes
return an error result, never modify the host filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from ..models.tool import ToolDefinition, ToolPermission, ToolSafety
from ..workspace import PathEscapeError, Workspace


PatchOpType = Literal["add", "update", "delete"]


@dataclass
class _Hunk:
    """A single ``@@`` block inside an Update operation."""

    before_lines: list[str] = field(default_factory=list)
    after_lines: list[str] = field(default_factory=list)


@dataclass
class _PatchOperation:
    op_type: PatchOpType
    path: str
    hunks: list[_Hunk] = field(default_factory=list)
    add_content: str = ""  # only used for op_type == "add"


class PatchParseError(ValueError):
    """Raised when the patch envelope is malformed."""


class PatchApplyError(RuntimeError):
    """Raised when an operation can't be applied (file missing, hunk
    context not found, etc.)."""


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


_BEGIN = "*** Begin Patch"
_END = "*** End Patch"
_ADD = "*** Add File: "
_UPDATE = "*** Update File: "
_DELETE = "*** Delete File: "


def parse_patch(patch_text: str) -> list[_PatchOperation]:
    """Parse a patch envelope into structured operations.

    Tolerates leading / trailing blank lines outside the envelope but
    requires the ``Begin Patch`` / ``End Patch`` markers.
    """
    if not patch_text or _BEGIN not in patch_text or _END not in patch_text:
        raise PatchParseError(
            "patch is missing '*** Begin Patch' / '*** End Patch' markers"
        )
    # Slice between the markers; keep raw line splits.
    body = patch_text.split(_BEGIN, 1)[1].split(_END, 1)[0]
    lines = body.splitlines()

    ops: list[_PatchOperation] = []
    current: _PatchOperation | None = None
    current_hunk: _Hunk | None = None
    add_buf: list[str] = []

    def _commit_current() -> None:
        nonlocal current, current_hunk, add_buf
        if current is None:
            return
        if current.op_type == "update" and current_hunk is not None:
            current.hunks.append(current_hunk)
        if current.op_type == "add":
            current.add_content = "\n".join(add_buf)
            # Preserve trailing newline if the LLM provided one.
            if add_buf and not current.add_content.endswith("\n"):
                # No-op — we won't auto-append.
                pass
        ops.append(current)
        current = None
        current_hunk = None
        add_buf = []

    for raw in lines:
        line = raw.rstrip("\r")  # tolerate CRLF
        # Skip leading whitespace-only lines before any operation.
        if current is None and not line.strip():
            continue

        if line.startswith(_ADD):
            _commit_current()
            current = _PatchOperation(op_type="add", path=line[len(_ADD):].strip())
            continue
        if line.startswith(_UPDATE):
            _commit_current()
            current = _PatchOperation(
                op_type="update", path=line[len(_UPDATE):].strip()
            )
            current_hunk = None
            continue
        if line.startswith(_DELETE):
            _commit_current()
            current = _PatchOperation(
                op_type="delete", path=line[len(_DELETE):].strip()
            )
            continue

        if current is None:
            # Stray content between operations.
            if line.strip():
                raise PatchParseError(
                    f"unexpected content outside any operation: {line!r}"
                )
            continue

        if current.op_type == "add":
            if line.startswith("+"):
                add_buf.append(line[1:])
            elif not line.strip():
                add_buf.append("")
            else:
                raise PatchParseError(
                    f"Add File body lines must start with '+': {line!r}"
                )
            continue

        if current.op_type == "update":
            if line.startswith("@@"):
                if current_hunk is not None:
                    current.hunks.append(current_hunk)
                current_hunk = _Hunk()
                continue
            if current_hunk is None:
                # Tolerate blank lines between Update header and first @@.
                if not line.strip():
                    continue
                raise PatchParseError(
                    f"Update File body must begin with '@@': {line!r}"
                )
            if line.startswith("-"):
                current_hunk.before_lines.append(line[1:])
            elif line.startswith("+"):
                current_hunk.after_lines.append(line[1:])
            elif line.startswith(" "):
                # Context line — present in both before and after.
                ctx = line[1:]
                current_hunk.before_lines.append(ctx)
                current_hunk.after_lines.append(ctx)
            elif line == "":
                # Blank line is context (preserve in both sides).
                current_hunk.before_lines.append("")
                current_hunk.after_lines.append("")
            else:
                raise PatchParseError(
                    f"unrecognised line inside hunk: {line!r}"
                )
            continue

        # Delete: no body allowed.
        if current.op_type == "delete":
            if line.strip():
                raise PatchParseError(
                    f"Delete File takes no body: {line!r}"
                )
            continue

    _commit_current()
    if not ops:
        raise PatchParseError("patch contained no operations")
    return ops


# ---------------------------------------------------------------------------
# Applier
# ---------------------------------------------------------------------------


def _apply_hunk(content: str, hunk: _Hunk) -> str:
    """Apply one hunk to ``content``. Raises :class:`PatchApplyError`
    when the before block isn't an exact unique match."""
    before = "\n".join(hunk.before_lines)
    after = "\n".join(hunk.after_lines)
    if before == after:
        # No-op hunk: skip silently rather than fail.
        return content
    occurrences = content.count(before)
    if occurrences == 0:
        raise PatchApplyError(
            "hunk context not found in file; ensure the surrounding "
            "context lines (` ` prefix) are present and indentation matches"
        )
    if occurrences > 1:
        raise PatchApplyError(
            f"hunk context occurs {occurrences} times — add more "
            "surrounding context lines to disambiguate"
        )
    return content.replace(before, after, 1)


def apply_patch(workspace: Workspace, patch_text: str) -> str:
    """Apply a parsed patch to ``workspace``. Returns a summary string.

    Failures during apply (missing file, hunk mismatch, path escape)
    raise :class:`PatchApplyError` with the operation context. The
    workspace state may be partially mutated when a later operation
    fails — atomicity across operations is left to the caller (which
    can inspect the workspace after a failed apply).
    """
    ops = parse_patch(patch_text)
    summary_lines: list[str] = []

    for idx, op in enumerate(ops, start=1):
        try:
            target = workspace.safe_path(op.path)
        except PathEscapeError as exc:
            raise PatchApplyError(
                f"operation {idx} ({op.op_type} {op.path!r}): {exc}"
            ) from exc

        if op.op_type == "add":
            if target.exists():
                raise PatchApplyError(
                    f"operation {idx}: cannot Add File {op.path!r} — already exists"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(op.add_content, encoding="utf-8")
            summary_lines.append(
                f"added  {op.path} ({len(op.add_content)} chars)"
            )
            continue

        if op.op_type == "delete":
            if not target.exists():
                raise PatchApplyError(
                    f"operation {idx}: cannot Delete File {op.path!r} — does not exist"
                )
            if not target.is_file():
                raise PatchApplyError(
                    f"operation {idx}: {op.path!r} is not a regular file"
                )
            target.unlink()
            summary_lines.append(f"deleted {op.path}")
            continue

        # update
        if not target.is_file():
            raise PatchApplyError(
                f"operation {idx}: cannot Update File {op.path!r} — not a regular file"
            )
        try:
            current = target.read_text(encoding="utf-8")
        except OSError as exc:
            raise PatchApplyError(
                f"operation {idx}: read {op.path!r} failed: {exc}"
            ) from exc
        updated = current
        for hunk_idx, hunk in enumerate(op.hunks, start=1):
            try:
                updated = _apply_hunk(updated, hunk)
            except PatchApplyError as exc:
                raise PatchApplyError(
                    f"operation {idx} hunk {hunk_idx} ({op.path!r}): {exc}"
                ) from exc
        target.write_text(updated, encoding="utf-8")
        summary_lines.append(
            f"updated {op.path} ({len(op.hunks)} hunk"
            f"{'s' if len(op.hunks) != 1 else ''})"
        )

    return "\n".join(summary_lines) if summary_lines else "(no changes)"


# ---------------------------------------------------------------------------
# Tool factory
# ---------------------------------------------------------------------------


def apply_patch_tool(workspace: Workspace) -> ToolDefinition:
    """Apply a multi-file V4A-style patch to ``workspace``.

    The tool returns a one-line-per-op summary on success. Failures
    surface as ``ToolResult(is_error=True)`` with the failing op index
    + reason so the LLM can fix and re-emit.
    """

    async def _impl(input: dict[str, Any], **_: Any) -> str:
        if not workspace.caps.fs_write:
            return "apply_patch error: capability fs_write denied"
        patch_text = input.get("patch", "")
        if not isinstance(patch_text, str) or not patch_text.strip():
            return "apply_patch error: 'patch' is required (V4A envelope)"
        try:
            return apply_patch(workspace, patch_text)
        except PatchParseError as exc:
            return f"apply_patch parse error: {exc}"
        except PatchApplyError as exc:
            return f"apply_patch error: {exc}"

    return ToolDefinition(
        name="apply_patch",
        description=(
            "Apply a multi-file V4A-style patch to the workspace. "
            "Envelope: '*** Begin Patch' / '*** End Patch' wrapping "
            "'*** Add File: <path>', '*** Update File: <path>' (with "
            "'@@' hunks: ' '/'-'/'+' lines), or '*** Delete File: "
            "<path>'. Hunks need surrounding context to disambiguate "
            "the match — add more ' ' lines if you see "
            "'occurs N times'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "patch": {
                    "type": "string",
                    "description": (
                        "Full V4A patch envelope. See the tool "
                        "description for format."
                    ),
                },
            },
            "required": ["patch"],
        },
        safety=ToolSafety.DESTRUCTIVE,
        permission=ToolPermission.ALLOW,
        implementation=_impl,
    )


__all__ = [
    "apply_patch_tool",
    "apply_patch",
    "parse_patch",
    "PatchParseError",
    "PatchApplyError",
]
