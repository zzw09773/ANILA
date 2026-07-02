"""Tests for tool guardrails (Sprint 12 PR 5)."""

from __future__ import annotations

import pytest

from anila_core.engine.guardrails import (
    GuardrailChainResult,
    GuardrailResult,
    InputGuardrail,
    MaxLengthOutput,
    OutputGuardrail,
    RegexBlockInput,
    RegexBlockOutput,
    apply_input_guardrails,
    apply_output_guardrails,
)
from anila_core.models.message import ToolCall
from anila_core.models.tool import ToolDefinition, ToolSafety
from anila_core.router.tool_router import ToolRegistry


# ---------------------------------------------------------------------------
# GuardrailResult convenience constructors
# ---------------------------------------------------------------------------


def test_guardrail_result_ok() -> None:
    r = GuardrailResult.ok()
    assert r.passed is True
    assert r.modified_value is None
    assert r.reason is None


def test_guardrail_result_modified() -> None:
    r = GuardrailResult.modified({"x": 1})
    assert r.passed is True
    assert r.modified_value == {"x": 1}


def test_guardrail_result_reject_carries_reason() -> None:
    r = GuardrailResult.reject("nope")
    assert r.passed is False
    assert r.reason == "nope"


# ---------------------------------------------------------------------------
# RegexBlockInput
# ---------------------------------------------------------------------------


def test_regex_block_input_passes_when_no_match() -> None:
    g = RegexBlockInput(pattern=r"sk-[a-z0-9]+")
    res = g.check(tool_name="t", tool_input={"q": "hello"})
    assert res.passed is True
    assert res.modified_value is None


def test_regex_block_input_rejects_on_match() -> None:
    g = RegexBlockInput(pattern=r"sk-[a-z0-9]+", mode="reject")
    res = g.check(
        tool_name="t",
        tool_input={"q": "my key is sk-abc123 ok"},
    )
    assert res.passed is False
    assert "sk-" in (res.reason or "")


def test_regex_block_input_redact_substitutes() -> None:
    g = RegexBlockInput(pattern=r"sk-[a-z0-9]+", mode="redact")
    res = g.check(
        tool_name="t",
        tool_input={"a": "key sk-foo here", "b": "no"},
    )
    assert res.passed is True
    assert res.modified_value == {"a": "key [REDACTED] here", "b": "no"}


def test_regex_block_input_walks_nested_dicts_and_lists() -> None:
    g = RegexBlockInput(pattern=r"\d{3}", mode="redact")
    res = g.check(
        tool_name="t",
        tool_input={
            "outer": [
                {"k": "abc 123 def"},
                {"k": "no digits"},
            ]
        },
    )
    assert res.passed is True
    assert res.modified_value["outer"][0]["k"] == "abc [REDACTED] def"
    assert res.modified_value["outer"][1]["k"] == "no digits"


def test_regex_block_input_invalid_mode_raises() -> None:
    with pytest.raises(ValueError, match="reject\\|redact"):
        RegexBlockInput(pattern="x", mode="invalid")


# ---------------------------------------------------------------------------
# RegexBlockOutput
# ---------------------------------------------------------------------------


def test_regex_block_output_redacts_string() -> None:
    g = RegexBlockOutput(pattern=r"\d+", mode="redact", replacement="N")
    res = g.check(tool_name="t", output="age 42 score 99")
    assert res.passed is True
    assert res.modified_value == "age N score N"


def test_regex_block_output_passes_dict_through() -> None:
    g = RegexBlockOutput(pattern=r"\d+")
    res = g.check(tool_name="t", output={"x": "123"})
    assert res.passed is True
    assert res.modified_value is None


def test_regex_block_output_reject_mode() -> None:
    g = RegexBlockOutput(pattern=r"password=\S+", mode="reject")
    res = g.check(tool_name="t", output="config: password=secret123")
    assert res.passed is False
    assert "password=" in (res.reason or "")


# ---------------------------------------------------------------------------
# MaxLengthOutput
# ---------------------------------------------------------------------------


def test_max_length_passes_short_output() -> None:
    g = MaxLengthOutput(max_chars=20)
    res = g.check(tool_name="t", output="short")
    assert res.passed is True
    assert res.modified_value is None


def test_max_length_truncates_long_output() -> None:
    g = MaxLengthOutput(max_chars=10)
    res = g.check(tool_name="t", output="x" * 50)
    assert res.passed is True
    assert res.modified_value is not None
    assert res.modified_value.startswith("x" * 10)
    assert "truncated" in res.modified_value


def test_max_length_only_acts_on_strings() -> None:
    g = MaxLengthOutput(max_chars=2)
    res = g.check(tool_name="t", output={"big": "data"})
    assert res.passed is True
    assert res.modified_value is None


def test_max_length_rejects_zero() -> None:
    with pytest.raises(ValueError, match="positive"):
        MaxLengthOutput(max_chars=0)


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_built_ins_satisfy_input_protocol() -> None:
    assert isinstance(RegexBlockInput(pattern="x"), InputGuardrail)


def test_built_ins_satisfy_output_protocol() -> None:
    assert isinstance(RegexBlockOutput(pattern="x"), OutputGuardrail)
    assert isinstance(MaxLengthOutput(max_chars=10), OutputGuardrail)


# ---------------------------------------------------------------------------
# apply_input_guardrails — composition
# ---------------------------------------------------------------------------


