"""FastAPI entrypoint for flux2-dev-agent.

Wires runtime config (env vars) to the four collaborators
(translator, flux client factory, image store, chat handler) and
exposes ``/health``, ``/v1/models``, ``/v1/chat/completions``.

``build_app`` takes the collaborators as parameters so tests can
inject mocks; the module-level ``app`` instance built from env is
what uvicorn imports.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from .backend_resolver import BackendResolver
from .chat_handler import ChatHandler, _BackendResolverProto, _FluxClientCtxProto
from .flux_client import FluxClient
from .image_primary_fetcher import ImagePrimaryFetcher
from .image_store import ImageStore
from .prompt_translator import PromptTranslator
from .schemas import ChatCompletionRequest

logger = logging.getLogger(__name__)


def build_app(
    *,
    translator,
    flux_client_factory: Callable[[str, str], _FluxClientCtxProto],
    image_store: ImageStore,
    backend_resolver: _BackendResolverProto,
    default_aspect_ratio: str,
) -> FastAPI:
    app = FastAPI(title="flux2-dev-agent", version="0.1.0")
    handler = ChatHandler(
        translator=translator,
        flux_client_factory=flux_client_factory,
        image_store=image_store,
        backend_resolver=backend_resolver,
        default_aspect_ratio=default_aspect_ratio,
    )

    @app.get("/health")
    def _health() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ok"}

    # OpenAI /v1/models 的 created 是 int epoch;這裡取 app 建立時間,
    # 服務生命週期內回穩定值。
    manifest_created = int(time.time())

    @app.get("/v1/models")
    def _list_models() -> dict:  # pyright: ignore[reportUnusedFunction]
        # 欄位對齊 csp 自己的 /v1/models(app/api/proxy.py list_models_openai:
        # id / object / created / owned_by),並補 model_type:"agent" 標記,
        # 與 compose AUTO_REGISTER_MODELS 內 image-generator 的
        # model_type:"agent" 註冊一致(csp 靠註冊資料而非本 manifest 辨識
        # agent;此欄位供人工檢視 / 之後 manifest 掃描對齊用)。
        return {
            "object": "list",
            "data": [
                {
                    "id": "image-generator",
                    "object": "model",
                    "created": manifest_created,
                    "owned_by": "anila",
                    "model_type": "agent",
                },
            ],
        }

    @app.post("/v1/chat/completions")  # response_model removed — supports both JSON and SSE
    async def _chat_completions(
        req: ChatCompletionRequest, request: Request
    ):  # pyright: ignore[reportUnusedFunction]
        try:
            response = await handler.handle(
                req,
                task_id=request.headers.get("X-ANILA-Task-Id"),
                user_identity=request.headers.get("X-ANILA-User-Id"),
            )
        except Exception:
            logger.exception("flux generation failed")
            raise HTTPException(status_code=502, detail="image generation failed")

        if not req.stream:
            return response

        # Streaming branch: emit one role chunk, one content chunk, one
        # finish chunk, then [DONE]. The ANILA Router (anila-core-router)
        # expects SSE when it dispatches a streaming request to an agent;
        # returning JSON makes Router emit 0 content chunks back to the UI.
        content = response.choices[0].message.content

        def _chunk(delta: dict, finish_reason=None) -> str:
            payload = {
                "id": response.id,
                "object": "chat.completion.chunk",
                "created": response.created,
                "model": response.model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

        async def _sse():
            yield _chunk({"role": "assistant"})
            yield _chunk({"content": content})
            yield _chunk({}, finish_reason="stop")
            yield "data: [DONE]\n\n"

        return StreamingResponse(_sse(), media_type="text/event-stream")

    return app


def _build_from_env() -> FastAPI:
    # FLUX_BACKEND_URL / FLUX_MODEL:env fallback,當 CSP 沒有指定「主圖像
    # 模型」(image-primary,見 image_primary_fetcher.py)或連不上/被拒時
    # 使用。base URL 伺服器根或含 /v1 皆可(FluxClient 會做 /v1 正規化)。
    flux_backend_url = os.environ.get("FLUX_BACKEND_URL", "http://flux2-dev:8000")
    # FLUX_MODEL:request body 的 model 欄位;FLUX_API_KEY:有值才帶 Bearer。
    flux_model = os.environ.get("FLUX_MODEL", "flux.2-dev").strip() or "flux.2-dev"
    flux_api_key = os.environ.get("FLUX_API_KEY", "").strip()
    csp_base_url = os.environ.get("CSP_BASE_URL", "http://csp:8000")
    csp_api_key = os.environ.get("CSP_API_KEY", "")
    # CSP_SERVICE_TOKEN:X-CSP-Service-Token,打 GET /api/models/image-primary
    # 用的 s2s 認證。flux2-dev-agent 原本沒有這個機制(prompt_translator 走
    # CSP_API_KEY Bearer,是給 gemma4 chat completions 用的,跟這裡的服務
    # 端認證是兩回事);沒設就送不帶 header 的請求,CSP 會回 401,fetcher
    # 照樣 fallback 到 env(見錯誤處理表),行為等同「功能關閉」。
    csp_service_token = os.environ.get("CSP_SERVICE_TOKEN", "").strip()
    image_via_csp = os.environ.get("GATE2_IMAGE_VIA_CSP", "1") == "1"
    gemma_model = os.environ.get("GEMMA_MODEL", "gemma4")
    enable_translation = os.environ.get("ENABLE_PROMPT_TRANSLATION", "1") == "1"
    share_dir = Path(os.environ.get("SHARE_DIR", "/share/flux"))
    public_prefix = os.environ.get("PUBLIC_URL_PREFIX", "/uploads/flux")
    aspect_ratio = os.environ.get("DEFAULT_ASPECT_RATIO", "16:9")
    flux_timeout = float(os.environ.get("FLUX_TIMEOUT_SECONDS", "180"))

    if enable_translation and not csp_api_key:
        logger.warning(
            "ENABLE_PROMPT_TRANSLATION=1 but CSP_API_KEY is empty; "
            "prompt translation is disabled. FLUX will receive raw user input."
        )
    if not csp_service_token:
        logger.info(
            "CSP_SERVICE_TOKEN 未設定;image-primary 熱抓取會被 CSP 拒絕"
            "(401),FLUX 端點/模型固定用 env FLUX_BACKEND_URL/FLUX_MODEL。"
        )

    translator = PromptTranslator(
        csp_base_url=csp_base_url,
        csp_api_key=csp_api_key,
        gemma_model=gemma_model,
        enabled=enable_translation and bool(csp_api_key),
    )

    image_primary_fetcher = ImagePrimaryFetcher(
        csp_base_url=csp_base_url,
        service_token=csp_service_token,
    )
    backend_resolver = BackendResolver(
        fetcher=image_primary_fetcher,
        fallback_endpoint=flux_backend_url,
        fallback_model=flux_model,
    )

    def flux_factory(endpoint: str, model: str) -> FluxClient:
        return FluxClient(
            base_url=csp_base_url if image_via_csp else endpoint,
            timeout=flux_timeout,
            model=model,
            api_key=flux_api_key,
            service_token=csp_service_token if image_via_csp else "",
        )

    store = ImageStore(local_dir=share_dir, public_url_prefix=public_prefix)

    return build_app(
        translator=translator,
        flux_client_factory=flux_factory,
        image_store=store,
        backend_resolver=backend_resolver,
        default_aspect_ratio=aspect_ratio,
    )


app = _build_from_env()
