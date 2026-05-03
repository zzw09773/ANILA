"""Sprint 2 tests — Middleware framework + 5 built-in middleware.

Covers:
- compose_chain ordering / short-circuit / pass-through
- Runner integration (run-level + action-level chains)
- TraceMiddleware spans, parent threading, error capture
- CostMiddleware tool budget gate, soft warn, local-model "free" default
- GuardrailMiddleware allow / deny / modify-params / modify-output
- ShellHookMiddleware (with a tiny Python script as the hook command)
- RetryMiddleware exception + error-result paths
"""

from __future__ import annotations

import json
import sys
from io import StringIO

import pytest

from agentic_rag.runtime.framework import (
    Action,
    ActionContext,
    ActionKind,
    ActionResult,
    Agent,
    ChatCompletionResponse,
    CostEstimate,
    FinishReason,
    Message,
    Runner,
    SideEffectClass,
    ToolCall,
    Usage,
)
from agentic_rag.runtime.framework.middleware import (
    Allow,
    CostBudget,
    CostBudgetExceeded,
    CostMiddleware,
    CostTracker,
    Deny,
    GuardrailMiddleware,
    InMemoryBackend,
    ModelPrice,
    ModifyOutput,
    ModifyParams,
    PriceTable,
    RetryMiddleware,
    RetryPolicy,
    ShellHookMiddleware,
    Span,
    StdoutBackend,
    TraceMiddleware,
    compose_chain,
    input_guardrail,
    output_guardrail,
    record_llm_usage_from_run,
)


# ── Test helpers ────────────────────────────────────────────────────────


async def _ok_handler(ctx: ActionContext) -> ActionResult:
    return ActionResult(output={"params": ctx.params})


def _action(
    name: str = "t",
    *,
    handler=_ok_handler,
    middleware: tuple = (),
    kind: ActionKind = ActionKind.SYNC_TOOL,
    cost_dollars: float = 0.0,
) -> Action:
    return Action(
        name=name,
        description="",
        kind=kind,
        handler=handler,
        middleware=middleware,
        cost_estimate=CostEstimate(dollars=cost_dollars),
    )


def _ctx(params: dict | None = None) -> ActionContext:
    return ActionContext(
        run_id="r1", agent_name="a", params=params or {}, history=()
    )


class _ScriptedProvider:
    """Provider scripted with a list of ChatCompletionResponse."""

    def __init__(self, responses: list[ChatCompletionResponse]) -> None:
        self._scripted = list(responses)

    async def chat_completion(self, messages, tools=None, *, model, stream=False, **kw):
        return self._scripted.pop(0)

    async def embeddings(self, texts, *, model, **kw):
        raise NotImplementedError


def _resp(text="", tool_calls=(), finish=FinishReason.STOP, tokens=(10, 5)):
    return ChatCompletionResponse(
        message=Message.assistant(content=text, tool_calls=tool_calls),
        usage=Usage(
            requests=1,
            input_tokens=tokens[0],
            output_tokens=tokens[1],
            total_tokens=tokens[0] + tokens[1],
        ),
        finish_reason=finish,
    )


# ── compose_chain unit tests ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compose_empty_chain_returns_handler() -> None:
    action = _action()
    chain = compose_chain(action, [])
    result = await chain(_ctx({"q": "x"}))
    assert result.output == {"params": {"q": "x"}}


@pytest.mark.asyncio
async def test_compose_chain_runs_outermost_first() -> None:
    """First middleware in the list sees input first AND output last."""
    log: list[str] = []

    async def outer(action, ctx, next_):
        log.append("outer-pre")
        r = await next_(ctx)
        log.append("outer-post")
        return r

    async def inner(action, ctx, next_):
        log.append("inner-pre")
        r = await next_(ctx)
        log.append("inner-post")
        return r

    chain = compose_chain(_action(), [outer, inner])
    await chain(_ctx())
    # Outer wraps inner wraps handler — inner.post must fire before outer.post.
    assert log == ["outer-pre", "inner-pre", "inner-post", "outer-post"]


