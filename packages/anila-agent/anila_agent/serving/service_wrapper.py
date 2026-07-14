"""把 anila-agent 包成 CSP 可派工的 OpenAI-compatible HTTP 服務。

端點（平台契約，形狀逐字保留）：
    GET  /health               health 探針（CSP 以此註冊 agent）
    GET  /v1/models            manifest（model_type=agent ← CSP 註冊標記）
    POST /v1/chat/completions   主入口——CSP Router 把對話轉發到這裡

認證（見 serving.auth）：驗 X-CSP-Service-Token（csk-，fail-closed），驗過才信
X-ANILA-User-*；不驗使用者 JWT（Router 不轉發）。

RAG：走 CSP HTTP search（無直連 DB），以 build_agent(retriever=...) 注入。S-Q1 一把
金鑰：search 重用 agent 自己的 csk-（CSP_SEARCH_TOKEN 未設時退回 CSP_SERVICE_TOKEN）。

跑：``uvicorn anila_agent.serving.service_wrapper:app --host 0.0.0.0 --port 8200``
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from anila_contracts import Classification
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from openai.types.responses import ResponseTextDeltaEvent
from pydantic import BaseModel

from anila_agent.config import load_config
from anila_agent.memory.runtime import MemdirRuntime, auto_memory_enabled
from anila_agent.observability.hooks import AuditHooks
from anila_agent.observability.timeline import (
    TimelineEmitter,
    TimelineRetriever,
    TimelineRunHooks,
)
from anila_agent.retrieval.csp_http import CspHttpRetriever
from anila_agent.runtime.agent_factory import build_agent
from anila_agent.runtime.model import build_model
from anila_agent.runtime.run import run_once, run_streamed
from anila_agent.serving.auth import trusted_user_identity, verify_service_token
from anila_agent.tracing import (
    OUTPUT,
    TraceEmitter,
    TracingRetriever,
    TracingRunHooks,
    extract_citations,
)

logger = logging.getLogger(__name__)

MODEL_NAME = os.environ.get("ANILA_AGENT_NAME", "anila-agent")
COLLECTION_ID = int(os.environ.get("ANILA_COLLECTION_ID", "0") or "0")
CSP_BASE_URL = os.environ.get("CSP_BASE_URL", "https://172.16.120.35")
SSL_VERIFY = os.environ.get("ANILA_SSL_VERIFY", "1").lower() not in ("0", "false", "no", "off")
# Inbound: Router 的 X-CSP-Service-Token 必須等於這個（agent 的 csk-）。
# 空 → 預設拒絕（fail-closed）；prod 必設。
CSP_SERVICE_TOKEN = os.environ.get("CSP_SERVICE_TOKEN", "")
# 明確的本地開發 opt-out：未設 token 時允許 inbound。否則未設 token 的 agent 拒絕每次派工。
ALLOW_NO_SERVICE_TOKEN = os.environ.get("ANILA_ALLOW_NO_SERVICE_TOKEN", "") == "1"
# S-Q1 一把金鑰：search 重用 agent 自己的 csk-；CSP_SEARCH_TOKEN 為可選覆寫。
CSP_SEARCH_TOKEN = os.environ.get("CSP_SEARCH_TOKEN", "") or CSP_SERVICE_TOKEN
# 檢索分數門檻；與 CLI from_env 路徑讀同一個 env（ANILA_CSP_MIN_SCORE），預設 0.25。
try:
    CSP_MIN_SCORE = float(os.environ.get("ANILA_CSP_MIN_SCORE", "0.25") or "0.25")
except ValueError:
    CSP_MIN_SCORE = 0.25

# Full Trace（doc-05 §6）：CSP dispatch 帶 X-ANILA-Trace-Id 時，把 run/step/model/tool/
# retrieval/output/error spans callback POST 回 CSP。端點預設 = CSP_BASE_URL；憑證重用
# agent 自己的 csk-（與 RAG 出向同一把 CSP_SEARCH_TOKEN）。無 trace header → emitter 停用
# → 零行為變化。
TRACE_ENDPOINT = os.environ.get("ANILA_TRACE_ENDPOINT", "") or CSP_BASE_URL
TRACE_ENABLED = os.environ.get("ANILA_TRACE_ENABLED", "1").lower() not in (
    "0", "false", "no", "off"
)
# agent 的分類上限（doc-06 §6「classification level」必備 trace 屬性）；隨 run + output span 帶出。
CLASSIFICATION_LEVEL = os.environ.get("ANILA_CLASSIFICATION_LEVEL", "") or None


def _build_emitter(trace_id: str | None, task_id: str | None) -> TraceEmitter:
    """由入向 trace header 建 emitter（缺 trace_id/endpoint 時自動停用）。"""
    return TraceEmitter.from_context(
        trace_id=trace_id,
        task_id=task_id,
        endpoint=TRACE_ENDPOINT,
        api_key=CSP_SEARCH_TOKEN,  # 與 RAG 出向同一把 Agent Integration Key（csk-）
        agent_id=MODEL_NAME,
        enabled=TRACE_ENABLED,
        verify_ssl=SSL_VERIFY,
        classification_level=CLASSIFICATION_LEVEL,
    )


# 未帶 trace header 的路徑用這顆停用 emitter，讓串流程式碼結構一致又零行為變化。
_NULL_EMITTER = TraceEmitter(trace_id=None, endpoint=None, api_key=None, enabled=False)


def _annotate_output(out: Any, answer: str, usage: dict[str, int] | None) -> None:
    """把 final output span 補上 citations / 分類等級 / 長度等屬性（doc-06 §6）。"""
    cites = extract_citations(answer)
    if cites:
        out.attributes["citations"] = cites
        out.attributes["has_citations"] = True
    if CLASSIFICATION_LEVEL:
        out.attributes["classification_level"] = CLASSIFICATION_LEVEL
    out.attributes["output_chars"] = len(answer)
    if usage:
        out.attributes["total_tokens"] = usage.get("total_tokens", 0)


def _usage_from_run_result(result: Any) -> dict[str, int] | None:
    """從 Runner 結果抽 token usage —— 真值或 ``None``（未知）。

    OpenAI Agents SDK 的 ``RunResult`` / ``RunResultStreaming`` 把 token 統計
    放在 ``context_wrapper.usage``（``input_tokens`` / ``output_tokens`` /
    ``total_tokens``）。抽不到、或值全為 0（真實完成不可能零耗用）→ 回
    ``None``，呼叫端**省略 usage 欄位／不送 usage chunk**。

    不能退回全 0 物件：CSP 消費端（proxy service 的 ``chunk.get("usage")``）
    把任何非空 usage dict 當權威值，全 0 會蓋掉它的本地 token 估算、記帳歸零。
    缺欄位時 CSP 會落回估算，才是對消費端誠實的行為。
    """
    usage_obj = getattr(getattr(result, "context_wrapper", None), "usage", None)
    if usage_obj is None:
        return None
    usage = {
        "prompt_tokens": getattr(usage_obj, "input_tokens", 0) or 0,
        "completion_tokens": getattr(usage_obj, "output_tokens", 0) or 0,
        "total_tokens": getattr(usage_obj, "total_tokens", 0) or 0,
    }
    if not any(usage.values()):
        return None
    return usage


_CONFIG: Any = None
_MODEL: Any = None  # 共用的 OpenAIChatCompletionsModel（避免每請求新建 httpx client）


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _CONFIG, _MODEL
    if not CSP_SERVICE_TOKEN:
        if ALLOW_NO_SERVICE_TOKEN:
            logger.warning(
                "CSP_SERVICE_TOKEN unset + ANILA_ALLOW_NO_SERVICE_TOKEN=1 — inbound "
                "service-token verification DISABLED (local dev only)."
            )
        else:
            logger.error(
                "CSP_SERVICE_TOKEN unset — every dispatch will be REJECTED (401, "
                "fail-closed). Set the agent's csk-, or ANILA_ALLOW_NO_SERVICE_TOKEN=1 "
                "for local dev only."
            )
    if not SSL_VERIFY:
        # 自簽憑證時翻 litellm 的全域 ssl_verify（僅在裝了 [litellm] extra 時）。
        try:
            import litellm

            litellm.ssl_verify = False
        except ImportError:
            pass
    _CONFIG = load_config()
    _MODEL = build_model(_CONFIG.model)  # 一次建立、全程重用底層 httpx client
    if COLLECTION_ID <= 0:
        logger.error(
            "ANILA_COLLECTION_ID 未設或 <=0（got %s）——/v1/chat/completions 的檢索會失敗。"
            "請設定正整數 collection id。",
            COLLECTION_ID,
        )
    yield
    # shutdown：關閉共用 model 的底層 client，避免 fd 殘留。
    client = getattr(_MODEL, "_client", None)
    if client is not None:
        with contextlib.suppress(Exception):
            await client.close()


app = FastAPI(title=MODEL_NAME, lifespan=lifespan)


class ChatMessage(BaseModel):
    role: str
    # OpenAI 標準也允許 content 為 parts 陣列（多模態：text/image_url/…）。
    # 這個 agent 不支援影像輸入——array content 只抽 text parts（見
    # ``_extract_text_content`` / ``_has_only_non_text_parts``），純非文字
    # parts 在 chat_completions 內擋 422。
    content: str | list[dict[str, Any]] | None = None


class StreamOptions(BaseModel):
    include_usage: bool = False


class ChatCompletionRequest(BaseModel):
    model: str = MODEL_NAME
    messages: list[ChatMessage]
    stream: bool = False
    stream_options: StreamOptions | None = None


def _extract_text_content(content: str | list[dict[str, Any]] | None) -> str:
    """把 OpenAI content（純字串或多模態 parts 陣列）攤平成純文字。

    array content 只抽 ``{"type": "text", "text": ...}`` parts、依序串接；
    其他 part 型別（``image_url`` 等）——此 agent 不支援影像輸入——靜默忽略。
    整則訊息只有非文字 parts 的情況由呼叫端 ``_has_only_non_text_parts`` 先擋
    422，不會走到這裡的「攤平成空字串」。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "text":
            continue
        text = part.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _has_only_non_text_parts(content: str | list[dict[str, Any]] | None) -> bool:
    """content 是非空 parts 陣列、但一個 text part 都沒有（例如純圖片）。"""
    if not isinstance(content, list) or not content:
        return False
    return not any(isinstance(part, dict) and part.get("type") == "text" for part in content)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "model": MODEL_NAME}


