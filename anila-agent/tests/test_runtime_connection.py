"""Tests for the P1-11 first-cut ConnectionStrategy abstraction.

Covers:

- The ABC refuses direct instantiation.
- :class:`Message` / :class:`Response` / :class:`Chunk` dataclasses carry the
  expected schema.
- :class:`OpenAIAgentsConnection` translates ``Message`` → openai-agents call
  → :class:`Response` correctly when fed a mock ``Runner``.
- :class:`ConnectionRegistry` register / get / default fallback / unknown
  backend behaviour.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from anila_agent.runtime import (
    Chunk,
    ConnectionRegistry,
    ConnectionStrategy,
    Message,
    OpenAIAgentsConnection,
    Response,
    ToolCall,
    Usage,
    default_registry,
)

# ---------------------------------------------------------------------------
# ConnectionStrategy ABC
# ---------------------------------------------------------------------------


def test_connection_strategy_cannot_be_instantiated_directly() -> None:
    with pytest.raises(TypeError):
        ConnectionStrategy()  # type: ignore[abstract]


def test_connection_strategy_subclass_missing_methods_still_abstract() -> None:
    class HalfBaked(ConnectionStrategy):
        @property
        def name(self) -> str:
            return "half"

    with pytest.raises(TypeError):
        HalfBaked()  # type: ignore[abstract]


@pytest.mark.asyncio
async def test_connection_strategy_concrete_subclass_works() -> None:
    class Echo(ConnectionStrategy):
        @property
        def name(self) -> str:
            return "echo"

        async def send_message(
            self, messages: Any, **kwargs: Any
        ) -> Response:
            return Response(message=Message(role="assistant", content="ok"))

        async def stream_message(self, messages: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
            yield Chunk(delta="ok", finish_reason="stop")

        async def close(self) -> None:
            return None

    conn = Echo()
    assert conn.name == "echo"
    resp = await conn.send_message([Message(role="user", content="hi")])
    assert resp.message.content == "ok"

    chunks = [chunk async for chunk in conn.stream_message([])]
    assert chunks == [Chunk(delta="ok", finish_reason="stop")]
    await conn.close()


@pytest.mark.asyncio
async def test_connection_strategy_async_context_manager_closes() -> None:
    close_calls: list[int] = []

    class Counting(ConnectionStrategy):
        @property
        def name(self) -> str:
            return "counting"

        async def send_message(self, messages: Any, **kwargs: Any) -> Response:
            return Response(message=Message(role="assistant", content=None))

        async def stream_message(self, messages: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
            if False:
                yield Chunk()

        async def close(self) -> None:
            close_calls.append(1)

    async with Counting() as conn:
        assert conn.name == "counting"
    assert close_calls == [1]


# ---------------------------------------------------------------------------
# Schema dataclasses
# ---------------------------------------------------------------------------


def test_message_schema_basic_fields() -> None:
    msg = Message(role="user", content="hello")
    assert msg.role == "user"
    assert msg.content == "hello"
    assert msg.tool_calls == ()
    assert msg.tool_call_id is None
    assert msg.metadata == {}


def test_message_with_tool_calls() -> None:
    tc = ToolCall(id="call_1", name="search", arguments='{"q":"anila"}')
    msg = Message(role="assistant", content=None, tool_calls=(tc,))
    assert msg.content is None
    assert msg.tool_calls == (tc,)
    assert msg.tool_calls[0].name == "search"


def test_message_tool_role_carries_tool_call_id() -> None:
    msg = Message(
        role="tool",
        content="result",
        name="search",
        tool_call_id="call_1",
    )
    assert msg.role == "tool"
    assert msg.tool_call_id == "call_1"
    assert msg.name == "search"


def test_message_is_frozen() -> None:
    msg = Message(role="user", content="hi")
    # frozen=True dataclasses raise FrozenInstanceError (a TypeError subclass)
    # when mutation is attempted; assert on the precise type to avoid blind
    # ``Exception`` catches.
    import dataclasses

    with pytest.raises(dataclasses.FrozenInstanceError):
        msg.content = "mutated"  # type: ignore[misc]


def test_response_carries_usage_and_raw() -> None:
    resp = Response(
        message=Message(role="assistant", content="hi"),
        finish_reason="stop",
        usage=Usage(prompt_tokens=3, completion_tokens=4, total_tokens=7),
        raw={"some": "payload"},
    )
    assert resp.usage.total_tokens == 7
    assert resp.finish_reason == "stop"
    assert resp.raw == {"some": "payload"}


def test_chunk_terminal_signal() -> None:
    terminal = Chunk(delta="", finish_reason="stop")
    assert terminal.finish_reason == "stop"
    assert terminal.delta == ""
    assert terminal.tool_calls == ()


# ---------------------------------------------------------------------------
# OpenAIAgentsConnection (mocked Runner)
# ---------------------------------------------------------------------------


class _MockResult:
    """Mimic the shape of openai-agents' ``Result``."""

    def __init__(
        self,
        final_output: Any,
        *,
        finish_reason: str = "stop",
        usage: Any | None = None,
        tool_calls: Any | None = None,
    ) -> None:
        self.final_output = final_output
        self.finish_reason = finish_reason
        self.usage = usage
        self.tool_calls = tool_calls or []


