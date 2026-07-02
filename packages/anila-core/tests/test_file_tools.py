"""Tests for workspace-scoped file tools (Sprint 12 PR 2)."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

from anila_core.tools.files import (
    all_file_tools,
    file_edit_tool,
    file_read_tool,
    file_write_tool,
    glob_tool,
    grep_tool,
)
from anila_core.workspace import WorkspaceCaps, make_workspace


@pytest.fixture
def ws(tmp_path: Path) -> Iterator:
    workspace = make_workspace(root=str(tmp_path))
    yield workspace
    workspace.cleanup()


# ---------------------------------------------------------------------------
# file_read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_returns_line_numbered_content(ws) -> None:
    (ws.path / "x.txt").write_text("alpha\nbeta\ngamma\n")
    tool = file_read_tool(ws)
    out = await tool.implementation({"path": "x.txt"})
    assert "1\talpha" in out
    assert "2\tbeta" in out
    assert "3\tgamma" in out


@pytest.mark.asyncio
async def test_read_offset_and_limit(ws) -> None:
    (ws.path / "lines.txt").write_text("\n".join(f"line-{i}" for i in range(10)))
    tool = file_read_tool(ws)
    out = await tool.implementation({"path": "lines.txt", "offset": 3, "limit": 2})
    assert "4\tline-3" in out
    assert "5\tline-4" in out
    assert "6\tline-5" not in out
    # Truncation note appears when there are more lines past the window.
    assert "more lines" in out


@pytest.mark.asyncio
async def test_read_missing_file_returns_error_string(ws) -> None:
    tool = file_read_tool(ws)
    out = await tool.implementation({"path": "ghost.txt"})
    assert "does not exist" in out


@pytest.mark.asyncio
async def test_read_path_escape_returns_error_string(ws) -> None:
    tool = file_read_tool(ws)
    out = await tool.implementation({"path": "../../etc/passwd"})
    assert out.startswith("file_read error:") and "outside" in out


@pytest.mark.asyncio
async def test_read_blocked_when_fs_read_disabled(tmp_path: Path) -> None:
    no_read = make_workspace(
        root=str(tmp_path),
        caps=WorkspaceCaps(fs_read=False, fs_write=True),
    )
    try:
        (no_read.path / "x").write_text("hi")
        tool = file_read_tool(no_read)
        out = await tool.implementation({"path": "x"})
        assert "fs_read denied" in out
    finally:
        no_read.cleanup()


@pytest.mark.asyncio
async def test_read_handles_empty_file(ws) -> None:
    (ws.path / "empty.txt").write_text("")
    tool = file_read_tool(ws)
    out = await tool.implementation({"path": "empty.txt"})
    assert out == "(empty file)"


# ---------------------------------------------------------------------------
# file_write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_creates_file_and_parents(ws) -> None:
    tool = file_write_tool(ws)
    out = await tool.implementation(
        {"path": "deep/nested/x.txt", "content": "hello"}
    )
    assert "wrote" in out and "deep/nested/x.txt" in out
    assert (ws.path / "deep/nested/x.txt").read_text() == "hello"


@pytest.mark.asyncio
async def test_write_overwrites_existing(ws) -> None:
    (ws.path / "x.txt").write_text("old")
    tool = file_write_tool(ws)
    await tool.implementation({"path": "x.txt", "content": "new"})
    assert (ws.path / "x.txt").read_text() == "new"


@pytest.mark.asyncio
async def test_write_blocked_when_fs_write_disabled(tmp_path: Path) -> None:
    no_write = make_workspace(
        root=str(tmp_path),
        caps=WorkspaceCaps(fs_read=True, fs_write=False),
    )
    try:
        tool = file_write_tool(no_write)
        out = await tool.implementation({"path": "x", "content": "y"})
        assert "fs_write denied" in out
    finally:
        no_write.cleanup()


@pytest.mark.asyncio
async def test_write_path_escape_returns_error_string(ws) -> None:
    tool = file_write_tool(ws)
    out = await tool.implementation(
        {"path": "../../etc/oops", "content": "boom"}
    )
    assert out.startswith("file_write error:") and "outside" in out


@pytest.mark.asyncio
async def test_write_size_cap_blocks(tmp_path: Path) -> None:
    # 1 MB cap, write 2 MB.
    tight = make_workspace(
        root=str(tmp_path),
        caps=WorkspaceCaps(max_workspace_size_mb=1),
    )
    try:
        tool = file_write_tool(tight)
        big = "x" * (2 * 1024 * 1024)
        out = await tool.implementation({"path": "big.txt", "content": big})
        assert "size cap exceeded" in out
    finally:
        tight.cleanup()


# ---------------------------------------------------------------------------
# file_edit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_unique_string_replaces(ws) -> None:
    (ws.path / "x.py").write_text("def add(a, b):\n    return a + b\n")
    tool = file_edit_tool(ws)
    out = await tool.implementation(
        {
            "path": "x.py",
            "old_string": "return a + b",
            "new_string": "return a - b",
        }
    )
    assert "1 replacement" in out
    assert "return a - b" in (ws.path / "x.py").read_text()


@pytest.mark.asyncio
async def test_edit_refuses_when_old_string_appears_multiple_times(ws) -> None:
    (ws.path / "x").write_text("foo foo foo")
    tool = file_edit_tool(ws)
    out = await tool.implementation(
        {"path": "x", "old_string": "foo", "new_string": "bar"}
    )
    assert "occurs 3 times" in out
    # Original unchanged.
    assert (ws.path / "x").read_text() == "foo foo foo"


@pytest.mark.asyncio
async def test_edit_replace_all(ws) -> None:
    (ws.path / "x").write_text("foo foo foo")
    tool = file_edit_tool(ws)
    out = await tool.implementation(
        {
            "path": "x",
            "old_string": "foo",
            "new_string": "bar",
            "replace_all": True,
        }
    )
    assert "3 replacements" in out
    assert (ws.path / "x").read_text() == "bar bar bar"


@pytest.mark.asyncio
async def test_edit_old_string_not_found_errors(ws) -> None:
    (ws.path / "x").write_text("hello")
    tool = file_edit_tool(ws)
    out = await tool.implementation(
        {"path": "x", "old_string": "absent", "new_string": "z"}
    )
    assert "not found" in out


@pytest.mark.asyncio
async def test_edit_rejects_identical_strings(ws) -> None:
    (ws.path / "x").write_text("foo")
    tool = file_edit_tool(ws)
    out = await tool.implementation(
        {"path": "x", "old_string": "foo", "new_string": "foo"}
    )
    assert "identical" in out


@pytest.mark.asyncio
async def test_edit_empty_old_string_errors(ws) -> None:
    (ws.path / "x").write_text("foo")
    tool = file_edit_tool(ws)
    out = await tool.implementation(
        {"path": "x", "old_string": "", "new_string": "y"}
    )
    assert "non-empty" in out


@pytest.mark.asyncio
async def test_edit_blocked_when_fs_write_disabled(tmp_path: Path) -> None:
    no_write = make_workspace(
        root=str(tmp_path),
        caps=WorkspaceCaps(fs_read=True, fs_write=False),
    )
    try:
        (no_write.path / "x").write_text("foo")
        tool = file_edit_tool(no_write)
        out = await tool.implementation(
            {"path": "x", "old_string": "foo", "new_string": "bar"}
        )
        assert "fs_write denied" in out
    finally:
        no_write.cleanup()


# ---------------------------------------------------------------------------
# glob
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_glob_recursive_pattern(ws) -> None:
    (ws.path / "src").mkdir()
    (ws.path / "src" / "a.py").write_text("")
    (ws.path / "src" / "b.py").write_text("")
    (ws.path / "src" / "nested").mkdir()
    (ws.path / "src" / "nested" / "c.py").write_text("")
    (ws.path / "README.md").write_text("")
    tool = glob_tool(ws)
    out = await tool.implementation({"pattern": "**/*.py"})
    assert "src/a.py" in out
    assert "src/b.py" in out
    assert "src/nested/c.py" in out
    assert "README.md" not in out


@pytest.mark.asyncio
async def test_glob_no_matches(ws) -> None:
    tool = glob_tool(ws)
    out = await tool.implementation({"pattern": "*.zzz"})
    assert "no matches" in out


@pytest.mark.asyncio
async def test_glob_caps_results(ws) -> None:
    # 300 files, glob caps at 250.
    for i in range(300):
        (ws.path / f"f{i:03}.txt").write_text("")
    tool = glob_tool(ws)
    out = await tool.implementation({"pattern": "*.txt"})
    lines = [line for line in out.split("\n") if line.endswith(".txt")]
    assert len(lines) == 250
    assert "more matches" in out


# ---------------------------------------------------------------------------
# grep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grep_files_with_matches_default(ws) -> None:
    (ws.path / "a.py").write_text("def hello():\n    pass\n")
    (ws.path / "b.py").write_text("def world():\n    pass\n")
    (ws.path / "c.py").write_text("nothing here\n")
    tool = grep_tool(ws)
    out = await tool.implementation({"pattern": r"^def\s+\w+\("})
    assert "a.py" in out and "b.py" in out
    assert "c.py" not in out


@pytest.mark.asyncio
async def test_grep_content_mode_returns_file_line_text(ws) -> None:
    (ws.path / "a.py").write_text("first\nTODO: x\nthird\n")
    tool = grep_tool(ws)
    out = await tool.implementation(
        {"pattern": "TODO", "output_mode": "content"}
    )
    assert "a.py:2:TODO: x" in out


@pytest.mark.asyncio
async def test_grep_case_insensitive(ws) -> None:
    (ws.path / "a.py").write_text("Hello World\n")
    tool = grep_tool(ws)
    out_match = await tool.implementation(
        {"pattern": "hello", "-i": True, "output_mode": "content"}
    )
    assert "Hello World" in out_match


@pytest.mark.asyncio
async def test_grep_glob_filter(ws) -> None:
    (ws.path / "a.py").write_text("TODO\n")
    (ws.path / "a.md").write_text("TODO\n")
    tool = grep_tool(ws)
    out = await tool.implementation(
        {"pattern": "TODO", "glob": "*.py"}
    )
    assert "a.py" in out
    assert "a.md" not in out


@pytest.mark.asyncio
async def test_grep_invalid_regex_returns_error(ws) -> None:
    tool = grep_tool(ws)
    out = await tool.implementation({"pattern": "(unclosed"})
    assert "invalid regex" in out


@pytest.mark.asyncio
async def test_grep_no_matches(ws) -> None:
    (ws.path / "a").write_text("hi")
    tool = grep_tool(ws)
    out = await tool.implementation({"pattern": "nothing-matches-this"})
    assert "no files match" in out


@pytest.mark.asyncio
async def test_grep_path_escape_rejected(ws) -> None:
    tool = grep_tool(ws)
    out = await tool.implementation(
        {"pattern": "x", "path": "../../etc"}
    )
    assert out.startswith("grep error:") and "outside" in out


# ---------------------------------------------------------------------------
# all_file_tools convenience
# ---------------------------------------------------------------------------


def test_all_file_tools_returns_five_distinct(ws) -> None:
    tools = all_file_tools(ws)
    names = {t.name for t in tools}
    assert names == {"file_read", "file_write", "file_edit", "glob", "grep"}