@app.get("/v1/models")
async def list_models() -> dict[str, Any]:
    # model_type=agent 是 CSP 註冊 manifest 標記；勿改欄位名/形狀。
    # created/owned_by 是 OpenAI /v1/models 規格必含欄位，補上但不動既有欄位。
    return {
        "object": "list",
        "data": [{
            "id": MODEL_NAME,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "anila-agent",
            "model_type": "agent",
        }],
    }


# 背景抽取任務的強參考集合，避免被 GC（asyncio 只保 weakref）。
_ABSORB_TASKS: set[asyncio.Task] = set()


def _schedule_absorb(memory: MemdirRuntime | None, user_text: str, answer_text: str) -> None:
    """turn 結束後在背景抽取記憶寫入該租戶 store——不阻塞回應。

    gated by ANILA_AUTO_MEMORY；memory 為 None（無記憶/缺身分）或無回答則跳過。
    寫入走 memory.absorb_turn（已去敏、fail-closed），失敗只記 log。
    """
    if memory is None or not answer_text or not auto_memory_enabled():
        return

    async def _run() -> None:
        try:
            await memory.absorb_turn(user_text, answer_text)
        except Exception:
            logger.warning("auto-memory absorb failed", exc_info=True)

    task = asyncio.create_task(_run())
    _ABSORB_TASKS.add(task)
    task.add_done_callback(_ABSORB_TASKS.discard)


