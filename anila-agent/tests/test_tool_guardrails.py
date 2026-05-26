"""Unit tests:tool 層 Input/Output Guardrails 三種 behavior 與 tripwire 行為。

對應 P0-6 第 3 與 第 4 種 guardrail。涵蓋:
* `ToolInputGuardrail` ALLOW / BLOCK / REPLACE_CONTENT 三種行為。
* `ToolOutputGuardrail` ALLOW / BLOCK / REPLACE_CONTENT 三種行為。
* `ToolGuardrailResult` factory(`allow` / `block` / `replace_content`)行為正確。
* `enforce()` 在 BLOCK 時 raise `GuardrailTripwireTriggered`,並帶 stage / tool_name。
* `@tool_input_guardrail` / `@tool_output_guardrail` decorator 註冊。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from anila_agent.core.guardrails import GuardrailTripwireTriggered
from anila_agent.tools.guardrails import (
    ToolGuardrailBehavior,
    ToolGuardrailResult,
    ToolInputGuardrail,
    ToolOutputGuardrail,
    tool_input_guardrail,
    tool_output_guardrail,
)


# ---------------------------------------------------------------------------
# ToolGuardrailResult factory
# ---------------------------------------------------------------------------


def test_result_allow_factory_defaults() -> None:
    """`.allow()` 應產生 ALLOW behavior 且 tripwire_triggered=False。"""
    result = ToolGuardrailResult.allow()
    assert result.behavior is ToolGuardrailBehavior.ALLOW
    assert result.tripwire_triggered is False
    assert result.replacement_content is None
    assert result.output_info == {}


def test_result_block_factory_marks_tripwire() -> None:
    """`.block()` 的 behavior 為 BLOCK,且 `tripwire_triggered` property 為 True。"""
    result = ToolGuardrailResult.block({"reason": "destructive"})
    assert result.behavior is ToolGuardrailBehavior.BLOCK
    assert result.tripwire_triggered is True
    assert result.output_info == {"reason": "destructive"}


def test_result_replace_content_factory() -> None:
    """`.replace_content()` 必須帶 replacement string 且不觸發 tripwire。"""
    result = ToolGuardrailResult.replace_content(
        "redacted", output_info={"matched": ["secret"]}
    )
    assert result.behavior is ToolGuardrailBehavior.REPLACE_CONTENT
    assert result.tripwire_triggered is False
    assert result.replacement_content == "redacted"
    assert result.output_info == {"matched": ["secret"]}


# ---------------------------------------------------------------------------
# ToolInputGuardrail — 三種行為
# ---------------------------------------------------------------------------


def test_tool_input_guardrail_allow() -> None:
    """正常 args 應通過 ALLOW。"""

    @tool_input_guardrail(name="workspace_only")
    def _check(ctx: Any, tool_name: str, args: dict[str, Any]) -> ToolGuardrailResult:
        if str(args.get("path", "")).startswith("/workspace"):
            return ToolGuardrailResult.allow()
        return ToolGuardrailResult.block({"path": args.get("path")})

    assert isinstance(_check, ToolInputGuardrail)
    assert _check.name == "workspace_only"
    result = _check.check(
        ctx=None, tool_name="read_file", args={"path": "/workspace/foo.txt"}
    )
    assert result.behavior is ToolGuardrailBehavior.ALLOW
    assert result.tripwire_triggered is False


def test_tool_input_guardrail_block_raises_tripwire_via_enforce() -> None:
    """違反規則時 enforce() 應 raise `GuardrailTripwireTriggered`,info 含 stage / tool_name。"""

    @tool_input_guardrail
    def workspace_only(
        ctx: Any, tool_name: str, args: dict[str, Any]
    ) -> ToolGuardrailResult:
        if not str(args.get("path", "")).startswith("/workspace"):
            return ToolGuardrailResult.block({"path": args.get("path")})
        return ToolGuardrailResult.allow()

    with pytest.raises(GuardrailTripwireTriggered) as exc_info:
        workspace_only.enforce(
            ctx=None, tool_name="read_file", args={"path": "/etc/passwd"}
        )

    err = exc_info.value
    assert err.guardrail_name == "workspace_only"
    assert err.info["stage"] == "input"
    assert err.info["tool_name"] == "read_file"
    assert err.info["path"] == "/etc/passwd"


def test_tool_input_guardrail_replace_content() -> None:
    """REPLACE_CONTENT 應回傳 replacement_content,不 raise。"""

    @tool_input_guardrail(name="redact_secrets")
    def _check(ctx: Any, tool_name: str, args: dict[str, Any]) -> ToolGuardrailResult:
        cmd = str(args.get("cmd", ""))
        if "API_KEY" in cmd:
            return ToolGuardrailResult.replace_content(
                "[blocked] command contained secret literal",
                output_info={"matched": ["API_KEY"]},
            )
        return ToolGuardrailResult.allow()

    # enforce 不應 raise(因為 REPLACE_CONTENT 不是 BLOCK)。
    result = _check.enforce(
        ctx=None,
        tool_name="shell",
        args={"cmd": "echo $API_KEY"},
    )
    assert result.behavior is ToolGuardrailBehavior.REPLACE_CONTENT
    assert result.replacement_content is not None
    assert "blocked" in result.replacement_content
    assert result.output_info == {"matched": ["API_KEY"]}


# ---------------------------------------------------------------------------
# ToolOutputGuardrail — 三種行為
# ---------------------------------------------------------------------------


def test_tool_output_guardrail_allow() -> None:
    """正常 output 應通過。"""

    @tool_output_guardrail
    def size_limit(ctx: Any, tool_name: str, result: Any) -> ToolGuardrailResult:
        if isinstance(result, str) and len(result) > 1000:
            return ToolGuardrailResult.block({"length": len(result)})
        return ToolGuardrailResult.allow()

    assert isinstance(size_limit, ToolOutputGuardrail)
    out = size_limit.check(ctx=None, tool_name="read_file", result="hello")
    assert out.behavior is ToolGuardrailBehavior.ALLOW


def test_tool_output_guardrail_block_raises_tripwire_via_enforce() -> None:
    """output 違規時 enforce() 應 raise tripwire,stage='output'。"""

    @tool_output_guardrail(name="size_limit")
    def _check(ctx: Any, tool_name: str, result: Any) -> ToolGuardrailResult:
        if isinstance(result, str) and len(result) > 10:
            return ToolGuardrailResult.block({"length": len(result)})
        return ToolGuardrailResult.allow()

    huge = "x" * 100
    with pytest.raises(GuardrailTripwireTriggered) as exc_info:
        _check.enforce(ctx=None, tool_name="read_file", result=huge)

    err = exc_info.value
    assert err.guardrail_name == "size_limit"
    assert err.info["stage"] == "output"
    assert err.info["tool_name"] == "read_file"
    assert err.info["length"] == 100


def test_tool_output_guardrail_replace_content() -> None:
    """output 命中敏感詞時用 REPLACE_CONTENT 重寫。"""

    @tool_output_guardrail
    def redact_pii(ctx: Any, tool_name: str, result: Any) -> ToolGuardrailResult:
        text = str(result)
        if "ssn=" in text:
            return ToolGuardrailResult.replace_content(
                text.replace("ssn=123-45-6789", "ssn=[REDACTED]"),
                output_info={"redacted": ["ssn"]},
            )
        return ToolGuardrailResult.allow()

    leaky_output = "User: john, ssn=123-45-6789, status=active"
    outcome = redact_pii.enforce(
        ctx=None, tool_name="db_query", result=leaky_output
    )
    assert outcome.behavior is ToolGuardrailBehavior.REPLACE_CONTENT
    assert outcome.replacement_content is not None
    assert "[REDACTED]" in outcome.replacement_content
    assert "123-45-6789" not in outcome.replacement_content


# ---------------------------------------------------------------------------
# Decorator 註冊 + async 支援
# ---------------------------------------------------------------------------


def test_tool_input_guardrail_decorator_without_parens_uses_fn_name() -> None:
    """無括號形式應 fallback 用 fn.__name__。"""

    @tool_input_guardrail
    def my_check(ctx: Any, tool_name: str, args: dict[str, Any]) -> ToolGuardrailResult:
        return ToolGuardrailResult.allow()

    assert isinstance(my_check, ToolInputGuardrail)
    assert my_check.name == "my_check"


def test_tool_output_guardrail_manual_construction() -> None:
    """直接 new ToolOutputGuardrail 應可運作。"""

    def _fn(ctx: Any, tool_name: str, result: Any) -> ToolGuardrailResult:
        return ToolGuardrailResult.allow()

    guardrail = ToolOutputGuardrail(guardrail_function=_fn)
    assert guardrail.name == "_fn"
    outcome = guardrail.check(ctx=None, tool_name="anything", result=None)
    assert outcome.behavior is ToolGuardrailBehavior.ALLOW


def test_tool_input_guardrail_async_run() -> None:
    """async guardrail 透過 .run() 應可拿到結果。"""

    async def _async(
        ctx: Any, tool_name: str, args: dict[str, Any]
    ) -> ToolGuardrailResult:
        return ToolGuardrailResult.block({"async": True})

    guardrail = ToolInputGuardrail(guardrail_function=_async, name="async_block")
    outcome = asyncio.run(
        guardrail.run(ctx=None, tool_name="anything", args={})
    )
    assert outcome.behavior is ToolGuardrailBehavior.BLOCK
    assert outcome.output_info == {"async": True}


def test_tool_input_guardrail_async_sync_check_raises() -> None:
    """async guardrail 用同步 check() 應丟 TypeError 提示改 .run()。"""
    created_coros: list[Any] = []

    async def _async(
        ctx: Any, tool_name: str, args: dict[str, Any]
    ) -> ToolGuardrailResult:
        return ToolGuardrailResult.allow()

    # 用 wrapper 攔下未 await 的 coroutine,raise 後手動 close。
    def _factory(ctx: Any, tool_name: str, args: dict[str, Any]) -> Any:
        coro = _async(ctx, tool_name, args)
        created_coros.append(coro)
        return coro

    guardrail = ToolInputGuardrail(guardrail_function=_factory)
    with pytest.raises(TypeError, match="async"):
        guardrail.check(ctx=None, tool_name="anything", args={})
    for coro in created_coros:
        coro.close()
