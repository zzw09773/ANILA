"""ShellHookMiddleware — run a shell command before / after an Action.

Modeled on claude-code's ``PreToolUse`` / ``PostToolUse`` hook contract:

  - The framework spawns the configured shell command
  - JSON describing the Action invocation goes in via stdin
  - The command may emit a JSON decision on stdout
  - The decision is one of:
      ``{"decision": "allow"}``                      — proceed unchanged
      ``{"decision": "deny", "reason": "…"}``        — short-circuit
      ``{"decision": "modify", "params": {…}}``      — input-stage only
      ``{"decision": "modify", "output": …}``        — output-stage only

This is the "external auditor" pattern: ops teams ship a sidecar
script (Python / Bash / Go binary, anything) that the framework
consults around each Action without having to import / link it.
Particularly useful for:

- piping every tool call into a corporate audit log written by some
  in-house Go binary
- enforcing a deny-list maintained by the security team without
  redeploying the agent
- shelling out to ``opa eval`` for fine-grained policy decisions

The middleware is the same Protocol shape as Python middleware —
it composes alongside Trace / Cost / Guardrail without ceremony.

**Sandbox warning**: spawning subprocesses crosses a trust boundary.
The middleware (a) accepts the command as a list to avoid shell
interpretation, (b) sets a per-call timeout, and (c) caps stdout at a
configurable byte limit. Operators are responsible for the script
itself being safe to invoke per-request.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from agentic_rag.runtime.framework.action import Action, ActionContext, ActionResult
from agentic_rag.runtime.framework.middleware.protocol import NextHandler

logger = logging.getLogger(__name__)


HookStage = Literal["before", "after"]


# ── Wire payload (what the script sees on stdin) ───────────────────────


def _build_payload(
    action: Action,
    context: ActionContext,
    stage: HookStage,
    result: ActionResult | None = None,
) -> dict[str, Any]:
    """Serialise the hook payload.

    Matches claude-code's hook input shape closely enough that scripts
    written for that ecosystem mostly port over with rename-only
    changes — the field names use the framework's own vocabulary
    (action / kind / side_effect_class) rather than claude-code's
    (tool_name / tool_input).
    """
    payload: dict[str, Any] = {
        "stage": stage,
        "action": {
            "name": action.name,
            "kind": action.kind.value,
            "side_effect_class": action.side_effect_class.value,
        },
        "run_id": context.run_id,
        "agent_name": context.agent_name,
        "params": dict(context.params),
    }
    if stage == "after" and result is not None:
        payload["result"] = {
            "is_error": result.is_error,
            "output": result.output,
            "error": result.error,
        }
    return payload


# ── Decision parsing ───────────────────────────────────────────────────


@dataclass(frozen=True)
class _ShellDecision:
    """Internal-only parsed view of the shell script's stdout JSON."""

    decision: Literal["allow", "deny", "modify"]
    reason: str | None = None
    params: dict[str, Any] | None = None
    output: Any = None


