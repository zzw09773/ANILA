"""Sprint 3 tests — RunState / StateMachine / RunSerializer / resume / reflection."""

from __future__ import annotations

import pytest

from agentic_rag.runtime.framework import (
    Action,
    ActionKind,
    ActionResult,
    Agent,
    ChatCompletionResponse,
    FinishReason,
    HandoffItem,
    Message,
    MessageOutputItem,
    Role,
    Runner,
    ToolCall,
    ToolCallItem,
    Usage,
)
from agentic_rag.runtime.framework.serialization import RunSerializer
from agentic_rag.runtime.framework.state import (
    PendingToolCall,
    RunPhase,
    RunState,
)
from agentic_rag.runtime.framework.state_machine import (
    StateMachine,
    create_initial_state,
)


# ── Test scaffolding ─────────────────────────────────────────────────


class _ScriptedProvider:
    def __init__(self, responses):
        self._scripted = list(responses)
        self.calls: list[list[Message]] = []

    async def chat_completion(self, messages, tools=None, *, model, stream=False, **kw):
        self.calls.append(list(messages))
        if not self._scripted:
            raise AssertionError("provider exhausted")
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


def _agent(provider, *, name="a", actions=(), handoffs=(), reflection=False):
    return Agent(
        name=name,
        instructions="be helpful",
        provider=provider,
        model="m",
        actions=tuple(actions),
        handoffs=tuple(handoffs),
        reflection_enabled=reflection,
    )


# ── RunPhase / RunState basics ───────────────────────────────────────


def test_runphase_terminal_set() -> None:
    assert RunPhase.DONE.is_terminal
    assert RunPhase.ERROR.is_terminal
    assert not RunPhase.PLANNING.is_terminal


def test_runstate_with_phase_returns_new_instance_with_updated_at_bumped() -> None:
    s = RunState(run_id="r1", agent_name="a", model="m")
    s2 = s.with_phase(RunPhase.ACTING)
    assert s.phase is RunPhase.PLANNING
    assert s2.phase is RunPhase.ACTING
    assert s2 is not s
    assert s2.updated_at >= s.updated_at


def test_runstate_append_history_immutable() -> None:
    s = RunState(run_id="r1", agent_name="a", model="m")
    s2 = s.append_history(Message.user("hi"))
    assert s.history == ()
    assert len(s2.history) == 1
    assert s2.history[0].content == "hi"


def test_runstate_with_usage_added_does_not_mutate_source() -> None:
    s = RunState(run_id="r1", agent_name="a", model="m")
    delta = Usage(requests=1, input_tokens=100, output_tokens=20, total_tokens=120)
    s2 = s.with_usage_added(delta)
    assert s.usage.input_tokens == 0
    assert s2.usage.input_tokens == 100


# ── create_initial_state ─────────────────────────────────────────────


def test_create_initial_state_seeds_history_with_system_and_user() -> None:
    agent = _agent(_ScriptedProvider([]))
    state = create_initial_state(agent, "hello")
    assert state.phase is RunPhase.PLANNING
    assert state.agent_name == "a"
    assert state.history[0].role is Role.SYSTEM
    assert state.history[1].role is Role.USER
    assert state.history[1].content == "hello"


def test_create_initial_state_carries_correlation_fields() -> None:
    agent = _agent(_ScriptedProvider([]))
    state = create_initial_state(
        agent,
        "hi",
        parent_run_id="p",
        group_id="g",
        trace_metadata={"k": "v"},
    )
    assert state.parent_run_id == "p"
    assert state.group_id == "g"
    assert state.trace_metadata == {"k": "v"}


# ── StateMachine.step single-phase transitions ──────────────────────


@pytest.mark.asyncio
async def test_step_planning_to_done_when_no_tool_calls() -> None:
    provider = _ScriptedProvider([_resp(text="42 is the answer")])
    agent = _agent(provider)
    machine = StateMachine({"a": agent})
    state = create_initial_state(agent, "what's the answer?")

    state = await machine.step(state)  # PLANNING → DONE
    assert state.phase is RunPhase.DONE
    assert state.final_output == "42 is the answer"
    assert state.turns_completed == 1