def _chunk(
    cid: str,
    created: int,
    *,
    delta: dict[str, Any] | None = None,
    finish_reason: str | None = None,
    usage: dict[str, int] | None = None,
) -> str:
    """組一個 OpenAI chat.completion.chunk 的 SSE 區塊（``data: ...\\n\\n``）。"""
    payload: dict[str, Any] = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": MODEL_NAME,
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish_reason}],
    }
    if usage is not None:
        payload["usage"] = usage
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _usage_chunk(cid: str, created: int, usage: dict[str, int]) -> str:
    """組 usage-only 的 OpenAI chat.completion.chunk（``choices: []``）。

    OpenAI 標準行為：只在 client 帶 ``stream_options.include_usage=true`` 時才
    送這塊，位置在 finish chunk 之後、``[DONE]`` 之前；``choices`` 必須是空
    陣列（不是帶 delta 的正常 choice）。
    """
    payload: dict[str, Any] = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": MODEL_NAME,
        "choices": [],
        "usage": usage,
    }
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


async def _sse_stream(
    assembled: Any,
    user_prompt: str,
    hooks: AuditHooks,
    *,
    emitter: TraceEmitter | None = None,
    timeline: TimelineEmitter | None = None,
    include_usage: bool = False,
) -> AsyncIterator[str]:
    """agent 串流輸出 → OpenAI SSE：role → content deltas → finish → [usage] → ``[DONE]``。

    只轉發 ResponseTextDeltaEvent（最終可見答案）；工具呼叫 / reasoning 軌跡不外送。
    usage 在串流跑完才定案（SDK 註明 context_wrapper.usage 末包前為 stale），故收尾才讀。
    Router 端（proxy_service.proxy_stream）以 ``resp.aiter_lines()`` 逐行解析這個格式。

    OpenAI 標準：finish chunk（``choices`` 非空、帶 ``finish_reason``）本身不帶
    usage；usage 只在 ``include_usage`` 為真（client 帶
    ``stream_options.include_usage: true``）時，才在 finish chunk 之後、
    ``[DONE]`` 之前補一個獨立的 usage-only chunk（``choices: []``）。CSP 轉發
    agent 串流時一律強制 ``stream_options.include_usage=True``（見
    ``services/csp/app/services/proxy/service.py`` 的
    ``_proxy_stream_impl``），所以正式部署（經 CSP）路徑行為不變；差別只在
    「繞過 CSP 直打 agent 且未帶 include_usage」的呼叫——現在不會再收到
    usage（符合 OpenAI 規格；先前版本無條件把 usage 掛在 finish chunk 上）。
    ``proxy_service.py`` 的解析只認 ``chunk.get("usage")``，不管 usage 掛在
    finish chunk 還是獨立 chunk，兩種形狀都相容。

    Full Trace 為 out-of-band callback（POST 回 CSP），不動 SSE 格式；``emitter`` 未給或
    停用時完全 no-op。run/model/tool/retrieval/output/error spans 於此收攏 flush。
    """
    em = emitter if emitter is not None else _NULL_EMITTER
    cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    yield _chunk(cid, created, delta={"role": "assistant"})

    parts: list[str] = []  # 累積最終答案，供 turn 結束後自動抽取記憶。
    async with em.run_span(MODEL_NAME, attributes={"stream": True}):
        result = run_streamed(assembled, user_prompt, hooks=hooks)
        try:
            async for event in result.stream_events():
                if timeline is not None:
                    for frame in timeline.drain():
                        yield frame
                if (
                    event.type == "raw_response_event"
                    and isinstance(event.data, ResponseTextDeltaEvent)
                    and event.data.delta
                ):
                    parts.append(event.data.delta)
                    yield _chunk(cid, created, delta={"content": event.data.delta})
            if timeline is not None:
                for frame in timeline.drain():
                    yield frame
        except asyncio.CancelledError:
            # Starlette cancels this generator when CSP closes the downstream
            # socket.  Explicitly cancel the SDK background tasks; merely
            # abandoning ``stream_events`` leaves model/tool work running.
            result.cancel(mode="immediate")
            if timeline is not None:
                timeline.cancel()
            raise
        except Exception as exc:
            # 串流中途失敗：headers 已送出、status 無法再改，記錄 + error span 後乾淨收尾。
            logger.exception("streaming run failed mid-flight")
            em.error(repr(exc))
            if timeline is not None:
                timeline.fail()
                for frame in timeline.drain():
                    yield frame

        usage_payload = _usage_from_run_result(result)
        yield _chunk(cid, created, delta={}, finish_reason="stop")
        if include_usage and usage_payload is not None:
            yield _usage_chunk(cid, created, usage_payload)
        yield "data: [DONE]\n\n"

        answer_text = "".join(parts)
        async with em.span(OUTPUT, MODEL_NAME) as out:
            _annotate_output(out, answer_text, usage_payload)
    await em.flush()

    # 答案已全部串出 → 背景抽取記憶（不延後回應）。
    memory = getattr(getattr(assembled, "context", None), "memory", None)
    _schedule_absorb(memory, user_prompt, answer_text)


