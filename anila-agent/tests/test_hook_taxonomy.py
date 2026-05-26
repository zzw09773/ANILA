"""P0-5 Hook 三類強型別分類單元測試。

驗證項目:

* `Decision` immutable + 三個便捷建構子 (allow / deny / skip) 行為一致。
* `InspectHook` chain 不影響 ctx / payload (但會被觀察到)。
* `DecideHook` 對 ALLOW / DENY / SKIP 三種回傳的合成規則。
* `DecideHook` chain 內任一 DENY 即 early-exit,後續 hook 不被呼叫。
* `TransformHook` chain monadic 串接 (兩個 transform 接力)。
* 完整 pipeline:Inspect 全跑 -> Decide DENY 就停 -> Transform 不執行。
* 靜態型別:hook 用 `typing.Protocol` 結構式比對,不需 ABC subclass。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from anila_agent.core import (
    Decision,
    DecideHook,
    DecisionVerdict,
    HookExecutor,
    InspectHook,
    PipelineResult,
    TransformHook,
)


# ---------------------------------------------------------------------------
# Fixtures / Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeCtx:
    """測試用最小 hook context:可記錄 hook 寫入的痕跡。"""

    trace: list[str] = field(default_factory=list)


# ---- Inspect fakes ---------------------------------------------------------


@dataclass
class _RecordInspect:
    """InspectHook —— 把 payload 記到 ctx.trace。"""

    label: str

    def inspect(self, ctx: _FakeCtx, payload: Any) -> None:
        ctx.trace.append(f"inspect:{self.label}:{payload}")


# ---- Decide fakes ----------------------------------------------------------


@dataclass
class _AlwaysAllow:
    label: str

    def decide(self, ctx: _FakeCtx, payload: Any) -> Decision:
        ctx.trace.append(f"decide:{self.label}:allow")
        return Decision.allow(reason=f"{self.label}_ok", source=self.label)


@dataclass
class _AlwaysDeny:
    label: str

    def decide(self, ctx: _FakeCtx, payload: Any) -> Decision:
        ctx.trace.append(f"decide:{self.label}:deny")
        return Decision.deny(reason=f"{self.label}_bad", source=self.label)


@dataclass
class _AlwaysSkip:
    label: str

    def decide(self, ctx: _FakeCtx, payload: Any) -> Decision:
        ctx.trace.append(f"decide:{self.label}:skip")
        return Decision.skip(source=self.label)


# ---- Transform fakes -------------------------------------------------------


@dataclass
class _AddSuffix:
    """TransformHook —— 把 str payload 加後綴。"""

    suffix: str

    def transform(self, ctx: _FakeCtx, payload: str) -> str:
        ctx.trace.append(f"transform:add:{self.suffix}")
        return payload + self.suffix


@dataclass
class _Upper:
    """TransformHook —— 把 str payload 變大寫。"""

    def transform(self, ctx: _FakeCtx, payload: str) -> str:
        ctx.trace.append("transform:upper")
        return payload.upper()


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


def test_decision_factory_allow() -> None:
    d = Decision.allow(reason="ok", source="my_hook")
    assert d.is_allow is True
    assert d.is_deny is False
    assert d.is_skip is False
    assert d.verdict is DecisionVerdict.ALLOW
    assert d.reason == "ok"
    assert d.source == "my_hook"


def test_decision_factory_deny() -> None:
    d = Decision.deny(reason="nope", source="policy")
    assert d.is_deny is True
    assert d.is_allow is False
    assert d.verdict is DecisionVerdict.DENY


def test_decision_factory_skip() -> None:
    d = Decision.skip(source="audit")
    assert d.is_skip is True
    assert d.verdict is DecisionVerdict.SKIP


def test_decision_is_immutable() -> None:
    d = Decision.allow()
    with pytest.raises(Exception):
        # `frozen=True` 應擋下任何 attribute 寫入
        d.reason = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Protocol 結構式型別比對 (runtime_checkable)
# ---------------------------------------------------------------------------


def test_inspect_protocol_is_runtime_checkable() -> None:
    hook = _RecordInspect(label="a")
    assert isinstance(hook, InspectHook)


def test_decide_protocol_is_runtime_checkable() -> None:
    hook = _AlwaysAllow(label="a")
    assert isinstance(hook, DecideHook)


def test_transform_protocol_is_runtime_checkable() -> None:
    hook = _AddSuffix(suffix="!")
    assert isinstance(hook, TransformHook)


# ---------------------------------------------------------------------------
# InspectHook —— 不影響 payload / ctx (除了 trace)
# ---------------------------------------------------------------------------


def test_inspect_chain_does_not_mutate_payload() -> None:
    ctx = _FakeCtx()
    payload = {"a": 1}
    hooks = [_RecordInspect("h1"), _RecordInspect("h2")]

    HookExecutor.run_inspect(hooks, ctx, payload)

    # payload 本身不變
    assert payload == {"a": 1}
    # 但兩個 hook 都被呼叫過
    assert ctx.trace == [
        "inspect:h1:{'a': 1}",
        "inspect:h2:{'a': 1}",
    ]


def test_inspect_empty_chain_is_noop() -> None:
    ctx = _FakeCtx()
    HookExecutor.run_inspect([], ctx, payload="x")
    assert ctx.trace == []


# ---------------------------------------------------------------------------
# DecideHook —— ALLOW / DENY / SKIP 三種行為
# ---------------------------------------------------------------------------


def test_decide_all_allow_returns_last_allow() -> None:
    ctx = _FakeCtx()
    hooks = [_AlwaysAllow("first"), _AlwaysAllow("second")]

    result = HookExecutor.run_decide(hooks, ctx, payload="x")

    assert result.is_allow is True
    # 應回最後一個 ALLOW (有 reason 可追)
    assert result.source == "second"
    assert result.reason == "second_ok"


def test_decide_all_skip_defaults_to_allow() -> None:
    ctx = _FakeCtx()
    hooks = [_AlwaysSkip("a"), _AlwaysSkip("b")]

    result = HookExecutor.run_decide(hooks, ctx, payload="x")

    assert result.is_allow is True
    assert result.reason == "all_skip"


def test_decide_deny_short_circuits_chain() -> None:
    """DENY early-exit:後續 hook 不應被呼叫。"""
    ctx = _FakeCtx()
    hooks = [
        _AlwaysAllow("first"),
        _AlwaysDeny("middle"),
        _AlwaysAllow("never_called"),
    ]

    result = HookExecutor.run_decide(hooks, ctx, payload="x")

    assert result.is_deny is True
    assert result.source == "middle"
    # third hook 沒被呼叫
    assert "decide:never_called:allow" not in ctx.trace
    assert ctx.trace == [
        "decide:first:allow",
        "decide:middle:deny",
    ]


def test_decide_skip_then_allow_returns_allow() -> None:
    ctx = _FakeCtx()
    hooks = [_AlwaysSkip("a"), _AlwaysAllow("b"), _AlwaysSkip("c")]

    result = HookExecutor.run_decide(hooks, ctx, payload="x")

    assert result.is_allow is True
    assert result.source == "b"


def test_decide_empty_chain_defaults_to_allow() -> None:
    ctx = _FakeCtx()
    result = HookExecutor.run_decide([], ctx, payload="x")
    assert result.is_allow is True


# ---------------------------------------------------------------------------
# TransformHook —— monadic 串接
# ---------------------------------------------------------------------------


def test_transform_chain_monadic() -> None:
    """兩個 transform 接力:前者 output -> 後者 input。"""
    ctx = _FakeCtx()
    hooks = [_AddSuffix(suffix="_X"), _Upper()]

    out = HookExecutor.run_transform(hooks, ctx, payload="hello")

    # hello -> hello_X -> HELLO_X
    assert out == "HELLO_X"
    assert ctx.trace == [
        "transform:add:_X",
        "transform:upper",
    ]


def test_transform_empty_chain_returns_payload_unchanged() -> None:
    ctx = _FakeCtx()
    out = HookExecutor.run_transform([], ctx, payload="raw")
    assert out == "raw"


# ---------------------------------------------------------------------------
# Pipeline —— Inspect -> Decide -> Transform 完整流程
# ---------------------------------------------------------------------------


def test_pipeline_full_allow_runs_transform() -> None:
    """Decide ALLOW -> Transform 應執行。"""
    ctx = _FakeCtx()

    result = HookExecutor.run_pipeline(
        ctx,
        payload="hello",
        inspect_hooks=[_RecordInspect("audit")],
        decide_hooks=[_AlwaysAllow("policy")],
        transform_hooks=[_AddSuffix(suffix="!"), _Upper()],
    )

    assert isinstance(result, PipelineResult)
    assert result.decision.is_allow is True
    assert result.transform_executed is True
    assert result.payload == "HELLO!"
    # 順序檢查:Inspect 先、Decide 中、Transform 後
    assert ctx.trace == [
        "inspect:audit:hello",
        "decide:policy:allow",
        "transform:add:!",
        "transform:upper",
    ]


def test_pipeline_deny_skips_transform() -> None:
    """Decide DENY -> Transform 不應執行;但 Inspect 全跑完。"""
    ctx = _FakeCtx()

    result = HookExecutor.run_pipeline(
        ctx,
        payload="hello",
        inspect_hooks=[_RecordInspect("audit"), _RecordInspect("metrics")],
        decide_hooks=[_AlwaysDeny("policy")],
        transform_hooks=[_AddSuffix(suffix="!"), _Upper()],
    )

    assert result.decision.is_deny is True
    assert result.transform_executed is False
    assert result.payload is None  # transform 沒跑 -> payload 不暴露
    # Inspect 兩個都跑完,Transform 一個都沒跑
    assert "transform:add:!" not in ctx.trace
    assert "transform:upper" not in ctx.trace
    assert ctx.trace == [
        "inspect:audit:hello",
        "inspect:metrics:hello",
        "decide:policy:deny",
    ]


def test_pipeline_order_inspect_before_decide_before_transform() -> None:
    """強制執行順序:即便 hook list 順序顛倒,Inspect 永遠在 Decide 之前。"""
    ctx = _FakeCtx()

    HookExecutor.run_pipeline(
        ctx,
        payload="x",
        # 故意把 inspect 放最後一個 (但 executor 仍會優先跑它)
        inspect_hooks=[_RecordInspect("late_inspect")],
        decide_hooks=[_AlwaysAllow("d")],
        transform_hooks=[_AddSuffix(suffix=".")],
    )

    # 第一個 trace 一定是 inspect,最後一個一定是 transform
    assert ctx.trace[0].startswith("inspect:")
    assert ctx.trace[-1].startswith("transform:")


def test_pipeline_all_skip_decide_still_runs_transform() -> None:
    """所有 Decide SKIP -> 視為 ALLOW -> Transform 仍執行。"""
    ctx = _FakeCtx()

    result = HookExecutor.run_pipeline(
        ctx,
        payload="data",
        decide_hooks=[_AlwaysSkip("a"), _AlwaysSkip("b")],
        transform_hooks=[_AddSuffix(suffix="!")],
    )

    assert result.decision.is_allow is True
    assert result.decision.reason == "all_skip"
    assert result.transform_executed is True
    assert result.payload == "data!"


def test_pipeline_empty_hooks_returns_payload_as_allow() -> None:
    """空的 hook list 應視為 no-op、預設 ALLOW、payload 不變。"""
    ctx = _FakeCtx()

    result = HookExecutor.run_pipeline(ctx, payload="raw")

    assert result.decision.is_allow is True
    assert result.transform_executed is True
    assert result.payload == "raw"