@pytest.mark.asyncio
async def test_compose_chain_middleware_can_short_circuit() -> None:
    handler_called = False

    async def handler(ctx):
        nonlocal handler_called
        handler_called = True
        return ActionResult(output="ran")

    async def short(action, ctx, next_):
        return ActionResult(error="blocked")

    chain = compose_chain(_action(handler=handler), [short])
    r = await chain(_ctx())
    assert r.is_error
    assert handler_called is False


@pytest.mark.asyncio
async def test_compose_no_late_binding_bug() -> None:
    """All middlewares must keep their own captured context, no late-binding."""
    seen: list[str] = []

    def make(name):
        async def mw(action, ctx, next_):
            seen.append(name)
            return await next_(ctx)
        return mw

    chain = compose_chain(_action(), [make("a"), make("b"), make("c")])
    await chain(_ctx())
    assert seen == ["a", "b", "c"]


# ── Runner integration ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_runner_invokes_run_level_middleware() -> None:
    """Runner.middleware wraps every Action in the run."""
    invocations: list[str] = []

    async def trace_mw(action, ctx, next_):
        invocations.append(f"pre:{action.name}")
        r = await next_(ctx)
        invocations.append(f"post:{action.name}")
        return r

    async def search(ctx):
        return ActionResult(output="results")

    action = Action(
        name="search", description="", kind=ActionKind.SYNC_TOOL, handler=search
    )
    tc = ToolCall(id="c1", name="search", arguments="{}")
    provider = _ScriptedProvider(
        [
            _resp(tool_calls=(tc,), finish=FinishReason.TOOL_CALLS),
            _resp(text="done"),
        ]
    )
    agent = Agent(
        name="a", instructions="", provider=provider, model="m", actions=[action]
    )
    await Runner(middleware=[trace_mw]).run(agent, "go")

    assert invocations == ["pre:search", "post:search"]


@pytest.mark.asyncio
async def test_runner_action_level_middleware_runs_inside_run_level() -> None:
    """Action.middleware composes inside Runner.middleware."""
    log: list[str] = []

    async def run_mw(action, ctx, next_):
        log.append("run-pre")
        r = await next_(ctx)
        log.append("run-post")
        return r

    async def action_mw(action, ctx, next_):
        log.append("action-pre")
        r = await next_(ctx)
        log.append("action-post")
        return r

    async def handler(ctx):
        log.append("handler")
        return ActionResult(output="x")

    action = Action(
        name="t",
        description="",
        kind=ActionKind.SYNC_TOOL,
        handler=handler,
        middleware=(action_mw,),
    )
    tc = ToolCall(id="c1", name="t", arguments="{}")
    provider = _ScriptedProvider(
        [
            _resp(tool_calls=(tc,), finish=FinishReason.TOOL_CALLS),
            _resp(text="done"),
        ]
    )
    agent = Agent(name="a", instructions="", provider=provider, model="m", actions=[action])

    await Runner(middleware=[run_mw]).run(agent, "go")
    assert log == ["run-pre", "action-pre", "handler", "action-post", "run-post"]


# ── TraceMiddleware ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_trace_middleware_records_span_per_action() -> None:
    backend = InMemoryBackend()
    trace = TraceMiddleware(backend)

    async def search(ctx):
        return ActionResult(output={"hits": 3})

    action = Action(
        name="search", description="", kind=ActionKind.SYNC_TOOL,
        handler=search, side_effect_class=SideEffectClass.PURE,
    )
    tc = ToolCall(id="c1", name="search", arguments='{"q":"x"}')
    provider = _ScriptedProvider(
        [
            _resp(tool_calls=(tc,), finish=FinishReason.TOOL_CALLS),
            _resp(text="done"),
        ]
    )
    agent = Agent(name="a", instructions="", provider=provider, model="m", actions=[action])

    await Runner(middleware=[trace]).run(agent, "go")

    assert len(backend.spans) == 1
    span = backend.spans[0]
    assert span.action_name == "search"
    assert span.action_kind == "sync_tool"
    assert span.side_effect_class == "pure"
    assert span.params == {"q": "x"}
    assert span.output == {"hits": 3}
    assert span.error is None
    assert span.elapsed_seconds >= 0.0


