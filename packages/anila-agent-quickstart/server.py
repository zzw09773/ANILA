"""ASGI 外殼：三端點、派工 JWT、單輪回答、SSE。"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

import agent as agent_mod
import llm as llm_mod
import platform_io as pio

REQUEST_DEADLINE_SECONDS = 240
TOKEN_SAFETY_MARGIN = 5
OUTPUT_CHAR_LIMIT = 100_000
HEARTBEAT_SECONDS = 15

Respond = Callable[..., AsyncIterator[str]]


def create_app(
    settings: pio.Settings | None = None,
    *,
    jwks: pio.JwksCache | None = None,
    respond: Respond | None = None,
    llm_client=None,
    search_client=None,
    background: bool = True,
) -> FastAPI:
    cfg = settings or pio.load_settings()
    cache = jwks or pio.JwksCache(cfg)
    answer = respond or agent_mod.respond

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        owned: list = []
        if background and app.state.llm is None and cfg.llm_base_url:
            # Default LLM host is this CSP's /v1, so it uses the same public
            # CA as JWKS. LLM_CA_FILE still wins when the developer set one.
            http = llm_mod.make_async_client(ca_file=cfg.llm_ca_file or cfg.ca_file)
            owned.append(http)
            app.state.llm = llm_mod.LlmClient(cfg, http)
        if background and app.state.search is None and cfg.csp_base_url:
            http = llm_mod.make_async_client(ca_file=cfg.ca_file)
            owned.append(http)
            app.state.search = http
        task = asyncio.create_task(_jwks_loop(app)) if background else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
            for http in owned:
                await http.aclose()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.settings = cfg
    app.state.jwks = cache
    app.state.respond = answer
    app.state.llm = llm_client
    app.state.search = search_client
    app.state.shutting_down = False

    @app.exception_handler(RequestValidationError)
    async def bad_body(_request: Request, _exc: RequestValidationError):
        return _error(400, "invalid_request", "請求格式錯誤")

    @app.middleware("http")
    async def no_forwarded(request: Request, call_next):
        return await call_next(request)

    @app.get("/health")
    async def health():
        # Don't wait on JWKS when the developer still has to fill a placeholder.
        if app.state.settings.readiness_reason() is None and not app.state.jwks.fresh:
            await app.state.jwks.refresh()
        code = _readiness(app)
        if code != "ok":
            return JSONResponse(
                {
                    "status": code,
                    "reason": code,
                    "hint": _hint(app.state.settings, code),
                },
                status_code=503,
            )
        return {
            "status": "ok",
            "model": agent_mod.AGENT_NAME,
            "rag": False,
        }

    @app.get("/v1/models")
    async def models(request: Request):
        denied = await _authorize(request)
        if denied is not None:
            return denied
        return {
            "object": "list",
            "data": [
                {
                    "id": agent_mod.AGENT_NAME,
                    "object": "model",
                    "owned_by": "agent-developer",
                }
            ],
        }

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        denied = await _authorize(request)
        if denied is not None:
            return denied
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _error(400, "invalid_request", "請求格式錯誤")
        try:
            messages = _messages(body)
        except ValueError as exc:
            return _error(400, "invalid_request", str(exc))
        deadline = _deadline(request.state.claims)
        stream = bool(body.get("stream"))
        if stream:
            return StreamingResponse(
                _stream(app, request, messages, deadline),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache, no-transform",
                    "X-Accel-Buffering": "no",
                },
            )
        try:
            text, result = await _collect(app, request, messages, deadline)
        except pio.SearchError as exc:
            return _error(exc.status, "search_failed", "資料來源查詢失敗")
        except llm_mod.UpstreamError:
            return _error(502, "upstream_error", "模型呼叫失敗")
        except TimeoutError:
            return _error(504, "deadline", "超過本輪時限")
        except OutputLimit:
            return _error(400, "output_limit", "回答超過長度上限")
        payload = _completion(text, result)
        return JSONResponse(payload)

    return app


async def _jwks_loop(app: FastAPI) -> None:
    """啟動先抓；失敗每 10 秒重試。成功後等滿 240 秒再更新。"""
    while True:
        await app.state.jwks.refresh()
        if app.state.jwks.fresh:
            age = time.monotonic() - app.state.jwks._fetched_at
            await asyncio.sleep(max(1.0, pio.JWKS_REFRESH_SECONDS - age))
        else:
            await asyncio.sleep(pio.JWKS_RETRY_SECONDS)


def _readiness(app: FastAPI) -> str:
    if app.state.shutting_down:
        return "shutting_down"
    reason = app.state.settings.readiness_reason()
    if reason is not None:
        return reason
    if not app.state.jwks.fresh:
        return "jwks_unavailable"
    return "ok"


def _hint(settings: pio.Settings, reason: str) -> str:
    """Short Traditional Chinese next step. ``reason`` is the machine code."""
    if reason == "not_registered":
        return "尚未填 ANILA_AGENT_ID。註冊後把數字 id 寫進 deployment.env，再執行 ./run.sh restart。"
    if reason == "llm_not_configured":
        missing = [name for name in ("LLM_MODEL", "LLM_API_KEY") if name in settings.gaps]
        if missing == ["LLM_API_KEY"]:
            return "尚未設定 LLM_API_KEY。在 lab 執行 export LLM_API_KEY 後重啟，不要寫進 deployment.env。"
        if missing == ["LLM_MODEL"]:
            return "尚未填 LLM_MODEL。在 deployment.env 寫上你獲准使用的模型名稱後重啟。"
        return "尚未設定 LLM_MODEL 與 LLM_API_KEY。填模型名稱，並在 lab export API key 後重啟。"
    if reason == "jwks_unavailable":
        return "還抓不到 CSP 的 JWKS。確認 CSP_BASE_URL、ca.pem 與連線，不要關掉 TLS。"
    if reason == "config_missing":
        return "部署設定不完整。CSP_BASE_URL、ANILA_CA_FILE、LLM_BASE_URL 必須齊全。"
    if reason == "shutting_down":
        return "服務正在停止。"
    return "服務尚未就緒。"


async def _authorize(request: Request):
    settings: pio.Settings = request.app.state.settings
    reason = settings.readiness_reason()
    if reason is not None:
        return _error(503, reason, _hint(settings, reason))
    if not request.app.state.jwks.fresh:
        await request.app.state.jwks.refresh(force=True)
    if not request.app.state.jwks.fresh:
        return _error(503, "jwks_unavailable", "JWKS 暫不可用")
    header = request.headers.get("authorization")
    try:
        claims = await request.app.state.jwks.verify(header)
        pio.check_identity(claims, settings.agent_id)
    except pio.NotReady:
        return _error(503, "jwks_unavailable", "JWKS 暫不可用")
    except pio.AgentMismatch:
        return _error(403, "agent_mismatch", "派工對象不符")
    except pio.IdentityError:
        return _error(401, "invalid_token", "派工憑證無效")
    except Exception as exc:
        name = type(exc).__name__
        if name == "AnilaVerifyError":
            return _error(401, "invalid_token", "派工憑證無效")
        raise
    request.state.claims = claims
    request.state.authorization = header
    return None


def _messages(body: dict) -> list[dict]:
    raw = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(raw, list) or not raw:
        raise ValueError("messages 不可為空")
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("訊息格式錯誤")
        role = item.get("role")
        content = item.get("content")
        if role not in {"system", "user", "assistant"}:
            raise ValueError("不支援的訊息角色")
        if isinstance(content, list):
            texts = []
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "text":
                    raise ValueError("不支援的內容類型")
                if not isinstance(part.get("text"), str):
                    raise ValueError("不支援的內容類型")
                texts.append(part["text"])
            content = "".join(texts)
        if not isinstance(content, str):
            raise ValueError("訊息內容必須是文字")
        out.append({"role": role, "content": content})
    if not any(m["role"] == "user" and m["content"].strip() for m in out):
        raise ValueError("缺少使用者問題")
    return out


def _deadline(claims: dict) -> float:
    exp = float(claims["exp"])
    token_left = exp - time.time() - TOKEN_SAFETY_MARGIN
    budget = min(REQUEST_DEADLINE_SECONDS, max(token_left, 0))
    return time.monotonic() + budget


def _error(status: int, code: str, message: str) -> JSONResponse:
    headers = {}
    if status == 401:
        headers["WWW-Authenticate"] = 'Bearer realm="anila-agent"'
    return JSONResponse(
        {"error": {"message": message, "type": "invalid_request_error", "code": code}},
        status_code=status,
        headers=headers,
    )


def _last_user(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message["role"] == "user":
            return message["content"]
    return ""


async def _prepare(app: FastAPI, request: Request, messages: list[dict], deadline: float):
    context = ""
    collection = agent_mod.COLLECTION_ID
    if isinstance(collection, int) and collection > 0:
        client = app.state.search
        if client is None:
            raise pio.SearchError(503)
        context = await pio.search_once(
            client,
            app.state.settings,
            collection_id=collection,
            query=_last_user(messages),
            authorization=request.state.authorization,
            deadline=deadline,
        )
    client = app.state.llm
    if client is None:
        raise llm_mod.UpstreamError("no llm")
    bound = client.bind(deadline)
    # 派工請求把同一枚 JWT 轉去 CSP；沒有這個方法的測試雙生物件維持原樣。
    use_dispatch = getattr(bound, "use_dispatch_authorization", None)
    if use_dispatch is not None:
        use_dispatch(getattr(request.state, "authorization", None))

    holder = {"result": getattr(bound, "result", llm_mod.LlmResult())}

    async def call(msgs, *, instructions: str, context: str):
        stream = await bound.complete(msgs, instructions=instructions, context=context)
        holder["result"] = stream.result
        async for piece in stream.chunks:
            yield piece

    return context, call, holder


async def _collect(app, request, messages, deadline):
    context, call, holder = await _prepare(app, request, messages, deadline)
    parts: list[str] = []
    size = 0
    try:
        async with asyncio.timeout(_remaining(deadline)):
            async for piece in app.state.respond(messages, context, call):
                size += len(piece)
                if size > OUTPUT_CHAR_LIMIT:
                    raise OutputLimit
                parts.append(piece)
    except TimeoutError:
        raise
    result = holder["result"]
    if not "".join(parts).strip():
        raise llm_mod.UpstreamError("empty")
    return "".join(parts), result


async def _stream(app, request, messages, deadline) -> AsyncIterator[str]:
    created = int(time.time())
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    try:
        context, call, holder = await _prepare(app, request, messages, deadline)
    except pio.SearchError:
        yield _sse_error("search_failed", "資料來源查詢失敗")
        yield _done()
        return
    except llm_mod.UpstreamError:
        yield _sse_error("upstream_error", "模型呼叫失敗")
        yield _done()
        return
    yield _chunk(completion_id, created, {"role": "assistant"}, None)
    saw = False
    result = llm_mod.LlmResult()
    try:
        async with asyncio.timeout(_remaining(deadline)):
            agen = app.state.respond(messages, context, call).__aiter__()
            while True:
                try:
                    piece = await asyncio.wait_for(agen.__anext__(), HEARTBEAT_SECONDS)
                except StopAsyncIteration:
                    result = holder["result"]
                    break
                except TimeoutError:
                    if time.monotonic() >= deadline:
                        raise
                    yield ": ping\n\n"
                    continue
                if piece:
                    saw = True
                    if len(piece) > OUTPUT_CHAR_LIMIT:
                        raise OutputLimit
                    yield _chunk(completion_id, created, {"content": piece}, None)
    except (asyncio.CancelledError, GeneratorExit):
        raise
    except OutputLimit:
        yield _sse_error("output_limit", "回答超過長度上限")
        yield _done()
        return
    except (llm_mod.UpstreamError, TimeoutError, asyncio.TimeoutError):
        yield _sse_error("upstream_error", "模型呼叫失敗或逾時")
        yield _done()
        return
    if not saw:
        yield _sse_error("empty_completion", "沒有回答文字")
        yield _done()
        return
    reason = result.finish_reason or "stop"
    yield _chunk(completion_id, created, {}, reason)
    yield _done()


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError
    return left


def _chunk(completion_id: str, created: int, delta: dict, reason: str | None) -> str:
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": agent_mod.AGENT_NAME,
        "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _sse_error(code: str, message: str) -> str:
    body = {"error": {"message": message, "type": "server_error", "code": code}}
    return f"data: {json.dumps(body, ensure_ascii=False)}\n\n"


def _done() -> str:
    return "data: [DONE]\n\n"


def _completion(text: str, result: llm_mod.LlmResult) -> dict:
    payload = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": agent_mod.AGENT_NAME,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": result.finish_reason or "stop",
            }
        ],
    }
    if result.usage:
        payload["usage"] = result.usage
    return payload


class OutputLimit(Exception):
    pass


def main() -> None:
    import uvicorn

    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=8200,
        proxy_headers=False,
        server_header=False,
        timeout_graceful_shutdown=30,
    )


app = create_app()
