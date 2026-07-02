"""Tests for ``anila_core.tracing.sdk`` — the Full-Trace producer SDK.

Covers:
  * TraceExporter batching / flush / group-by-trace / ≤256 cap.
  * TraceExporter drop-on-persistent-failure (no real network — a stub
    client that raises) + queue cap drop-counting.
  * TraceSession context-manager timing, ok/error status (re-raise),
    nested parentage.
  * ExportingProcessor bridging Tracer/Span → wire span dicts.
"""

from __future__ import annotations

import pytest

from anila_core.tracing import (
    ExportingProcessor,
    InMemoryProcessor,  # noqa: F401  (import sanity — package surface)
    SpanKind,
    TraceExporter,
    TraceSession,
    Tracer,
)
from anila_core.tracing.sdk import SPAN_TYPES, _MAX_BATCH


@pytest.fixture(autouse=True)
def _isolated_tracer_stack():
    """The in-tree ``Tracer`` keeps its current-span stack in a process-global
    contextvar (shared across independent ``Tracer`` instances). Reset it around
    each test so residue from another module's tests can't reparent our spans
    onto a foreign trace_id."""
    from anila_core.tracing import tracer as _tracer_mod

    _tracer_mod._current_stack.set([])
    yield
    _tracer_mod._current_stack.set([])


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status_code: int = 202, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class _RecordingClient:
    """Sync httpx.Client stand-in that records POSTs."""

    def __init__(self, sink: list, status_code: int = 202) -> None:
        self._sink = sink
        self._status = status_code
        self.closed = False

    def post(self, url, json=None, headers=None):
        self._sink.append({"url": url, "json": json, "headers": headers or {}})
        return _Resp(self._status)

    def close(self) -> None:
        self.closed = True


class _RaisingClient:
    def __init__(self) -> None:
        self.closed = False

    def post(self, *a, **k):
        raise RuntimeError("connection refused")

    def close(self) -> None:
        self.closed = True


def _make_exporter(sink, *, status_code=202, **kw) -> TraceExporter:
    return TraceExporter(
        "http://csp.local",
        token_provider=lambda: "csk-token",
        start_worker=False,  # deterministic: drive flush() manually
        client_factory=lambda: _RecordingClient(sink, status_code=status_code),
        **kw,
    )


# ---------------------------------------------------------------------------
# Exporter — batching / flush
# ---------------------------------------------------------------------------


def test_flush_posts_to_frozen_endpoint_with_service_token() -> None:
    sink: list = []
    exp = _make_exporter(sink)
    exp.enqueue("trace-1", {"span_id": "a", "span_type": "agent.run.finished",
                            "name": "x", "started_at": "t", "status": "ok"})
    exp.flush()

    assert len(sink) == 1
    call = sink[0]
    assert call["url"] == "http://csp.local/v1/traces/trace-1/spans"
    assert call["json"] == {"spans": [{"span_id": "a",
                                       "span_type": "agent.run.finished",
                                       "name": "x", "started_at": "t",
                                       "status": "ok"}]}
    assert call["headers"]["X-CSP-Service-Token"] == "csk-token"
    assert exp.stats()["sent"] == 1


def test_flush_groups_by_trace_id() -> None:
    sink: list = []
    exp = _make_exporter(sink)
    exp.enqueue("t-A", {"span_id": "1"})
    exp.enqueue("t-B", {"span_id": "2"})
    exp.enqueue("t-A", {"span_id": "3"})
    exp.flush()

    by_url = {c["url"]: c["json"]["spans"] for c in sink}
    assert by_url["http://csp.local/v1/traces/t-A/spans"] == [
        {"span_id": "1"}, {"span_id": "3"}
    ]
    assert by_url["http://csp.local/v1/traces/t-B/spans"] == [{"span_id": "2"}]


def test_batch_capped_at_256() -> None:
    sink: list = []
    exp = _make_exporter(sink, batch_size=256)
    for i in range(_MAX_BATCH + 10):
        exp.enqueue("t", {"span_id": str(i)})
    exp.flush()
    # 266 spans for one trace → 256 + 10 across two POSTs.
    sizes = [len(c["json"]["spans"]) for c in sink]
    assert sizes == [_MAX_BATCH, 10]


def test_batch_size_ceiling_clamped_to_256() -> None:
    exp = _make_exporter([], batch_size=99999)
    assert exp._batch_size == _MAX_BATCH


def test_no_token_omits_header() -> None:
    sink: list = []
    exp = TraceExporter(
        "http://csp.local",
        token_provider=lambda: None,
        start_worker=False,
        client_factory=lambda: _RecordingClient(sink),
    )
    exp.enqueue("t", {"span_id": "a"})
    exp.flush()
    assert "X-CSP-Service-Token" not in sink[0]["headers"]


# ---------------------------------------------------------------------------
# Exporter — drop / failure
# ---------------------------------------------------------------------------


def test_drop_and_log_on_post_failure_never_raises() -> None:
    exp = TraceExporter(
        "http://csp.local",
        token_provider=lambda: "t",
        start_worker=False,
        client_factory=lambda: _RaisingClient(),
    )
    exp.enqueue("t", {"span_id": "a"})
    exp.enqueue("t", {"span_id": "b"})
    exp.flush()  # must not raise
    assert exp.stats()["failed"] == 2
    assert exp.stats()["sent"] == 0


def test_non_2xx_counts_as_failed() -> None:
    sink: list = []
    exp = _make_exporter(sink, status_code=500)
    exp.enqueue("t", {"span_id": "a"})
    exp.flush()
    assert exp.stats()["failed"] == 1


