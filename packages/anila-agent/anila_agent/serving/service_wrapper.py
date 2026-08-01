"""把 anila-agent 包成 CSP 可派工的 OpenAI-compatible HTTP 服務。

端點（平台契約，形狀逐字保留）：
    GET  /health               health 探針（CSP 以此註冊 agent）
    GET  /v1/models            manifest（model_type=agent ← CSP 註冊標記）
    POST /v1/chat/completions   主入口——CSP Router 把對話轉發到這裡

認證（P2.1，見 serving.auth）：驗 ``Authorization: Bearer <dispatch JWT>``
（RS256、JWKS），身分取自已驗證 claims（user_id / department / agent_id）。
相關聯標頭 ``X-ANILA-Task-Id`` / ``X-ANILA-Trace-Id`` 仍可讀。

RAG / Full Trace 出向（P2.1 W5）：把**同一條**入向 dispatch JWT 以
``Authorization: Bearer …`` 帶回 CSP（request-scoped ``dispatch_bearer_scope``，
非 process-global、非靜態 csk-）。

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

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from openai.types.responses import ResponseTextDeltaEvent
from pydantic import BaseModel

from anila_agent.config import load_config
from anila_agent.dispatch_token import (
    MissingDispatchTokenError,
    dispatch_bearer_scope,
)
from anila_agent.memory.runtime import MemdirRuntime, auto_memory_enabled
from anila_agent.observability.hooks import AuditHooks
from anila_agent.retrieval.csp_http import CspHttpRetriever
from anila_agent.runtime.agent_factory import build_agent
from anila_agent.runtime.model import build_model
from anila_agent.runtime.run import run_once, run_streamed
from anila_agent.serving.auth import (
    DispatchAuthError,
    configure_jwks_client,
    extract_bearer,
    get_jwks_client,
    identity_from_claims,
    verify_dispatch_authorization,
)
from anila_core.api.middleware.dispatch_jwt import DispatchTokenError
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
# CSPKI CA bundle for JWKS HTTPS fetch. Never SSL_CERT_FILE.
ANILA_CA_FILE = (os.environ.get("ANILA_CA_FILE") or "").strip() or None
# 檢索分數門檻；與 CLI from_env 路徑讀同一個 env（ANILA_CSP_MIN_SCORE），預設 0.25。
try:
    CSP_MIN_SCORE = float(os.environ.get("ANILA_CSP_MIN_SCORE", "0.25") or "0.25")
except ValueError:
    CSP_MIN_SCORE = 0.25

# Full Trace（doc-05 §6）：CSP dispatch 帶 X-ANILA-Trace-Id 時，把 run/step/model/tool/
# retrieval/output/error spans callback POST 回端點。端點預設 = CSP_BASE_URL；
# 當端點 origin == CSP 時憑證為當次 dispatch JWT（見 dispatch_bearer_scope），
# 否則拒貼 JWT（須另備靜態憑證）。無 trace header → emitter 停用 → 零行為變化。
TRACE_ENDPOINT = os.environ.get("ANILA_TRACE_ENDPOINT", "") or CSP_BASE_URL
TRACE_ENABLED = os.environ.get("ANILA_TRACE_ENABLED", "1").lower() not in (
    "0", "false", "no", "off"
)
# agent 的分類上限（doc-06 §6「classification level」必備 trace 屬性）；隨 run + output span 帶出。
CLASSIFICATION_LEVEL = os.environ.get("ANILA_CLASSIFICATION_LEVEL", "") or None


def _build_emitter(trace_id: str | None, task_id: str | None) -> TraceEmitter:
    """由入向 trace header 建 emitter（缺 trace_id/endpoint 時自動停用）。

    ``api_key`` 刻意不填：flush 時從 request-scoped dispatch JWT 取值。
    ``csp_base_url`` 是 JWT 可貼上的信任錨——``TRACE_ENDPOINT`` 若指向非 CSP
    collector，resolve 會拒絕附上使用者 JWT。
    """
    return TraceEmitter.from_context(
        trace_id=trace_id,
        task_id=task_id,
        endpoint=TRACE_ENDPOINT,
        api_key=None,
        csp_base_url=CSP_BASE_URL,
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

_CONFIG: Any = None
_MODEL: Any = None  # 共用的 OpenAIChatCompletionsModel（避免每請求新建 httpx client）


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _CONFIG, _MODEL
    # Bind JWKS client to this process's platform URL + CA (fail-closed if blank).
    jwks = configure_jwks_client(csp_base_url=CSP_BASE_URL, ca_file=ANILA_CA_FILE)
    if not jwks.configured:
        logger.error(
            "CSP_BASE_URL unset/blank — inbound dispatch JWT verification cannot "
            "reach JWKS; every /v1/chat/completions will be REJECTED (fail-closed)."
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
    content: str


class ChatCompletionRequest(BaseModel):
    model: str = MODEL_NAME
    messages: list[ChatMessage]
    stream: bool = False


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "model": MODEL_NAME}


@app.get("/v1/models")
async def list_models() -> dict[str, Any]:
    # model_type=agent 是 CSP 註冊 manifest 標記；勿改欄位名/形狀。
    return {
        "object": "list",
        "data": [{"id": MODEL_NAME, "object": "model", "model_type": "agent"}],
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


async def _sse_stream(
    assembled: Any,
    user_prompt: str,
    hooks: AuditHooks,
    *,
    emitter: TraceEmitter | None = None,
    dispatch_bearer: str | None = None,
) -> AsyncIterator[str]:
    """agent 串流輸出 → OpenAI SSE：role → content deltas → finish+usage → ``[DONE]``。

    只轉發 ResponseTextDeltaEvent（最終可見答案）；工具呼叫 / reasoning 軌跡不外送。
    usage 在串流跑完才定案（SDK 註明 context_wrapper.usage 末包前為 stale），故收尾才讀。
    Router 端（proxy_service.proxy_stream）以 ``resp.aiter_lines()`` 逐行解析這個格式。

    Full Trace 為 out-of-band callback（POST 回 CSP），不動 SSE 格式；``emitter`` 未給或
    停用時完全 no-op。run/model/tool/retrieval/output/error spans 於此收攏 flush。

    ``dispatch_bearer`` 綁在**獨立 Task**（自有 Context 複本）裡——不可跨 async
    generator 的 yield 邊界 set/reset ContextVar：SSE 斷線時 ASGI 會在別的
    Context 裡 aclose generator，``Token.reset`` 會炸 ``ValueError`` 且 scope
    殘留。Producer Task 的 set/reset 始終在同一 Context。
    """
    # Unbounded, deliberately. A bounded queue would restore the backpressure
    # the producer Task removed, but maxsize=1 deadlocks the streaming tests:
    # the producer blocks in put() and the cancellation path cannot drain it.
    # Recorded as an open item - a correct bound needs the consumer to signal,
    # not just a smaller queue. Cost meanwhile: a stalled client no longer
    # throttles the model.
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    error_box: list[BaseException] = []

    async def _produce() -> None:
        try:
            scope_cm = (
                dispatch_bearer_scope(dispatch_bearer)
                if dispatch_bearer
                else contextlib.nullcontext(None)
            )
            with scope_cm:
                async for chunk in _sse_stream_body(
                    assembled, user_prompt, hooks, emitter=emitter
                ):
                    await queue.put(chunk)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 — bridge to consumer
            error_box.append(exc)
        finally:
            await queue.put(None)

    task = asyncio.create_task(_produce())
    try:
        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            yield chunk
        if error_box:
            raise error_box[0]
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        elif not task.cancelled():
            # Drain done-task exception so it isn't "never retrieved".
            with contextlib.suppress(BaseException):
                task.result()


async def _sse_stream_body(
    assembled: Any,
    user_prompt: str,
    hooks: AuditHooks,
    *,
    emitter: TraceEmitter | None = None,
) -> AsyncIterator[str]:
    em = emitter if emitter is not None else _NULL_EMITTER
    cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    yield _chunk(cid, created, delta={"role": "assistant"})

    parts: list[str] = []  # 累積最終答案，供 turn 結束後自動抽取記憶。
    async with em.run_span(MODEL_NAME, attributes={"stream": True}):
        result = run_streamed(assembled, user_prompt, hooks=hooks)
        try:
            async for event in result.stream_events():
                if (
                    event.type == "raw_response_event"
                    and isinstance(event.data, ResponseTextDeltaEvent)
                    and event.data.delta
                ):
                    parts.append(event.data.delta)
                    yield _chunk(cid, created, delta={"content": event.data.delta})
        except Exception as exc:
            # 串流中途失敗：headers 已送出、status 無法再改，記錄 + error span 後乾淨收尾。
            logger.exception("streaming run failed mid-flight")
            em.error(repr(exc))

        usage_obj = getattr(getattr(result, "context_wrapper", None), "usage", None)
        usage_payload: dict[str, int] | None = None
        if usage_obj is not None:
            usage_payload = {
                "prompt_tokens": getattr(usage_obj, "input_tokens", 0) or 0,
                "completion_tokens": getattr(usage_obj, "output_tokens", 0) or 0,
                "total_tokens": getattr(usage_obj, "total_tokens", 0) or 0,
            }
        yield _chunk(cid, created, delta={}, finish_reason="stop", usage=usage_payload)
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
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_anila_trace_id: str | None = Header(default=None, alias="X-ANILA-Trace-Id"),
    x_anila_task_id: str | None = Header(default=None, alias="X-ANILA-Task-Id"),
) -> Any:
    try:
        claims = await verify_dispatch_authorization(
            authorization, jwks_client=get_jwks_client()
        )
        # Same raw JWT CSP just issued — presented on in-task outbound callbacks.
        # Folded into this try: extract_bearer raises DispatchTokenError (not
        # DispatchAuthError) which must stay 401, never 500.
        dispatch_bearer = extract_bearer(authorization)
    except (DispatchAuthError, DispatchTokenError, MissingDispatchTokenError) as exc:
        raise HTTPException(
            status_code=401,
            detail=(
                "missing or invalid dispatch token. CSP Router sends "
                "Authorization: Bearer <short-lived JWT>; agent verifies it "
                f"against platform JWKS ({exc})."
            ),
        ) from exc
    identity = identity_from_claims(claims)

    user_prompt = next((m.content for m in reversed(req.messages) if m.role == "user"), "")
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

    # Retrieval via CSP HTTP（無 DB）；auth = request-scoped dispatch JWT（無 csk-）。
    retriever: Any = CspHttpRetriever(
        csp_base_url=CSP_BASE_URL,
        collection_id=COLLECTION_ID,
        api_key=None,
        trusted_csp_base_url=CSP_BASE_URL,
        min_score=CSP_MIN_SCORE,
        verify_ssl=SSL_VERIFY,
    )
    if emitter.active:
        # 包一層 → search 前後送 agent.retrieval span（掛在工具 span 下）。
        retriever = TracingRetriever(retriever, emitter)
    # 重用 lifespan 建好的共用 model client；掛 AuditHooks 做 per-user 稽核/計量。
    # 多租戶記憶：以已驗證 JWT claim 的 user_id 當分艙 key，記憶不跨用戶。
    # memory_requires_tenant=True：此為多人共用部署，無可辨識身分一律不給記憶。
    uid = (identity.get("user_id") or "").strip()
    tenant = uid or None
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
    hooks: Any = TracingRunHooks(emitter, inner=audit) if emitter.active else audit

    # 串流：CSP Router 對 agent 強制 stream=true 並逐行解析 OpenAI SSE
    # （proxy_service.proxy_stream）。回 text/event-stream 的 chat.completion.chunk。
    # Scope is re-bound inside a producer Task (see _sse_stream).
    if req.stream:
        return StreamingResponse(
            _sse_stream(
                assembled,
                user_prompt,
                hooks,
                emitter=emitter,
                dispatch_bearer=dispatch_bearer,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 非串流：Router 對 agent 回應走 resp.json()（也容忍 SSE，但我們回 JSON）。
    try:
        with dispatch_bearer_scope(dispatch_bearer):
            try:
                async with emitter.run_span(MODEL_NAME, attributes={"stream": False}):
                    result = await run_once(assembled, user_prompt, hooks=hooks)
                    answer = result.final_output or ""
                    async with emitter.span(OUTPUT, MODEL_NAME) as out:
                        _annotate_output(out, answer, None)
            finally:
                await emitter.flush()
    except MissingDispatchTokenError as exc:
        # The cause can be a misconfigured outbound origin, which is a
        # deployment fault, not a bad credential — and its message names two
        # internal base URLs. Log the detail, tell the caller nothing internal.
        logger.error("dispatch token unavailable for outbound call: %s", exc)
        raise HTTPException(
            status_code=401,
            detail="missing or invalid dispatch token.",
        ) from exc
    # 回應建好後在背景抽取記憶（不延後 JSON 回應）。
    memory = getattr(getattr(assembled, "context", None), "memory", None)
    _schedule_absorb(memory, user_prompt, answer)
    return {
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8200")))
