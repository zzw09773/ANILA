"""prompt_translator: rewrite Chinese natural language into a
FLUX-friendly English prompt by calling gemma4 via CSP proxy.

Can be disabled via constructor flag (then it's a no-op pass-through).
"""
from __future__ import annotations

import httpx
import pytest
import respx

from app.prompt_translator import PromptTranslator


@pytest.mark.asyncio
async def test_translator_passthrough_when_disabled():
    translator = PromptTranslator(
        csp_base_url="http://csp:8000",
        csp_api_key="sk-test",
        gemma_model="gemma4",
        enabled=False,
    )
    out = await translator.translate("畫一張坦克")
    assert out == "畫一張坦克"


@pytest.mark.asyncio
@respx.mock
async def test_translator_calls_gemma_and_extracts_english():
    respx.post("http://csp:8000/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "A military tank in mountainous terrain, cinematic, photorealistic",
                        }
                    }
                ]
            },
        )
    )

    translator = PromptTranslator(
        csp_base_url="http://csp:8000",
        csp_api_key="sk-test",
        gemma_model="gemma4",
        enabled=True,
    )
    out = await translator.translate("畫一張在山上的坦克")

    assert "tank" in out.lower()


@pytest.mark.asyncio
@respx.mock
async def test_translator_sends_authorization_header():
    route = respx.post("http://csp:8000/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "x"}}]},
        )
    )

    translator = PromptTranslator(
        csp_base_url="http://csp:8000",
        csp_api_key="sk-secret-key",
        gemma_model="gemma4",
        enabled=True,
    )
    await translator.translate("hi")

    assert route.called
    assert route.calls.last.request.headers["authorization"] == "Bearer sk-secret-key"


@pytest.mark.asyncio
@respx.mock
async def test_translator_falls_back_to_original_on_gemma_error():
    respx.post("http://csp:8000/v1/chat/completions").mock(
        return_value=httpx.Response(500, json={"detail": "vllm down"})
    )

    translator = PromptTranslator(
        csp_base_url="http://csp:8000",
        csp_api_key="sk-test",
        gemma_model="gemma4",
        enabled=True,
    )
    out = await translator.translate("原始輸入")

    # Translation failed but we still got a usable string back.
    assert out == "原始輸入"