def test_queue_cap_drops_newest_and_counts() -> None:
    sink: list = []
    exp = _make_exporter(sink, max_queue=3)
    for i in range(5):
        exp.enqueue("t", {"span_id": str(i)})
    assert exp.stats()["dropped"] == 2
    exp.flush()
    assert [s["span_id"] for s in sink[0]["json"]["spans"]] == ["0", "1", "2"]


def test_enqueue_ignores_bad_input() -> None:
    exp = _make_exporter([])
    exp.enqueue("", {"span_id": "a"})     # no trace id
    exp.enqueue("t", "not-a-dict")        # type: ignore[arg-type]
    assert exp.stats()["queued"] == 0


# ---------------------------------------------------------------------------
# TraceSession — context managers
# ---------------------------------------------------------------------------


def test_span_records_timing_and_ok_status() -> None:
    exp = _make_exporter([])
    session = TraceSession(exp, "trace-xyz", producer="anila-router")
    with session.span("agent.model_call.finished", "call") as h:
        h.set_attribute("model", "gpt-oss")

    assert len(session.spans) == 1
    span = session.spans[0]
    assert span["span_type"] == "agent.model_call.finished"
    assert span["name"] == "call"
    assert span["status"] == "ok"
    assert span["started_at"] and span["ended_at"]
    assert span["attributes"] == {"model": "gpt-oss"}
    assert span["producer"] == "anila-router"
    assert "parent_span_id" not in span  # root


def test_span_marks_error_and_reraises() -> None:
    exp = _make_exporter([])
    session = TraceSession(exp, "trace-err")
    with pytest.raises(ValueError):
        with session.span("agent.tool_call.finished", "boom"):
            raise ValueError("kaboom")

    span = session.spans[0]
    assert span["status"] == "error"
    assert "kaboom" in span["attributes"]["error"]


def test_nested_spans_autoparent() -> None:
    exp = _make_exporter([])
    session = TraceSession(exp, "trace-nest")
    with session.span("agent.run.finished", "run") as root:
        with session.span("agent.step.finished", "step") as child:
            pass
    # child closes first, root second.
    child_dict, root_dict = session.spans
    assert child_dict["parent_span_id"] == root.span_id
    assert "parent_span_id" not in root_dict
    assert child_dict["span_id"] == child.span_id


def test_explicit_parent_overrides_stack() -> None:
    exp = _make_exporter([])
    session = TraceSession(exp, "trace-p")
    with session.span("agent.step.finished", "s", parent_span_id="ext-1") as h:
        assert h.parent_span_id == "ext-1"
    assert session.spans[0]["parent_span_id"] == "ext-1"


@pytest.mark.asyncio
async def test_async_span_ok_and_error() -> None:
    exp = _make_exporter([])
    session = TraceSession(exp, "trace-async")
    async with session.async_span("agent.output.finished", "out") as h:
        h.set_attribute("k", "v")
    assert session.spans[0]["status"] == "ok"

    with pytest.raises(RuntimeError):
        async with session.async_span("agent.error", "err"):
            raise RuntimeError("nope")
    assert session.spans[1]["status"] == "error"


def test_session_enqueues_to_exporter() -> None:
    sink: list = []
    exp = _make_exporter(sink)
    session = TraceSession(exp, "trace-enq")
    with session.span("agent.run.finished", "r"):
        pass
    exp.flush()
    assert sink[0]["url"].endswith("/v1/traces/trace-enq/spans")
    assert sink[0]["json"]["spans"][0]["name"] == "r"


def test_session_without_exporter_still_records() -> None:
    session = TraceSession(None, "trace-none")
    with session.span("agent.run.finished", "r"):
        pass
    assert len(session.spans) == 1


# ---------------------------------------------------------------------------
# doc-05 §6 span-type strings
# ---------------------------------------------------------------------------


def test_span_types_are_the_13_frozen_strings() -> None:
    assert SPAN_TYPES == (
        "agent.run.started",
        "agent.step.started",
        "agent.step.finished",
        "agent.model_call.started",
        "agent.model_call.finished",
        "agent.tool_call.started",
        "agent.tool_call.finished",
        "agent.retrieval.started",
        "agent.retrieval.finished",
        "agent.output.started",
        "agent.output.finished",
        "agent.error",
        "agent.run.finished",
    )
    assert len(SPAN_TYPES) == 13


# ---------------------------------------------------------------------------
# ExportingProcessor bridge
# ---------------------------------------------------------------------------


def test_exporting_processor_bridges_tracer_spans() -> None:
    sink: list = []
    exp = _make_exporter(sink)
    proc = ExportingProcessor(exp, producer="anila-agent")
    tracer = Tracer(processors=[proc], trace_id="tr-bridge")

    with tracer.span("tool:search", kind=SpanKind.TOOL, tool="rag"):
        pass
    exp.flush()

    spans = sink[0]["json"]["spans"]
    assert len(spans) == 1
    sp = spans[0]
    assert sp["span_type"] == "agent.tool_call.finished"  # TOOL → tool_call
    assert sp["name"] == "tool:search"
    assert sp["status"] == "ok"
    assert sp["attributes"]["tool"] == "rag"
    assert sp["producer"] == "anila-agent"
    assert sink[0]["url"].endswith("/v1/traces/tr-bridge/spans")


def test_exporting_processor_maps_error_to_agent_error() -> None:
    sink: list = []
    exp = _make_exporter(sink)
    proc = ExportingProcessor(exp)
    tracer = Tracer(processors=[proc], trace_id="tr-e")
    try:
        with tracer.span("llm:call", kind=SpanKind.LLM):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    exp.flush()
    assert sink[0]["json"]["spans"][0]["span_type"] == "agent.error"
