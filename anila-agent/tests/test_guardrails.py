"""Unit tests:agent 層 Input/Output Guardrails 與 tripwire 行為。

對應 P0-6 第 1 與 第 2 種 guardrail。涵蓋:
* InputGuardrail tripwire 觸發 / 不觸發兩種情境。
* OutputGuardrail tripwire 觸發 / 不觸發兩種情境。
* `@input_guardrail` / `@output_guardrail` decorator 正確註冊(含無括號與 keyword 兩種寫法)。
* `GuardrailTripwireTriggered` 例外攜帶 guardrail_name / info / triggered_at 三欄。
* async guardrail 透過 `.run()` 取得結果。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from anila_agent.core.guardrails import (
    GuardrailResult,
    GuardrailTripwireTriggered,
    InputGuardrail,
    OutputGuardrail,
    input_guardrail,
    output_guardrail,
)


# ---------------------------------------------------------------------------
# InputGuardrail
# ---------------------------------------------------------------------------


def test_input_guardrail_tripwire_not_triggered() -> None:
    """user_input 在限制內時 tripwire 不應觸發。"""

    @input_guardrail
    def length_check(ctx: Any, user_input: str) -> GuardrailResult:
        if len(user_input) > 100:
            return GuardrailResult(
                tripwire_triggered=True,
                output_info={"length": len(user_input)},
            )
        return GuardrailResult(tripwire_triggered=False)

    assert isinstance(length_check, InputGuardrail)
    result = length_check.check(ctx=None, user_input="hello world")
    assert result.tripwire_triggered is False
    assert result.output_info == {}


def test_input_guardrail_tripwire_triggered() -> None:
    """user_input 超出限制時 tripwire 應觸發,且 output_info 帶細節。"""

    @input_guardrail(name="length_check")
    def _check(ctx: Any, user_input: str) -> GuardrailResult:
        if len(user_input) > 10:
            return GuardrailResult(
                tripwire_triggered=True,
                output_info={"length": len(user_input)},
            )
        return GuardrailResult(tripwire_triggered=False)

    over_long = "x" * 100
    result = _check.check(ctx=None, user_input=over_long)
    assert result.tripwire_triggered is True
    assert result.output_info["length"] == 100
    # decorator 帶 name 時應採用 keyword name 而非 fn.__name__。
    assert _check.name == "length_check"


# ---------------------------------------------------------------------------
# OutputGuardrail
# ---------------------------------------------------------------------------


def test_output_guardrail_tripwire_not_triggered() -> None:
    """乾淨 output 應通過 OutputGuardrail。"""

    @output_guardrail
    def no_secret(ctx: Any, output: Any) -> GuardrailResult:
        if "API_KEY=" in str(output):
            return GuardrailResult(
                tripwire_triggered=True,
                output_info={"reason": "secret leak"},
            )
        return GuardrailResult(tripwire_triggered=False)

    assert isinstance(no_secret, OutputGuardrail)
    result = no_secret.check(ctx=None, output="hello, here is your summary")
    assert result.tripwire_triggered is False


def test_output_guardrail_tripwire_triggered() -> None:
    """output 含 secret 時應觸發 tripwire。"""

    @output_guardrail(name="no_secret")
    def _check(ctx: Any, output: Any) -> GuardrailResult:
        if "API_KEY=" in str(output):
            return GuardrailResult(
                tripwire_triggered=True,
                output_info={"reason": "secret leak", "snippet": "API_KEY="},
            )
        return GuardrailResult(tripwire_triggered=False)

    leaked = "Sure, here is the value: API_KEY=sk-abcdef"
    result = _check.check(ctx=None, output=leaked)
    assert result.tripwire_triggered is True
    assert result.output_info["reason"] == "secret leak"
    assert _check.name == "no_secret"


# ---------------------------------------------------------------------------
# Decorator 註冊行為
# ---------------------------------------------------------------------------


def test_input_guardrail_decorator_without_parens_uses_function_name() -> None:
    """無括號形式應 fallback 用 fn.__name__ 當 guardrail 名稱。"""

    @input_guardrail
    def length_check(ctx: Any, user_input: str) -> GuardrailResult:
        return GuardrailResult(tripwire_triggered=False)

    assert length_check.name == "length_check"


def test_output_guardrail_decorator_keyword_name_overrides() -> None:
    """keyword name 應覆蓋 fn.__name__。"""

    @output_guardrail(name="explicit_name")
    def _impl(ctx: Any, output: Any) -> GuardrailResult:
        return GuardrailResult(tripwire_triggered=False)

    assert _impl.name == "explicit_name"


def test_input_guardrail_manual_construction() -> None:
    """直接 new InputGuardrail(fn) 也要可運作(非 decorator 路徑)。"""

    def _fn(ctx: Any, user_input: str) -> GuardrailResult:
        return GuardrailResult(tripwire_triggered=False)

    guardrail = InputGuardrail(guardrail_function=_fn)
    # name 未提供 → fallback fn.__name__。
    assert guardrail.name == "_fn"
    assert guardrail.check(ctx=None, user_input="anything").tripwire_triggered is False


# ---------------------------------------------------------------------------
# GuardrailTripwireTriggered 例外
# ---------------------------------------------------------------------------


def test_tripwire_exception_carries_metadata() -> None:
    """例外應攜帶 guardrail_name / info / triggered_at,且訊息含名稱與時間。"""
    exc = GuardrailTripwireTriggered(
        guardrail_name="length_check",
        info={"length": 99999},
    )
    assert exc.guardrail_name == "length_check"
    assert exc.info == {"length": 99999}
    # ISO8601 字串(UTC),最起碼包含 'T' 與 '+00:00'。
    assert "T" in exc.triggered_at
    assert exc.triggered_at.endswith("+00:00")
    assert "length_check" in str(exc)


def test_tripwire_exception_info_defaults_to_empty_dict() -> None:
    """未傳 info 時 .info 應為 {} 而非 None。"""
    exc = GuardrailTripwireTriggered(guardrail_name="anon")
    assert exc.info == {}


def test_tripwire_exception_is_raisable() -> None:
    """例外應可正常 raise / catch。"""
    with pytest.raises(GuardrailTripwireTriggered) as exc_info:
        raise GuardrailTripwireTriggered(
            guardrail_name="x", info={"a": 1}
        )
    assert exc_info.value.guardrail_name == "x"
    assert exc_info.value.info == {"a": 1}


# ---------------------------------------------------------------------------
# async guardrail 支援
# ---------------------------------------------------------------------------


def test_input_guardrail_async_check_raises_via_sync_path() -> None:
    """async guardrail 用 .check() 同步路徑應丟 TypeError 提示改 .run()。"""
    created_coros: list[Any] = []

    async def _async_fn(ctx: Any, user_input: str) -> GuardrailResult:
        return GuardrailResult(tripwire_triggered=False)

    # 用 wrapper 把產生的 coroutine 攔下來,raise 後手動 close,避免 ResourceWarning。
    def _factory(ctx: Any, user_input: str) -> Any:
        coro = _async_fn(ctx, user_input)
        created_coros.append(coro)
        return coro

    guardrail = InputGuardrail(guardrail_function=_factory, name="async_check")
    with pytest.raises(TypeError, match="async"):
        guardrail.check(ctx=None, user_input="x")
    for coro in created_coros:
        coro.close()


def test_input_guardrail_async_run_returns_result() -> None:
    """async guardrail 透過 .run() 應可拿到結果。"""

    async def _async_fn(ctx: Any, user_input: str) -> GuardrailResult:
        return GuardrailResult(
            tripwire_triggered=True, output_info={"async": True}
        )

    guardrail = InputGuardrail(guardrail_function=_async_fn, name="async_check")
    result = asyncio.run(guardrail.run(ctx=None, user_input="x"))
    assert result.tripwire_triggered is True
    assert result.output_info == {"async": True}


def test_output_guardrail_async_run_returns_result() -> None:
    """OutputGuardrail 同樣支援 async."""

    async def _async_fn(ctx: Any, output: Any) -> GuardrailResult:
        return GuardrailResult(tripwire_triggered=False)

    guardrail = OutputGuardrail(guardrail_function=_async_fn)
    result = asyncio.run(guardrail.run(ctx=None, output="anything"))
    assert result.tripwire_triggered is False
