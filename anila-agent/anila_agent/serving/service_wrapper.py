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
from dataclasses import replace
from typing import Any

from agents import MaxTurnsExceeded
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from openai.types.responses import ResponseTextDeltaEvent
from pydantic import BaseModel

from anila_agent.config import load_config
from anila_agent.memory.runtime import MemdirRuntime, auto_memory_enabled
from anila_agent.observability.hooks import AuditHooks
from anila_agent.retrieval.csp_http import CspHttpRetriever
from anila_agent.runtime.agent_factory import build_agent
from anila_agent.runtime.model import build_model
from anila_agent.runtime.run import run_once, run_streamed
from anila_agent.serving.auth import trusted_user_identity, verify_service_token

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


def _terminal_event(reason: str, detail: str | None = None) -> str:
    """typed-terminal SSE frame (frozen contract §6). The Router forwards this
    ``anila.*`` event verbatim (its ``_with_terminal`` wrapper won't add a
    duplicate) → the ANILA UI renders a '為何停' badge."""
    payload: dict[str, Any] = {"reason": reason}
    if detail:
        payload["detail"] = detail
    return "event: anila.terminal\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


async def _sse_stream(
    assembled: Any, user_prompt: str, hooks: AuditHooks
) -> AsyncIterator[str]:
    """agent 串流輸出 → OpenAI SSE：role → content deltas → finish+usage → ``[DONE]``。

    只轉發 ResponseTextDeltaEvent（最終可見答案）；工具呼叫 / reasoning 軌跡不外送。
    usage 在串流跑完才定案（SDK 註明 context_wrapper.usage 末包前為 stale），故收尾才讀。
    Router 端（proxy_service.proxy_stream）以 ``resp.aiter_lines()`` 逐行解析這個格式。
    """
    cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    yield _chunk(cid, created, delta={"role": "assistant"})

    result = run_streamed(assembled, user_prompt, hooks=hooks)
    parts: list[str] = []  # 累積最終答案，供 turn 結束後自動抽取記憶。
    try:
        async for event in result.stream_events():
            if (
                event.type == "raw_response_event"
                and isinstance(event.data, ResponseTextDeltaEvent)
                and event.data.delta
            ):
                parts.append(event.data.delta)
                yield _chunk(cid, created, delta={"content": event.data.delta})
    except MaxTurnsExceeded:
        # max-turns remediation (SDK path): the agent didn't converge within
        # max_turns (known gpt-oss failure). Force ONE final answer with tools
        # disabled so the user gets real text instead of an empty response, and
        # emit anila.terminal{max_turns} so the Router → ANILA UI shows why.
        logger.warning("agent hit max_turns; forcing a tools-off final answer")
        if assembled is not None:
            try:
                forced = await run_once(
                    replace(assembled, agent=assembled.agent.clone(tools=[])),
                    user_prompt,
                    max_turns=1,
                )
                forced_text = getattr(forced, "final_output", None) or ""
                if forced_text:
                    parts.append(forced_text)
                    yield _chunk(cid, created, delta={"content": forced_text})
            except Exception:
                logger.exception("forced final answer failed")
        yield _terminal_event("max_turns")
    except Exception:
        # 串流中途失敗：headers 已送出、status 無法再改，記錄後乾淨收尾。
        # 發 error terminal，讓 Router→ANILA UI 顯示「為何停」而非誤標 completed。
        logger.exception("streaming run failed mid-flight")
        yield _terminal_event("error")

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

    # 答案已全部串出 → 背景抽取記憶（不延後回應）。
    memory = getattr(getattr(assembled, "context", None), "memory", None)
    _schedule_absorb(memory, user_prompt, "".join(parts))


@app.post("/v1/chat/completions")
async def chat_completions(
    req: ChatCompletionRequest,
    x_csp_service_token: str | None = Header(default=None, alias="X-CSP-Service-Token"),
    x_anila_user_id: str | None = Header(default=None, alias="X-ANILA-User-Id"),
    x_anila_user_email: str | None = Header(default=None, alias="X-ANILA-User-Email"),
    x_anila_user_groups: str | None = Header(default=None, alias="X-ANILA-User-Groups"),
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

    # Retrieval via CSP HTTP（無 DB）；build_agent(retriever=) escape hatch 直接注入。
    retriever = CspHttpRetriever(
        csp_base_url=CSP_BASE_URL,
        collection_id=COLLECTION_ID,
        api_key=CSP_SEARCH_TOKEN,
        min_score=CSP_MIN_SCORE,
        verify_ssl=SSL_VERIFY,
    )
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
    hooks = AuditHooks(user_id=identity.get("user_id"))

    # 串流：CSP Router 對 agent 強制 stream=true 並逐行解析 OpenAI SSE
    # （proxy_service.proxy_stream）。回 text/event-stream 的 chat.completion.chunk。
    if req.stream:
        return StreamingResponse(
            _sse_stream(assembled, user_prompt, hooks),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 非串流：Router 對 agent 回應走 resp.json()（也容忍 SSE，但我們回 JSON）。
    result = await run_once(assembled, user_prompt, hooks=hooks)
    answer = result.final_output or ""
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
