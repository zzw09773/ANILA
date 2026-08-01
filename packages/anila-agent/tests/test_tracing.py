"""Full Trace 發送器（doc-05 §6 / doc-06 §6）：批次 / 容錯 / span 集 / 父子 / 停用 / header。

不連網：以 monkeypatch 換掉 ``httpx.AsyncClient``，攔截 POST 內容。span 集直接讀 emitter
緩衝（flush 前），不需真實模型或網路。
"""

from __future__ import annotations

from collections import Counter
from typing import ClassVar

import httpx
import pytest

from anila_agent.tracing import (
    ERROR,
    MAX_SPANS_PER_BATCH,
    MODEL_CALL,
    OUTPUT,
    RETRIEVAL,
    RUN,
    STEP,
    TOOL_CALL,
    TraceEmitter,
    TracingRetriever,
    TracingRunHooks,
    extract_citations,
)

pytestmark = pytest.mark.unit


# ---- 假 httpx：攔截 POST，不連網 -------------------------------------------------


class _FakeResp:
    def __init__(self, status_code: int = 202) -> None:
        self.status_code = status_code


class _FakeClient:
    """記錄所有 post()；status 可設；raise_on_post 模擬網路失敗。"""

    calls: ClassVar[list[dict]] = []
    status_code = 202
    raise_on_post = False

    def __init__(self, **kw) -> None:
        self.kw = kw

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *a) -> bool:
        return False

    async def post(self, url, headers=None, json=None) -> _FakeResp:
        _FakeClient.calls.append({"url": url, "headers": headers, "json": json})
        if _FakeClient.raise_on_post:
            raise RuntimeError("simulated network failure")
        return _FakeResp(_FakeClient.status_code)


@pytest.fixture
def fake_http(monkeypatch):
    _FakeClient.calls = []
    _FakeClient.status_code = 202
    _FakeClient.raise_on_post = False
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    return _FakeClient


def _active(**kw) -> TraceEmitter:
    base = dict(trace_id="trace_123", endpoint="https://csp.local/", api_key="csk-abc",
                agent_id="risk-agent")
    base.update(kw)
    return TraceEmitter(**base)


# ---- 假 run 物件（模擬 openai-agents 傳給 hook 的形狀）--------------------------


class _FakeModel:
    model = "gpt-oss-20b"


class _FakeAgent:
    name = "risk-agent"
    model = _FakeModel()


class _FakeTool:
    name = "search_documents"


class _FakeUsage:
    input_tokens = 11
    output_tokens = 7
    total_tokens = 18


class _FakeResponse:
    usage = _FakeUsage()


class _Doc:
    def __init__(self, doc_id: str, document_id: str) -> None:
        self.id = doc_id
        self.text = "..."
        self.metadata = {"document_id": document_id}


class _DummyRetriever:
    name = "dummy"
    metadata: ClassVar[dict] = {"collection_id": 12, "backend": "csp-http"}

    async def search(self, query: str, k: int = 5) -> list[_Doc]:
        return [_Doc("chunk_1", "doc_1"), _Doc("chunk_2", "doc_1")]

    async def fetch(self, doc_id: str):
        return None


async def _scripted_run(emitter: TraceEmitter) -> None:
    """一趟：1 model call + 1 tool call + 1 retrieval + final output。"""
    async with emitter.run_span("risk-agent"):
        hooks = TracingRunHooks(emitter)
        agent, tool = _FakeAgent(), _FakeTool()
        await hooks.on_agent_start(None, agent)
        await hooks.on_llm_start(None, agent, None, [])
        await hooks.on_llm_end(None, agent, _FakeResponse())
        await hooks.on_tool_start(None, agent, tool)
        await TracingRetriever(_DummyRetriever(), emitter).search("風險", k=3)
        await hooks.on_tool_end(None, agent, tool, "result-text")
        await hooks.on_agent_end(None, agent, "done")
        async with emitter.span(OUTPUT, "risk-agent") as out:
            out.attributes["citations"] = extract_citations("依規定辦理【來源：doc_1】。")
            out.attributes["classification_level"] = "機密"


# ---- 停用模式：完全不送 ---------------------------------------------------------


async def test_disabled_no_trace_id_emits_nothing(fake_http):
    em = TraceEmitter(trace_id=None, endpoint="https://csp.local", api_key="csk-x")
    assert em.active is False
    await _scripted_run(em)
    await em.flush()
    assert em._buffer == []
    assert fake_http.calls == []


async def test_disabled_no_endpoint_emits_nothing(fake_http):
    em = TraceEmitter(trace_id="trace_1", endpoint=None, api_key="csk-x")
    assert em.active is False
    await _scripted_run(em)
    await em.flush()
    assert fake_http.calls == []


async def test_enable_flag_off_disables(fake_http):
    em = _active(enabled=False)
    assert em.active is False
    await _scripted_run(em)
    await em.flush()
    assert fake_http.calls == []


# ---- header extraction / activation --------------------------------------------


