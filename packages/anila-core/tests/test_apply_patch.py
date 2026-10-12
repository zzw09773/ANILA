"""Tests for the V4A-style apply_patch tool (Sprint 12 PR 4)."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

from anila_core.tools.apply_patch import (
    PatchApplyError,
    PatchParseError,
    apply_patch,
    apply_patch_tool,
    parse_patch,
)
from anila_core.workspace import WorkspaceCaps, make_workspace


@pytest.fixture
def ws(tmp_path: Path) -> Iterator:
    workspace = make_workspace(root=str(tmp_path))
    yield workspace
    workspace.cleanup()


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "match, parse_patch_arg",
    [
        pytest.param("markers", "just some text", id='parse_rejects_missing_envelope'),
        pytest.param("no operations", "*** Begin Patch\n*** End Patch\n", id='parse_empty_envelope_raises'),
    ],
)
def test_parse_rejects_missing_envelope_cases(match, parse_patch_arg) -> None:
    with pytest.raises(PatchParseError, match=match):
        parse_patch(parse_patch_arg)


def test_parse_add_file() -> None:
    body = (
        "*** Begin Patch\n"
        "*** Add File: hello.txt\n"
        "+line one\n"
        "+line two\n"
        "*** End Patch\n"
    )
    [op] = parse_patch(body)
    assert op.op_type == "add"
    assert op.path == "hello.txt"
    assert op.add_content == "line one\nline two"


def test_parse_delete_file() -> None:
    body = (
        "*** Begin Patch\n"
        "*** Delete File: stale.txt\n"
        "*** End Patch\n"
    )
    [op] = parse_patch(body)
    assert op.op_type == "delete"
    assert op.path == "stale.txt"


def test_parse_update_file_single_hunk() -> None:
    body = (
        "*** Begin Patch\n"
        "*** Update File: src/x.py\n"
        "@@\n"
        " context line\n"
        "-old\n"
        "+new\n"
        "*** End Patch\n"
    )
    [op] = parse_patch(body)
    assert op.op_type == "update"
    assert op.path == "src/x.py"
    assert len(op.hunks) == 1
    h = op.hunks[0]
    assert h.before_lines == ["context line", "old"]
    assert h.after_lines == ["context line", "new"]


def test_parse_update_file_multiple_hunks() -> None:
    body = (
        "*** Begin Patch\n"
        "*** Update File: x\n"
        "@@\n"
        "-a\n"
        "+A\n"
        "@@\n"
        "-b\n"
        "+B\n"
        "*** End Patch\n"
    )
    [op] = parse_patch(body)
    assert len(op.hunks) == 2


def test_parse_multiple_operations() -> None:
    body = (
        "*** Begin Patch\n"
        "*** Add File: new.txt\n"
        "+hi\n"
        "*** Delete File: old.txt\n"
        "*** Update File: src.py\n"
        "@@\n"
        "-x\n"
        "+y\n"
        "*** End Patch\n"
    )
    ops = parse_patch(body)
    assert [op.op_type for op in ops] == ["add", "delete", "update"]


@pytest.mark.parametrize(
    "body, match",
    [
        pytest.param('*** Begin Patch\n*** Add File: x\n+ok\nno plus\n*** End Patch\n', "must start with '\\+'", id='parse_add_rejects_non_plus_lines'),
        pytest.param('*** Begin Patch\n*** Delete File: x\nstray content\n*** End Patch\n', "takes no body", id='parse_delete_rejects_body'),
        pytest.param('*** Begin Patch\n*** Update File: x\n-no preceding @@\n*** End Patch\n', "begin with '@@'", id='parse_update_must_start_with_at_at'),
    ],
)
def test_parse_add_rejects_non_plus_lines_cases(body, match) -> None:
    with pytest.raises(PatchParseError, match=match):
        parse_patch(body)


def test_parse_tolerates_crlf_line_endings() -> None:
    body = (
        "*** Begin Patch\r\n"
        "*** Add File: x\r\n"
        "+hi\r\n"
        "*** End Patch\r\n"
    )
    [op] = parse_patch(body)
    assert op.add_content == "hi"


# ---------------------------------------------------------------------------
# apply_patch — Add
# ---------------------------------------------------------------------------


def test_add_new_file(ws) -> None:
    body = (
        "*** Begin Patch\n"
        "*** Add File: greet.txt\n"
        "+hello\n"
        "+world\n"
        "*** End Patch\n"
    )
    summary = apply_patch(ws, body)
    assert "added" in summary and "greet.txt" in summary
    assert (ws.path / "greet.txt").read_text() == "hello\nworld"


def test_add_creates_parent_dirs(ws) -> None:
    body = (
        "*** Begin Patch\n"
        "*** Add File: deep/nested/file.txt\n"
        "+content\n"
        "*** End Patch\n"
    )
    apply_patch(ws, body)
    assert (ws.path / "deep/nested/file.txt").read_text() == "content"


@pytest.mark.parametrize(
    "write_text_arg, body, match",
    [
        pytest.param("already here", '*** Begin Patch\n*** Add File: x\n+new\n*** End Patch\n', "already exists", id='add_refuses_existing_file'),
        pytest.param("foo\nfoo\nfoo\n", '*** Begin Patch\n*** Update File: x\n@@\n-foo\n+bar\n*** End Patch\n', "occurs 3 times", id='update_rejects_when_context_not_unique'),
        pytest.param("hello world\n", '*** Begin Patch\n*** Update File: x\n@@\n-not present\n+something\n*** End Patch\n', "context not found", id='update_rejects_when_context_missing'),
    ],
)
def test_add_refuses_existing_file_cases(write_text_arg, body, match, ws) -> None:
    (ws.path / "x").write_text(write_text_arg)
    with pytest.raises(PatchApplyError, match=match):
        apply_patch(ws, body)


# ---------------------------------------------------------------------------
# apply_patch — Delete
# ---------------------------------------------------------------------------


def test_delete_existing_file(ws) -> None:
    (ws.path / "stale").write_text("bye")
    body = (
        "*** Begin Patch\n"
        "*** Delete File: stale\n"
        "*** End Patch\n"
    )
    summary = apply_patch(ws, body)
    assert "deleted" in summary
    assert not (ws.path / "stale").exists()


@pytest.mark.parametrize(
    "body, match",
    [
        pytest.param('*** Begin Patch\n*** Delete File: ghost\n*** End Patch\n', "does not exist", id='delete_missing_file_errors'),
        pytest.param('*** Begin Patch\n*** Update File: ghost\n@@\n-x\n+y\n*** End Patch\n', "not a regular file", id='update_missing_file_errors'),
        pytest.param('*** Begin Patch\n*** Add File: ../../etc/oops\n+boom\n*** End Patch\n', "outside", id='path_escape_rejected'),
    ],
)
def test_delete_missing_file_errors_cases(body, match, ws) -> None:
    with pytest.raises(PatchApplyError, match=match):
        apply_patch(ws, body)


# ---------------------------------------------------------------------------
# apply_patch — Update
# ---------------------------------------------------------------------------


def test_update_simple_replacement(ws) -> None:
    (ws.path / "x.py").write_text(
        "def add(a, b):\n    return a + b\n"
    )
    body = (
        "*** Begin Patch\n"
        "*** Update File: x.py\n"
        "@@\n"
        " def add(a, b):\n"
        "-    return a + b\n"
        "+    return a - b\n"
        "*** End Patch\n"
    )
    apply_patch(ws, body)
    assert (ws.path / "x.py").read_text() == (
        "def add(a, b):\n    return a - b\n"
    )


def test_update_multiple_hunks(ws) -> None:
    (ws.path / "x").write_text("alpha\nbeta\ngamma\n")
    body = (
        "*** Begin Patch\n"
        "*** Update File: x\n"
        "@@\n"
        "-alpha\n"
        "+ALPHA\n"
        "@@\n"
        "-gamma\n"
        "+GAMMA\n"
        "*** End Patch\n"
    )
    apply_patch(ws, body)
    assert (ws.path / "x").read_text() == "ALPHA\nbeta\nGAMMA\n"


def test_update_with_surrounding_context_disambiguates(ws) -> None:
    (ws.path / "x").write_text(
        "block-a\nfoo\nblock-a-end\n"
        "block-b\nfoo\nblock-b-end\n"
    )
    body = (
        "*** Begin Patch\n"
        "*** Update File: x\n"
        "@@\n"
        " block-a\n"
        "-foo\n"
        "+FOO-A\n"
        " block-a-end\n"
        "*** End Patch\n"
    )
    apply_patch(ws, body)
    text = (ws.path / "x").read_text()
    assert "FOO-A" in text
    # The block-b foo stays.
    assert text.count("foo") == 1


# ---------------------------------------------------------------------------
# apply_patch — multi-op + path safety
# ---------------------------------------------------------------------------


def test_multi_op_apply_summary_lines(ws) -> None:
    (ws.path / "stale").write_text("x")
    (ws.path / "src.py").write_text("a\n")
    body = (
        "*** Begin Patch\n"
        "*** Add File: new.txt\n"
        "+hi\n"
        "*** Delete File: stale\n"
        "*** Update File: src.py\n"
        "@@\n"
        "-a\n"
        "+A\n"
        "*** End Patch\n"
    )
    summary = apply_patch(ws, body)
    assert "added" in summary
    assert "deleted" in summary
    assert "updated" in summary


# ---------------------------------------------------------------------------
# Tool factory + cap denial
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_definition_basics(ws) -> None:
    tool = apply_patch_tool(ws)
    assert tool.name == "apply_patch"
    assert tool.input_schema["required"] == ["patch"]


@pytest.mark.asyncio
async def test_tool_blocked_when_fs_write_disabled(tmp_path: Path) -> None:
    no_write = make_workspace(
        root=str(tmp_path),
        caps=WorkspaceCaps(fs_read=True, fs_write=False),
    )
    try:
        tool = apply_patch_tool(no_write)
        out = await tool.implementation(
            {"patch": "*** Begin Patch\n*** Add File: x\n+a\n*** End Patch\n"}
        )
        assert "fs_write denied" in out
    finally:
        no_write.cleanup()


@pytest.mark.asyncio
async def test_tool_returns_parse_error_string(ws) -> None:
    tool = apply_patch_tool(ws)
    out = await tool.implementation({"patch": "garbage"})
    assert out.startswith("apply_patch parse error:")


@pytest.mark.asyncio
async def test_tool_returns_apply_error_string(ws) -> None:
    (ws.path / "x").write_text("foo\nfoo\n")
    tool = apply_patch_tool(ws)
    body = (
        "*** Begin Patch\n"
        "*** Update File: x\n"
        "@@\n"
        "-foo\n"
        "+bar\n"
        "*** End Patch\n"
    )
    out = await tool.implementation({"patch": body})
    assert out.startswith("apply_patch error:")
    assert "occurs 2 times" in out


@pytest.mark.asyncio
async def test_tool_blank_patch_returns_error(ws) -> None:
    tool = apply_patch_tool(ws)
    out = await tool.implementation({"patch": "   "})
    assert "is required" in out
