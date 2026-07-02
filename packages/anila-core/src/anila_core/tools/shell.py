"""Workspace-scoped shell tools (Sprint 12 PR 3).

Two factories:

- :func:`exec_bash_tool` — runs a shell command in the workspace.
- :func:`exec_python_tool` — runs a Python script (string) in the
  workspace.

Both honour :class:`WorkspaceCaps`:

- ``exec_bash`` / ``exec_python`` cap must be enabled or the tool
  returns an error result.
- ``command_allowlist`` (when non-empty) restricts the first token of
  bash commands.
- ``max_exec_seconds`` clamps wall-clock per call (kill on timeout).
- ``network``: when False, the subprocess env is scrubbed of
  ``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``ALL_PROXY`` and equivalents.
  This is **soft** enforcement — the actual network boundary is the
  per-agent Docker container.

Output is capped (8 KB stdout, 8 KB stderr) to keep LLM context
small; the agent gets a `[…truncated]` marker if the run produced
more.

Caveats:

- Uses ``asyncio.create_subprocess_shell`` (i.e. shell=True). Pipes
  and redirection work; the price is that the agent can chain
  commands. For tighter agents, set a non-empty
  ``command_allowlist`` so only known-safe binaries pass the gate.
- Cross-platform: on Linux/macOS goes through /bin/sh; on Windows
  through cmd.exe. Tests skip the shell paths on Windows.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import sys
import tempfile
from typing import Any

from ..models.tool import ToolDefinition, ToolPermission, ToolSafety
from ..workspace import Workspace

logger = logging.getLogger(__name__)


_MAX_OUTPUT_BYTES = 8 * 1024
_TRUNCATED_MARKER = "\n[…truncated]"
_PROXY_ENV_VARS = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
    "NO_PROXY", "no_proxy",
)


def _build_env(workspace: Workspace) -> dict[str, str]:
    """Workspace-aware subprocess env.

    - Inherits the parent's env so PATH / locale / etc. remain sensible.
    - Scrubs proxy vars when network cap is False.
    - Sets ``ANILA_WORKSPACE`` so subprocesses can introspect their
      sandbox root if they want.
    """
    env = dict(os.environ)
    if not workspace.caps.network:
        for var in _PROXY_ENV_VARS:
            env.pop(var, None)
    env["ANILA_WORKSPACE"] = str(workspace.path)
    return env


def _trim(blob: bytes) -> str:
    """Decode + soft-cap a byte stream so the LLM sees a small result."""
    if not blob:
        return ""
    if len(blob) > _MAX_OUTPUT_BYTES:
        head = blob[:_MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
        return head + _TRUNCATED_MARKER
    return blob.decode("utf-8", errors="replace")


def _format_result(
    *,
    cmd_label: str,
    returncode: int,
    stdout_text: str,
    stderr_text: str,
    duration_s: float,
) -> str:
    """Compact human-readable result the LLM can consume directly."""
    parts: list[str] = []
    parts.append(
        f"$ {cmd_label}\n"
        f"exit={returncode} duration={duration_s:.2f}s"
    )
    if stdout_text:
        parts.append(f"--- stdout ---\n{stdout_text}")
    if stderr_text:
        parts.append(f"--- stderr ---\n{stderr_text}")
    if not stdout_text and not stderr_text:
        parts.append("(no output)")
    return "\n".join(parts)


def _allowlist_check(
    workspace: Workspace, command: str
) -> str | None:
    """Return an error message if ``command`` is blocked by the allowlist."""
    allow = workspace.caps.command_allowlist
    if not allow:
        return None
    try:
        tokens = shlex.split(command, posix=(os.name != "nt"))
    except ValueError as exc:
        return f"invalid shell quoting: {exc}"
    if not tokens:
        return "command is empty"
    first = tokens[0]
    if first not in allow:
        return (
            f"command {first!r} is not in the workspace allowlist "
            f"({list(allow)})"
        )
    return None


# ---------------------------------------------------------------------------
# exec_bash
# ---------------------------------------------------------------------------


def exec_bash_tool(workspace: Workspace) -> ToolDefinition:
    """Run a shell command inside the workspace.

    Returns the formatted output as a string the LLM can read directly.
    Errors (timeout, cap denial, allowlist rejection) are surfaced as
    ``ToolResult(is_error=True)`` content via the tool's caller —
    here we return a normal string and the LLM gets a `(no output)` /
    `exit=…` envelope.
    """

    async def _impl(input: dict[str, Any], **_: Any) -> str:
        if not workspace.caps.exec_bash:
            return "exec_bash error: capability exec_bash denied"
        command = str(input.get("command", "")).strip()
        if not command:
            return "exec_bash error: 'command' is required"

        gate_msg = _allowlist_check(workspace, command)
        if gate_msg is not None:
            return f"exec_bash error: {gate_msg}"

        env = _build_env(workspace)
        timeout = workspace.caps.max_exec_seconds
        loop_started = asyncio.get_running_loop().time()
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(workspace.path),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return f"exec_bash error: failed to spawn shell: {exc}"

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except (ProcessLookupError, OSError):
                pass
            return (
                f"exec_bash error: command exceeded "
                f"{timeout}s timeout and was killed"
            )

        duration = asyncio.get_running_loop().time() - loop_started
        return _format_result(
            cmd_label=command,
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout_text=_trim(stdout),
            stderr_text=_trim(stderr),
            duration_s=duration,
        )

    return ToolDefinition(
        name="exec_bash",
        description=(
            "Run a shell command inside the agent's workspace. The "
            "command runs with the workspace as cwd, inherits PATH, "
            "and is killed after the workspace's max_exec_seconds. "
            "Network calls require the workspace's `network` cap. "
            "Output is truncated at 8 KB stdout + 8 KB stderr."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "Shell command to execute. Pipes / redirection "
                        "work (runs through /bin/sh on POSIX, cmd.exe "
                        "on Windows)."
                    ),
                },
            },
            "required": ["command"],
        },
        # Shell commands aren't safe to run concurrently with each
        # other or with file edits, so mark sequential.
        safety=ToolSafety.DESTRUCTIVE,
        # Permission stays ALLOW by default; agent factories can flip
        # to ASK when they want per-call user approval.
        permission=ToolPermission.ALLOW,
        implementation=_impl,
    )


# ---------------------------------------------------------------------------
# exec_python
# ---------------------------------------------------------------------------


def exec_python_tool(workspace: Workspace) -> ToolDefinition:
    """Run a Python script (string) inside the workspace.

    Implementation: writes the script to a temp file inside the
    workspace, then invokes ``sys.executable script.py``. The script
    file is left in the workspace for inspection; ``cleanup()`` will
    remove it alongside the rest.
    """

    async def _impl(input: dict[str, Any], **_: Any) -> str:
        if not workspace.caps.exec_python:
            return "exec_python error: capability exec_python denied"
        code = input.get("code", "")
        if not isinstance(code, str) or not code.strip():
            return "exec_python error: 'code' is required"

        # Write script into a workspace-rooted temp file so the agent
        # can inspect it later if needed.
        scripts_dir = workspace.path / ".anila_python"
        try:
            scripts_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".py",
                dir=str(scripts_dir),
                delete=False,
            ) as fp:
                fp.write(code)
                script_path = fp.name
        except OSError as exc:
            return f"exec_python error: could not write script: {exc}"

        env = _build_env(workspace)
        timeout = workspace.caps.max_exec_seconds
        loop_started = asyncio.get_running_loop().time()
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                script_path,
                cwd=str(workspace.path),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return f"exec_python error: failed to spawn python: {exc}"

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except (ProcessLookupError, OSError):
                pass
            return (
                f"exec_python error: script exceeded {timeout}s "
                "timeout and was killed"
            )

        duration = asyncio.get_running_loop().time() - loop_started
        return _format_result(
            cmd_label=f"python {os.path.basename(script_path)}",
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout_text=_trim(stdout),
            stderr_text=_trim(stderr),
            duration_s=duration,
        )

    return ToolDefinition(
        name="exec_python",
        description=(
            "Run a Python script inside the agent's workspace. The "
            "script is written to a temp file under the workspace, "
            "executed with the same Python interpreter the agent is "
            "running on, with the workspace as cwd. Killed after "
            "max_exec_seconds. Network requires the `network` cap."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": (
                        "Python source. Multi-line is fine. Use stdout "
                        "(print) or files in the workspace to surface "
                        "results back to the conversation."
                    ),
                },
            },
            "required": ["code"],
        },
        safety=ToolSafety.DESTRUCTIVE,
        permission=ToolPermission.ALLOW,
        implementation=_impl,
    )


def all_shell_tools(workspace: Workspace) -> list[ToolDefinition]:
    """Convenience: both shell tools for a workspace."""
    return [exec_bash_tool(workspace), exec_python_tool(workspace)]


__all__ = [
    "exec_bash_tool",
    "exec_python_tool",
    "all_shell_tools",
]