def test_from_context_activation():
    on = TraceEmitter.from_context(trace_id="t1", task_id="task_9", endpoint="https://csp",
                                   api_key="csk-a")
    assert on.active is True
    assert on.task_id == "task_9"
    assert TraceEmitter.from_context(trace_id=None, endpoint="https://csp").active is False
    assert TraceEmitter.from_context(trace_id="t1", endpoint=None).active is False
    assert TraceEmitter.from_context(trace_id="  ", endpoint="https://csp").active is False


# ---- 批次：≤256/batch -----------------------------------------------------------


async def test_batching_splits_at_256(fake_http):
    em = _active()
    for i in range(300):
        em.begin("agent.step", f"s{i}", parent="root")
    await em.flush()
    assert len(fake_http.calls) == 2
    assert len(fake_http.calls[0]["json"]["spans"]) == MAX_SPANS_PER_BATCH
    assert len(fake_http.calls[1]["json"]["spans"]) == 300 - MAX_SPANS_PER_BATCH
    # 端點 + 授權 + producer/形狀
    assert fake_http.calls[0]["url"] == "https://csp.local/v1/traces/trace_123/spans"
    assert fake_http.calls[0]["headers"]["Authorization"] == "Bearer csk-abc"
    span0 = fake_http.calls[0]["json"]["spans"][0]
    assert span0["producer"] == "agent"
    assert span0["span_type"] == "agent.step.started"
    assert set(span0) >= {"span_id", "span_type", "name", "started_at", "status", "producer"}


async def test_flush_drains_buffer(fake_http):
    em = _active()
    em.begin("agent.step", "s", parent="root")
    await em.flush()
    assert em._buffer == []
    await em.flush()  # 二次 flush 無新 POST
    assert len(fake_http.calls) == 1


# ---- 容錯：ship 失敗不得拋 ------------------------------------------------------


async def test_failure_tolerance_post_raises(fake_http):
    fake_http.raise_on_post = True
    em = _active()
    em.begin("agent.step", "s", parent="root")
    await em.flush()  # 不得拋例外
    assert em._buffer == []  # 已 drop（不無限累積）


async def test_failure_tolerance_http_error_status(fake_http):
    fake_http.status_code = 500
    em = _active()
    em.begin("agent.step", "s", parent="root")
    await em.flush()  # 5xx 也只 log、不拋
    assert len(fake_http.calls) == 1


# ---- span 集：scripted run 產出必備 span 型別 -----------------------------------


async def test_scripted_run_span_type_multiset(fake_http):
    em = _active()
    await _scripted_run(em)
    types = Counter(s["span_type"] for s in em._buffer)
    assert types == Counter({
        f"{RUN}.started": 1, f"{RUN}.finished": 1,
        f"{STEP}.started": 1, f"{STEP}.finished": 1,
        f"{MODEL_CALL}.started": 1, f"{MODEL_CALL}.finished": 1,
        f"{TOOL_CALL}.started": 1, f"{TOOL_CALL}.finished": 1,
        f"{RETRIEVAL}.started": 1, f"{RETRIEVAL}.finished": 1,
        f"{OUTPUT}.started": 1, f"{OUTPUT}.finished": 1,
    })


async def test_final_output_carries_citations_and_classification(fake_http):
    em = _active()
    await _scripted_run(em)
    out_fin = next(s for s in em._buffer if s["span_type"] == f"{OUTPUT}.finished")
    assert out_fin["attributes"]["citations"] == ["doc_1"]
    assert out_fin["attributes"]["classification_level"] == "機密"


async def test_retrieval_span_has_chunk_and_collection(fake_http):
    em = _active()
    await _scripted_run(em)
    ret_fin = next(s for s in em._buffer if s["span_type"] == f"{RETRIEVAL}.finished")
    assert ret_fin["attributes"]["collection_ids"] == [12]
    assert ret_fin["attributes"]["chunk_ids"] == ["chunk_1", "chunk_2"]
    assert ret_fin["attributes"]["top_k"] == 3


async def test_model_call_span_carries_usage(fake_http):
    em = _active()
    await _scripted_run(em)
    m = next(s for s in em._buffer if s["span_type"] == f"{MODEL_CALL}.finished")
    assert m["attributes"]["total_tokens"] == 18
    assert m["name"] == "gpt-oss-20b"


# ---- 父子：所有 span 皆可回溯到 run root ---------------------------------------


async def test_parentage_all_children_reach_run_root(fake_http):
    em = _active()
    await _scripted_run(em)
    parent_of: dict[str, str | None] = {}
    for s in em._buffer:
        parent_of[s["span_id"]] = s.get("parent_span_id")
    roots = [sid for sid, p in parent_of.items() if p is None]
    assert len(roots) == 1  # 唯一 root
    root = roots[0]
    # root 必為 agent.run
    run_started = next(s for s in em._buffer if s["span_type"] == f"{RUN}.started")
    assert run_started["span_id"] == root
    # 每個非 root span 沿 parent 鏈都能走到 root（不成環、不孤兒）
    for sid in parent_of:
        if sid == root:
            continue
        seen: set[str] = set()
        cur = parent_of[sid]
        while cur is not None and cur not in seen:
            seen.add(cur)
            cur = parent_of.get(cur)
        assert root in seen, f"{sid} 無法回溯 run root"


