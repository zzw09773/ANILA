"""Middleware Protocol + chain composer.

The Protocol shape (see architecture spec §3.1):

    async def __call__(
        self,
        action: Action,
        context: ActionContext,
        next_: Callable[[ActionContext], Awaitable[ActionResult]],
    ) -> ActionResult:
        ...

A Middleware can:

- Inspect the Action / context before forwarding (input guardrails,
  pre-cost gates, request tracing)
- Modify the context before forwarding (query rewriting, parameter
  defaults, header injection)
- Short-circuit by returning an ActionResult without calling ``next_``
  (deny by guardrail, cache hit, budget breach)
- Inspect or modify the result after ``next_`` (output guardrails,
  cost accumulation, span closure)
- Wrap ``next_`` in retry / timeout / catch logic

Composition is intentional and explicit: the chain is a linear list,
the order is what's registered. Order is part of the Action's identity
when middleware is attached at the Action level.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol, Union, runtime_checkable

from agentic_rag.runtime.framework.action import Action, ActionContext, ActionResult


# ── Type aliases ────────────────────────────────────────────────────────


NextHandler = Callable[[ActionContext], Awaitable[ActionResult]]
"""Callable that runs the next stage in the chain (next middleware or,
for the innermost wrap, the Action's handler itself)."""


MiddlewareCallable = Callable[[Action, ActionContext, NextHandler], Awaitable[ActionResult]]
"""Bare-callable shape — what users write when they don't want to
implement the full ``Middleware`` Protocol class."""


@runtime_checkable
class Middleware(Protocol):
    """The Middleware Protocol — runtime-checkable.

    Both class instances with ``__call__`` and bare async callables
    that match the signature satisfy this Protocol. The framework
    accepts either everywhere middleware is consumed.
    """

    async def __call__(
        self,
        action: Action,
        context: ActionContext,
        next_: NextHandler,
    ) -> ActionResult:
        ...


# ── Chain composer ──────────────────────────────────────────────────────


def compose_chain(
    action: Action,
    middlewares: Sequence[Union[Middleware, MiddlewareCallable]],
) -> NextHandler:
    """Build the wrapped handler for one Action invocation.

    The returned callable takes an ``ActionContext`` and runs the full
    middleware chain ending with the Action's handler.

    Wrap order: the LAST middleware in the sequence is the INNERMOST
    wrap (closest to the handler). The FIRST middleware is the
    OUTERMOST wrap. So a sequence of ``[Trace, Cost, Guardrail]``
    means the handler is wrapped by Guardrail, then by Cost, then by
    Trace — Trace sees the request first and the response last.

    Why a closure-based composer rather than a class-based pipeline:
    closures keep the per-invocation state local to the call stack
    (no shared mutable middleware state, no thread-safety questions),
    and they let any user-written async callable participate without
    inheriting from a base class.

    The composition is built once per invocation; for hot paths the
    cost is one closure allocation per middleware in the chain plus
    a small per-await overhead. Negligible compared to LLM / IO
    latency the middleware exists to wrap.
    """
    if not middlewares:
        return action.handler

    # Start from the handler and wrap inwards-to-outwards. ``current``
    # always represents "the next stage" from the perspective of the
    # middleware about to wrap it.
    current: NextHandler = action.handler
    for mw in reversed(list(middlewares)):
        current = _wrap(action, mw, current)
    return current


def _wrap(
    action: Action,
    middleware: Union[Middleware, MiddlewareCallable],
    next_: NextHandler,
) -> NextHandler:
    """Closure factory — binds ``action`` / ``middleware`` / ``next_``
    so each layer has its own captured trio without late-binding bugs.

    The default-argument trick on the inner async function pins the
    captured values at definition time. Without it, a loop body that
    re-binds ``mw`` / ``next_`` on each iteration would have every
    closure end up referencing the *last* iteration's values (the
    classic Python late-binding gotcha).
    """

    async def _wrapped(
        ctx: ActionContext,
        _action: Action = action,
        _mw: Union[Middleware, MiddlewareCallable] = middleware,
        _next: NextHandler = next_,
    ) -> ActionResult:
        return await _mw(_action, ctx, _next)

    return _wrapped


# ── Public surface ──────────────────────────────────────────────────────


__all__ = [
    "Middleware",
    "MiddlewareCallable",
    "NextHandler",
    "compose_chain",
]
