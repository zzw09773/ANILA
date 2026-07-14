"""Pure composition of the Gate 5 R3 routing pipeline.

The runtime intentionally has no HTTP client and no direct Agent call.  An
optional dispatcher is an adapter seam for tests or a later CSP-owned wiring
layer; it is invoked only after both structured decision and policy gate allow.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from anila_contracts import PolicyGateResult, RouteDecision
from anila_contracts.routing import RouteType

from .candidate_filter import (
    CapabilityFilter,
    CandidateFilterResult,
    RegistryEntry,
    RegistrySnapshot,
)
from .decision_engine import DecisionEngine, DecisionResult
from .policy_gate import ExecutionGrantInput, PolicyGate
from .request_context import RequestContext, RequestContextBuilder


class Dispatcher(Protocol):
    """Optional side-effect adapter; CSP remains the production authority."""

    def __call__(self, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class RuntimeResult:
    """Typed result of one no-surprises RouterRuntime evaluation."""

    context: RequestContext
    candidates: CandidateFilterResult
    decision_result: DecisionResult
    policy_result: PolicyGateResult | None
    entry: RegistryEntry | None
    grant_input: ExecutionGrantInput | None
    dispatch_result: Any = None
    dispatcher_called: bool = False

    @property
    def decision(self) -> RouteDecision | None:
        return self.decision_result.decision

    @property
    def policy(self) -> PolicyGateResult | None:
        return self.policy_result

    @property
    def allowed(self) -> bool:
        return bool(self.policy_result is not None and self.policy_result.allowed)

    @property
    def dispatched(self) -> bool:
        return self.dispatcher_called

    @property
    def reason_codes(self) -> tuple[str, ...]:
        values = list(self.candidates.reason_codes)
        values.extend(self.decision_result.reason_codes)
        if self.policy_result is not None:
            values.extend(self.policy_result.reason_codes)
        return tuple(dict.fromkeys(values))

    @property
    def reasons(self) -> tuple[str, ...]:
        return self.reason_codes


class ExecutionRuntime:
    """Compose builder → filter → decision → policy gate.

    No fallback direct-model call is made when an Agent route is denied.  The
    caller receives a typed denied result and may render a safe clarification.
    """

    def __init__(
        self,
        *,
        context_builder: RequestContextBuilder | None = None,
        capability_filter: CapabilityFilter | None = None,
        decision_engine: DecisionEngine | None = None,
        policy_gate: PolicyGate | None = None,
        dispatcher: Dispatcher | Callable[..., Any] | None = None,
    ) -> None:
        self.context_builder = context_builder or RequestContextBuilder()
        self.capability_filter = capability_filter or CapabilityFilter()
        self.decision_engine = decision_engine or DecisionEngine()
        self.policy_gate = policy_gate or PolicyGate()
        self.dispatcher = dispatcher

    def _build_context(
        self, context_input: RequestContext | Mapping[str, Any] | None
    ) -> RequestContext:
        if isinstance(context_input, RequestContext):
            return context_input
        return self.context_builder.build(context_input)

    def _evaluate(
        self,
        context_input: RequestContext | Mapping[str, Any] | None,
        provider_output: object,
        snapshot: RegistrySnapshot | None,
        *,
        now: datetime | None = None,
    ) -> tuple[
        RequestContext,
        CandidateFilterResult,
        DecisionResult,
        PolicyGateResult | None,
        RegistryEntry | None,
        ExecutionGrantInput | None,
    ]:
        context = self._build_context(context_input)
        candidates = self.capability_filter.filter(context, snapshot, now=now)
        decision_result = self.decision_engine.decide(
            provider_output,
            candidates,
            snapshot,
            context=context,
            now=now,
        )
        if decision_result.decision is None:
            return context, candidates, decision_result, None, None, None

        decision = decision_result.decision
        entry: RegistryEntry | None = None
        if decision.selected_agent_id is not None:
            entry = next(
                (
                    candidate
                    for candidate in candidates.candidates
                    if candidate.agent_id == decision.selected_agent_id
                ),
                None,
            )
        policy_result = self.policy_gate.evaluate(decision, context, snapshot, entry, now=now)
        grant_input: ExecutionGrantInput | None = None
        if (
            policy_result.allowed
            and entry is not None
            and decision.route_type is RouteType.SINGLE_AGENT
        ):
            grant_input = self.policy_gate.grant_input(decision, policy_result, context, entry)
        return context, candidates, decision_result, policy_result, entry, grant_input

    @staticmethod
    def _call_dispatcher(
        dispatcher: Dispatcher | Callable[..., Any],
        *,
        context: RequestContext,
        entry: RegistryEntry,
        decision: RouteDecision,
        policy: PolicyGateResult,
        grant_input: ExecutionGrantInput | None,
    ) -> Any:
        """Call an injected adapter through the one keyword-only contract."""

        return dispatcher(
            context=context,
            entry=entry,
            decision=decision,
            policy_result=policy,
            grant_input=grant_input,
        )

    @staticmethod
    def _is_async_dispatcher(dispatcher: Dispatcher | Callable[..., Any]) -> bool:
        return bool(
            inspect.iscoroutinefunction(dispatcher)
            or inspect.iscoroutinefunction(getattr(dispatcher, "__call__", None))
        )

    def execute(
        self,
        context_input: RequestContext | Mapping[str, Any] | None = None,
        provider_output: object = None,
        snapshot: RegistrySnapshot | None = None,
        *,
        dispatcher: Dispatcher | Callable[..., Any] | None = None,
        now: datetime | None = None,
    ) -> RuntimeResult:
        """Synchronously evaluate the pipeline.

        Async dispatch adapters should use :meth:`run`; an awaitable returned
        by a synchronous adapter is not executed here and therefore cannot
        accidentally start a downstream call on the wrong event loop.
        """

        context, candidates, decision_result, policy_result, entry, grant_input = self._evaluate(
            context_input, provider_output, snapshot, now=now
        )
        selected_dispatcher = dispatcher if dispatcher is not None else self.dispatcher
        dispatch_result: Any = None
        dispatcher_called = False
        if (
            selected_dispatcher is not None
            and policy_result is not None
            and policy_result.allowed
            and decision_result.is_dispatch
            and entry is not None
            and decision_result.decision is not None
            and not self._is_async_dispatcher(selected_dispatcher)
        ):
            maybe_result = self._call_dispatcher(
                selected_dispatcher,
                context=context,
                entry=entry,
                decision=decision_result.decision,
                policy=policy_result,
                grant_input=grant_input,
            )
            if inspect.isawaitable(maybe_result):
                close = getattr(maybe_result, "close", None)
                if callable(close):
                    close()
                raise TypeError("async dispatcher requires ExecutionRuntime.run()")
            dispatch_result = maybe_result
            dispatcher_called = True
        return RuntimeResult(
            context=context,
            candidates=candidates,
            decision_result=decision_result,
            policy_result=policy_result,
            entry=entry,
            grant_input=grant_input,
            dispatch_result=dispatch_result,
            dispatcher_called=dispatcher_called,
        )

    async def run(
        self,
        context_input: RequestContext | Mapping[str, Any] | None = None,
        provider_output: object = None,
        snapshot: RegistrySnapshot | None = None,
        *,
        dispatcher: Dispatcher | Callable[..., Any] | None = None,
        now: datetime | None = None,
    ) -> RuntimeResult:
        """Async equivalent that can await an injected dispatcher double."""

        context, candidates, decision_result, policy_result, entry, grant_input = self._evaluate(
            context_input, provider_output, snapshot, now=now
        )
        selected_dispatcher = dispatcher if dispatcher is not None else self.dispatcher
        dispatch_result: Any = None
        dispatcher_called = False
        if (
            selected_dispatcher is not None
            and policy_result is not None
            and policy_result.allowed
            and decision_result.is_dispatch
            and entry is not None
            and decision_result.decision is not None
        ):
            maybe_result = self._call_dispatcher(
                selected_dispatcher,
                context=context,
                entry=entry,
                decision=decision_result.decision,
                policy=policy_result,
                grant_input=grant_input,
            )
            if inspect.isawaitable(maybe_result):
                dispatch_result = await maybe_result
            else:
                dispatch_result = maybe_result
            dispatcher_called = True
        return RuntimeResult(
            context=context,
            candidates=candidates,
            decision_result=decision_result,
            policy_result=policy_result,
            entry=entry,
            grant_input=grant_input,
            dispatch_result=dispatch_result,
            dispatcher_called=dispatcher_called,
        )

    evaluate = execute


__all__ = ["Dispatcher", "ExecutionRuntime", "RuntimeResult"]