@app.post("/v1/chat/completions")
async def chat_completions(
    req: ChatCompletionRequest,
    x_csp_service_token: str | None = Header(default=None, alias="X-CSP-Service-Token"),
    x_anila_user_id: str | None = Header(default=None, alias="X-ANILA-User-Id"),
    x_anila_user_email: str | None = Header(default=None, alias="X-ANILA-User-Email"),
    x_anila_user_groups: str | None = Header(default=None, alias="X-ANILA-User-Groups"),
    x_anila_trace_id: str | None = Header(default=None, alias="X-ANILA-Trace-Id"),
    x_anila_task_id: str | None = Header(default=None, alias="X-ANILA-Task-Id"),
    x_anila_agent_id: str | None = Header(default=None, alias="X-ANILA-Agent-Id"),
    x_anila_session_id: str | None = Header(default=None, alias="X-ANILA-Session-Id"),
    x_anila_run_id: str | None = Header(default=None, alias="X-ANILA-Run-Id"),
    x_anila_classification_level: str | None = Header(
        default=None, alias="X-ANILA-Classification-Level"
    ),
) -> Any:
    allowed = verify_service_token(
        x_csp_service_token, CSP_SERVICE_TOKEN, allow_unset=ALLOW_NO_SERVICE_TOKEN
    )
    if not allowed:
        raise HTTPException(
            status_code=401,
            detail=(
                "missing or invalid X-CSP-Service-Token. The CSP Router dispatch sends "
                "the agent's csk- automatically; for a direct local test send the header "
                "(or set ANILA_ALLOW_NO_SERVICE_TOKEN=1 to disable)."
            ),
        )
    # 驗過 service token 後才信任 X-ANILA-User-* 身分。
    identity = trusted_user_identity(
        allowed, user_id=x_anila_user_id, email=x_anila_user_email, groups=x_anila_user_groups
    )

    last_user_msg = next((m for m in reversed(req.messages) if m.role == "user"), None)
    if last_user_msg is None:
        raise HTTPException(status_code=400, detail="no user message in `messages`")
    if _has_only_non_text_parts(last_user_msg.content):
        raise HTTPException(
            status_code=422,
            detail=(
                "此 agent 不支援影像輸入：訊息 content 只含非文字 part（例如 "
                "image_url），請改用純文字查詢，或在 array content 中附上至少一個 "
                "{\"type\": \"text\", ...} part。"
            ),
        )
    user_prompt = _extract_text_content(last_user_msg.content)
    if not user_prompt:
        raise HTTPException(status_code=400, detail="no user message in `messages`")

    # 設定守衛：缺 collection id 時回明確 503，而非讓裸 ValueError 變不可解讀的 500。
    if COLLECTION_ID <= 0:
        raise HTTPException(
            status_code=503,
            detail=(
                "agent misconfigured: ANILA_COLLECTION_ID must be a positive collection id; "
                "set the env var before dispatching."
            ),
        )

    # Full Trace：CSP dispatch 帶 X-ANILA-Trace-Id 才啟用；否則 emitter 停用、零行為變化。
    emitter = _build_emitter(x_anila_trace_id, x_anila_task_id)

    # Retrieval via CSP HTTP（無 DB）；build_agent(retriever=) escape hatch 直接注入。
    retriever: Any = CspHttpRetriever(
        csp_base_url=CSP_BASE_URL,
        collection_id=COLLECTION_ID,
        api_key=CSP_SEARCH_TOKEN,
        min_score=CSP_MIN_SCORE,
        verify_ssl=SSL_VERIFY,
    )
    try:
        timeline_classification = Classification.from_storage(
            x_anila_classification_level or CLASSIFICATION_LEVEL or "無機密"
        )
    except ValueError:
        # A malformed classification must not downgrade a direct/dev call.
        timeline_classification = Classification.TOP_SECRET
    timeline = TimelineEmitter(
        task_id=x_anila_task_id or "legacy-no-task",
        trace_id=x_anila_trace_id or "legacy-no-trace",
        agent_id=x_anila_agent_id or MODEL_NAME,
        session_id=x_anila_session_id or x_anila_task_id or "legacy-no-session",
        run_id=x_anila_run_id or uuid.uuid4().hex,
        classification=timeline_classification,
    )
    retriever = TimelineRetriever(retriever, timeline)
    if emitter.active:
        # 包一層 → search 前後送 agent.retrieval span（掛在工具 span 下）。
        retriever = TracingRetriever(retriever, emitter)
    # 重用 lifespan 建好的共用 model client；掛 AuditHooks 做 per-user 稽核/計量。
    # 多租戶記憶：以 CSP 轉發的 X-ANILA-User-Id（退回 email）當分艙 key，記憶不跨用戶。
    # 必須 strip 後再判斷——全空白 id 是 truthy，靠 `or` 會繞過 email 並塌縮成共用桶。
    # memory_requires_tenant=True：此為多人共用部署，無可辨識身分一律不給記憶。
    uid = (identity.get("user_id") or "").strip()
    email = (identity.get("email") or "").strip()
    tenant = uid or email or None
    assembled = build_agent(
        _CONFIG,
        retriever=retriever,
        name=MODEL_NAME,
        model=_MODEL,
        memory_tenant=tenant,
        memory_requires_tenant=True,
    )
    # 追蹤啟用時把 AuditHooks 包進 TracingRunHooks（step/model/tool spans + 稽核 fan-out）。
    audit = AuditHooks(user_id=identity.get("user_id"))
    timeline_hooks = TimelineRunHooks(timeline, inner=audit)
    hooks: Any = (
        TracingRunHooks(emitter, inner=timeline_hooks)
        if emitter.active
        else timeline_hooks
    )

    # 串流：CSP Router 對 agent 強制 stream=true 並逐行解析 OpenAI SSE
    # （proxy_service.proxy_stream）。回 text/event-stream 的 chat.completion.chunk。
    if req.stream:
        include_usage = bool(req.stream_options and req.stream_options.include_usage)
        return StreamingResponse(
            _sse_stream(
                assembled,
                user_prompt,
                hooks,
                emitter=emitter,
                timeline=timeline,
                include_usage=include_usage,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 非串流：Router 對 agent 回應走 resp.json()（也容忍 SSE，但我們回 JSON）。
    try:
        async with emitter.run_span(MODEL_NAME, attributes={"stream": False}):
            result = await run_once(assembled, user_prompt, hooks=hooks)
            answer = result.final_output or ""
            usage_payload = _usage_from_run_result(result)
            async with emitter.span(OUTPUT, MODEL_NAME) as out:
                _annotate_output(out, answer, usage_payload)
    finally:
        await emitter.flush()
    # 回應建好後在背景抽取記憶（不延後 JSON 回應）。
    memory = getattr(getattr(assembled, "context", None), "memory", None)
    _schedule_absorb(memory, user_prompt, answer)
    response: dict[str, Any] = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL_NAME,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": answer},
                "finish_reason": "stop",
            }
        ],
    }
    # usage 只在拿得到真值時附上；未知時省略讓 CSP 落回本地估算
    # （見 _usage_from_run_result 的說明）。
    if usage_payload is not None:
        response["usage"] = usage_payload
    return response


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8200")))
