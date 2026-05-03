"""Middleware framework — the one chain that wraps every Action.

Architecture spec: ``docs/anila-agent-framework-architecture.md`` §3.1.

Lifecycle, guardrails, tracing, cost, retry — all of these are
*middleware around an Action's execution*, not separate frameworks.
A Middleware is anything matching the Protocol shape:

    async def __call__(action, context, next_) -> ActionResult: ...

Composition: middlewares wrap the handler in registration order. The
first middleware in the list is the outermost wrap (sees input first,
sees output last); the last is the innermost wrap (closest to the
handler). Both ``Runner.middleware`` (run-level, applied to every
Action in a run) and ``Action.middleware`` (per-Action, registered at
Action construction) are honoured. Run-level wraps action-level wraps
the handler.

Built-in middleware shipped in v0.1 Sprint 2:

- ``TraceMiddleware`` — opens spans, records input/output/timing
- ``CostMiddleware`` — accumulates token / dollar cost via a per-model
  price table; can pre-gate against a CostBudget
- ``GuardrailMiddleware`` — composes input / output ``Guardrail``
  instances (PII checks, content policies, citation enforcement, …)
- ``ShellHookMiddleware`` — claude-code-style PreToolUse / PostToolUse
  hooks: a shell command receives JSON on stdin and emits a JSON
  decision (allow / deny / modify) on stdout
- ``RetryMiddleware`` — retries on configurable exception or error
  result patterns with exponential backoff

User-defined middleware is just a Python callable matching the
Protocol — no inheritance, no framework registration.
"""

from agentic_rag.runtime.framework.middleware.cost import (
    CostBudget,
    CostBudgetExceeded,
    CostMiddleware,
    CostTracker,
    DEFAULT_ANTHROPIC_PRICES,
    DEFAULT_OPENAI_PRICES,
    ModelPrice,
    PriceTable,
    record_llm_usage_from_run,
)
from agentic_rag.runtime.framework.middleware.guardrail import (
    Allow,
    Deny,
    Guardrail,
    GuardrailDecision,
    GuardrailMiddleware,
    ModifyOutput,
    ModifyParams,
    input_guardrail,
    output_guardrail,
)
from agentic_rag.runtime.framework.middleware.output_trimmer import (
    ToolOutputTrimmerMiddleware,
)
from agentic_rag.runtime.framework.middleware.protocol import (
    Middleware,
    MiddlewareCallable,
    NextHandler,
    compose_chain,
)
from agentic_rag.runtime.framework.middleware.retry import (
    ErrorPredicate,
    RetryMiddleware,
    RetryPolicy,
)
from agentic_rag.runtime.framework.middleware.shell_hook import ShellHookMiddleware
from agentic_rag.runtime.framework.middleware.tracing import (
    InMemoryBackend,
    Span,
    StdoutBackend,
    TraceMiddleware,
    TracingBackend,
)

__all__ = [
    "Allow",
    "CostBudget",
    "CostBudgetExceeded",
    "CostMiddleware",
    "CostTracker",
    "DEFAULT_ANTHROPIC_PRICES",
    "DEFAULT_OPENAI_PRICES",
    "Deny",
    "ErrorPredicate",
    "Guardrail",
    "GuardrailDecision",
    "GuardrailMiddleware",
    "InMemoryBackend",
    "Middleware",
    "MiddlewareCallable",
    "ModelPrice",
    "ModifyOutput",
    "ModifyParams",
    "NextHandler",
    "PriceTable",
    "RetryMiddleware",
    "RetryPolicy",
    "ShellHookMiddleware",
    "Span",
    "StdoutBackend",
    "ToolOutputTrimmerMiddleware",
    "TraceMiddleware",
    "TracingBackend",
    "compose_chain",
    "input_guardrail",
    "output_guardrail",
    "record_llm_usage_from_run",
]
