"""triggers：事件派發、predicate gating、失敗隔離。"""

from __future__ import annotations

import pytest

from anila_agent.triggers.runner import Trigger, TriggerRunner

pytestmark = pytest.mark.unit


async def test_dispatch_runs_matching_event():
    seen = []

    async def action(payload):
        seen.append(payload)

    r = TriggerRunner()
    r.register(Trigger(name="t", event="turn_end", action=action))
    r.register(Trigger(name="other", event="tool_error", action=action))
    ran = await r.dispatch("turn_end", "payload-x")
    assert ran == 1 and seen == ["payload-x"]


async def test_predicate_gates():
    seen = []

    async def action(payload):
        seen.append(payload)

    r = TriggerRunner()
    r.register(Trigger(name="t", event="e", action=action, when=lambda p: p > 10))
    assert await r.dispatch("e", 5) == 0
    assert await r.dispatch("e", 20) == 1
    assert seen == [20]


async def test_failure_isolated():
    seen = []

    async def boom(payload):
        raise RuntimeError("bad trigger")

    async def ok(payload):
        seen.append(payload)

    r = TriggerRunner()
    r.register(Trigger(name="boom", event="e", action=boom))
    r.register(Trigger(name="ok", event="e", action=ok))
    ran = await r.dispatch("e", "x")
    assert ran == 1 and seen == ["x"]  # 壞的被隔離，好的照跑