class _MockUsage:
    def __init__(self, prompt: int, completion: int) -> None:
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.total_tokens = prompt + completion


class _MockRunner:
    """Mock that records the kwargs ``Runner.run`` was called with."""

    last_kwargs: ClassVar[dict[str, Any]] = {}
    result: ClassVar[Any] = None

    @classmethod
    async def run(cls, **kwargs: Any) -> Any:
        cls.last_kwargs = kwargs
        return cls.result


@pytest.fixture(autouse=True)
def _reset_mock_runner() -> None:
    _MockRunner.last_kwargs = {}
    _MockRunner.result = None


@pytest.mark.asyncio
async def test_openai_agents_connection_send_message_passes_through() -> None:
    agent_marker = object()
    _MockRunner.result = _MockResult(
        "hello back",
        usage=_MockUsage(prompt=2, completion=3),
    )
    conn = OpenAIAgentsConnection(
        agent_marker, runner_cls=_MockRunner, max_turns=7
    )

    resp = await conn.send_message(
        [
            Message(role="system", content="you are helpful"),
            Message(role="user", content="hi"),
        ]
    )

    assert _MockRunner.last_kwargs["starting_agent"] is agent_marker
    assert _MockRunner.last_kwargs["input"] == "hi"
    assert _MockRunner.last_kwargs["max_turns"] == 7
    assert resp.message.role == "assistant"
    assert resp.message.content == "hello back"
    assert resp.usage.total_tokens == 5
    assert resp.finish_reason == "stop"
    assert resp.raw is _MockRunner.result


@pytest.mark.asyncio
async def test_openai_agents_connection_serialises_non_string_output() -> None:
    _MockRunner.result = _MockResult({"answer": 42})
    conn = OpenAIAgentsConnection(object(), runner_cls=_MockRunner)
    resp = await conn.send_message([Message(role="user", content="q")])
    assert resp.message.content is not None
    assert "answer" in resp.message.content
    assert "42" in resp.message.content


@pytest.mark.asyncio
async def test_openai_agents_connection_kwargs_override_defaults() -> None:
    _MockRunner.result = _MockResult("ok")
    conn = OpenAIAgentsConnection(
        object(), runner_cls=_MockRunner, max_turns=5
    )
    await conn.send_message(
        [Message(role="user", content="hi")], max_turns=99, model="gemma-3"
    )
    assert _MockRunner.last_kwargs["max_turns"] == 99
    assert _MockRunner.last_kwargs["model"] == "gemma-3"


@pytest.mark.asyncio
async def test_openai_agents_connection_close_blocks_further_calls() -> None:
    conn = OpenAIAgentsConnection(object(), runner_cls=_MockRunner)
    await conn.close()
    # Idempotent close
    await conn.close()
    with pytest.raises(RuntimeError):
        await conn.send_message([Message(role="user", content="hi")])