@pytest.mark.asyncio
async def test_trace_middleware_records_error_and_re_raises_exception() -> None:
    backend = InMemoryBackend()
    trace = TraceMiddleware(backend)

    async def boom_handler(ctx):
        raise RuntimeError("kaboom")

    chain = compose_chain(_action(handler=boom_handler), [trace])
    with pytest.raises(RuntimeError):
        await chain(_ctx())
    assert len(backend.spans) == 1
    assert "kaboom" in backend.spans[0].error
    assert backend.spans[0].ended_at is not None


@pytest.mark.asyncio
async def test_trace_middleware_threads_parent_span_id() -> None:
    backend = InMemoryBackend()
    trace = TraceMiddleware(backend)

    async def inner_handler(ctx):
        # Verify the parent span id flowed through context.metadata
        return ActionResult(
            output={"parent": ctx.metadata.get("_trace_parent_span_id")}
        )

    inner = _action(name="inner", handler=inner_handler)

    async def outer_handler(ctx):
        # Manually drive the inner action through the same trace mw
        # so we exercise the parent threading.
        result = await compose_chain(inner, [trace])(ctx)
        return ActionResult(output=result.output)

    outer = _action(name="outer", handler=outer_handler)
    await compose_chain(outer, [trace])(_ctx())

    assert len(backend.spans) == 2
    inner_span = next(s for s in backend.spans if s.action_name == "inner")
    outer_span = next(s for s in backend.spans if s.action_name == "outer")
    assert inner_span.parent_span_id == outer_span.span_id


@pytest.mark.asyncio
async def test_trace_middleware_capture_toggles_drop_payloads() -> None:
    backend = InMemoryBackend()
    trace = TraceMiddleware(backend, capture_params=False, capture_output=False)
    chain = compose_chain(_action(), [trace])
    await chain(_ctx({"secret": "abc"}))
    assert backend.spans[0].params == {}
    assert backend.spans[0].output is None


@pytest.mark.asyncio
async def test_stdout_backend_writes_json_line() -> None:
    buf = StringIO()
    backend = StdoutBackend(stream=buf)
    span = Span(
        span_id="abc", parent_span_id=None, run_id="r", agent_name="a",
        action_name="t", action_kind="sync_tool", side_effect_class="pure",
        params={"q": "x"},
        started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        elapsed_seconds=0.1,
    )
    await backend.record(span)
    line = buf.getvalue().strip()
    parsed = json.loads(line)
    assert parsed["action_name"] == "t"


@pytest.mark.asyncio
async def test_trace_backend_failure_does_not_break_run() -> None:
    """Backend exceptions must be swallowed — tracing is best-effort."""

    class BrokenBackend:
        async def record(self, span):
            raise RuntimeError("backend down")

    trace = TraceMiddleware(BrokenBackend())
    chain = compose_chain(_action(), [trace])
    result = await chain(_ctx())
    assert result.output == {"params": {}}  # handler ran, run survived


# ── CostMiddleware ─────────────────────────────────────────────────────


def test_price_table_exact_match_and_prefix_fallback() -> None:
    table = PriceTable({"gpt-4o-mini": ModelPrice(0.15, 0.60)})
    assert table.get("gpt-4o-mini") is not None
    # Longest-prefix fallback
    assert table.get("gpt-4o-mini-2024-07-18") is not None
    assert table.get("gpt-4o-mini-2024-07-18").input_per_million == 0.15


def test_price_table_unknown_model_returns_none_for_local_default() -> None:
    """Local models (vLLM / Ollama / NIM) — empty table → None → free."""
    table = PriceTable()
    assert table.get("gemma-2-9b-it") is None
    assert table.get("anything") is None
    assert "anything" not in table


def test_model_price_cost_for_usage_includes_cached_discount() -> None:
    price = ModelPrice(input_per_million=10.0, output_per_million=30.0,
                       cached_input_per_million=5.0)
    from agentic_rag.runtime.framework.usage import InputTokensDetails
    usage = Usage(
        requests=1, input_tokens=100, output_tokens=50, total_tokens=150,
        input_tokens_details=InputTokensDetails(cached_tokens=40),
    )
    # 60 regular @ $10/M + 40 cached @ $5/M + 50 output @ $30/M
    expected = 60 * 10 / 1e6 + 40 * 5 / 1e6 + 50 * 30 / 1e6
    assert abs(price.cost_for_usage(usage) - expected) < 1e-9


