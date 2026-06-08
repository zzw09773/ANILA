"""P2-15 — analytics log_event 測試。"""

from __future__ import annotations

import logging

import pytest

from anila_agent.utils.analytics import (
    EVENT_PREFIX,
    AnalyticsEmitter,
    AnalyticsEvent,
    CallableSink,
    InMemorySink,
    LoggingSink,
    canonical_event_name,
    get_analytics_emitter,
    log_event,
    reset_analytics_emitter,
)


def test_canonical_event_name_adds_prefix():
    assert canonical_event_name("agent_start") == f"{EVENT_PREFIX}agent_start"
    # 已含 prefix 不重複加
    assert canonical_event_name(f"{EVENT_PREFIX}tool_call") == f"{EVENT_PREFIX}tool_call"


def test_canonical_event_name_rejects_empty():
    with pytest.raises(ValueError):
        canonical_event_name("")


def test_event_to_dict_serializable():
    event = AnalyticsEvent(
        name="anila_agent_start",
        properties={"model": "gemma", "latency_ms": 120},
        chain_id="chain-1",
    )
    d = event.to_dict()
    assert d["name"] == "anila_agent_start"
    assert d["properties"]["model"] == "gemma"
    assert d["chain_id"] == "chain-1"
    assert isinstance(d["timestamp"], str)


def test_event_is_frozen():
    event = AnalyticsEvent(name="anila_x")
    with pytest.raises(Exception):
        event.name = "anila_y"  # type: ignore[misc]


def test_inmemory_sink_captures_events():
    sink = InMemorySink(capacity=5)
    emitter = AnalyticsEmitter(sinks=[sink])
    emitter.emit("agent_start", {"k": 1})
    emitter.emit("tool_call", {"name": "bash"})
    snap = sink.snapshot()
    assert len(snap) == 2
    assert snap[0].name == f"{EVENT_PREFIX}agent_start"
    assert snap[1].properties["name"] == "bash"


def test_inmemory_sink_evicts_oldest():
    sink = InMemorySink(capacity=2)
    emitter = AnalyticsEmitter(sinks=[sink])
    for i in range(5):
        emitter.emit("step", {"i": i})
    snap = sink.snapshot()
    assert len(snap) == 2
    assert snap[0].properties["i"] == 3
    assert snap[1].properties["i"] == 4


def test_inmemory_sink_latest():
    sink = InMemorySink(capacity=10)
    for i in range(5):
        sink.emit(AnalyticsEvent(name=f"anila_{i}"))
    last2 = sink.latest(2)
    assert [e.name for e in last2] == ["anila_3", "anila_4"]


def test_inmemory_sink_invalid_capacity():
    with pytest.raises(ValueError):
        InMemorySink(capacity=0)


def test_callable_sink_invokes_callback():
    received: list[AnalyticsEvent] = []
    sink = CallableSink(callback=received.append)
    emitter = AnalyticsEmitter(sinks=[sink])
    emitter.emit("user_query", {"q": "hi"})
    assert len(received) == 1
    assert received[0].name == f"{EVENT_PREFIX}user_query"


def test_logging_sink_outputs_record(caplog):
    sink = LoggingSink(logger=logging.getLogger("anila_test"))
    emitter = AnalyticsEmitter(sinks=[sink])
    with caplog.at_level(logging.INFO, logger="anila_test"):
        emitter.emit("hop", {"x": 1})
    assert any("event=anila_hop" in r.message for r in caplog.records)


def test_sink_exception_does_not_break_others():
    class Boom:
        def emit(self, event: AnalyticsEvent) -> None:
            raise RuntimeError("boom")

    inmem = InMemorySink()
    emitter = AnalyticsEmitter(sinks=[Boom(), inmem])
    emitter.emit("ok_event")
    # 第二個 sink 仍應收到
    assert len(inmem.snapshot()) == 1


def test_emitter_with_no_sinks_returns_event():
    emitter = AnalyticsEmitter()
    event = emitter.emit("x", {"v": 1})
    assert event is not None
    assert event.name == f"{EVENT_PREFIX}x"


def test_emitter_add_sink_dynamically():
    sink = InMemorySink()
    emitter = AnalyticsEmitter()
    emitter.emit("before", {})  # 沒 sink 收
    emitter.add_sink(sink)
    emitter.emit("after", {})
    assert len(sink.snapshot()) == 1
    assert sink.snapshot()[0].name == f"{EVENT_PREFIX}after"


def test_disabled_env_skips_emit(monkeypatch):
    monkeypatch.setenv("ANILA_ANALYTICS_DISABLED", "1")
    sink = InMemorySink()
    emitter = AnalyticsEmitter(sinks=[sink])
    result = emitter.emit("ignored")
    assert result is None
    assert len(sink.snapshot()) == 0


def test_global_log_event_uses_singleton():
    sink = InMemorySink()
    reset_analytics_emitter(sinks=[sink])
    log_event("global_test", {"k": "v"})
    assert len(sink.snapshot()) == 1
    assert sink.snapshot()[0].properties["k"] == "v"


def test_reset_creates_fresh_emitter():
    sink1 = InMemorySink()
    reset_analytics_emitter(sinks=[sink1])
    log_event("a")
    reset_analytics_emitter(sinks=[InMemorySink()])
    log_event("b")
    # 原本 sink1 只該有 a
    assert [e.name for e in sink1.snapshot()] == [f"{EVENT_PREFIX}a"]


def test_get_emitter_lazy_init():
    # 直接 set 為 None 然後 get,應該回新 emitter(無 sink)
    from anila_agent.utils import analytics as mod

    mod._global_emitter = None
    emitter = get_analytics_emitter()
    assert isinstance(emitter, AnalyticsEmitter)
    assert emitter.sinks == []
