"""The Action abstraction — the framework's single primitive for "thing
an agent does".

Architecture spec: ``docs/anila-agent-framework-architecture.md`` §2.

Three kinds — same shape:

- ``SYNC_TOOL``  the LLM calls a tool, runtime blocks until it returns
- ``BG_TASK``    long-running work spawned by the runtime (Sprint 5+)
- ``HANDOFF``    transfer of control to another agent

(``USER_SKILL`` was deliberately removed — see architecture doc §0,
deployment-shape constraint.)

Every Action is **frozen** — once registered with an Agent it does not
mutate. Mutation would invalidate the trace tree, the cost estimate, and
the middleware chain assembled at registration time. If you need a
"different version" of an Action, build a new one.

This module is intentionally machinery-free: no scheduler, no
middleware engine, no run loop. Those land in Sprint 2 / 3. Stage B
ships only the dataclasses + enums + the lightweight
``ActionContext`` / ``ActionResult`` shapes the runner consumes.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from agentic_rag.runtime.framework.exceptions import UserError
from agentic_rag.runtime.framework.items import Message

if TYPE_CHECKING:
    # Forward-only reference: ``Action.handler`` may return an Agent
    # instance for a HANDOFF kind. Keeping the import behind TYPE_CHECKING
    # avoids the agent.py → action.py → agent.py cycle.
    from agentic_rag.runtime.framework.agent import Agent


# ── Enums ────────────────────────────────────────────────────────────────


class ActionKind(StrEnum):
    """What kind of work the Action represents.

    Stage B only implements ``SYNC_TOOL`` end-to-end in the runner.
    ``BG_TASK`` and ``HANDOFF`` are accepted by the registry / typing
    so authors can declare them today, but the runner raises a clear
    ``UserError`` if asked to execute one before its sprint lands.
    """

    SYNC_TOOL = "sync_tool"
    BG_TASK = "bg_task"
    HANDOFF = "handoff"


class SideEffectClass(StrEnum):
    """Pure metadata for tracing / observability.

    NOT a runtime gate. Authorization happens at the API gateway before
    the request reaches the framework (see architecture §0).

    Useful downstream for: tracing dashboards, audit logs, cost
    attribution, rate-limiting middleware that consumers may add.
    """

    PURE = "pure"
    """No observable effect outside the return value."""

    LOCAL = "local"
    """Touches local filesystem / process state."""

    NETWORKED = "networked"
    """Calls remote APIs / writes to remote DBs."""

    IRREVERSIBLE = "irreversible"
    """Once executed, cannot be undone (charge a card, send email, etc.)."""


# ── Cost estimate ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CostEstimate:
    """Author-declared upper-bound cost for budgeting middleware.

    Real cost gets attributed at execution time by ``CostMiddleware``
    (Sprint 2). This estimate is what the cost-budget middleware uses
    *before* executing — to abort a run that would blow the budget.

    All three fields default to zero; consumers should fill in what
    they care about. ``time_seconds`` is wall-clock, intended for
    deadline-budget middleware.
    """

    tokens: int = 0
    dollars: float = 0.0
    time_seconds: float = 0.0


# ── Action context (passed into handler) ────────────────────────────────


@dataclass(frozen=True)
class ActionContext:
    """Per-invocation context handed to an Action's handler.

    Frozen for the same reason ``Action`` is frozen — handlers must not
    mutate context. If a handler needs to thread state forward, it
    returns it via ``ActionResult.metadata``.

    ``run_id`` lets handlers attribute logs / spans to the run.
    ``params`` is the parsed tool-arguments dict (the runner has
    already JSON-decoded the LLM's raw string).
    ``history`` is a snapshot of the conversation up to this Action; a
    handler that needs to look back (e.g. an answer-finalising tool)
    can read it without the runner having to plumb a session object.
    ``agent_name`` identifies the agent whose handler this is, useful
    for handoff-aware tools.
    """

    run_id: str
    agent_name: str
    params: dict[str, Any]
    history: tuple[Message, ...]
    metadata: dict[str, Any] = field(default_factory=dict)


# ── Action result ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class ActionResult:
    """The handler's return value.

    Exactly one of ``output`` or ``error`` is set, mirroring
    ``ToolResult``. ``output`` may be any JSON-serialisable value; the
    runner stringifies on the way back to the LLM so handler authors
    can return dicts / lists naturally. ``handoff_target`` is set only
    for ``HANDOFF``-kind Actions and points at the next agent. Plain
    SYNC_TOOL handlers leave it ``None``.

    ``metadata`` is a free-form bag for middleware to scribble in
    (tracing span ids, cost details, retry counts). It does not reach
    the LLM.
    """

    output: Any = None
    error: str | None = None
    handoff_target: Agent | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.error is not None and self.output is not None:
            raise UserError(
                "ActionResult must set at most one of output or error "
                f"(got output={self.output!r}, error={self.error!r})"
            )

    @property
    def is_error(self) -> bool:
        return self.error is not None

    def output_as_string(self) -> str:
        """Render output for the tool-role message body the LLM consumes.

        Strings pass through; everything else is JSON-encoded. Errors
        are formatted as ``[error] {message}`` so the LLM sees a
        consistent shape and can decide whether to retry.
        """
        if self.is_error:
            return f"[error] {self.error}"
        if self.output is None:
            return ""
        if isinstance(self.output, str):
            return self.output
        # Fall back to JSON for structured payloads. ``default=str``
        # rescues datetimes / Decimals / dataclasses-converted-to-str
        # without forcing handler authors to pre-serialise.
        import json

        return json.dumps(self.output, default=str, ensure_ascii=False)


# ── The Action itself ───────────────────────────────────────────────────


ActionHandler = Callable[[ActionContext], Awaitable[ActionResult]]
"""Coroutine signature every Action handler satisfies."""


@dataclass(frozen=True)
class Action:
    """The single primitive for "thing an agent does".

    See architecture spec §2 for the design rationale. Locked decisions:

    - ``frozen=True`` — strict immutability after registration
    - ``handler`` must be ``async``
    - ``kind`` cannot be changed after construction; if you want a
      different kind, build a different Action
    - ``middleware`` is a tuple (not list) so the registered chain
      order is part of the Action's identity

    The ``middleware`` field exists in stage B but is not yet honored
    by the runner — Sprint 2 wires it. Authors declaring it today will
    have it apply automatically once the middleware framework lands.
    """

    name: str
    description: str
    kind: ActionKind
    handler: ActionHandler
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] | None = None
    cost_estimate: CostEstimate = field(default_factory=CostEstimate)
    side_effect_class: SideEffectClass = SideEffectClass.PURE
    middleware: tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise UserError("Action.name must be non-empty")
        if not isinstance(self.kind, ActionKind):
            raise UserError(
                f"Action.kind must be ActionKind, got {type(self.kind).__name__}"
            )
        if not isinstance(self.side_effect_class, SideEffectClass):
            raise UserError(
                f"Action.side_effect_class must be SideEffectClass, got "
                f"{type(self.side_effect_class).__name__}"
            )
        if not callable(self.handler):
            raise UserError(f"Action.handler must be callable, got {type(self.handler)}")
        if not inspect.iscoroutinefunction(self.handler):
            # Allow coroutine-returning callables (e.g. partials, classes
            # with __call__) by sniffing the call result lazily — but
            # plain sync functions must be rejected up front to avoid
            # confusing failures inside the runner.
            #
            # ``functools.partial(async_fn, ...)`` reports False here, so
            # we accept any non-trivial callable; the runner will await
            # the returned awaitable and raise if it isn't one.
            pass


# ── Public surface ───────────────────────────────────────────────────────


__all__ = [
    "Action",
    "ActionContext",
    "ActionHandler",
    "ActionKind",
    "ActionResult",
    "CostEstimate",
    "SideEffectClass",
]