def test_cost_tracker_local_model_records_tokens_zero_dollars() -> None:
    tracker = CostTracker()
    table = PriceTable()  # empty — local
    usage = Usage(
        requests=1, input_tokens=1000, output_tokens=200, total_tokens=1200,
    )
    tracker.record_llm_usage("gemma-2-9b-it", usage, table)
    assert tracker.total_dollars == 0.0
    assert tracker.token_totals["gemma-2-9b-it"].input_tokens == 1000
    assert tracker.token_totals["gemma-2-9b-it"].output_tokens == 200


def test_cost_tracker_records_priced_model() -> None:
    tracker = CostTracker()
    table = PriceTable({"gpt-4o": ModelPrice(2.50, 10.00)})
    usage = Usage(requests=1, input_tokens=1000, output_tokens=500, total_tokens=1500)
    tracker.record_llm_usage("gpt-4o", usage, table)
    expected = 1000 * 2.5 / 1e6 + 500 * 10 / 1e6  # = 0.0025 + 0.005
    assert abs(tracker.total_dollars - expected) < 1e-9


@pytest.mark.asyncio
async def test_cost_middleware_attributes_action_estimate() -> None:
    tracker = CostTracker()
    cost = CostMiddleware(tracker)
    action = _action(cost_dollars=0.01)
    chain = compose_chain(action, [cost])
    await chain(_ctx())
    assert tracker.by_action["t"] == 0.01
    assert tracker.total_dollars == 0.01


@pytest.mark.asyncio
async def test_cost_middleware_does_not_attribute_failed_actions() -> None:
    tracker = CostTracker()
    cost = CostMiddleware(tracker)

    async def failing(ctx):
        return ActionResult(error="api 500")

    action = _action(handler=failing, cost_dollars=0.01)
    chain = compose_chain(action, [cost])
    await chain(_ctx())
    assert tracker.total_dollars == 0.0


@pytest.mark.asyncio
async def test_cost_middleware_hard_cap_raises() -> None:
    tracker = CostTracker(total_dollars=4.95)
    cost = CostMiddleware(tracker, budget=CostBudget(hard_cap_dollars=5.00))
    action = _action(cost_dollars=0.10)
    chain = compose_chain(action, [cost])
    with pytest.raises(CostBudgetExceeded):
        await chain(_ctx())
    assert tracker.total_dollars == 4.95  # not incremented on rejection


@pytest.mark.asyncio
async def test_cost_middleware_soft_warn_fires_once() -> None:
    tracker = CostTracker()
    warned: list[tuple[str, float, float]] = []

    async def on_warn(name, projected, threshold):
        warned.append((name, projected, threshold))

    cost = CostMiddleware(
        tracker,
        budget=CostBudget(soft_warn_dollars=1.00),
        on_soft_warn=on_warn,
    )
    action = _action(cost_dollars=2.00)
    chain = compose_chain(action, [cost])
    await chain(_ctx())
    await chain(_ctx())  # second call shouldn't fire warning again
    assert len(warned) == 1


@pytest.mark.asyncio
async def test_record_llm_usage_from_run_walks_message_items() -> None:
    """Helper attributes LLM cost from a run's MessageOutputItems."""
    tracker = CostTracker()
    table = PriceTable({"gpt-4o": ModelPrice(2.50, 10.00)})

    provider = _ScriptedProvider([_resp(text="done", tokens=(100, 50))])
    agent = Agent(name="a", instructions="", provider=provider, model="gpt-4o")
    result = await Runner().run(agent, "hi")

    record_llm_usage_from_run(tracker, table, result.items, model="gpt-4o")
    expected = 100 * 2.5 / 1e6 + 50 * 10 / 1e6
    assert abs(tracker.total_dollars - expected) < 1e-9


