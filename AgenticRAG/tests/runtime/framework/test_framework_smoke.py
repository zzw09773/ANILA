"""Sprint 1 stage A smoke tests.

Just enough to prove the package imports cleanly, the public surface
works, and the `Usage.add` arithmetic is correct. Heavier behavioural
tests land alongside agent.py / runner.py in stage B.
"""

from __future__ import annotations

import pytest

from agentic_rag.runtime.framework import (
    AgentsException,
    InputTokensDetails,
    MaxTurnsExceeded,
    ModelBehaviorError,
    ModelRefusalError,
    OutputTokensDetails,
    RequestUsage,
    ToolTimeoutError,
    Usage,
    UserError,
    __version__,
)
from agentic_rag.runtime.framework.providers import LLMProvider
from agentic_rag.runtime.framework.usage import deserialize_usage, serialize_usage


def test_version_present() -> None:
    assert __version__.startswith("0.1.")


def test_exception_hierarchy() -> None:
    """Every framework exception inherits AgentsException so callers can
    catch one type and not chase per-subsystem error classes."""
    assert issubclass(MaxTurnsExceeded, AgentsException)
    assert issubclass(ModelBehaviorError, AgentsException)
    assert issubclass(ModelRefusalError, AgentsException)
    assert issubclass(UserError, AgentsException)
    assert issubclass(ToolTimeoutError, AgentsException)


def test_max_turns_exceeded_carries_message() -> None:
    err = MaxTurnsExceeded("hit cap at 10 turns")
    assert err.message == "hit cap at 10 turns"
    assert "10 turns" in str(err)


def test_tool_timeout_format() -> None:
    err = ToolTimeoutError(tool_name="vector_search", timeout_seconds=5.5)
    assert err.tool_name == "vector_search"
    assert err.timeout_seconds == 5.5
    assert "vector_search" in str(err)
    assert "5.5" in str(err)


def test_usage_zero_default() -> None:
    u = Usage()
    assert u.requests == 0
    assert u.input_tokens == 0
    assert u.output_tokens == 0
    assert u.total_tokens == 0
    assert u.request_usage_entries == []
    assert u.input_tokens_details.cached_tokens == 0
    assert u.output_tokens_details.reasoning_tokens == 0


def test_usage_add_aggregates_totals() -> None:
    u = Usage()
    u.add(
        Usage(
            requests=1,
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            input_tokens_details=InputTokensDetails(cached_tokens=20),
            output_tokens_details=OutputTokensDetails(reasoning_tokens=10),
        )
    )
    u.add(
        Usage(
            requests=1,
            input_tokens=200,
            output_tokens=80,
            total_tokens=280,
            input_tokens_details=InputTokensDetails(cached_tokens=50),
            output_tokens_details=OutputTokensDetails(reasoning_tokens=15),
        )
    )

    assert u.requests == 2
    assert u.input_tokens == 300
    assert u.output_tokens == 130
    assert u.total_tokens == 430
    assert u.input_tokens_details.cached_tokens == 70
    assert u.output_tokens_details.reasoning_tokens == 25
    # Per-request entries preserve the breakdown
    assert len(u.request_usage_entries) == 2
    assert u.request_usage_entries[0].input_tokens == 100
    assert u.request_usage_entries[1].input_tokens == 200


def test_usage_add_preexisting_entries_passthrough() -> None:
    """When ``other`` already has request_usage_entries, they extend in
    rather than collapsing into one synthetic entry."""
    pre_built_entries = [
        RequestUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        RequestUsage(input_tokens=20, output_tokens=10, total_tokens=30),
    ]
    other = Usage(
        requests=2,
        input_tokens=30,
        output_tokens=15,
        total_tokens=45,
        request_usage_entries=pre_built_entries,
    )
    aggregate = Usage()
    aggregate.add(other)
    assert len(aggregate.request_usage_entries) == 2
    assert aggregate.request_usage_entries[0].input_tokens == 10


def test_usage_round_trip_serialization() -> None:
    """serialize_usage / deserialize_usage are inverse on a populated Usage."""
    original = Usage(
        requests=1,
        input_tokens=42,
        output_tokens=17,
        total_tokens=59,
        input_tokens_details=InputTokensDetails(cached_tokens=12),
        output_tokens_details=OutputTokensDetails(reasoning_tokens=3),
        request_usage_entries=[
            RequestUsage(
                input_tokens=42,
                output_tokens=17,
                total_tokens=59,
                input_tokens_details=InputTokensDetails(cached_tokens=12),
                output_tokens_details=OutputTokensDetails(reasoning_tokens=3),
            )
        ],
    )
    payload = serialize_usage(original)
    rebuilt = deserialize_usage(payload)

    assert rebuilt.requests == 1
    assert rebuilt.input_tokens == 42
    assert rebuilt.output_tokens == 17
    assert rebuilt.total_tokens == 59
    assert rebuilt.input_tokens_details.cached_tokens == 12
    assert rebuilt.output_tokens_details.reasoning_tokens == 3
    assert len(rebuilt.request_usage_entries) == 1


def test_provider_protocol_runtime_checkable() -> None:
    """Anyone matching the Protocol shape is acceptable, no nominal
    inheritance required."""

    class FakeProvider:
        async def chat_completion(self, messages, tools=None, *, model, stream=False, **kw):
            return {"id": "fake", "model": model}

        async def embeddings(self, texts, *, model, **kw):
            raise NotImplementedError

    p = FakeProvider()
    assert isinstance(p, LLMProvider)


def test_user_error_carries_message() -> None:
    err = UserError("missing required field 'system_prompt'")
    assert err.message == "missing required field 'system_prompt'"
    with pytest.raises(UserError):
        raise err