def _parse_decision(stdout: str) -> _ShellDecision:
    """Parse the script's JSON decision. Empty / unparseable → ``allow``.

    Permissive on parse errors: scripts that write nothing, or write
    plain log lines instead of JSON, default to allowing the call.
    Operators who want strict mode can layer a Python guardrail on
    top.
    """
    text = stdout.strip()
    if not text:
        return _ShellDecision(decision="allow")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Some scripts print logs to stdout. Try to find a final JSON
        # object — last line that parses as JSON wins.
        for line in reversed(text.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
        else:
            logger.debug("shell hook stdout had no parseable JSON; allowing")
            return _ShellDecision(decision="allow")

    if not isinstance(data, dict):
        return _ShellDecision(decision="allow")
    decision = str(data.get("decision", "allow")).lower()
    if decision not in {"allow", "deny", "modify"}:
        return _ShellDecision(decision="allow")
    return _ShellDecision(
        decision=decision,  # type: ignore[arg-type]
        reason=data.get("reason"),
        params=data.get("params") if isinstance(data.get("params"), dict) else None,
        output=data.get("output"),
    )


# ── Middleware ─────────────────────────────────────────────────────────


class ShellHookMiddleware:
    """Run a shell command around the Action.

    Construction:

        ShellHookMiddleware(
            when="before",                        # or "after"
            command=["./scripts/audit.sh"],       # list, NOT a shell string
            timeout_seconds=5.0,
            max_stdout_bytes=64 * 1024,
            env={"AUDIT_VERSION": "1"},           # merged into os.environ
        )

    ``when="before"`` ignores any output payload from the script;
    ``when="after"`` ignores ``params``. The middleware logs but does
    not raise on wrong-stage decisions.

    Multiple shell hooks compose by registering several instances on
    the runner — each runs independently, in order.
    """

    def __init__(
        self,
        *,
        when: HookStage,
        command: Sequence[str],
        timeout_seconds: float = 5.0,
        max_stdout_bytes: int = 64 * 1024,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        if when not in ("before", "after"):
            raise ValueError(f"when must be 'before' or 'after', got {when!r}")
        if not command:
            raise ValueError("command must be a non-empty argv list")
        if not isinstance(command, (list, tuple)):
            raise TypeError(
                "command must be a list/tuple (no shell interpretation); "
                "use ['/bin/sh', '-c', 'cmd ...'] if you need a shell"
            )
        self._when = when
        self._command = list(command)
        self._timeout = timeout_seconds
        self._max_bytes = max_stdout_bytes
        self._env = env
        self._cwd = cwd

    async def __call__(
        self,
        action: Action,
        context: ActionContext,
        next_: NextHandler,
    ) -> ActionResult:
        # Before-hook
        if self._when == "before":
            decision = await self._invoke(action, context, "before")
            if decision.decision == "deny":
                return ActionResult(
                    error=f"[shell-hook] {decision.reason or 'denied'}"
                )
            if decision.decision == "modify" and decision.params is not None:
                context = ActionContext(
                    run_id=context.run_id,
                    agent_name=context.agent_name,
                    params=decision.params,
                    history=context.history,
                    metadata=context.metadata,
                )

        result = await next_(context)

        # After-hook
        if self._when == "after":
            decision = await self._invoke(action, context, "after", result)
            if decision.decision == "deny":
                # Replace the handler's result with a denial. The handler
                # already ran (its side effects may have happened) — the
                # script is denying the *return*, not the execution. This
                # is intentional: the "after" hook is a redaction /
                # sanitisation layer, not a rollback mechanism.
                return ActionResult(
                    error=f"[shell-hook] {decision.reason or 'denied after execution'}"
                )
            if decision.decision == "modify" and decision.output is not None:
                if not result.is_error:
                    result = ActionResult(
                        output=decision.output,
                        metadata=result.metadata,
                        handoff_target=result.handoff_target,
                    )

        return result

    # ── Subprocess invocation ────────────────────────────────────────

    async def _invoke(
        self,
        action: Action,
        context: ActionContext,
        stage: HookStage,
        result: ActionResult | None = None,
    ) -> _ShellDecision:
        payload = _build_payload(action, context, stage, result)
        stdin_bytes = (json.dumps(payload, default=str) + "\n").encode("utf-8")

        env = None
        if self._env is not None:
            env = {**os.environ, **self._env}

        try:
            proc = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=self._cwd,
            )
        except FileNotFoundError:
            logger.error("shell hook command not found: %s", self._command[0])
            return _ShellDecision(decision="allow")  # fail-open on misconfig
        except Exception as exc:  # noqa: BLE001
            logger.exception("shell hook spawn failed: %s", exc)
            return _ShellDecision(decision="allow")

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=stdin_bytes),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.warning(
                "shell hook %r timed out after %.1fs (action=%s)",
                self._command[0],
                self._timeout,
                action.name,
            )
            return _ShellDecision(decision="allow")

        if stderr_bytes:
            logger.debug(
                "shell hook stderr (%s): %s",
                action.name,
                stderr_bytes[:1024].decode("utf-8", errors="replace"),
            )

        if proc.returncode != 0:
            logger.warning(
                "shell hook %r exited %d (action=%s); allowing by default",
                self._command[0],
                proc.returncode,
                action.name,
            )
            return _ShellDecision(decision="allow")

        if len(stdout_bytes) > self._max_bytes:
            stdout_bytes = stdout_bytes[: self._max_bytes]
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        return _parse_decision(stdout)


__all__ = ["ShellHookMiddleware"]