# ── GuardrailMiddleware ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_guardrail_input_deny_short_circuits() -> None:
    @input_guardrail
    async def block_empty(action, ctx, _):
        if not ctx.params.get("query"):
            return Deny("query required")
        return Allow()

    handler_called = False

    async def handler(ctx):
        nonlocal handler_called
        handler_called = True
        return ActionResult(output="ok")

    chain = compose_chain(
        _action(handler=handler), [GuardrailMiddleware([block_empty])]
    )
    result = await chain(_ctx({}))  # missing query
    assert result.is_error
    assert "query required" in result.error
    assert handler_called is False


@pytest.mark.asyncio
async def test_guardrail_input_modify_params_rewrites_context() -> None:
    @input_guardrail
    async def add_default_top_k(action, ctx, _):
        params = dict(ctx.params)
        params.setdefault("top_k", 5)
        return ModifyParams(params)

    captured = {}

    async def handler(ctx):
        captured.update(ctx.params)
        return ActionResult(output="ok")

    chain = compose_chain(
        _action(handler=handler), [GuardrailMiddleware([add_default_top_k])]
    )
    await chain(_ctx({"q": "rag"}))
    assert captured == {"q": "rag", "top_k": 5}


@pytest.mark.asyncio
async def test_guardrail_output_modify_replaces_output() -> None:
    @output_guardrail
    async def add_citation(action, ctx, result):
        new_out = dict(result.output)
        new_out["_cited"] = True
        return ModifyOutput(new_out)

    chain = compose_chain(
        _action(), [GuardrailMiddleware([add_citation])]
    )
    result = await chain(_ctx({"q": "x"}))
    assert result.output["_cited"] is True


@pytest.mark.asyncio
async def test_guardrail_output_deny_replaces_with_error() -> None:
    @output_guardrail
    async def needs_citation(action, ctx, result):
        if "_cited" not in result.output:
            return Deny("no citation in output")
        return Allow()

    chain = compose_chain(
        _action(), [GuardrailMiddleware([needs_citation])]
    )
    result = await chain(_ctx({"q": "x"}))
    assert result.is_error
    assert "no citation" in result.error


def test_guardrail_middleware_rejects_invalid_stage() -> None:
    class WeirdGuardrail:
        stage = "unknown"

        async def __call__(self, action, ctx, result):
            return Allow()

    with pytest.raises(ValueError, match="invalid stage"):
        GuardrailMiddleware([WeirdGuardrail()])


# ── ShellHookMiddleware ────────────────────────────────────────────────


@pytest.fixture
def hook_script(tmp_path):
    """Build a tiny Python script that reads stdin and writes a fixed
    JSON decision back. Returns the argv list."""
    script = tmp_path / "hook.py"
    script.write_text(
        'import json, sys\n'
        'data = json.load(sys.stdin)\n'
        'decision = {"decision": "allow"}\n'
        'if data["params"].get("blocked"):\n'
        '    decision = {"decision": "deny", "reason": "blocked by policy"}\n'
        'elif data["params"].get("rewrite"):\n'
        '    decision = {"decision": "modify", "params": {"rewritten": True}}\n'
        'print(json.dumps(decision))\n'
    )
    return [sys.executable, str(script)]


@pytest.mark.asyncio
async def test_shell_hook_allow_passes_through(hook_script) -> None:
    hook = ShellHookMiddleware(when="before", command=hook_script, timeout_seconds=10)
    chain = compose_chain(_action(), [hook])
    result = await chain(_ctx({"q": "ok"}))
    assert not result.is_error
    assert result.output == {"params": {"q": "ok"}}


@pytest.mark.asyncio
async def test_shell_hook_deny_short_circuits(hook_script) -> None:
    hook = ShellHookMiddleware(when="before", command=hook_script, timeout_seconds=10)
    chain = compose_chain(_action(), [hook])
    result = await chain(_ctx({"blocked": True}))
    assert result.is_error
    assert "blocked by policy" in result.error


@pytest.mark.asyncio
async def test_shell_hook_modify_rewrites_params(hook_script) -> None:
    hook = ShellHookMiddleware(when="before", command=hook_script, timeout_seconds=10)
    chain = compose_chain(_action(), [hook])
    result = await chain(_ctx({"rewrite": True}))
    assert result.output == {"params": {"rewritten": True}}