@pytest.mark.asyncio
async def test_step_planning_with_tool_calls_transitions_to_acting() -> None:
    tc = ToolCall(id="c1", name="search", arguments="{}")
    provider = _ScriptedProvider([_resp(tool_calls=(tc,), finish=FinishReason.TOOL_CALLS)])

    async def search(ctx):
        return ActionResult(output="hits")

    action = Action(name="search", description="", kind=ActionKind.SYNC_TOOL, handler=search)
    agent = _agent(provider, actions=[action])
    machine = StateMachine({"a": agent})
    state = create_initial_state(agent, "go")

    state = await machine.step(state)
    assert state.phase is RunPhase.ACTING
    assert len(state.pending_tool_calls) == 1
    assert state.pending_tool_calls[0].call.name == "search"


@pytest.mark.asyncio
async def test_step_acting_dispatches_one_tool_then_observing() -> None:
    async def search(ctx):
        return ActionResult(output="results")

    action = Action(name="search", description="", kind=ActionKind.SYNC_TOOL, handler=search)
    tc = ToolCall(id="c1", name="search", arguments="{}")
    provider = _ScriptedProvider([_resp(tool_calls=(tc,), finish=FinishReason.TOOL_CALLS)])
    agent = _agent(provider, actions=[action])
    machine = StateMachine({"a": agent})
    state = create_initial_state(agent, "go")
    state = await machine.step(state)  # PLANNING → ACTING

    state = await machine.step(state)  # ACTING → OBSERVING
    assert state.phase is RunPhase.OBSERVING
    assert state.pending_tool_calls == ()
    # Tool result message appended to history
    assert state.history[-1].role is Role.TOOL


@pytest.mark.asyncio
async def test_step_observing_returns_to_planning_when_no_pending() -> None:
    state = RunState(
        run_id="r", agent_name="a", model="m", phase=RunPhase.OBSERVING
    )
    machine = StateMachine({"a": _agent(_ScriptedProvider([]))})
    state = machine._observing(state)
    assert state.phase is RunPhase.PLANNING


@pytest.mark.asyncio
async def test_step_observing_returns_to_acting_when_more_pending() -> None:
    pending = (
        PendingToolCall(call=ToolCall(id="c2", name="x", arguments="{}"), index=1),
    )
    state = RunState(
        run_id="r",
        agent_name="a",
        model="m",
        phase=RunPhase.OBSERVING,
        pending_tool_calls=pending,
    )
    machine = StateMachine({"a": _agent(_ScriptedProvider([]))})
    state = machine._observing(state)
    assert state.phase is RunPhase.ACTING


@pytest.mark.asyncio
async def test_step_full_loop_two_tools_then_final() -> None:
    """Drive a complete two-tool run via discrete step() calls."""
    async def t(ctx):
        return ActionResult(output="r")

    action = Action(name="t", description="", kind=ActionKind.SYNC_TOOL, handler=t)
    tcs = (
        ToolCall(id="c1", name="t", arguments="{}"),
        ToolCall(id="c2", name="t", arguments="{}"),
    )
    provider = _ScriptedProvider(
        [
            _resp(tool_calls=tcs, finish=FinishReason.TOOL_CALLS),
            _resp(text="done"),
        ]
    )
    agent = _agent(provider, actions=[action])
    machine = StateMachine({"a": agent})
    state = create_initial_state(agent, "go")

    while not state.is_terminal:
        state = await machine.step(state)

    assert state.phase is RunPhase.DONE
    assert state.final_output == "done"
    assert state.turns_completed == 2  # 2 LLM calls
    # Audit trail: msg + tool_call + tool_result + tool_call + tool_result + msg
    types = [type(i).__name__ for i in state.items]
    assert types == [
        "MessageOutputItem",
        "ToolCallItem",
        "ToolResultItem",
        "ToolCallItem",
        "ToolResultItem",
        "MessageOutputItem",
    ]


@pytest.mark.asyncio
async def test_step_handoff_switches_active_agent() -> None:
    verifier_provider = _ScriptedProvider([_resp(text="verified")])
    verifier = _agent(verifier_provider, name="verifier")

    handoff_call = ToolCall(
        id="c1",
        name="transfer_to_verifier",
        arguments='{"reason": "needs check"}',
    )
    primary_provider = _ScriptedProvider(
        [_resp(tool_calls=(handoff_call,), finish=FinishReason.TOOL_CALLS)]
    )
    primary = _agent(primary_provider, name="primary", handoffs=[verifier])
    machine = StateMachine({"primary": primary, "verifier": verifier})
    state = create_initial_state(primary, "x")

    while not state.is_terminal:
        state = await machine.step(state)

    assert state.phase is RunPhase.DONE
    assert state.final_output == "verified"
    assert state.agent_name == "verifier"
    assert any(isinstance(i, HandoffItem) for i in state.items)