@pytest.mark.asyncio
async def test_openai_agents_connection_stream_fallback_single_chunk() -> None:
    _MockRunner.result = _MockResult("streamed")
    conn = OpenAIAgentsConnection(object(), runner_cls=_MockRunner)
    # _MockRunner has no ``run_streamed``, so the connection must fall back to
    # the single-chunk path built from the non-streaming response.
    chunks = [c async for c in conn.stream_message([Message(role="user", content="hi")])]
    assert len(chunks) == 1
    assert chunks[0].delta == "streamed"
    assert chunks[0].finish_reason == "stop"


def test_openai_agents_connection_name() -> None:
    conn = OpenAIAgentsConnection(object(), runner_cls=_MockRunner)
    assert conn.name == "openai_agents"


def test_openai_agents_connection_messages_to_input_fallback() -> None:
    """Non-user terminal message → joined content fallback."""
    result = OpenAIAgentsConnection._messages_to_input(
        [
            Message(role="system", content="sys"),
            Message(role="assistant", content="prev"),
        ]
    )
    assert "sys" in result
    assert "prev" in result


def test_openai_agents_connection_empty_messages_yields_empty_input() -> None:
    assert OpenAIAgentsConnection._messages_to_input([]) == ""


# ---------------------------------------------------------------------------
# ConnectionRegistry
# ---------------------------------------------------------------------------


def test_default_registry_has_openai_agents() -> None:
    assert default_registry.has("openai_agents")
    assert default_registry.default == "openai_agents"


def test_registry_get_default_returns_openai_agents_connection() -> None:
    conn = default_registry.get(agent=object(), runner_cls=_MockRunner)
    assert isinstance(conn, OpenAIAgentsConnection)
    assert conn.name == "openai_agents"


def test_registry_get_by_explicit_name() -> None:
    conn = default_registry.get(
        "openai_agents", agent=object(), runner_cls=_MockRunner
    )
    assert isinstance(conn, OpenAIAgentsConnection)


def test_registry_register_custom_factory() -> None:
    reg = ConnectionRegistry()

    class _Stub(ConnectionStrategy):
        @property
        def name(self) -> str:
            return "stub"

        async def send_message(self, messages: Any, **kwargs: Any) -> Response:
            return Response(message=Message(role="assistant", content=None))

        async def stream_message(self, messages: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
            if False:
                yield Chunk()

        async def close(self) -> None:
            return None

    reg.register("stub", lambda **kw: _Stub())
    assert reg.has("stub")
    assert reg.names() == ("stub",)
    conn = reg.get("stub")
    assert conn.name == "stub"


def test_registry_unknown_backend_raises() -> None:
    reg = ConnectionRegistry()
    with pytest.raises(KeyError):
        reg.get("missing")


def test_registry_register_rejects_empty_name() -> None:
    reg = ConnectionRegistry()
    with pytest.raises(ValueError):
        reg.register("", lambda **kw: None)  # type: ignore[arg-type]


def test_registry_set_default_requires_registration() -> None:
    reg = ConnectionRegistry()
    with pytest.raises(KeyError):
        reg.set_default("nope")


def test_registry_set_default_updates_default() -> None:
    reg = ConnectionRegistry()

    class _Stub(ConnectionStrategy):
        @property
        def name(self) -> str:
            return "stub"

        async def send_message(self, messages: Any, **kwargs: Any) -> Response:
            return Response(message=Message(role="assistant", content=None))

        async def stream_message(self, messages: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
            if False:
                yield Chunk()

        async def close(self) -> None:
            return None

    reg.register("stub", lambda **kw: _Stub())
    reg.set_default("stub")
    assert reg.default == "stub"
    assert reg.get().name == "stub"


def test_registry_unregister_removes_name() -> None:
    reg = ConnectionRegistry()
    reg.register("x", lambda **kw: OpenAIAgentsConnection(object()))
    assert reg.has("x")
    reg.unregister("x")
    assert not reg.has("x")
    # Idempotent
    reg.unregister("x")


def test_registry_re_register_overwrites() -> None:
    reg = ConnectionRegistry()
    reg.register("a", lambda **kw: OpenAIAgentsConnection(object()))
    sentinel = object()
    reg.register(
        "a", lambda **kw: OpenAIAgentsConnection(sentinel, runner_cls=_MockRunner)
    )
    conn = reg.get("a")
    assert isinstance(conn, OpenAIAgentsConnection)
    assert conn.agent is sentinel
