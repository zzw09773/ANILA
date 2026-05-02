"""Sprint 1 stage B tests — Action / Agent / Runner end-to-end.

Acceptance gate: an Agent driven by Runner against a FakeProvider
completes a one-tool-call cycle, emits the expected RunItems, and
returns the assistant's final answer.

The FakeProvider is scripted: callers seed it with a list of
ChatCompletionResponse objects and it returns them in order. That
keeps the runner contract honest (it must work against any
LLMProvider impl, not just OpenAICompatProvider) and keeps the tests
hermetic — no network, no openai dependency required.
"""

from __future__ import annotations

import json

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
    HandoffItem,
    MaxTurnsExceeded,
    Message,
    MessageOutputItem,
    ModelSettings,
    Role,
    Runner,
    SideEffectClass,
    ToolCall,
    ToolCallItem,
    ToolDefinition,
    ToolRegistry,
    ToolResultItem,
    Usage,
    UserError,
)


# ── FakeProvider ────────────────────────────────────────────────────────


class FakeProvider:
    """Scripted provider for tests.

    Hand it a list of ``ChatCompletionResponse`` objects; each call
    pops the front of the queue. ``calls`` records every invocation
    so tests can assert on what the runner sent.
    """

    def __init__(self, scripted: list[ChatCompletionResponse]) -> None:
        self._scripted = list(scripted)
        self.calls: list[dict] = []

    async def chat_completion(
        self,
        messages,
        tools=None,
        *,
        model: str,
        stream: bool = False,
        **kwargs,
    ):
        self.calls.append(
            {
                "messages": list(messages),
                "tools": list(tools) if tools else None,
                "model": model,
                "stream": stream,
                "kwargs": dict(kwargs),
            }
        )
        if not self._scripted:
            raise AssertionError(
                "FakeProvider exhausted — runner asked for more turns than scripted"
            )
        return self._scripted.pop(0)

    async def embeddings(self, texts, *, model, **kwargs):
        raise NotImplementedError("FakeProvider doesn't implement embeddings")


def _resp(
    *,
    text: str = "",
    tool_calls: tuple[ToolCall, ...] = (),
    finish: FinishReason = FinishReason.STOP,
    tokens: tuple[int, int] = (10, 5),
) -> ChatCompletionResponse:
    """Build a minimal ChatCompletionResponse for scripting."""
    msg = Message.assistant(content=text, tool_calls=tool_calls)
    usage = Usage(
        requests=1,
        input_tokens=tokens[0],
        output_tokens=tokens[1],
        total_tokens=tokens[0] + tokens[1],
    )
    return ChatCompletionResponse(message=msg, usage=usage, finish_reason=finish)


# ── Items / Action / Tool unit tests ────────────────────────────────────


def test_message_factories_and_role_enforcement() -> None:
    sys_ = Message.system("you are helpful")
    user = Message.user("hello")
    assert sys_.role is Role.SYSTEM
    assert user.role is Role.USER

    with pytest.raises(ValueError):
        # tool message without tool_call_id is rejected
        Message(role=Role.TOOL, content="result")

    with pytest.raises(ValueError):
        # tool_calls only allowed on assistant
        Message(role=Role.USER, content="x", tool_calls=(
            ToolCall(id="c1", name="f", arguments="{}"),
        ))


def test_tool_call_parsed_arguments_decodes_json() -> None:
    call = ToolCall(id="c1", name="search", arguments='{"q": "rag"}')
    assert call.parsed_arguments() == {"q": "rag"}

    call_empty = ToolCall(id="c2", name="search", arguments="")
    assert call_empty.parsed_arguments() == {}


