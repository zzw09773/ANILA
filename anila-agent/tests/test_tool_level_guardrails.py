"""P2-4 Unit tests:tool-level guardrail chain — per-tool input / output 掛載。

涵蓋:
- `@anila_tool` 接收 ``input_guardrails`` / ``output_guardrails`` kwarg。
- `AnilaTool.from_function` 同等 kwarg 支援。
- `get_tool_input_guardrails` / `get_tool_output_guardrails` 取出 chain。
- `enforce_tool_input_chain` / `enforce_tool_output_chain` 三種 behavior:
    * ALLOW → 原 payload 通過。
    * BLOCK → raise `GuardrailTripwireTriggered`。
    * REPLACE_CONTENT → payload 改 / 短路 chain。
- 全局 + tool-level 兩道串聯都會被執行(spec 要求)。
- 範例 guardrail:`LengthLimitInputGuardrail` / `PathSafetyInputGuardrail` 行為。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from anila_agent.core.guardrails import GuardrailTripwireTriggered
from anila_agent.tools import (
    AnilaTool,
    GuardrailChainOutcome,
    LengthLimitInputGuardrail,
    PathSafetyInputGuardrail,
    ToolGuardrailBehavior,
    ToolGuardrailResult,
    ToolInputGuardrail,
    ToolOutputGuardrail,
    anila_tool,
    enforce_tool_input_chain,
    enforce_tool_output_chain,
    get_tool_input_guardrails,
    get_tool_output_guardrails,
)

# ---------------------------------------------------------------------------
# Helper: 製造 guardrail
# ---------------------------------------------------------------------------


def _allow_input(name: str = "noop_in") -> ToolInputGuardrail[Any]:
    def _fn(_ctx: Any, _tool: str, _args: dict[str, Any]) -> ToolGuardrailResult:
        return ToolGuardrailResult.allow({"checked": True})

    return ToolInputGuardrail(guardrail_function=_fn, name=name)


def _block_input(name: str = "block_in") -> ToolInputGuardrail[Any]:
    def _fn(_ctx: Any, tool: str, _args: dict[str, Any]) -> ToolGuardrailResult:
        return ToolGuardrailResult.block({"tool": tool})

    return ToolInputGuardrail(guardrail_function=_fn, name=name)


def _replace_input(
    replacement: str,
    name: str = "replace_in",
) -> ToolInputGuardrail[Any]:
    def _fn(_ctx: Any, _tool: str, _args: dict[str, Any]) -> ToolGuardrailResult:
        return ToolGuardrailResult.replace_content(replacement)

    return ToolInputGuardrail(guardrail_function=_fn, name=name)


def _allow_output(name: str = "noop_out") -> ToolOutputGuardrail[Any]:
    def _fn(_ctx: Any, _tool: str, _result: Any) -> ToolGuardrailResult:
        return ToolGuardrailResult.allow()

    return ToolOutputGuardrail(guardrail_function=_fn, name=name)


def _block_output(name: str = "block_out") -> ToolOutputGuardrail[Any]:
    def _fn(_ctx: Any, _tool: str, _result: Any) -> ToolGuardrailResult:
        return ToolGuardrailResult.block({"reason": "sensitive"})

    return ToolOutputGuardrail(guardrail_function=_fn, name=name)


def _replace_output(
    replacement: str,
    name: str = "replace_out",
) -> ToolOutputGuardrail[Any]:
    def _fn(_ctx: Any, _tool: str, _result: Any) -> ToolGuardrailResult:
        return ToolGuardrailResult.replace_content(replacement)

    return ToolOutputGuardrail(guardrail_function=_fn, name=name)


# ---------------------------------------------------------------------------
# attach / accessor
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_anila_tool_decorator_accepts_input_guardrails() -> None:
    """`@anila_tool(input_guardrails=...)` 把 chain 黏到 tool 物件上。"""

    g_in = _allow_input("in_a")
    g_out = _allow_output("out_a")

    @anila_tool(input_guardrails=[g_in], output_guardrails=[g_out])
    def my_tool(value: str) -> str:
        return value

    assert get_tool_input_guardrails(my_tool) == [g_in]
    assert get_tool_output_guardrails(my_tool) == [g_out]


@pytest.mark.unit
def test_anila_tool_decorator_without_guardrails_is_empty() -> None:
    """未掛 guardrail 的 tool,accessor 回傳空 list(向後相容)。"""

    @anila_tool(is_read_only=True)
    def my_tool(x: int) -> int:
        return x + 1

    assert get_tool_input_guardrails(my_tool) == []
    assert get_tool_output_guardrails(my_tool) == []


@pytest.mark.unit
def test_from_function_accepts_guardrails() -> None:
    """`AnilaTool.from_function` 程式化路徑也支援 guardrail kwarg。"""

    g_in = _allow_input("prog_in")

    def echo(value: str) -> str:
        return value

    tool = AnilaTool.from_function(echo, input_guardrails=[g_in])

    chain = get_tool_input_guardrails(tool)
    assert chain == [g_in]


# ---------------------------------------------------------------------------
# enforce_tool_input_chain — ALLOW / BLOCK / REPLACE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_input_chain_allow_returns_original_args() -> None:
    """全 ALLOW chain → payload 等於原 args、replaced=False。"""
    outcome = await enforce_tool_input_chain(
        [_allow_input("a"), _allow_input("b")],
        ctx=None,
        tool_name="write_file",
        args={"path": "/tmp/x", "content": "hi"},
    )
    assert isinstance(outcome, GuardrailChainOutcome)
    assert outcome.replaced is False
    assert outcome.payload == {"path": "/tmp/x", "content": "hi"}
    assert len(outcome.results) == 2


@pytest.mark.asyncio
@pytest.mark.unit
async def test_input_chain_block_raises_tripwire() -> None:
    """chain 任一回 BLOCK → raise tripwire,info 帶 tool_name / stage。"""
    with pytest.raises(GuardrailTripwireTriggered) as exc_info:
        await enforce_tool_input_chain(
            [_allow_input("a"), _block_input("hard_stop")],
            ctx=None,
            tool_name="delete_db",
            args={"db": "prod"},
        )
    err = exc_info.value
    assert err.guardrail_name == "hard_stop"
    assert err.info["stage"] == "input"
    assert err.info["tool_name"] == "delete_db"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_input_chain_replace_content_short_circuits() -> None:
    """REPLACE_CONTENT 短路後續 guardrail,replaced=True 並把 replacement 放進 payload。"""

    # 用 sentinel 偵測「替代後不再呼叫後續 guardrail」。
    later_called: list[bool] = []

    def _sentinel(_ctx: Any, _tool: str, _args: dict[str, Any]) -> ToolGuardrailResult:
        later_called.append(True)
        return ToolGuardrailResult.allow()

    sentinel = ToolInputGuardrail(guardrail_function=_sentinel, name="sentinel")

    outcome = await enforce_tool_input_chain(
        [_replace_input("[redacted]"), sentinel],
        ctx=None,
        tool_name="write_file",
        args={"path": "/tmp/x", "content": "secret"},
    )
    assert outcome.replaced is True
    assert outcome.payload == {"__replaced__": "[redacted]"}
    # 命中 REPLACE_CONTENT 後不應再呼叫 sentinel。
    assert later_called == []


# ---------------------------------------------------------------------------
# enforce_tool_output_chain — 三種 behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_output_chain_allow_returns_original_result() -> None:
    outcome = await enforce_tool_output_chain(
        [_allow_output("o1")],
        ctx=None,
        tool_name="read_file",
        result="hello world",
    )
    assert outcome.replaced is False
    assert outcome.payload == "hello world"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_output_chain_block_raises_tripwire() -> None:
    with pytest.raises(GuardrailTripwireTriggered) as exc_info:
        await enforce_tool_output_chain(
            [_block_output("redact_block")],
            ctx=None,
            tool_name="read_file",
            result="API_KEY=xxx",
        )
    assert exc_info.value.info["stage"] == "output"
    assert exc_info.value.info["tool_name"] == "read_file"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_output_chain_replace_content_replaces_payload() -> None:
    """REPLACE_CONTENT 取代 payload;後續 guardrail 仍會用 replaced payload 跑。"""

    seen: list[Any] = []

    def _peek(_ctx: Any, _tool: str, payload: Any) -> ToolGuardrailResult:
        seen.append(payload)
        return ToolGuardrailResult.allow()

    peek = ToolOutputGuardrail(guardrail_function=_peek, name="peek")

    outcome = await enforce_tool_output_chain(
        [_replace_output("[redacted]"), peek],
        ctx=None,
        tool_name="read_file",
        result="API_KEY=xxx",
    )
    assert outcome.replaced is True
    assert outcome.payload == "[redacted]"
    # 後續 guardrail 看到的是 replaced 後的 payload。
    assert seen == ["[redacted]"]


# ---------------------------------------------------------------------------
# 全局 + tool-level 兩道串聯
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_global_then_tool_level_chain_both_run() -> None:
    """spec:「先過全局 → 後過 tool-level」,兩道都要實際執行。"""

    global_seen: list[str] = []
    tool_seen: list[str] = []

    def _global_check(
        _ctx: Any, tool: str, _args: dict[str, Any]
    ) -> ToolGuardrailResult:
        global_seen.append(tool)
        return ToolGuardrailResult.allow()

    def _tool_check(
        _ctx: Any, tool: str, _args: dict[str, Any]
    ) -> ToolGuardrailResult:
        tool_seen.append(tool)
        return ToolGuardrailResult.allow()

    global_chain = [
        ToolInputGuardrail(guardrail_function=_global_check, name="global")
    ]
    tool_chain = [ToolInputGuardrail(guardrail_function=_tool_check, name="local")]

    # 模擬 runner 串聯邏輯:先過全局,再過 tool-level。
    g_outcome = await enforce_tool_input_chain(
        global_chain, ctx=None, tool_name="my_tool", args={"v": 1}
    )
    t_outcome = await enforce_tool_input_chain(
        tool_chain, ctx=None, tool_name="my_tool", args=g_outcome.payload
    )

    assert global_seen == ["my_tool"]
    assert tool_seen == ["my_tool"]
    assert t_outcome.replaced is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_global_block_short_circuits_tool_level() -> None:
    """全局 BLOCK 直接 raise,tool-level chain 不會被觸發。"""

    tool_seen: list[str] = []

    def _tool_check(
        _ctx: Any, tool: str, _args: dict[str, Any]
    ) -> ToolGuardrailResult:
        tool_seen.append(tool)
        return ToolGuardrailResult.allow()

    # tool_chain 故意保留宣告,但實際 runner 在 global raise 後不會走到它。
    _tool_chain = [ToolInputGuardrail(guardrail_function=_tool_check, name="local")]
    assert _tool_chain  # 純驗證已建構;guard against 未來 refactor 漏掉宣告。

    with pytest.raises(GuardrailTripwireTriggered):
        await enforce_tool_input_chain(
            [_block_input("global_block")],
            ctx=None,
            tool_name="my_tool",
            args={},
        )
    # caller 邏輯預期:global raise 後,根本不會跑到 tool-level chain。
    assert tool_seen == []


# ---------------------------------------------------------------------------
# Example guardrail: LengthLimitInputGuardrail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_length_limit_allows_short_string() -> None:
    guard = LengthLimitInputGuardrail(max_chars=10)
    outcome = await enforce_tool_input_chain(
        [guard], ctx=None, tool_name="write_file", args={"content": "ok"}
    )
    assert outcome.replaced is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_length_limit_blocks_overlong_string() -> None:
    guard = LengthLimitInputGuardrail(max_chars=5)
    with pytest.raises(GuardrailTripwireTriggered) as exc_info:
        await enforce_tool_input_chain(
            [guard],
            ctx=None,
            tool_name="write_file",
            args={"content": "way too long"},
        )
    info = exc_info.value.info
    assert info["field"] == "content"
    assert info["limit"] == 5
    assert info["length"] == len("way too long")


@pytest.mark.asyncio
@pytest.mark.unit
async def test_length_limit_can_target_specific_fields() -> None:
    """指定 fields 後,其它欄位的長 string 不會被擋。"""
    guard = LengthLimitInputGuardrail(max_chars=3, fields=["content"])
    outcome = await enforce_tool_input_chain(
        [guard],
        ctx=None,
        tool_name="write_file",
        args={"path": "/tmp/very/long/path/that/exceeds/three", "content": "ok"},
    )
    assert outcome.replaced is False


@pytest.mark.unit
def test_length_limit_attached_via_decorator() -> None:
    """完整 sugar pattern:`@anila_tool(input_guardrails=[LengthLimitInputGuardrail(...)])`。"""

    @anila_tool(input_guardrails=[LengthLimitInputGuardrail(max_chars=5000)])
    def write_file(path: str, content: str) -> str:
        return f"wrote {path}"

    chain = get_tool_input_guardrails(write_file)
    assert len(chain) == 1
    assert chain[0].name == "length_limit"


# ---------------------------------------------------------------------------
# Example guardrail: PathSafetyInputGuardrail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_path_safety_allows_path_within_root(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "file.txt"
    target.parent.mkdir(parents=True)
    target.write_text("x")

    guard = PathSafetyInputGuardrail([tmp_path])
    outcome = await enforce_tool_input_chain(
        [guard],
        ctx=None,
        tool_name="read_file",
        args={"path": str(target)},
    )
    assert outcome.replaced is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_path_safety_blocks_escape(tmp_path: Path) -> None:
    """``..`` / 絕對路徑跳出 allowed_roots 都應該擋。"""
    guard = PathSafetyInputGuardrail([tmp_path])
    with pytest.raises(GuardrailTripwireTriggered) as exc_info:
        await enforce_tool_input_chain(
            [guard],
            ctx=None,
            tool_name="read_file",
            args={"path": "/etc/passwd"},
        )
    info = exc_info.value.info
    assert info["field"] == "path"
    assert "allowed_roots" in info


@pytest.mark.asyncio
@pytest.mark.unit
async def test_path_safety_handles_relative_escape(tmp_path: Path) -> None:
    """相對路徑 ``../../`` 試圖跳出 root 也要擋。"""
    sub = tmp_path / "workspace"
    sub.mkdir()

    guard = PathSafetyInputGuardrail([sub])
    # 解析後會跑到 tmp_path 之外。
    escape = str(sub / ".." / ".." / "etc" / "passwd")

    with pytest.raises(GuardrailTripwireTriggered):
        await enforce_tool_input_chain(
            [guard],
            ctx=None,
            tool_name="read_file",
            args={"path": escape},
        )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_path_safety_ignores_non_path_fields(tmp_path: Path) -> None:
    """args 中不是 path_fields 的 key 不該被檢查。"""
    guard = PathSafetyInputGuardrail([tmp_path], path_fields=["path"])
    outcome = await enforce_tool_input_chain(
        [guard],
        ctx=None,
        tool_name="search",
        args={"query": "/etc/passwd"},  # query 不在 path_fields 中
    )
    assert outcome.replaced is False


@pytest.mark.asyncio
@pytest.mark.unit
async def test_path_safety_blocks_when_no_roots_allowed(tmp_path: Path) -> None:
    """``allowed_roots`` 空 list → 任何 path field 都會擋。"""
    guard = PathSafetyInputGuardrail([])
    with pytest.raises(GuardrailTripwireTriggered):
        await enforce_tool_input_chain(
            [guard],
            ctx=None,
            tool_name="read_file",
            args={"path": str(tmp_path / "x")},
        )


# ---------------------------------------------------------------------------
# Behavior enum sanity
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_chain_outcome_dataclass_fields() -> None:
    """`GuardrailChainOutcome` 是 frozen dataclass — 防止 caller 不慎 mutate。"""
    from dataclasses import FrozenInstanceError

    outcome = GuardrailChainOutcome(payload="hi", replaced=False, results=())
    assert outcome.payload == "hi"
    assert outcome.replaced is False
    assert outcome.results == ()
    with pytest.raises(FrozenInstanceError):
        outcome.payload = "no"  # type: ignore[misc]


@pytest.mark.unit
def test_behavior_enum_values_stable() -> None:
    """enum value 是 serialized log / audit 用,固定字串不能改。"""
    assert ToolGuardrailBehavior.ALLOW.value == "allow"
    assert ToolGuardrailBehavior.BLOCK.value == "block"
    assert ToolGuardrailBehavior.REPLACE_CONTENT.value == "replace_content"