async def test_retrieval_nested_under_tool(fake_http):
    em = _active()
    await _scripted_run(em)
    tool = next(s for s in em._buffer if s["span_type"] == f"{TOOL_CALL}.started")
    ret = next(s for s in em._buffer if s["span_type"] == f"{RETRIEVAL}.started")
    assert ret["parent_span_id"] == tool["span_id"]


# ---- error span：例外時補發 agent.error ----------------------------------------


async def test_error_span_on_exception(fake_http):
    em = _active()
    with pytest.raises(ValueError):
        async with em.run_span("risk-agent"):
            raise ValueError("boom")
    types = [s["span_type"] for s in em._buffer]
    assert ERROR in types
    run_fin = next(s for s in em._buffer if s["span_type"] == f"{RUN}.finished")
    assert run_fin["status"] == "error"


# ---- citations helper ----------------------------------------------------------


def test_extract_citations_dedupe_and_order():
    assert extract_citations("A【來源：doc_1】B【來源: doc_2, doc_1】") == ["doc_1", "doc_2"]
    assert extract_citations("無引用") == []
    assert extract_citations("") == []


# ---- TracingRetriever 符合 Retriever Protocol（build_agent 可接受）--------------


def test_tracing_retriever_matches_protocol():
    from anila_agent.retrieval.base import Retriever

    wrapped = TracingRetriever(_DummyRetriever(), _active())
    assert isinstance(wrapped, Retriever)


# ---- service_wrapper 整合：trace header → emitter → 回打 CSP -----------------


def test_service_wrapper_ships_spans_when_trace_header_present(fake_http, monkeypatch):
    fastapi = pytest.importorskip("fastapi")  # noqa: F841
    from fastapi.testclient import TestClient

    from anila_agent.serving import service_wrapper

    class _Result:
        final_output = "答案【來源：doc_1】"

    async def _fake_run_once(*a, **k):
        return _Result()

    async def _fake_claims(_authorization=None, **_kwargs):
        return {"user_id": 1, "department": None, "agent_id": 9}

    monkeypatch.setattr(service_wrapper, "COLLECTION_ID", 12)
    monkeypatch.setattr(
        service_wrapper, "verify_dispatch_authorization", _fake_claims
    )
    monkeypatch.setattr(service_wrapper, "CSP_SEARCH_TOKEN", "csk-test")
    monkeypatch.setattr(service_wrapper, "TRACE_ENDPOINT", "https://csp.local")
    monkeypatch.setattr(service_wrapper, "TRACE_ENABLED", True)
    monkeypatch.setattr(service_wrapper, "build_model", lambda *a, **k: object())
    monkeypatch.setattr(service_wrapper, "build_agent", lambda *a, **k: object())
    monkeypatch.setattr(service_wrapper, "run_once", _fake_run_once)

    with TestClient(service_wrapper.app) as client:
        resp = client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": "Bearer test",
                "X-ANILA-Trace-Id": "trace_xyz",
                "X-ANILA-Task-Id": "task_1",
            },
            json={"model": "anila-agent", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 200
    # emitter 應把 run + output span POST 回 CSP 的 /v1/traces/<id>/spans
    assert fake_http.calls, "沒有 trace span 被送出"
    assert fake_http.calls[0]["url"] == "https://csp.local/v1/traces/trace_xyz/spans"
    span_types = {s["span_type"] for c in fake_http.calls for s in c["json"]["spans"]}
    assert {"agent.run.started", "agent.run.finished",
            "agent.output.started", "agent.output.finished"} <= span_types


def test_service_wrapper_no_spans_without_trace_header(fake_http, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from anila_agent.serving import service_wrapper

    class _Result:
        final_output = "答案"

    async def _fake_run_once(*a, **k):
        return _Result()

    async def _fake_claims(_authorization=None, **_kwargs):
        return {"user_id": 1, "department": None, "agent_id": 9}

    monkeypatch.setattr(service_wrapper, "COLLECTION_ID", 12)
    monkeypatch.setattr(
        service_wrapper, "verify_dispatch_authorization", _fake_claims
    )
    monkeypatch.setattr(service_wrapper, "CSP_SEARCH_TOKEN", "csk-test")
    monkeypatch.setattr(service_wrapper, "build_model", lambda *a, **k: object())
    monkeypatch.setattr(service_wrapper, "build_agent", lambda *a, **k: object())
    monkeypatch.setattr(service_wrapper, "run_once", _fake_run_once)

    with TestClient(service_wrapper.app) as client:
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test"},
            json={"model": "anila-agent", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 200
    assert fake_http.calls == []  # 無 trace header → 零外送