@pytest.mark.asyncio
async def test_shell_hook_missing_command_fails_open(tmp_path) -> None:
    """Misconfigured hook (binary not found) → allow-by-default; never crash."""
    hook = ShellHookMiddleware(
        when="before",
        command=[str(tmp_path / "does_not_exist")],
        timeout_seconds=2,
    )
    chain = compose_chain(_action(), [hook])
    result = await chain(_ctx({"q": "x"}))
    assert not result.is_error  # allow-by-default kept the run going


def test_shell_hook_rejects_string_command() -> None:
    with pytest.raises(TypeError, match="list/tuple"):
        ShellHookMiddleware(when="before", command="not a list", timeout_seconds=1)


def test_shell_hook_rejects_invalid_when() -> None:
    with pytest.raises(ValueError):
        ShellHookMiddleware(when="middle", command=["true"], timeout_seconds=1)


# ── RetryMiddleware ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retry_succeeds_on_second_attempt() -> None:
    attempts = 0

    async def flaky(ctx):
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            raise ConnectionError("connection refused")
        return ActionResult(output="ok")

    retry = RetryMiddleware(
        policy=RetryPolicy(max_attempts=3, initial_delay_seconds=0.001),
        on_exceptions=(ConnectionError,),
    )
    chain = compose_chain(_action(handler=flaky), [retry])
    result = await chain(_ctx())
    assert not result.is_error
    assert attempts == 2


@pytest.mark.asyncio
async def test_retry_re_raises_after_max_attempts() -> None:
    async def always_fail(ctx):
        raise ConnectionError("perma down")

    retry = RetryMiddleware(
        policy=RetryPolicy(max_attempts=2, initial_delay_seconds=0.001),
        on_exceptions=(ConnectionError,),
    )
    chain = compose_chain(_action(handler=always_fail), [retry])
    with pytest.raises(ConnectionError):
        await chain(_ctx())


@pytest.mark.asyncio
async def test_retry_does_not_catch_unmatched_exception_type() -> None:
    async def wrong_type(ctx):
        raise ValueError("not retryable")

    retry = RetryMiddleware(
        policy=RetryPolicy(max_attempts=3, initial_delay_seconds=0.001),
        on_exceptions=(ConnectionError,),
    )
    chain = compose_chain(_action(handler=wrong_type), [retry])
    with pytest.raises(ValueError):
        await chain(_ctx())


@pytest.mark.asyncio
async def test_retry_on_error_result_predicate() -> None:
    attempts = 0

    async def rate_limited(ctx):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return ActionResult(error="rate limited; try again")
        return ActionResult(output="finally ok")

    retry = RetryMiddleware(
        policy=RetryPolicy(max_attempts=5, initial_delay_seconds=0.001),
        retry_on_error=lambda r: "rate limited" in (r.error or "").lower(),
    )
    chain = compose_chain(_action(handler=rate_limited), [retry])
    result = await chain(_ctx())
    assert not result.is_error
    assert attempts == 3


@pytest.mark.asyncio
async def test_retry_passes_through_non_matching_errors() -> None:
    async def other_error(ctx):
        return ActionResult(error="permission denied")

    retry = RetryMiddleware(
        policy=RetryPolicy(max_attempts=3, initial_delay_seconds=0.001),
        retry_on_error=lambda r: "rate limited" in (r.error or "").lower(),
    )
    chain = compose_chain(_action(handler=other_error), [retry])
    result = await chain(_ctx())
    assert result.is_error
    assert "permission denied" in result.error


def test_retry_policy_delay_grows_exponentially() -> None:
    p = RetryPolicy(
        initial_delay_seconds=0.1,
        backoff_multiplier=2.0,
        max_delay_seconds=10.0,
        jitter=0.0,
    )
    assert p.delay_for(1) == 0.1
    assert p.delay_for(2) == 0.2
    assert p.delay_for(3) == 0.4


def test_retry_policy_caps_delay() -> None:
    p = RetryPolicy(
        initial_delay_seconds=1.0,
        backoff_multiplier=10.0,
        max_delay_seconds=5.0,
        jitter=0.0,
    )
    assert p.delay_for(5) == 5.0  # capped
