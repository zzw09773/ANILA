"""FastAPI entrypoint for flux2-dev-agent.

Wires runtime config (env vars) to the four collaborators
(translator, flux client factory, image store, chat handler) and
exposes ``/health``, ``/v1/models``, ``/v1/chat/completions``.

``build_app`` takes the collaborators as parameters so tests can
inject mocks; the module-level ``app`` instance built from env is
what uvicorn imports.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException

from .chat_handler import ChatHandler, _FluxClientCtxProto
from .flux_client import FluxClient
from .image_store import ImageStore
from .prompt_translator import PromptTranslator
from .schemas import ChatCompletionRequest, ChatCompletionResponse

logger = logging.getLogger(__name__)


def build_app(
    *,
    translator,
    flux_client_factory: Callable[[], _FluxClientCtxProto],
    image_store: ImageStore,
    default_aspect_ratio: str,
) -> FastAPI:
    app = FastAPI(title="flux2-dev-agent", version="0.1.0")
    handler = ChatHandler(
        translator=translator,
        flux_client_factory=flux_client_factory,
        image_store=image_store,
        default_aspect_ratio=default_aspect_ratio,
    )

    @app.get("/health")
    def _health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/models")
    def _list_models() -> dict:
        return {
            "object": "list",
            "data": [
                {"id": "image-generator", "object": "model", "owned_by": "anila"},
            ],
        }

    @app.post("/v1/chat/completions", response_model=ChatCompletionResponse)
    async def _chat_completions(req: ChatCompletionRequest) -> ChatCompletionResponse:
        try:
            return await handler.handle(req)
        except Exception:
            logger.exception("flux generation failed")
            raise HTTPException(status_code=502, detail="image generation failed")

    return app


def _build_from_env() -> FastAPI:
    flux_backend_url = os.environ.get("FLUX_BACKEND_URL", "http://flux2-dev:8000")
    csp_base_url = os.environ.get("CSP_BASE_URL", "http://csp:8000")
    csp_api_key = os.environ.get("CSP_API_KEY", "")
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

    translator = PromptTranslator(
        csp_base_url=csp_base_url,
        csp_api_key=csp_api_key,
        gemma_model=gemma_model,
        enabled=enable_translation and bool(csp_api_key),
    )

    def flux_factory():
        return FluxClient(base_url=flux_backend_url, timeout=flux_timeout)

    store = ImageStore(local_dir=share_dir, public_url_prefix=public_prefix)

    return build_app(
        translator=translator,
        flux_client_factory=flux_factory,
        image_store=store,
        default_aspect_ratio=aspect_ratio,
    )


app = _build_from_env()