@pytest.mark.asyncio
async def test_step_max_turns_transitions_to_error() -> None:
    """Exceeding max_turns yields ERROR phase, not exception."""
    async def loop(ctx):
        return ActionResult(output="again")

    action = Action(name="loop", description="", kind=ActionKind.SYNC_TOOL, handler=loop)
    tc = ToolCall(id="c1", name="loop", arguments="{}")
    provider = _ScriptedProvider(
        [_resp(tool_calls=(tc,), finish=FinishReason.TOOL_CALLS) for _ in range(10)]
    )
    agent = Agent(
        name="a", instructions="", provider=provider, model="m",
        actions=[action], max_turns=2,
    )
    machine = StateMachine({"a": agent})
    state = create_initial_state(agent, "go")

    while not state.is_terminal:
        state = await machine.step(state)
    assert state.phase is RunPhase.ERROR
    assert state.error_type == "MaxTurnsExceeded"


# ── REFLECTING phase ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reflection_accept_finalises_immediately() -> None:
    provider = _ScriptedProvider(
        [
            _resp(text="my draft answer"),     # PLANNING
            _resp(text="ACCEPT"),              # REFLECTING
        ]
    )
    agent = _agent(provider, reflection=True)
    machine = StateMachine({"a": agent})
    state = create_initial_state(agent, "q")

    while not state.is_terminal:
        state = await machine.step(state)
    assert state.phase is RunPhase.DONE
    assert state.final_output == "my draft answer"
    assert state.reflection_count == 1


@pytest.mark.asyncio
async def test_reflection_critique_loops_back_to_planning() -> None:
    """Non-ACCEPT reflection feeds critique back as a user message."""
    provider = _ScriptedProvider(
        [
            _resp(text="my draft"),                    # PLANNING #1
            _resp(text="missing concrete examples"),   # REFLECTING #1
            _resp(text="revised with examples"),       # PLANNING #2 (after critique)
        ]
    )
    # max_reflections=1 → after one reflection loop, the next no-tool-call
    # PLANNING goes straight to DONE without entering REFLECTING again.
    agent = _agent(provider, reflection=True)
    machine = StateMachine({"a": agent})
    state = create_initial_state(agent, "q")

    while not state.is_terminal:
        state = await machine.step(state)
    assert state.phase is RunPhase.DONE
    assert state.final_output == "revised with examples"
    # Critique must have appeared in history as a [reflection] user message.
    reflections = [
        m for m in state.history if m.role is Role.USER and "[reflection]" in (m.content or "")
    ]
    assert len(reflections) == 1


@pytest.mark.asyncio
async def test_reflection_disabled_by_default() -> None:
    provider = _ScriptedProvider([_resp(text="just one turn")])
    agent = _agent(provider, reflection=False)
    machine = StateMachine({"a": agent})
    state = create_initial_state(agent, "q")
    state = await machine.step(state)
    assert state.phase is RunPhase.DONE
    assert state.reflection_count == 0


# ── RunSerializer round-trip ────────────────────────────────────────


def test_serializer_roundtrip_preserves_all_state() -> None:
    state = RunState(
        run_id="run_xyz",
        agent_name="primary",
        model="m",
        parent_run_id="parent",
        group_id="g",
        trace_metadata={"customer": "c1"},
        phase=RunPhase.ACTING,
        turns_completed=2,
        max_turns=5,
        pending_tool_calls=(
            PendingToolCall(
                call=ToolCall(id="c9", name="search", arguments='{"q":"x"}'),
                index=0,
            ),
        ),
        history=(
            Message.system("be helpful"),
            Message.user("hello"),
            Message.assistant("hi"),
        ),
        usage=Usage(requests=1, input_tokens=42, output_tokens=8, total_tokens=50),
    )
    payload = RunSerializer.dump(state)
    restored = RunSerializer.load(payload)

    assert restored.run_id == state.run_id
    assert restored.agent_name == state.agent_name
    assert restored.parent_run_id == "parent"
    assert restored.trace_metadata == {"customer": "c1"}
    assert restored.phase is RunPhase.ACTING
    assert restored.turns_completed == 2
    assert restored.max_turns == 5
    assert len(restored.pending_tool_calls) == 1
    assert restored.pending_tool_calls[0].call.name == "search"
    assert len(restored.history) == 3
    assert restored.history[0].role is Role.SYSTEM
    assert restored.usage.input_tokens == 42


