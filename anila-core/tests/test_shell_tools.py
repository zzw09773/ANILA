"""Tests for workspace-scoped shell tools (Sprint 12 PR 3)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest

from anila_core.tools.shell import (
    all_shell_tools,
    exec_bash_tool,
    exec_python_tool,
)
from anila_core.workspace import WorkspaceCaps, make_workspace


# Some shell-specific tests are POSIX-only (Windows uses cmd.exe with
# different syntax). Mark with this skip when needed.
posix_only = pytest.mark.skipif(
    os.name == "nt", reason="Test uses POSIX shell syntax"
)


@pytest.fixture
def ws_with_exec(tmp_path: Path) -> Iterator:
    workspace = make_workspace(
        root=str(tmp_path),
        caps=WorkspaceCaps(
            exec_bash=True, exec_python=True, max_exec_seconds=10
        ),
    )
    yield workspace
    workspace.cleanup()


# ---------------------------------------------------------------------------
# Default cap denial
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_bash_blocked_by_default_cap(tmp_path: Path) -> None:
    ws = make_workspace(root=str(tmp_path))  # default = exec disabled
    try:
        tool = exec_bash_tool(ws)
        out = await tool.implementation({"command": "echo hi"})
        assert "exec_bash denied" in out
    finally:
        ws.cleanup()


@pytest.mark.asyncio
async def test_exec_python_blocked_by_default_cap(tmp_path: Path) -> None:
    ws = make_workspace(root=str(tmp_path))
    try:
        tool = exec_python_tool(ws)
        out = await tool.implementation({"code": "print('hi')"})
        assert "exec_python denied" in out
    finally:
        ws.cleanup()


# ---------------------------------------------------------------------------
# exec_bash — basic execution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_bash_returns_stdout(ws_with_exec) -> None:
    tool = exec_bash_tool(ws_with_exec)
    out = await tool.implementation({"command": "echo workspace-hello"})
    assert "workspace-hello" in out
    assert "exit=0" in out


@pytest.mark.asyncio
async def test_exec_bash_returns_nonzero_exit(ws_with_exec) -> None:
    tool = exec_bash_tool(ws_with_exec)
    if os.name == "nt":
        cmd = "exit 7"
    else:
        cmd = "exit 7"  # both shells understand exit
    out = await tool.implementation({"command": cmd})
    assert "exit=7" in out


@pytest.mark.asyncio
async def test_exec_bash_runs_in_workspace_cwd(ws_with_exec) -> None:
    tool = exec_bash_tool(ws_with_exec)
    if os.name == "nt":
        out = await tool.implementation({"command": "cd"})
    else:
        out = await tool.implementation({"command": "pwd"})
    # The output should contain the workspace path.
    assert str(ws_with_exec.path) in out


@pytest.mark.asyncio
async def test_exec_bash_empty_command_errors(ws_with_exec) -> None:
    tool = exec_bash_tool(ws_with_exec)
    out = await tool.implementation({"command": ""})
    assert "is required" in out


@pytest.mark.asyncio
async def test_exec_bash_no_output_marker(ws_with_exec) -> None:
    """A command that produces neither stdout nor stderr surfaces a marker."""
    tool = exec_bash_tool(ws_with_exec)
    if os.name == "nt":
        cmd = "ver > nul 2>&1"  # no output to either stream
    else:
        cmd = "true"
    out = await tool.implementation({"command": cmd})
    assert "(no output)" in out


# ---------------------------------------------------------------------------
# exec_bash — timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_bash_timeout_kills_process(tmp_path: Path) -> None:
    ws = make_workspace(
        root=str(tmp_path),
        caps=WorkspaceCaps(exec_bash=True, max_exec_seconds=1),
    )
    try:
        tool = exec_bash_tool(ws)
        if os.name == "nt":
            cmd = "ping 127.0.0.1 -n 5"  # ~5s
        else:
            cmd = "sleep 5"
        out = await tool.implementation({"command": cmd})
        assert "timeout" in out
    finally:
        ws.cleanup()


# ---------------------------------------------------------------------------
# exec_bash — command allowlist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_bash_allowlist_passes_listed_command(tmp_path: Path) -> None:
    ws = make_workspace(
        root=str(tmp_path),
        caps=WorkspaceCaps(
            exec_bash=True,
            command_allowlist=("echo",),
            max_exec_seconds=5,
        ),
    )
    try:
        tool = exec_bash_tool(ws)
        out = await tool.implementation({"command": "echo allowed"})
        assert "allowed" in out
        assert "exit=0" in out
    finally:
        ws.cleanup()


@pytest.mark.asyncio
async def test_exec_bash_allowlist_rejects_unlisted_command(
    tmp_path: Path,
) -> None:
    ws = make_workspace(
        root=str(tmp_path),
        caps=WorkspaceCaps(
            exec_bash=True,
            command_allowlist=("echo",),
        ),
    )
    try:
        tool = exec_bash_tool(ws)
        out = await tool.implementation(
            {"command": "rm -rf /"}
        )
        assert "not in the workspace allowlist" in out
        # Sanity: rm wasn't actually invoked.
        assert "exit=" not in out
    finally:
        ws.cleanup()


# ---------------------------------------------------------------------------
# exec_bash — network cap scrubs proxy env
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_bash_network_off_scrubs_proxy_env(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.test:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.test:8080")
    ws = make_workspace(
        root=str(tmp_path),
        caps=WorkspaceCaps(exec_bash=True, network=False, max_exec_seconds=5),
    )
    try:
        tool = exec_bash_tool(ws)
        if os.name == "nt":
            cmd = "echo HTTP=%HTTP_PROXY%"
        else:
            cmd = 'echo "HTTP=$HTTP_PROXY"'
        out = await tool.implementation({"command": cmd})
        # When the var is unset, the shell substitutes empty string.
        # Cmd.exe leaves the literal '%HTTP_PROXY%' when undefined.
        if os.name == "nt":
            assert "%HTTP_PROXY%" in out  # not expanded → was unset
        else:
            assert "HTTP=" in out
            # The proxy value should NOT have leaked through.
            assert "proxy.test" not in out
    finally:
        ws.cleanup()


@pytest.mark.asyncio
async def test_exec_bash_network_on_keeps_proxy_env(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.test:8080")
    ws = make_workspace(
        root=str(tmp_path),
        caps=WorkspaceCaps(exec_bash=True, network=True, max_exec_seconds=5),
    )
    try:
        tool = exec_bash_tool(ws)
        if os.name == "nt":
            cmd = "echo %HTTP_PROXY%"
        else:
            cmd = 'echo "$HTTP_PROXY"'
        out = await tool.implementation({"command": cmd})
        assert "proxy.test" in out
    finally:
        ws.cleanup()


# ---------------------------------------------------------------------------
# exec_bash — ANILA_WORKSPACE env var exposed to subprocess
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_bash_exposes_anila_workspace_env(ws_with_exec) -> None:
    tool = exec_bash_tool(ws_with_exec)
    if os.name == "nt":
        cmd = "echo %ANILA_WORKSPACE%"
    else:
        cmd = 'echo "$ANILA_WORKSPACE"'
    out = await tool.implementation({"command": cmd})
    assert str(ws_with_exec.path) in out


# ---------------------------------------------------------------------------
# exec_python — basic execution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exec_python_returns_stdout(ws_with_exec) -> None:
    tool = exec_python_tool(ws_with_exec)
    out = await tool.implementation({"code": "print('python-hello')"})
    assert "python-hello" in out
    assert "exit=0" in out


@pytest.mark.asyncio
async def test_exec_python_multi_line_script(ws_with_exec) -> None:
    tool = exec_python_tool(ws_with_exec)
    code = "x = 21\ny = x * 2\nprint(y)"
    out = await tool.implementation({"code": code})
    assert "42" in out


@pytest.mark.asyncio
async def test_exec_python_exit_code_propagates(ws_with_exec) -> None:
    tool = exec_python_tool(ws_with_exec)
    out = await tool.implementation(
        {"code": "import sys\nsys.exit(3)"}
    )
    assert "exit=3" in out


@pytest.mark.asyncio
async def test_exec_python_writes_into_workspace(ws_with_exec) -> None:
    tool = exec_python_tool(ws_with_exec)
    code = (
        "import os\n"
        "with open('result.txt', 'w') as f:\n"
        "    f.write('cool')\n"
    )
    await tool.implementation({"code": code})
    assert (ws_with_exec.path / "result.txt").read_text() == "cool"


@pytest.mark.asyncio
async def test_exec_python_blank_code_errors(ws_with_exec) -> None:
    tool = exec_python_tool(ws_with_exec)
    out = await tool.implementation({"code": "   "})
    assert "is required" in out


@pytest.mark.asyncio
async def test_exec_python_timeout_kills_script(tmp_path: Path) -> None:
    ws = make_workspace(
        root=str(tmp_path),
        caps=WorkspaceCaps(exec_python=True, max_exec_seconds=1),
    )
    try:
        tool = exec_python_tool(ws)
        out = await tool.implementation(
            {"code": "import time\ntime.sleep(5)"}
        )
        assert "timeout" in out
    finally:
        ws.cleanup()


# ---------------------------------------------------------------------------
# Output truncation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_output_capped_with_truncation_marker(ws_with_exec) -> None:
    tool = exec_python_tool(ws_with_exec)
    # 16 KB of output — well over the 8 KB cap.
    code = "print('x' * 16384)"
    out = await tool.implementation({"code": code})
    assert "truncated" in out


# ---------------------------------------------------------------------------
# all_shell_tools convenience
# ---------------------------------------------------------------------------


def test_all_shell_tools_returns_two_distinct(ws_with_exec) -> None:
    tools = all_shell_tools(ws_with_exec)
    names = {t.name for t in tools}
    assert names == {"exec_bash", "exec_python"}
