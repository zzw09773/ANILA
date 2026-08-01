# -*- coding: utf-8 -*-
"""Custom HTTP（OpenAI-compatible）agent + ANILA Full Trace（doc-05 §9「Custom HTTP」）。

最小可獨立執行的第三方 agent 範例，示範**非 anila-agent runtime 如何以 adapter
達成 Full Trace**（doc-10 §7 Done：≥1 個非 anila-agent runtime 以 trace adapter
完成 Full Trace 的佐證）。實作 doc-05 §9 Custom HTTP 要求的四個端點：

    GET  /.well-known/anila-agent.json   —— manifest（doc-05 §4）
    GET  /health                          —— 健康檢查
    POST /v1/chat/completions             —— OpenAI-compatible chat（主流程）
    POST /anila/trace-test                —— 自我 trace 測試（doc-06 §8）

主流程收到 CSP dispatch 的 header（P2.1）：
    Authorization: Bearer <JWT>   —— 入向派工身分（JWKS 驗簽）+ 出向 trace 複用
    X-ANILA-Trace-Id              —— 有它才發 trace（缺 → adapter 停用、零外送）
    X-ANILA-Task-Id               —— 歸因
    X-ANILA-Classification-Level  —— 分類等級（隨 run / output span 帶出）

入向完整 JWKS 驗簽請接治理中心 anila_verify.py（本範例聚焦出向 Full Trace）。

跑起來：
    pip install fastapi uvicorn httpx
    ANILA_CSP_BASE=https://anila.ai.ncsist.org.tw \\
        python fastapi_agent_example.py       # → http://0.0.0.0:9100
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request

# 讓「複製整個 examples/trace-adapters/」的人不必先 pip install：把 common/ 上到 path。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "common"))

from anila_trace_adapter import (  # noqa: E402  (path 注入後才 import)
    MODEL_CALL,
    OUTPUT,
    RETRIEVAL,
    RUN,
    STEP,
    TOOL_CALL,
    AnilaTraceAdapter,
)

AGENT_ID = "demo-risk-agent"
BASE_MODEL = "gpt-oss-20b"

app = FastAPI(title="ANILA custom-http agent example")


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    value = authorization.strip()
    if value.lower().startswith("bearer "):
        return value[7:].strip() or None
    return value or None


def _adapter_for_request(request: Request) -> AnilaTraceAdapter:
    """由入向 header + 環境變數組出 adapter。

    - ``dispatch_token``：優先取 ``Authorization: Bearer <派工 JWT>``，
      否則退回 ``ANILA_DISPATCH_TOKEN``（僅本機示範；正式環境應只用當次 header）。
    - 缺 ``X-ANILA-Trace-Id`` → adapter 停用（no-op、零外送）。
    """
    return AnilaTraceAdapter(
        csp_base=os.environ.get("ANILA_CSP_BASE"),
        dispatch_token=(
            _extract_bearer(request.headers.get("Authorization"))
            or os.environ.get("ANILA_DISPATCH_TOKEN")
        ),
        trace_id=request.headers.get("X-ANILA-Trace-Id"),
        task_id=request.headers.get("X-ANILA-Task-Id"),
        classification_level=request.headers.get("X-ANILA-Classification-Level"),
    )


def _run_scripted_agent(adapter: AnilaTraceAdapter, question: str) -> str:
    """跑一段腳本化的 agent：一次 model call + 一次 tool call（內含 retrieval）+
    最終 output。發出 doc-05 §6 的完整 span 集（成功路徑不含 agent.error）。"""
    answer = ""
    with adapter.span(RUN, "chat.completions"):
        with adapter.span(STEP, "answer-step"):
            # 1) 模型思考（scripted）；usage 寫進 span 屬性，離場時隨 .finished 帶出。
            with adapter.span(MODEL_CALL, BASE_MODEL, model=BASE_MODEL) as m:
                time.sleep(0.001)
                m.attributes.update(input_tokens=42, output_tokens=18, total_tokens=60)
            # 2) 工具呼叫（scripted），內部做一次檢索。
            with adapter.span(TOOL_CALL, "search_documents",
                              tool_name="search_documents"):
                with adapter.span(RETRIEVAL, "collection-search",
                                  source_scope="collection", collection_ids=[12],
                                  top_k=4) as r:
                    hits = ["chunk_1", "chunk_2"]
                    r.attributes.update(chunk_ids=hits, document_ids=["doc_1"],
                                        result_count=len(hits))
            # 3) 產出最終答案（帶行內引用 → citations 驗收）。
            answer = f"根據既有文件，{question} 的風險為中等。【來源：doc_1】"
            with adapter.span(OUTPUT, "final", citations=["doc_1"]):
                pass
    adapter.flush()
    return answer


@app.get("/.well-known/anila-agent.json")
def manifest() -> dict[str, Any]:
    """doc-05 §4 manifest contract。"""
    return {
        "agent_id": AGENT_ID,
        "name": "示範風險分析 Agent",
        "version": "1.0.0",
        "runtime_type": "custom_http",
        "api_version": "v1",
        "supported_task_types": ["analyze", "compare"],
        "description_for_router": "分析文件風險並附引用來源。",
        "capabilities": {"retrieval": True, "tools": ["search_documents"],
                         "streaming": False},
        "trace": {"required": True, "protocol": "anila-full-trace-v1",
                  "callback_mode": "post"},
        "classification": {"ceiling": "機密", "default": "營業秘密"},
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "agent_id": AGENT_ID}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> dict[str, Any]:
    """OpenAI-compatible chat；同時發完整 Full Trace span 集回 CSP。"""
    body = await request.json()
    messages = body.get("messages") or []
    question = next(
        (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
        "此案",
    )
    adapter = _adapter_for_request(request)
    # FastAPI async handler：把阻塞式（同步 httpx）scripted 流程丟到 threadpool，
    # 避免卡住 event loop。
    from starlette.concurrency import run_in_threadpool

    answer = await run_in_threadpool(_run_scripted_agent, adapter, question)
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex[:24],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model", AGENT_ID),
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": answer},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 42, "completion_tokens": 18, "total_tokens": 60},
    }


@app.post("/anila/trace-test")
async def trace_test(request: Request) -> dict[str, Any]:
    """doc-06 §8 自我 trace 測試：跑一次 scripted 流程並回報 adapter 是否 active。"""
    adapter = _adapter_for_request(request)
    from starlette.concurrency import run_in_threadpool

    await run_in_threadpool(_run_scripted_agent, adapter, "trace-test")
    # active=False（缺 trace_id）時代表沒發任何 span，是 dev/test 常態。
    return {"ok": True, "trace_active": adapter.active}


if __name__ == "__main__":  # pragma: no cover - 手動啟動用
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "9100")))