def test_serializer_roundtrips_run_items() -> None:
    state = RunState(
        run_id="r",
        agent_name="a",
        model="m",
        items=(
            MessageOutputItem(message=Message.assistant("ans"), usage=Usage()),
            ToolCallItem(call=ToolCall(id="c1", name="t", arguments="{}")),
            HandoffItem(from_agent="a", to_agent="b", reason="r"),
        ),
    )
    restored = RunSerializer.load(RunSerializer.dump(state))
    types = [type(i).__name__ for i in restored.items]
    assert types == ["MessageOutputItem", "ToolCallItem", "HandoffItem"]


def test_serializer_rejects_wrong_schema_version() -> None:
    from agentic_rag.runtime.framework.exceptions import UserError

    bad = '{"_schema": 999, "run_id": "r"}'
    with pytest.raises(UserError, match="schema version mismatch"):
        RunSerializer.load(bad)


# ── Runner.resume_from_state ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_resume_from_initial_state_completes_run() -> None:
    provider = _ScriptedProvider([_resp(text="resumed-answer")])
    agent = _agent(provider)
    state = create_initial_state(agent, "hi")
    result = await Runner().resume_from_state(state, {"a": agent})
    assert result.final_output == "resumed-answer"
    assert result.final_agent_name == "a"


@pytest.mark.asyncio
async def test_resume_after_serialize_roundtrip() -> None:
    """Realistic flow: take a snapshot mid-run, serialise, deserialise, resume."""
    async def search(ctx):
        return ActionResult(output="results")

    action = Action(name="search", description="", kind=ActionKind.SYNC_TOOL, handler=search)
    tc = ToolCall(id="c1", name="search", arguments="{}")
    provider = _ScriptedProvider(
        [
            _resp(tool_calls=(tc,), finish=FinishReason.TOOL_CALLS),
            _resp(text="resumed-final"),
        ]
    )
    agent = _agent(provider, actions=[action])
    machine = StateMachine({"a": agent})

    # Step once: PLANNING → ACTING.
    state = create_initial_state(agent, "go")
    state = await machine.step(state)
    assert state.phase is RunPhase.ACTING

    # Serialise mid-run (simulating pod restart).
    payload = RunSerializer.dump(state)
    rebuilt = RunSerializer.load(payload)

    # Resume via Runner; new Runner instance with same agent registry.
    result = await Runner().resume_from_state(rebuilt, {"a": agent})
    assert result.final_output == "resumed-final"


@pytest.mark.asyncio
async def test_resume_propagates_max_turns_error() -> None:
    """ERROR phase from StateMachine becomes an exception at Runner level."""
    from agentic_rag.runtime.framework.exceptions import MaxTurnsExceeded

    async def loop(ctx):
        return ActionResult(output="again")

    action = Action(name="loop", description="", kind=ActionKind.SYNC_TOOL, handler=loop)
    tc = ToolCall(id="c1", name="loop", arguments="{}")
    provider = _ScriptedProvider(
        [_resp(tool_calls=(tc,), finish=FinishReason.TOOL_CALLS) for _ in range(10)]
    )
    agent = Agent(
        name="a", instructions="", provider=provider, model="m",
        actions=[action], max_turns=2,
    )
    state = create_initial_state(agent, "go")
    with pytest.raises(MaxTurnsExceeded):
        await Runner().resume_from_state(state, {"a": agent})


@pytest.mark.asyncio
async def test_resume_honors_cancel_signal() -> None:
    import asyncio
    from agentic_rag.runtime.framework.exceptions import RunCancelled

    signal = asyncio.Event()
    signal.set()  # already cancelled

    provider = _ScriptedProvider([_resp(text="never reached")])
    agent = _agent(provider)
    state = create_initial_state(agent, "hi")
    with pytest.raises(RunCancelled):
        await Runner().resume_from_state(state, {"a": agent}, cancel_signal=signal)
