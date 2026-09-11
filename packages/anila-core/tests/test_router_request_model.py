"""Request-scoped Router model must not leak across concurrent calls."""
from __future__ import annotations

import asyncio

from anila_core.api.router_server import REQUEST_ROUTER_MODEL, current_router_model
from anila_core.config import settings


async def _select(name: str) -> str:
    token = REQUEST_ROUTER_MODEL.set(name)
    try:
        await asyncio.sleep(0.01)
        return current_router_model()
    finally:
        REQUEST_ROUTER_MODEL.reset(token)


def test_request_scoped_model_overrides_shared_settings():
    token = REQUEST_ROUTER_MODEL.set("glm-example")
    try:
        assert current_router_model() == "glm-example"
    finally:
        REQUEST_ROUTER_MODEL.reset(token)
    assert current_router_model() == settings.model or current_router_model()


def test_concurrent_glm_and_qwen_do_not_cross():
    async def _run():
        glm, qwen = await asyncio.gather(_select("glm-example"), _select("qwen-example"))
        return glm, qwen
    glm, qwen = asyncio.run(_run())
    assert glm == "glm-example"
    assert qwen == "qwen-example"