def test_tool_call_invalid_json_raises() -> None:
    call = ToolCall(id="c1", name="search", arguments="{not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        call.parsed_arguments()


def test_tool_result_requires_exactly_one_of_output_or_error() -> None:
    from agentic_rag.runtime.framework import ToolResult

    with pytest.raises(ValueError):
        ToolResult(call_id="c1", name="search")  # neither
    with pytest.raises(ValueError):
        ToolResult(call_id="c1", name="search", output="x", error="y")  # both


def test_action_rejects_bad_inputs() -> None:
    async def handler(ctx: ActionContext) -> ActionResult:  # noqa: ARG001
        return ActionResult(output="ok")

    with pytest.raises(UserError, match="non-empty"):
        Action(name="", description="x", kind=ActionKind.SYNC_TOOL, handler=handler)

    with pytest.raises(UserError, match="ActionKind"):
        Action(name="ok", description="x", kind="sync_tool", handler=handler)  # type: ignore[arg-type]


def test_action_result_output_as_string_handles_dict() -> None:
    r = ActionResult(output={"results": [1, 2, 3]})
    assert json.loads(r.output_as_string()) == {"results": [1, 2, 3]}

    r_err = ActionResult(error="boom")
    assert r_err.output_as_string() == "[error] boom"


def test_tool_registry_blocks_duplicate_names() -> None:
    async def h(ctx):  # noqa: ARG001
        return ActionResult(output=None)

    a1 = Action(name="x", description="", kind=ActionKind.SYNC_TOOL, handler=h)
    a2 = Action(name="x", description="", kind=ActionKind.SYNC_TOOL, handler=h)

    registry = ToolRegistry([a1])
    with pytest.raises(UserError, match="conflict"):
        registry.register(a2)


def test_tool_registry_excludes_bg_task_from_llm_visible() -> None:
    async def h(ctx):  # noqa: ARG001
        return ActionResult(output=None)

    sync = Action(name="t1", description="", kind=ActionKind.SYNC_TOOL, handler=h)
    bg = Action(name="t2", description="", kind=ActionKind.BG_TASK, handler=h)
    registry = ToolRegistry([sync, bg])

    assert len(registry) == 2
    visible = registry.llm_visible()
    assert [a.name for a in visible] == ["t1"]


def test_tool_definition_to_openai_dict_shape() -> None:
    td = ToolDefinition(
        name="search",
        description="vector search",
        parameters={"type": "object", "properties": {"q": {"type": "string"}}},
    )
    payload = td.to_openai_dict()
    assert payload["type"] == "function"
    assert payload["function"]["name"] == "search"
    assert payload["function"]["parameters"]["properties"]["q"]["type"] == "string"


# ── Agent unit tests ────────────────────────────────────────────────────


def test_agent_builds_registry_and_synthetic_handoffs() -> None:
    async def h(ctx):  # noqa: ARG001
        return ActionResult(output="ok")

    search = Action(
        name="search", description="x", kind=ActionKind.SYNC_TOOL, handler=h
    )
    verifier = Agent(
        name="verifier",
        instructions="check facts",
        provider=FakeProvider([]),
        model="m",
    )
    primary = Agent(
        name="primary",
        instructions="answer questions",
        provider=FakeProvider([]),
        model="m",
        actions=[search],
        handoffs=[verifier],
    )
    assert "search" in primary.registry
    assert "transfer_to_verifier" in primary.registry


def test_agent_rejects_zero_max_turns() -> None:
    with pytest.raises(UserError):
        Agent(
            name="a",
            instructions="",
            provider=FakeProvider([]),
            model="m",
            max_turns=0,
        )


def test_model_settings_drops_none_kwargs() -> None:
    s = ModelSettings(temperature=0.7, top_p=None, extra={"seed": 42})
    kwargs = s.to_provider_kwargs()
    assert kwargs == {"temperature": 0.7, "seed": 42}


# ── Runner end-to-end ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_runner_returns_immediately_when_no_tool_calls() -> None:
    """Single-turn run: LLM responds with text, runner returns it."""
    provider = FakeProvider([_resp(text="42 is the answer")])
    agent = Agent(
        name="a",
        instructions="be helpful",
        provider=provider,
        model="m",
    )

    result = await Runner().run(agent, "what's the answer?")

    assert result.final_output == "42 is the answer"
    assert result.turns == 1
    assert result.final_agent_name == "a"
    assert len(provider.calls) == 1

    # System prompt was injected
    sent = provider.calls[0]["messages"]
    assert sent[0].role is Role.SYSTEM
    assert sent[0].content == "be helpful"
    assert sent[1].role is Role.USER

    # One MessageOutputItem in the audit trail
    assert len(result.items) == 1
    assert isinstance(result.items[0], MessageOutputItem)


@pytest.mark.asyncio
async def test_runner_dispatches_one_tool_then_returns_final() -> None:
    """Two-turn run: LLM calls a tool, then writes the answer."""

    async def search(ctx: ActionContext) -> ActionResult:
        assert ctx.params == {"q": "anila"}
        assert ctx.agent_name == "rag-agent"
        return ActionResult(output={"results": ["doc1", "doc2"]})

    search_action = Action(
        name="search",
        description="vector search",
        kind=ActionKind.SYNC_TOOL,
        handler=search,
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        side_effect_class=SideEffectClass.PURE,
        cost_estimate=CostEstimate(tokens=0, dollars=0.0001),
    )

    tool_call = ToolCall(id="call_1", name="search", arguments='{"q": "anila"}')
    provider = FakeProvider(
        [
            _resp(tool_calls=(tool_call,), finish=FinishReason.TOOL_CALLS),
            _resp(text="found doc1 and doc2"),
        ]
    )
    agent = Agent(
        name="rag-agent",
        instructions="use search",
        provider=provider,
        model="m",
        actions=[search_action],
    )

    result = await Runner().run(agent, "look up anila")

    assert result.final_output == "found doc1 and doc2"
    assert result.turns == 2
    assert result.usage.requests == 2
    assert result.usage.input_tokens == 20
    assert result.usage.output_tokens == 10

    # Audit trail: assistant tool-call message → ToolCallItem → ToolResultItem → final assistant
    assert isinstance(result.items[0], MessageOutputItem)
    assert isinstance(result.items[1], ToolCallItem)
    assert isinstance(result.items[2], ToolResultItem)
    assert isinstance(result.items[3], MessageOutputItem)
    # Tool call item carries the LLM-emitted call verbatim
    assert result.items[1].call.name == "search"
    # Tool result item carries the handler's serialised output
    assert result.items[2].result.output is not None

    # The tool result was visible to the second LLM call
    second_call_messages = provider.calls[1]["messages"]
    last_role = second_call_messages[-1].role
    assert last_role is Role.TOOL


@pytest.mark.asyncio
async def test_runner_surfaces_handler_exception_as_tool_error() -> None:
    """Handler raises → runner feeds an error message to the LLM, not crash."""

    async def boom(ctx):  # noqa: ARG001
        raise RuntimeError("disk on fire")

    boom_action = Action(
        name="boom", description="", kind=ActionKind.SYNC_TOOL, handler=boom
    )

    tc = ToolCall(id="c1", name="boom", arguments="{}")
    provider = FakeProvider(
        [
            _resp(tool_calls=(tc,), finish=FinishReason.TOOL_CALLS),
            _resp(text="recovered: skipping the broken tool"),
        ]
    )
    agent = Agent(
        name="a",
        instructions="",
        provider=provider,
        model="m",
        actions=[boom_action],
    )

    result = await Runner().run(agent, "go")

    assert result.final_output == "recovered: skipping the broken tool"
    # The tool message sent back to the LLM contains the error.
    second_messages = provider.calls[1]["messages"]
    tool_msg = second_messages[-1]
    assert tool_msg.role is Role.TOOL
    assert "[error]" in tool_msg.content
    assert "disk on fire" in tool_msg.content


@pytest.mark.asyncio
async def test_runner_handles_unknown_tool_name() -> None:
    """LLM hallucinates a tool → error feedback, not crash."""
    tc = ToolCall(id="c1", name="not_registered", arguments="{}")
    provider = FakeProvider(
        [
            _resp(tool_calls=(tc,), finish=FinishReason.TOOL_CALLS),
            _resp(text="ok, I'll skip that"),
        ]
    )
    agent = Agent(name="a", instructions="", provider=provider, model="m")

    result = await Runner().run(agent, "go")
    assert result.final_output == "ok, I'll skip that"
    second_messages = provider.calls[1]["messages"]
    tool_msg = second_messages[-1]
    assert tool_msg.role is Role.TOOL
    assert "Unknown tool" in tool_msg.content


@pytest.mark.asyncio
async def test_runner_raises_max_turns_exceeded() -> None:
    """Looping tool calls past max_turns raises cleanly."""

    async def loop(ctx):  # noqa: ARG001
        return ActionResult(output="again")

    action = Action(
        name="loop", description="", kind=ActionKind.SYNC_TOOL, handler=loop
    )
    tc = ToolCall(id="c1", name="loop", arguments="{}")
    # Provider always asks for another tool call — never produces a final.
    provider = FakeProvider(
        [_resp(tool_calls=(tc,), finish=FinishReason.TOOL_CALLS) for _ in range(5)]
    )
    agent = Agent(
        name="a",
        instructions="",
        provider=provider,
        model="m",
        actions=[action],
        max_turns=3,
    )

    with pytest.raises(MaxTurnsExceeded):
        await Runner().run(agent, "go")


@pytest.mark.asyncio
async def test_runner_handoff_switches_active_agent() -> None:
    """LLM calls transfer_to_<agent> → next turn is on the target agent."""

    # Verifier is the handoff target; it just answers.
    verifier_provider = FakeProvider([_resp(text="verified: looks good")])
    verifier = Agent(
        name="verifier",
        instructions="verify claims",
        provider=verifier_provider,
        model="m",
    )

    # Primary calls the synthetic handoff tool.
    handoff_call = ToolCall(
        id="c1",
        name="transfer_to_verifier",
        arguments='{"reason": "needs verification"}',
    )
    primary_provider = FakeProvider(
        [_resp(tool_calls=(handoff_call,), finish=FinishReason.TOOL_CALLS)]
    )
    primary = Agent(
        name="primary",
        instructions="answer or hand off",
        provider=primary_provider,
        model="m",
        handoffs=[verifier],
    )

    result = await Runner().run(primary, "is x true?")

    assert result.final_output == "verified: looks good"
    assert result.final_agent_name == "verifier"
    handoff_items = [i for i in result.items if isinstance(i, HandoffItem)]
    assert len(handoff_items) == 1
    assert handoff_items[0].from_agent == "primary"
    assert handoff_items[0].to_agent == "verifier"
    assert handoff_items[0].reason == "needs verification"


@pytest.mark.asyncio
async def test_runner_passes_tool_definitions_to_provider() -> None:
    """The provider receives ToolDefinition list when the agent has actions."""

    async def h(ctx):  # noqa: ARG001
        return ActionResult(output="ok")

    action = Action(
        name="search",
        description="vector search",
        kind=ActionKind.SYNC_TOOL,
        handler=h,
        input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
    )
    provider = FakeProvider([_resp(text="done")])
    agent = Agent(
        name="a", instructions="", provider=provider, model="m", actions=[action]
    )

    await Runner().run(agent, "go")

    sent_tools = provider.calls[0]["tools"]
    assert sent_tools is not None
    assert sent_tools[0].name == "search"


@pytest.mark.asyncio
async def test_runner_aggregates_usage_across_turns() -> None:
    """Per-call usage rolls up into the RunResult's Usage."""

    async def h(ctx):  # noqa: ARG001
        return ActionResult(output="ok")

    action = Action(
        name="t", description="", kind=ActionKind.SYNC_TOOL, handler=h
    )
    tc = ToolCall(id="c1", name="t", arguments="{}")
    provider = FakeProvider(
        [
            _resp(tool_calls=(tc,), finish=FinishReason.TOOL_CALLS, tokens=(100, 20)),
            _resp(text="done", tokens=(50, 10)),
        ]
    )
    agent = Agent(
        name="a",
        instructions="",
        provider=provider,
        model="m",
        actions=[action],
    )

    result = await Runner().run(agent, "go")

    assert result.usage.requests == 2
    assert result.usage.input_tokens == 150
    assert result.usage.output_tokens == 30
    assert result.usage.total_tokens == 180
    assert len(result.usage.request_usage_entries) == 2


@pytest.mark.asyncio
async def test_runner_normalizes_string_input() -> None:
    provider = FakeProvider([_resp(text="ok")])
    agent = Agent(name="a", instructions="hi", provider=provider, model="m")
    result = await Runner().run(agent, "string input")
    assert result.history[1].content == "string input"
    assert result.history[1].role is Role.USER


@pytest.mark.asyncio
async def test_runner_accepts_pre_built_message_list() -> None:
    """Caller can pass a full Message list (e.g. resuming a conversation)."""
    provider = FakeProvider([_resp(text="continuing")])
    agent = Agent(name="a", instructions="hi", provider=provider, model="m")

    seed = [
        Message.user("first turn"),
        Message.assistant("first reply"),
        Message.user("second turn"),
    ]
    result = await Runner().run(agent, seed)
    assert result.final_output == "continuing"
    # System was prepended (no system in the seed) and seed preserved
    assert result.history[0].role is Role.SYSTEM
    assert [m.content for m in result.history[1:4]] == [
        "first turn",
        "first reply",
        "second turn",
    ]


@pytest.mark.asyncio
async def test_runner_does_not_re_inject_system_when_caller_supplies_one() -> None:
    provider = FakeProvider([_resp(text="ok")])
    agent = Agent(name="a", instructions="agent default", provider=provider, model="m")

    seed = [Message.system("caller-supplied system"), Message.user("hi")]
    result = await Runner().run(agent, seed)
    system_msgs = [m for m in result.history if m.role is Role.SYSTEM]
    assert len(system_msgs) == 1
    assert system_msgs[0].content == "caller-supplied system"


# ── OpenAICompatProvider import safety ──────────────────────────────────


def test_openai_compat_module_is_import_safe_without_openai() -> None:
    """The module must import even when the optional openai package isn't
    installed; the error only surfaces on instantiation."""
    from agentic_rag.runtime.framework.providers import openai_compat

    assert hasattr(openai_compat, "OpenAICompatProvider")
    assert hasattr(openai_compat, "ChatCompletionChunk")
