"""P2-9 queryTracking chainId / depth / query_id 多層追蹤測試。

涵蓋:

- :class:`Trace` 新增欄位(``chain_id`` / ``parent_trace_id`` / ``depth``)
  的 default 與 to_dict 序列化(維持 P0-9 既有 callsite 的 backward compat)。
- :class:`Span` 新增欄位(``chain_id`` / ``query_id``)的 default 與序列化。
- 巢狀 ``start_trace`` 自動繼承 chain_id(parent → child)且 depth + 1。
- 顯式指定 chain_id / parent_trace_id / depth 時優先採用。
- root trace 預設以自己的 trace_id 當 chain 起點。
- span 從當前 trace 繼承 chain_id。
- ``SessionContext.start_query`` / ``end_query`` 自動 set / clear
  ``current_query_id``,且若有傳 ``tracer`` 也會同步到 tracer thread-local,
  使 span 寫入 query_id。
- 模擬 sub-agent fork dispatch(P0-8/P1-1)時 depth + 1 行為。
- :meth:`Tracer.find_traces_by_chain` 找出同 chain 的所有 trace。
- :class:`JsonlTracingProcessor` 寫的 JSONL 包含 ``chain_id`` /
  ``parent_trace_id`` / ``depth`` / ``query_id`` 欄位。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from anila_agent.core.hook_context import SessionContext
from anila_agent.core.prompt_cache import (
    build_fork_directive_message,
    is_in_fork_child,
)
from anila_agent.tracing import (
    JsonlTracingProcessor,
    Span,
    Trace,
    Tracer,
)

# ---------------------------------------------------------------------------
# Trace / Span 新欄位 default 與 to_dict
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_trace_new_fields_default_to_none_and_zero() -> None:
    """``Trace`` 新欄位預設:chain_id / parent_trace_id 為 None,depth 為 0。"""
    trace = Trace(name="x")
    assert trace.chain_id is None
    assert trace.parent_trace_id is None
    assert trace.depth == 0


@pytest.mark.unit
def test_trace_to_dict_includes_chain_fields() -> None:
    """``Trace.to_dict`` 應序列化 chain_id / parent_trace_id / depth 欄位。"""
    trace = Trace(name="x", chain_id="c1", parent_trace_id="t0", depth=2)
    payload = trace.to_dict()
    assert payload["chain_id"] == "c1"
    assert payload["parent_trace_id"] == "t0"
    assert payload["depth"] == 2


@pytest.mark.unit
def test_span_new_fields_default_to_none() -> None:
    """``Span`` 新欄位預設:chain_id / query_id 皆 None。"""
    span = Span(name="op")
    assert span.chain_id is None
    assert span.query_id is None


@pytest.mark.unit
def test_span_to_dict_includes_chain_and_query() -> None:
    """``Span.to_dict`` 應序列化 chain_id / query_id 欄位。"""
    span = Span(name="op", chain_id="c1", query_id="q1")
    payload = span.to_dict()
    assert payload["chain_id"] == "c1"
    assert payload["query_id"] == "q1"


# ---------------------------------------------------------------------------
# Tracer.start_trace 自動繼承 chain
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_root_trace_uses_self_trace_id_as_chain() -> None:
    """無 outer trace + 未顯式指定 chain_id 時,root trace 以自己 trace_id 起 chain。"""
    tracer = Tracer()
    with tracer.start_trace("root") as root:
        assert root.chain_id == root.trace_id
        assert root.parent_trace_id is None
        assert root.depth == 0


@pytest.mark.unit
def test_nested_trace_inherits_chain_and_increments_depth() -> None:
    """巢狀 start_trace 自動 inherit chain_id、設 parent_trace_id、depth + 1。"""
    tracer = Tracer()
    with tracer.start_trace("root") as root, tracer.start_trace("sub") as sub:
        assert sub.chain_id == root.chain_id
        assert sub.parent_trace_id == root.trace_id
        assert sub.depth == 1
        with tracer.start_trace("subsub") as subsub:
            assert subsub.chain_id == root.chain_id
            assert subsub.parent_trace_id == sub.trace_id
            assert subsub.depth == 2


@pytest.mark.unit
def test_explicit_chain_overrides_auto_inheritance() -> None:
    """顯式指定 chain_id / parent_trace_id / depth 時優先採用。"""
    tracer = Tracer()
    with tracer.start_trace("root"), tracer.start_trace(
        "sub",
        chain_id="custom-chain",
        parent_trace_id="custom-parent",
        depth=42,
    ) as sub:
        assert sub.chain_id == "custom-chain"
        assert sub.parent_trace_id == "custom-parent"
        assert sub.depth == 42


@pytest.mark.unit
def test_span_inherits_chain_id_from_active_trace() -> None:
    """span 應從當前 trace 繼承 chain_id。"""
    tracer = Tracer()
    with tracer.start_trace("root") as root, tracer.start_span("op") as span:
        assert span.chain_id == root.chain_id


# ---------------------------------------------------------------------------
# QueryContext: SessionContext.start_query / end_query
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_session_start_query_sets_and_clears_query_id() -> None:
    """``start_query`` 進入時 set current_query_id;離開時 clear。"""
    session = SessionContext(session_id="s1")
    assert session.current_query_id is None
    assert session.query_started_at is None
    with session.start_query() as qid:
        assert session.current_query_id == qid
        assert session.query_started_at is not None
    assert session.current_query_id is None
    assert session.query_started_at is None


@pytest.mark.unit
def test_session_start_query_with_explicit_id() -> None:
    """顯式 query_id 應被採用。"""
    session = SessionContext(session_id="s1")
    with session.start_query(query_id="custom-q") as qid:
        assert qid == "custom-q"
        assert session.current_query_id == "custom-q"


@pytest.mark.unit
def test_session_start_query_with_tracer_syncs_thread_local() -> None:
    """傳 tracer 時,query_id 應同步到 tracer thread-local,span 寫入 query_id。"""
    session = SessionContext(session_id="s1")
    tracer = Tracer()
    with session.start_query(tracer=tracer) as qid:
        assert tracer.current_query_id == qid
        with tracer.start_trace("root"), tracer.start_span("op") as span:
            assert span.query_id == qid
    # 離開後 tracer thread-local 也清掉
    assert tracer.current_query_id is None


@pytest.mark.unit
def test_session_start_query_rejects_nested_query() -> None:
    """已有 active query 時再開新 query 應 raise RuntimeError。"""
    session = SessionContext(session_id="s1")
    with (
        session.start_query(),
        pytest.raises(RuntimeError, match="nested in active query"),
        session.start_query(),
    ):
        pass


@pytest.mark.unit
def test_session_end_query_clears_state() -> None:
    """手動 end_query 應清掉 current_query_id 與 query_started_at。"""
    session = SessionContext(session_id="s1")
    tracer = Tracer()
    # 模擬已被 set 過的狀態(不走 context manager 也能清)
    session.current_query_id = "q1"
    session.query_started_at = 123.0
    tracer.set_current_query_id("q1")
    session.end_query(tracer=tracer)
    assert session.current_query_id is None
    assert session.query_started_at is None
    assert tracer.current_query_id is None


# ---------------------------------------------------------------------------
# 模擬 sub-agent fork dispatch:depth + 1
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_fork_dispatch_increments_depth() -> None:
    """模擬 parent agent → sub-agent fork dispatch,sub-agent 內開 trace 應 depth+1。

    這裡用 P1-1 prompt_cache 的 fork directive helper 模擬「sub-agent 收到 fork prompt」,
    並驗證若上層仍有 active trace,sub-agent trace 會自動 depth + 1。
    """
    tracer = Tracer()
    # parent agent 開 root trace
    with tracer.start_trace("parent.agent.run") as parent:
        assert parent.depth == 0
        # parent 派 sub-agent — 用 fork directive 模擬訊息流向
        fork_text = build_fork_directive_message("Do the X task")
        fork_msg = {"role": "user", "content": fork_text}
        assert is_in_fork_child([fork_msg]) is True

        # sub-agent runner 內部會 start_trace(實務上由 SubAgentRunner 觸發)
        with tracer.start_trace("sub.agent.run") as sub:
            assert sub.depth == 1
            assert sub.parent_trace_id == parent.trace_id
            assert sub.chain_id == parent.chain_id


@pytest.mark.unit
def test_fork_dispatch_three_levels() -> None:
    """三層 dispatch(root → sub → sub-sub)應正確記錄 depth 0/1/2 + 同 chain_id。"""
    tracer = Tracer()
    chain_id_seen = None
    with tracer.start_trace("a") as a:
        chain_id_seen = a.chain_id
        with tracer.start_trace("b") as b, tracer.start_trace("c") as c:
            assert a.depth == 0
            assert b.depth == 1
            assert c.depth == 2
            assert a.chain_id == b.chain_id == c.chain_id == chain_id_seen
            assert b.parent_trace_id == a.trace_id
            assert c.parent_trace_id == b.trace_id


# ---------------------------------------------------------------------------
# find_traces_by_chain
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_find_traces_by_chain_returns_all_traces_in_chain() -> None:
    """``find_traces_by_chain`` 應撈出同 chain_id 的所有 trace。"""
    tracer = Tracer()
    with tracer.start_trace("root") as root:
        chain_id = root.chain_id
        with tracer.start_trace("sub"):
            pass
        with tracer.start_trace("sub2"):
            pass

    assert chain_id is not None
    traces = tracer.find_traces_by_chain(chain_id)
    # root + sub + sub2
    assert len(traces) == 3
    names = [t.name for t in traces]
    assert names == ["root", "sub", "sub2"]
    # 所有 trace 共用 chain_id
    assert all(t.chain_id == chain_id for t in traces)


@pytest.mark.unit
def test_find_traces_by_chain_unknown_chain_returns_empty() -> None:
    """unknown chain_id 應回空 list 而非 raise。"""
    tracer = Tracer()
    assert tracer.find_traces_by_chain("nonexistent") == []


@pytest.mark.unit
def test_find_traces_by_chain_returns_copy() -> None:
    """回傳應為 copy,外部 mutate 不影響內部 index。"""
    tracer = Tracer()
    with tracer.start_trace("root") as root:
        pass
    traces = tracer.find_traces_by_chain(root.chain_id or "")
    traces.append(Trace(name="external"))
    # 第二次查不會看到外部加進來的
    again = tracer.find_traces_by_chain(root.chain_id or "")
    assert len(again) == 1
    assert again[0].name == "root"


# ---------------------------------------------------------------------------
# JsonlTracingProcessor 寫新欄位
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_jsonl_processor_writes_chain_id_and_query_id(tmp_path: Path) -> None:
    """JsonlTracingProcessor 應寫出 chain_id / parent_trace_id / depth / query_id。"""
    jsonl_path = tmp_path / "trace.jsonl"
    proc = JsonlTracingProcessor(jsonl_path)
    tracer = Tracer()
    tracer.register_processor(proc)
    session = SessionContext(session_id="s1")

    with (
        session.start_query(tracer=tracer) as qid,
        tracer.start_trace("root") as root,
        tracer.start_trace("sub") as sub,
        tracer.start_span("op"),
    ):
        pass

    proc.close()

    lines = jsonl_path.read_text(encoding="utf-8").strip().splitlines()
    events = [json.loads(line) for line in lines]

    trace_starts = [e for e in events if e["event"] == "trace.start"]
    assert len(trace_starts) == 2
    root_event, sub_event = trace_starts
    assert root_event["name"] == "root"
    assert root_event["chain_id"] == root.chain_id
    assert root_event["parent_trace_id"] is None
    assert root_event["depth"] == 0
    assert sub_event["name"] == "sub"
    assert sub_event["chain_id"] == root.chain_id
    assert sub_event["parent_trace_id"] == root.trace_id
    assert sub_event["depth"] == 1

    span_starts = [e for e in events if e["event"] == "span.start"]
    assert len(span_starts) == 1
    assert span_starts[0]["chain_id"] == sub.chain_id
    assert span_starts[0]["query_id"] == qid


@pytest.mark.unit
def test_jsonl_processor_writes_none_when_no_query(tmp_path: Path) -> None:
    """無 active query 時 span 的 query_id 應為 None(但欄位仍存在)。"""
    jsonl_path = tmp_path / "trace.jsonl"
    proc = JsonlTracingProcessor(jsonl_path)
    tracer = Tracer()
    tracer.register_processor(proc)

    with tracer.start_trace("root"), tracer.start_span("op"):
        pass

    proc.close()

    lines = jsonl_path.read_text(encoding="utf-8").strip().splitlines()
    events = [json.loads(line) for line in lines]
    span_starts = [e for e in events if e["event"] == "span.start"]
    assert len(span_starts) == 1
    # 欄位存在但為 None
    assert "query_id" in span_starts[0]
    assert span_starts[0]["query_id"] is None
