"""P2-15 — InMemoryErrorBuffer 測試。"""

from __future__ import annotations

import pytest

from anila_agent.utils.error_buffer import (
    DEFAULT_BUFFER_SIZE,
    InMemoryErrorBuffer,
    get_error_buffer,
    record_error,
    reset_error_buffer,
)


def test_default_buffer_size_constant_reasonable():
    assert DEFAULT_BUFFER_SIZE >= 50


def test_buffer_push_with_exception_captures_traceback():
    buf = InMemoryErrorBuffer(max_size=10)
    try:
        raise ValueError("boom")
    except ValueError as exc:
        buf.push(source="test", error=exc)

    snap = buf.snapshot()
    assert len(snap) == 1
    record = snap[0]
    assert record.kind == "ValueError"
    assert record.message == "boom"
    assert record.source == "test"
    assert record.traceback_text is not None
    assert "ValueError" in record.traceback_text


def test_buffer_push_with_message_only():
    buf = InMemoryErrorBuffer(max_size=10)
    buf.push(source="logic", message="something off", kind="LogicError")
    snap = buf.snapshot()
    assert len(snap) == 1
    assert snap[0].kind == "LogicError"
    assert snap[0].traceback_text is None


def test_buffer_push_requires_error_or_message():
    buf = InMemoryErrorBuffer(max_size=5)
    with pytest.raises(ValueError):
        buf.push(source="x")


def test_buffer_evicts_oldest_when_full():
    buf = InMemoryErrorBuffer(max_size=3)
    for i in range(5):
        buf.push(source="t", message=f"err {i}")
    snap = buf.snapshot()
    assert len(snap) == 3
    # 最舊兩筆應被擠掉,剩 2/3/4
    assert [r.message for r in snap] == ["err 2", "err 3", "err 4"]


def test_buffer_latest_n_returns_tail():
    buf = InMemoryErrorBuffer(max_size=10)
    for i in range(7):
        buf.push(source="t", message=f"err {i}")
    last3 = buf.latest(3)
    assert [r.message for r in last3] == ["err 4", "err 5", "err 6"]


def test_buffer_latest_zero_returns_empty():
    buf = InMemoryErrorBuffer(max_size=10)
    buf.push(source="t", message="x")
    assert buf.latest(0) == []


def test_buffer_latest_n_larger_than_size_returns_all():
    buf = InMemoryErrorBuffer(max_size=10)
    for i in range(3):
        buf.push(source="t", message=f"err {i}")
    assert len(buf.latest(100)) == 3


def test_buffer_filter_by_source():
    buf = InMemoryErrorBuffer(max_size=10)
    buf.push(source="runner", message="a")
    buf.push(source="tool.bash", message="b")
    buf.push(source="runner", message="c")
    runner_errs = buf.filter_by_source("runner")
    assert len(runner_errs) == 2
    assert all(r.source == "runner" for r in runner_errs)


def test_buffer_clear():
    buf = InMemoryErrorBuffer(max_size=5)
    buf.push(source="t", message="a")
    assert len(buf) == 1
    buf.clear()
    assert len(buf) == 0


def test_record_to_dict_serializable():
    buf = InMemoryErrorBuffer(max_size=5)
    buf.push(
        source="t",
        message="oops",
        kind="MyError",
        context={"path": "/tmp/x", "attempt": 3},
    )
    d = buf.snapshot()[0].to_dict()
    assert d["source"] == "t"
    assert d["kind"] == "MyError"
    assert d["context"]["attempt"] == 3
    assert isinstance(d["timestamp"], str)


def test_invalid_max_size_raises():
    with pytest.raises(ValueError):
        InMemoryErrorBuffer(max_size=0)
    with pytest.raises(ValueError):
        InMemoryErrorBuffer(max_size=-5)


def test_global_singleton_persistence():
    reset_error_buffer(max_size=5)
    record_error(source="g", message="first")
    record_error(source="g", message="second")
    buf = get_error_buffer()
    assert len(buf) == 2
    assert buf.snapshot()[1].message == "second"


def test_reset_global_clears_state():
    reset_error_buffer(max_size=5)
    record_error(source="g", message="leftover")
    reset_error_buffer(max_size=5)
    assert len(get_error_buffer()) == 0


def test_env_var_overrides_default_size(monkeypatch):
    monkeypatch.setenv("ANILA_ERROR_BUFFER_SIZE", "7")
    buf = reset_error_buffer()
    assert buf.max_size == 7


def test_env_var_invalid_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("ANILA_ERROR_BUFFER_SIZE", "not-a-number")
    buf = reset_error_buffer()
    assert buf.max_size == DEFAULT_BUFFER_SIZE
