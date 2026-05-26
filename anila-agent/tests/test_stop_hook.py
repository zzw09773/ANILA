"""P1-14 Stop hook prevent-continuation 單元測試。

驗證項目:

* `StopHookDecision` 的 `allow()` / `prevent()` 便利 constructor。
* 三個內建 stop hook:
    - `MaxIterationsStopHook` — iteration 上限保險。
    - `OutputLengthStopHook` — output 太短 prevent;達標 allow。
    - `KeywordStopHook` — output 缺 keyword prevent;case-insensitive 預設。
* Chain 早停:任一 prevent 就 early-exit,後續 hook 不執行。
* `AnilaRunHooks.register_stop_hook` / `fire_stop_hooks` 與 EventBus 整合。
* `AnilaStreamRunner` 在 final_output 前 fire stop hook chain;
  prevent 時不 yield final_output,改 yield 帶 inject_message 的 message event。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from anila_agent.core.events import EventBus
from anila_agent.core.hooks import AnilaRunHooks, HookRegistry
from anila_agent.core.stop_hook import (
    KeywordStopHook,
    MaxIterationsStopHook,
    OutputLengthStopHook,
    StopHook,
    StopHookDecision,
    fire_stop_hook_chain,
)
from anila_agent.core.streaming import (
    AnilaStreamRunner,
    RunItemStreamEvent,
    StreamEvent,
)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


async def _aiter(chunks: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    for chunk in chunks:
        yield chunk


async def _collect(events: AsyncIterator[StreamEvent]) -> list[StreamEvent]:
    out: list[StreamEvent] = []
    async for ev in events:
        out.append(ev)
    return out


class _CountingStopHook(StopHook):
    """記錄被呼叫次數的 stop hook;``decision`` 控制每次回什麼。"""

    def __init__(self, decision: StopHookDecision) -> None:
        self._decision = decision
        self.calls = 0

    def check(self, ctx: Any, agent_output: Any) -> StopHookDecision:
        self.calls += 1
        return self._decision


# ---------------------------------------------------------------------------
# StopHookDecision
# ---------------------------------------------------------------------------


def test_decision_allow_is_default() -> None:
    """``StopHookDecision()`` 預設不阻止;``allow()`` 應與之等價。"""
    default = StopHookDecision()
    allow = StopHookDecision.allow()
    assert default.prevent_continuation is False
    assert allow.prevent_continuation is False
    assert default.reason is None and default.inject_message is None


def test_decision_prevent_carries_reason_and_inject() -> None:
    """``prevent(reason=..., inject_message=...)`` 應帶上 metadata。"""
    decision = StopHookDecision.prevent(
        "需要 citation", inject_message="請補上來源引用"
    )
    assert decision.prevent_continuation is True
    assert decision.reason == "需要 citation"
    assert decision.inject_message == "請補上來源引用"


# ---------------------------------------------------------------------------
# MaxIterationsStopHook
# ---------------------------------------------------------------------------


def test_max_iterations_validates_positive() -> None:
    """``max_iterations`` < 1 應 raise ValueError。"""
    with pytest.raises(ValueError):
        MaxIterationsStopHook(max_iterations=0)


def test_max_iterations_allow_below_limit() -> None:
    """iteration 還在上限內 → allow(本 hook 不主動 prevent)。"""
    hook = MaxIterationsStopHook(max_iterations=3)
    decision = hook.check({"iteration": 1}, "any output")
    assert decision.prevent_continuation is False


def test_max_iterations_allow_at_limit_to_force_termination() -> None:
    """iteration >= 上限 → 強制 allow(避免無限迴圈)。

    注意:本 hook 的角色是「上限保險」,語義是「達上限後不再 prevent」,
    不是「達上限後額外 prevent」。
    """
    hook = MaxIterationsStopHook(max_iterations=3)
    # iteration 達到 max 與超過 max 都應 allow。
    assert hook.check({"iteration": 3}, "out").prevent_continuation is False
    assert hook.check({"iteration": 99}, "out").prevent_continuation is False


def test_max_iterations_extracts_from_attribute_ctx() -> None:
    """ctx 可以是帶 ``iteration`` attribute 的物件,不只是 dict。"""

    class _Ctx:
        iteration = 5

    hook = MaxIterationsStopHook(max_iterations=10)
    decision = hook.check(_Ctx(), "out")
    assert decision.prevent_continuation is False


# ---------------------------------------------------------------------------
# OutputLengthStopHook
# ---------------------------------------------------------------------------


def test_output_length_prevents_when_too_short() -> None:
    """output 字元數 < ``min_chars`` → prevent;reason 提到實際長度。"""
    hook = OutputLengthStopHook(min_chars=10)
    decision = hook.check(None, "short")
    assert decision.prevent_continuation is True
    assert decision.reason is not None
    assert "5" in decision.reason  # len("short") == 5
    assert decision.inject_message is not None


def test_output_length_allows_when_long_enough() -> None:
    """output 字元數 >= ``min_chars`` → allow。"""
    hook = OutputLengthStopHook(min_chars=5)
    decision = hook.check(None, "hello world")
    assert decision.prevent_continuation is False


def test_output_length_none_treated_as_empty() -> None:
    """``agent_output=None`` 視為 0 字元 → prevent(若 min_chars > 0)。"""
    hook = OutputLengthStopHook(min_chars=1)
    decision = hook.check(None, None)
    assert decision.prevent_continuation is True


def test_output_length_validates_non_negative() -> None:
    """``min_chars`` < 0 應 raise ValueError。"""
    with pytest.raises(ValueError):
        OutputLengthStopHook(min_chars=-1)


def test_output_length_custom_inject_message() -> None:
    """傳入的 ``inject_message`` 應原樣轉發。"""
    hook = OutputLengthStopHook(min_chars=50, inject_message="補完它")
    decision = hook.check(None, "tiny")
    assert decision.inject_message == "補完它"


# ---------------------------------------------------------------------------
# KeywordStopHook
# ---------------------------------------------------------------------------


def test_keyword_prevents_when_missing() -> None:
    """output 缺任一 required keyword → prevent;reason 列出缺漏項。"""
    hook = KeywordStopHook(["citation", "source"])
    decision = hook.check(None, "Here is the answer.")
    assert decision.prevent_continuation is True
    assert decision.reason is not None
    assert "citation" in decision.reason
    assert "source" in decision.reason


def test_keyword_allows_when_all_present_case_insensitive() -> None:
    """預設 case-insensitive — 全 keyword 出現 → allow。"""
    hook = KeywordStopHook(["Citation", "Source"])
    decision = hook.check(None, "see citation 1 and source code")
    assert decision.prevent_continuation is False


def test_keyword_case_sensitive_mode() -> None:
    """``case_sensitive=True`` — 大小寫不符視為缺漏。"""
    hook = KeywordStopHook(["Citation"], case_sensitive=True)
    decision = hook.check(None, "lowercase citation only")
    assert decision.prevent_continuation is True


def test_keyword_empty_list_always_allows() -> None:
    """空 required list → 永遠 allow。"""
    hook = KeywordStopHook([])
    decision = hook.check(None, "")
    assert decision.prevent_continuation is False


# ---------------------------------------------------------------------------
# Chain early-exit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chain_runs_all_hooks_when_all_allow() -> None:
    """全 allow → chain 跑完每個 hook,最終 return allow。"""
    a = _CountingStopHook(StopHookDecision.allow())
    b = _CountingStopHook(StopHookDecision.allow())
    c = _CountingStopHook(StopHookDecision.allow())

    decision = await fire_stop_hook_chain([a, b, c], None, "out")

    assert decision.prevent_continuation is False
    assert a.calls == 1 and b.calls == 1 and c.calls == 1


@pytest.mark.asyncio
async def test_chain_early_exits_on_first_prevent() -> None:
    """任一 prevent → 立刻 return,後續 hook 不執行。"""
    a = _CountingStopHook(StopHookDecision.allow())
    b = _CountingStopHook(
        StopHookDecision.prevent("stop here", inject_message="redo")
    )
    c = _CountingStopHook(StopHookDecision.allow())

    decision = await fire_stop_hook_chain([a, b, c], None, "out")

    assert decision.prevent_continuation is True
    assert decision.reason == "stop here"
    assert decision.inject_message == "redo"
    assert a.calls == 1 and b.calls == 1
    assert c.calls == 0  # chain early-exit, c 不該被呼叫


@pytest.mark.asyncio
async def test_chain_accepts_callable_entries() -> None:
    """`StopHookEntry` 也吃 callable,不一定要 subclass `StopHook`。"""
    sync_calls: list[str] = []

    def _sync_hook(ctx: Any, output: Any) -> StopHookDecision:
        sync_calls.append("sync")
        return StopHookDecision.allow()

    async def _async_hook(ctx: Any, output: Any) -> StopHookDecision:
        sync_calls.append("async")
        return StopHookDecision.prevent("from async")

    decision = await fire_stop_hook_chain([_sync_hook, _async_hook], None, "x")

    assert sync_calls == ["sync", "async"]
    assert decision.prevent_continuation is True
    assert decision.reason == "from async"


@pytest.mark.asyncio
async def test_chain_rejects_wrong_return_type() -> None:
    """Hook callback 回非 `StopHookDecision` → raise TypeError(silent failure 防呆)。"""

    def _bad_hook(ctx: Any, output: Any) -> Any:
        return "not a decision"  # 故意錯型別

    with pytest.raises(TypeError):
        await fire_stop_hook_chain([_bad_hook], None, "x")


# ---------------------------------------------------------------------------
# RunHooks (P0-1) 整合 — register_stop_hook + fire_stop_hooks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runhooks_register_and_fire_stop_hooks_allow_path() -> None:
    """`AnilaRunHooks.register_stop_hook` + `fire_stop_hooks` 全 allow path。"""
    bus = EventBus()
    events: list[tuple[str, dict[str, Any]]] = []
    bus.on_any(lambda e: events.append((e.kind, dict(e.payload))))

    hooks = AnilaRunHooks(HookRegistry(), bus, agent_name="root")
    hook = OutputLengthStopHook(min_chars=3)
    hooks.register_stop_hook(hook)

    assert hooks.stop_hooks == (hook,)

    decision = await hooks.fire_stop_hooks(ctx={"iteration": 0}, agent_output="abcdef")

    assert decision.prevent_continuation is False
    # 應有 stop_hook_fired event,但不應有 prevented_continuation event。
    kinds = [k for k, _ in events]
    assert "stop_hook_fired" in kinds
    assert "stop_hook_prevented_continuation" not in kinds


@pytest.mark.asyncio
async def test_runhooks_fire_stop_hooks_prevent_path_emits_events() -> None:
    """Prevent path:既 emit ``stop_hook_fired``、也 emit ``stop_hook_prevented_continuation``。"""
    bus = EventBus()
    events: list[tuple[str, dict[str, Any]]] = []
    bus.on_any(lambda e: events.append((e.kind, dict(e.payload))))

    hooks = AnilaRunHooks(HookRegistry(), bus, agent_name="root")
    hooks.register_stop_hook(KeywordStopHook(["citation"]))

    decision = await hooks.fire_stop_hooks(
        ctx={"iteration": 0}, agent_output="no source mentioned"
    )

    assert decision.prevent_continuation is True
    kinds = [k for k, _ in events]
    assert "stop_hook_fired" in kinds
    assert "stop_hook_prevented_continuation" in kinds
    # payload 應帶 inject_message 與 reason
    prevented = next(
        p for k, p in events if k == "stop_hook_prevented_continuation"
    )
    assert prevented["reason"] is not None
    assert prevented["inject_message"] is not None


@pytest.mark.asyncio
async def test_runhooks_empty_chain_always_allows() -> None:
    """沒註冊任何 stop hook → fire_stop_hooks 直接 allow。"""
    bus = EventBus()
    hooks = AnilaRunHooks(HookRegistry(), bus, agent_name="root")
    decision = await hooks.fire_stop_hooks(ctx=None, agent_output="anything")
    assert decision.prevent_continuation is False


# ---------------------------------------------------------------------------
# AnilaStreamRunner (P1-7) 整合
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_runner_yields_final_output_when_stop_hook_allows() -> None:
    """Stop hook allow → 正常 yield ``final_output``。"""
    runner = AnilaStreamRunner(HookRegistry(), EventBus())
    runner.register_stop_hook(OutputLengthStopHook(min_chars=1))

    events = await _collect(
        runner.run_streamed(_aiter([{"kind": "message", "text": "hello world"}]))
    )
    final = [
        e
        for e in events
        if isinstance(e, RunItemStreamEvent) and e.item_type == "final_output"
    ]
    assert len(final) == 1
    assert final[0].item == "hello world"


@pytest.mark.asyncio
async def test_stream_runner_skips_final_output_when_prevented() -> None:
    """Stop hook prevent → 不 yield ``final_output``,改 yield 帶 inject_message 的 message。"""
    bus = EventBus()
    runner = AnilaStreamRunner(HookRegistry(), bus)
    runner.register_stop_hook(OutputLengthStopHook(min_chars=100))

    events = await _collect(
        runner.run_streamed(_aiter([{"kind": "message", "text": "too short"}]))
    )

    final = [
        e
        for e in events
        if isinstance(e, RunItemStreamEvent) and e.item_type == "final_output"
    ]
    assert final == []  # final_output 應被跳過

    # 最後一個 event 應是 stop hook 的 inject_message
    last = events[-1]
    assert isinstance(last, RunItemStreamEvent)
    assert last.item_type == "message"
    assert isinstance(last.item, dict)
    assert last.item.get("stop_hook_prevented") is True
    assert last.item.get("reason") is not None
    assert last.item.get("text")  # inject_message 不空


@pytest.mark.asyncio
async def test_stream_runner_chain_early_exit_uses_first_prevent() -> None:
    """Chain 多 hook,第一個 prevent 的 reason / inject 應勝出,後續不執行。"""
    runner = AnilaStreamRunner(HookRegistry(), EventBus())

    second_called = False

    def _first_prevent(ctx: Any, output: Any) -> StopHookDecision:
        return StopHookDecision.prevent("first", inject_message="from first")

    def _second_should_not_run(ctx: Any, output: Any) -> StopHookDecision:
        nonlocal second_called
        second_called = True
        return StopHookDecision.prevent("second")

    runner.register_stop_hook(_first_prevent)
    runner.register_stop_hook(_second_should_not_run)

    events = await _collect(
        runner.run_streamed(_aiter([{"kind": "message", "text": "x"}]))
    )

    assert second_called is False
    last = events[-1]
    assert isinstance(last, RunItemStreamEvent)
    assert last.item["reason"] == "first"
    assert last.item["text"] == "from first"


@pytest.mark.asyncio
async def test_stream_runner_init_accepts_stop_hooks_kwarg() -> None:
    """`AnilaStreamRunner(..., stop_hooks=[...])` 應該與 `register_stop_hook` 等價。"""
    hook = OutputLengthStopHook(min_chars=1)
    runner = AnilaStreamRunner(
        HookRegistry(), EventBus(), stop_hooks=[hook]
    )
    assert runner.stop_hooks == (hook,)
