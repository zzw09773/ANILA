# -*- coding: utf-8 -*-
"""examples/trace-adapters 的驗收測試（用 services/csp/.venv 跑）。

覆蓋：
1. adapter 批次切分（>256 → 多次 POST）。
2. span-type 常數逐字對齊 doc-05 §6「Required Span Types」清單。
3. custom-http FastAPI 範例：帶 trace header → 對 mocked CSP POST 正確的
   span 多重集合；不帶 header → 零外送。
4. LangChain 範例：在未安裝 langchain 時仍可乾淨 import。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import httpx
import pytest
import respx

_EXAMPLES = Path(__file__).resolve().parent.parent
_REPO_ROOT = _EXAMPLES.parent.parent  # examples/trace-adapters → examples → repo 根
sys.path.insert(0, str(_EXAMPLES / "common"))
sys.path.insert(0, str(_EXAMPLES / "custom-http"))
sys.path.insert(0, str(_EXAMPLES / "langchain"))

import anila_trace_adapter as ata  # noqa: E402


# ── 1. span-type 常數對齊 doc-05 §6 ─────────────────────────────────────────

def _doc05_required_span_types() -> list[str]:
    """從 doc-05 §6『Required Span Types』的 fenced block 抽出 13 個型別。"""
    doc = (
        _REPO_ROOT
        / "docs" / "anila-redesign-docs"
        / "05-agent-registry-and-runtime-protocol.md"
    ).read_text(encoding="utf-8")
    section = doc.split("### Required Span Types", 1)[1]
    block = re.search(r"```text\n(.*?)```", section, re.DOTALL).group(1)
    return [ln.strip() for ln in block.splitlines() if ln.strip()]


def test_required_span_types_match_doc05():
    assert list(ata.REQUIRED_SPAN_TYPES) == _doc05_required_span_types()


def test_required_span_types_count_is_13():
    assert len(ata.REQUIRED_SPAN_TYPES) == 13
    # 6 對成對 base 的 started/finished + 單發 agent.error = 13。
    for base in ata.PAIRED_BASES:
        assert f"{base}.started" in ata.REQUIRED_SPAN_TYPES
        assert f"{base}.finished" in ata.REQUIRED_SPAN_TYPES
    assert ata.ERROR in ata.REQUIRED_SPAN_TYPES


# ── 2. adapter 批次切分 ─────────────────────────────────────────────────────

@respx.mock
def test_flush_batches_over_256():
    route = respx.post(
        "https://csp.test/v1/traces/trace_x/spans"
    ).mock(return_value=httpx.Response(202))

    adapter = ata.AnilaTraceAdapter(
        "https://csp.test", "csk-key", "trace_x", batch_size=ata.MAX_SPANS_PER_BATCH,
    )
    assert adapter.active
    # 送 300 個成對 span → 600 筆記錄 → 應切成 ceil(600/256)=3 批。
    for i in range(300):
        with adapter.span(ata.TOOL_CALL, f"t{i}"):
            pass
    adapter.flush()

    assert route.call_count == 3
    assert route.calls[0].request.headers["Authorization"] == "Bearer csk-key"
    import json
    sizes = [len(json.loads(c.request.content)["spans"]) for c in route.calls]
    assert sizes == [256, 256, 88]
    # 每筆都帶 frozen 必要欄位 + producer=="agent"。
    sample = json.loads(route.calls[0].request.content)["spans"][0]
    for field in ("span_id", "span_type", "name", "started_at", "status", "producer"):
        assert field in sample
    assert sample["producer"] == "agent"


@respx.mock
def test_inactive_adapter_no_egress():
    route = respx.post(url__regex=r".*").mock(return_value=httpx.Response(202))
    # 缺 trace_id → 停用。
    adapter = ata.AnilaTraceAdapter("https://csp.test", "csk-key", trace_id=None)
    assert not adapter.active
    with adapter.span(ata.RUN, "r"):
        with adapter.span(ata.MODEL_CALL, "m"):
            pass
    adapter.flush()
    assert route.call_count == 0


def test_error_span_on_exception():
    adapter = ata.AnilaTraceAdapter("https://csp.test", "csk-key", "trace_e")
    with pytest.raises(ValueError):
        with adapter.span(ata.RUN, "r"):
            with adapter.span(ata.TOOL_CALL, "boom"):
                raise ValueError("kaboom")
    types = [s["span_type"] for s in adapter._buffer]
    assert "agent.error" in types
    assert "agent.tool_call.finished" in types
    assert [t for t in types if t.endswith("tool_call.finished")]
    # 出錯的 span 以 status="error" 收尾。
    tool_fin = next(s for s in adapter._buffer if s["span_type"].endswith("tool_call.finished"))
    assert tool_fin["status"] == "error"


# ── 3. custom-http FastAPI 範例 ─────────────────────────────────────────────

_EXPECTED_MULTISET = sorted([
    "agent.run.started", "agent.run.finished",
    "agent.step.started", "agent.step.finished",
    "agent.model_call.started", "agent.model_call.finished",
    "agent.tool_call.started", "agent.tool_call.finished",
    "agent.retrieval.started", "agent.retrieval.finished",
    "agent.output.started", "agent.output.finished",
])


@respx.mock
def test_custom_http_full_trace(monkeypatch):
    monkeypatch.setenv("ANILA_CSP_BASE", "https://csp.test")
    import fastapi_agent_example as fae
    from fastapi.testclient import TestClient

    route = respx.post(
        "https://csp.test/v1/traces/trace_123/spans"
    ).mock(return_value=httpx.Response(202))

    client = TestClient(fae.app)
    # 注：X-ANILA-Classification-Level 帶繁中值，屬 CSP dispatch 的 header 編碼
    # 議題（非本 slice）；httpx TestClient 不送非 ASCII header，故此處以 task_id +
    # citations 驗證身分/引用鏈，分類等級的 run/output 帶出改由 adapter 單元測試覆蓋。
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "demo", "messages": [{"role": "user", "content": "本案"}]},
        headers={
            "X-ANILA-Trace-Id": "trace_123",
            "X-ANILA-Task-Id": "task_9",
            "X-CSP-Service-Token": "csk-abc",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert "【來源：doc_1】" in body["choices"][0]["message"]["content"]

    assert route.called
    import json
    spans = []
    for call in route.calls:
        spans.extend(json.loads(call.request.content)["spans"])
    got = sorted(s["span_type"] for s in spans)
    assert got == _EXPECTED_MULTISET
    # 出向 ship 帶 Bearer csk-（雙角色金鑰沿用）。
    assert route.calls[0].request.headers["Authorization"] == "Bearer csk-abc"
    # run span 帶 task_id，output span 帶 citations。
    run_started = next(s for s in spans if s["span_type"] == "agent.run.started")
    assert run_started["attributes"]["task_id"] == "task_9"
    output_started = next(s for s in spans if s["span_type"] == "agent.output.started")
    assert output_started["attributes"]["citations"] == ["doc_1"]


def test_run_span_carries_task_and_classification():
    """分類等級/task_id 隨 run span 帶出（HTTP header 不便帶繁中，改此單元覆蓋）。"""
    adapter = ata.AnilaTraceAdapter(
        "https://csp.test", "csk-k", "trace_c",
        task_id="task_1", classification_level="機密",
    )
    with adapter.span(ata.RUN, "chat"):
        pass
    run_started = next(s for s in adapter._buffer if s["span_type"] == "agent.run.started")
    assert run_started["attributes"]["classification_level"] == "機密"
    assert run_started["attributes"]["task_id"] == "task_1"


@respx.mock
def test_custom_http_no_headers_no_egress(monkeypatch):
    monkeypatch.setenv("ANILA_CSP_BASE", "https://csp.test")
    import fastapi_agent_example as fae
    from fastapi.testclient import TestClient

    route = respx.post(url__regex=r".*").mock(return_value=httpx.Response(202))
    client = TestClient(fae.app)
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "demo", "messages": [{"role": "user", "content": "本案"}]},
    )
    assert resp.status_code == 200
    assert route.call_count == 0


def test_custom_http_manifest_and_health(monkeypatch):
    import fastapi_agent_example as fae
    from fastapi.testclient import TestClient

    client = TestClient(fae.app)
    man = client.get("/.well-known/anila-agent.json").json()
    assert man["runtime_type"] == "custom_http"
    assert man["trace"]["required"] is True
    assert client.get("/health").json()["status"] == "ok"


# ── 4. LangChain 範例可乾淨 import（無 langchain 亦然）───────────────────────

def test_langchain_example_imports_clean():
    import adapter_example as lce

    # 未安裝 langchain 時 handler 為 None，但 module 與 helper 仍可用。
    assert hasattr(lce, "AnilaLangChainTracer")
    assert callable(lce.adapter_from_env)
    adapter = lce.adapter_from_env(csp_base=None, integration_key=None, trace_id=None)
    assert isinstance(adapter, ata.AnilaTraceAdapter)
    assert not adapter.active
    if not lce._LANGCHAIN_AVAILABLE:
        assert lce.AnilaLangChainTracer is None