def test_apply_input_guardrails_pass_through() -> None:
    res = apply_input_guardrails([], tool_name="t", tool_input={"a": 1})
    assert res.passed is True
    assert res.modified_value is None


def test_apply_input_guardrails_first_reject_wins() -> None:
    a = RegexBlockInput(pattern=r"alpha", mode="reject", name="g-a")
    b = RegexBlockInput(pattern=r"beta", mode="reject", name="g-b")
    res = apply_input_guardrails(
        [a, b],
        tool_name="t",
        tool_input={"q": "alpha and beta"},
    )
    assert res.passed is False
    assert res.rejected_by == "g-a"


def test_apply_input_guardrails_chain_modifications() -> None:
    a = RegexBlockInput(
        pattern=r"foo", mode="redact", replacement="FOO", name="a"
    )
    b = RegexBlockInput(
        pattern=r"bar", mode="redact", replacement="BAR", name="b"
    )
    res = apply_input_guardrails(
        [a, b],
        tool_name="t",
        tool_input={"q": "foo and bar"},
    )
    assert res.passed is True
    assert res.modified_value == {"q": "FOO and BAR"}


def test_apply_input_guardrails_returns_chain_result() -> None:
    res = apply_input_guardrails([], tool_name="t", tool_input={})
    assert isinstance(res, GuardrailChainResult)


# ---------------------------------------------------------------------------
# apply_output_guardrails — composition
# ---------------------------------------------------------------------------


def test_apply_output_guardrails_chain_redact_then_truncate() -> None:
    redact = RegexBlockOutput(pattern=r"\d+", mode="redact", replacement="X")
    truncate = MaxLengthOutput(max_chars=15)
    res = apply_output_guardrails(
        [redact, truncate],
        tool_name="t",
        output="number 12345 here in the long output",
    )
    assert res.passed is True
    assert res.modified_value is not None
    assert "X" in res.modified_value
    assert "truncated" in res.modified_value


# ---------------------------------------------------------------------------
# ToolRegistry integration
# ---------------------------------------------------------------------------


def _make_echo_tool(*, input_guards=None, output_guards=None) -> ToolDefinition:
    async def impl(input, **_):
        return f"echo:{input.get('text', '')}"

    return ToolDefinition(
        name="echo",
        description="echo",
        input_schema={"type": "object"},
        safety=ToolSafety.READ_ONLY,
        implementation=impl,
        input_guardrails=input_guards or [],
        output_guardrails=output_guards or [],
    )


@pytest.mark.asyncio
async def test_input_guardrail_rejects_via_registry() -> None:
    tool = _make_echo_tool(
        input_guards=[
            RegexBlockInput(pattern=r"forbidden", mode="reject", name="block")
        ]
    )
    registry = ToolRegistry()
    registry.register(tool)
    result = await registry.execute(
        ToolCall(name="echo", input={"text": "this is forbidden"})
    )
    assert result.is_error is True
    assert "block" in result.content
    assert "rejected by guardrail" in result.content


@pytest.mark.asyncio
async def test_input_guardrail_redacts_then_runs() -> None:
    tool = _make_echo_tool(
        input_guards=[
            RegexBlockInput(
                pattern=r"sk-\w+", mode="redact", replacement="[KEY]", name="r"
            )
        ]
    )
    registry = ToolRegistry()
    registry.register(tool)
    result = await registry.execute(
        ToolCall(name="echo", input={"text": "key sk-secret here"})
    )
    assert result.is_error is False
    # Tool received the redacted input.
    assert "echo:key [KEY] here" == result.content


@pytest.mark.asyncio
async def test_output_guardrail_truncates_via_registry() -> None:
    tool = _make_echo_tool(
        output_guards=[MaxLengthOutput(max_chars=10)]
    )
    registry = ToolRegistry()
    registry.register(tool)
    result = await registry.execute(
        ToolCall(name="echo", input={"text": "x" * 50})
    )
    assert result.is_error is False
    assert "truncated" in result.content


@pytest.mark.asyncio
async def test_output_guardrail_rejects_via_registry() -> None:
    tool = _make_echo_tool(
        output_guards=[
            RegexBlockOutput(
                pattern=r"echo:secret", mode="reject", name="leak-block"
            )
        ]
    )
    registry = ToolRegistry()
    registry.register(tool)
    result = await registry.execute(
        ToolCall(name="echo", input={"text": "secret"})
    )
    assert result.is_error is True
    assert "leak-block" in result.content


@pytest.mark.asyncio
async def test_no_guardrails_path_unchanged() -> None:
    tool = _make_echo_tool()
    registry = ToolRegistry()
    registry.register(tool)
    result = await registry.execute(
        ToolCall(name="echo", input={"text": "hello"})
    )
    assert result.is_error is False
    assert result.content == "echo:hello"


@pytest.mark.asyncio
async def test_guardrails_run_even_with_bypass_gates() -> None:
    """bypass_gates is a permission concept; data validation always runs."""
    tool = _make_echo_tool(
        input_guards=[
            RegexBlockInput(pattern=r"bad", mode="reject", name="g")
        ]
    )
    registry = ToolRegistry()
    registry.register(tool)
    result = await registry.execute(
        ToolCall(name="echo", input={"text": "bad input"}),
        bypass_gates=True,
    )
    assert result.is_error is True
    assert "rejected by guardrail" in result.content
