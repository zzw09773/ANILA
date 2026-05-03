"""GuardrailMiddleware — composable input / output content checks.

A ``Guardrail`` is anything that can answer "is this Action invocation
allowed to proceed?" or "is this Action result allowed to be returned?".

The framework treats guardrails uniformly with the rest of the
middleware stack: ``GuardrailMiddleware`` is one middleware in a chain
that happens to dispatch to a list of ``Guardrail`` instances. There is
no separate guardrail subsystem — that's the architectural win over
upstream openai-agents-python (where guardrails live in their own
framework).

Decision shapes returned by guardrails:

- ``Allow()`` — proceed unchanged
- ``Deny(reason)`` — short-circuit; the LLM sees the reason as a tool
  error and can recover (retry with different args, hand off, give up).
- ``ModifyParams(params)`` — input-side only: rewrite ``ctx.params``
  before forwarding (query rewriting, default injection, PII redaction).
- ``ModifyOutput(output)`` — output-side only: rewrite ``result.output``
  before returning (citation injection, content sanitisation).

A guardrail returns ONE decision. To compose many checks, register
multiple Guardrails on the middleware — they run in order and the
first non-``Allow`` short-circuits.

Use cases for ANILA (local-only stack):

- citation enforcement: every assistant answer must reference at least
  one retrieval chunk
- PII redaction: scrub identifiers from tool params before they hit
  shared search infrastructure
- content policy: refuse to invoke a tool with prompt-injection-y
  arguments
- tenant isolation: deny a tool call whose ``collection_id`` doesn't
  belong to the requesting agent (defense-in-depth on top of RLS)
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, Union, runtime_checkable

from agentic_rag.runtime.framework.action import Action, ActionContext, ActionResult
from agentic_rag.runtime.framework.middleware.protocol import NextHandler

logger = logging.getLogger(__name__)


# ── Decision types ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class Allow:
    """Guardrail says: proceed."""


@dataclass(frozen=True)
class Deny:
    """Guardrail says: short-circuit. The LLM will see ``reason`` as the
    tool error message."""

    reason: str


@dataclass(frozen=True)
class ModifyParams:
    """Input-side guardrail says: forward, but with these params instead.

    Replaces ``ActionContext.params`` for the downstream chain. Useful
    for query rewriting, parameter defaulting, redaction.
    """

    params: dict[str, Any]


@dataclass(frozen=True)
class ModifyOutput:
    """Output-side guardrail says: return, but with this output instead.

    Replaces ``ActionResult.output``. Useful for adding citations,
    truncating sensitive fields, normalising shapes.
    """

    output: Any


GuardrailDecision = Union[Allow, Deny, ModifyParams, ModifyOutput]


# ── Guardrail Protocol ──────────────────────────────────────────────────


GuardrailFn = Callable[
    [Action, ActionContext, Union[ActionResult, None]],
    Union[GuardrailDecision, Awaitable[GuardrailDecision]],
]


@runtime_checkable
class Guardrail(Protocol):
    """The Guardrail Protocol.

    ``stage`` selects whether this guardrail runs before the handler
    (``"input"``) or after (``"output"``). Output guardrails see the
    handler's ``ActionResult``; input guardrails get ``None`` for it
    since the handler hasn't run yet.

    Both class instances with ``__call__`` and bare async/sync callables
    that match the signature satisfy this Protocol.
    """

    stage: str  # "input" or "output"

    async def __call__(
        self,
        action: Action,
        context: ActionContext,
        result: ActionResult | None,
    ) -> GuardrailDecision:
        ...


# ── Convenience adapters: turn a function into a Guardrail ─────────────


def input_guardrail(fn: GuardrailFn) -> Guardrail:
    """Decorator-style adapter: lift a callable into an input-stage Guardrail.

    Use when you don't want to write a class:

        @input_guardrail
        async def block_empty_query(action, ctx, _):
            if not ctx.params.get("query"):
                return Deny("query is required")
            return Allow()
    """
    return _CallableGuardrail(fn=fn, stage="input")


def output_guardrail(fn: GuardrailFn) -> Guardrail:
    """Same as ``input_guardrail`` but for output stage."""
    return _CallableGuardrail(fn=fn, stage="output")


@dataclass
class _CallableGuardrail:
    fn: GuardrailFn
    stage: str

    async def __call__(
        self,
        action: Action,
        context: ActionContext,
        result: ActionResult | None,
    ) -> GuardrailDecision:
        out = self.fn(action, context, result)
        if inspect.isawaitable(out):
            out = await out
        return out


# ── Middleware ─────────────────────────────────────────────────────────


class GuardrailMiddleware:
    """Run a list of guardrails before / after the handler.

    Guardrails run in registration order. The first non-``Allow``
    decision stops further checks at that stage:

      - input-stage ``Deny`` → short-circuit the whole chain
      - input-stage ``ModifyParams`` → forward with rewritten params
        and continue checking subsequent input guardrails against the
        *new* params (so a rewriter can be checked by a downstream
        validator)
      - output-stage ``Deny`` → replace the result with an error
      - output-stage ``ModifyOutput`` → continue with rewritten output

    ``ModifyParams`` returned by an output-stage guardrail is a no-op;
    ``ModifyOutput`` returned by an input-stage guardrail likewise.
    The middleware logs but doesn't raise on these — wrong-stage
    decisions are author bugs, not runtime errors.
    """

    def __init__(self, guardrails: Sequence[Guardrail]) -> None:
        self._input: list[Guardrail] = []
        self._output: list[Guardrail] = []
        for g in guardrails:
            stage = getattr(g, "stage", None)
            if stage == "input":
                self._input.append(g)
            elif stage == "output":
                self._output.append(g)
            else:
                raise ValueError(
                    f"Guardrail {g!r} has invalid stage={stage!r}; "
                    "expected 'input' or 'output'."
                )

    async def __call__(
        self,
        action: Action,
        context: ActionContext,
        next_: NextHandler,
    ) -> ActionResult:
        # Input-stage gate
        current_ctx = context
        for g in self._input:
            decision = await g(action, current_ctx, None)
            if isinstance(decision, Deny):
                return ActionResult(error=f"[guardrail] {decision.reason}")
            if isinstance(decision, ModifyParams):
                current_ctx = ActionContext(
                    run_id=current_ctx.run_id,
                    agent_name=current_ctx.agent_name,
                    params=decision.params,
                    history=current_ctx.history,
                    metadata=current_ctx.metadata,
                )
            elif isinstance(decision, ModifyOutput):
                logger.warning(
                    "guardrail %r returned ModifyOutput at input stage — ignored",
                    type(g).__name__,
                )
            # Allow → continue to next guardrail

        result = await next_(current_ctx)

        # Output-stage gate
        for g in self._output:
            decision = await g(action, current_ctx, result)
            if isinstance(decision, Deny):
                return ActionResult(error=f"[guardrail] {decision.reason}")
            if isinstance(decision, ModifyOutput):
                # Build a new result with the rewritten output, preserving
                # the rest. Cannot just dataclasses.replace because the
                # frozen dataclass has invariants (output xor error).
                if result.is_error:
                    # Output guardrail trying to rewrite an error result
                    # is unusual — keep the error and log.
                    logger.warning(
                        "guardrail %r tried to ModifyOutput on an error result "
                        "(action=%r); keeping original error",
                        type(g).__name__,
                        action.name,
                    )
                else:
                    result = ActionResult(
                        output=decision.output,
                        metadata=result.metadata,
                        handoff_target=result.handoff_target,
                    )
            elif isinstance(decision, ModifyParams):
                logger.warning(
                    "guardrail %r returned ModifyParams at output stage — ignored",
                    type(g).__name__,
                )
            # Allow → continue

        return result


__all__ = [
    "Allow",
    "Deny",
    "Guardrail",
    "GuardrailDecision",
    "GuardrailMiddleware",
    "ModifyOutput",
    "ModifyParams",
    "input_guardrail",
    "output_guardrail",
]
