"""原生契約帶 `model` query:一個解碼端點可能載入多個模型。"""

from __future__ import annotations

import httpx
import numpy as np
import pytest

from app.decode_client import (
    DecodeClient,
    DecodeError,
    align_decode_client,
    native_model_param,
)
from app.decode_probe import REASON_OK, probe_decode_target
from tests.strict_decoder_stub import StrictDecoderStub

SHARED = "native-shared-secret"
MODELS = ("breeze-25", "breeze-26")
AUDIO = np.zeros(1600, dtype=np.float32)


def test_native_model_param_placeholder_and_empty_mean_unset():
    assert native_model_param(None) == ""
    assert native_model_param("  ") == ""
    assert native_model_param("whisper-1") == ""
    assert native_model_param(" breeze-26 ") == "breeze-26"


@pytest.mark.asyncio
async def test_native_sends_configured_model():
    with StrictDecoderStub(credential=SHARED, native_models=MODELS) as stub:
        client = DecodeClient(stub.url, SHARED, model="breeze-26")
        try:
            out = await client.decode(AUDIO, kind="final", prompt=None, beam=1)
        finally:
            await client.aclose()
        assert out["text"]
        assert stub.last_to("/transcribe").query["model"] == ["breeze-26"]


@pytest.mark.asyncio
@pytest.mark.parametrize("model", [None, "", "whisper-1"])
async def test_native_omits_model_when_unset(model):
    with StrictDecoderStub(credential=SHARED, native_models=MODELS) as stub:
        client = DecodeClient(stub.url, SHARED, model=model)
        try:
            await client.decode(AUDIO, kind="final", prompt=None, beam=1)
        finally:
            await client.aclose()
        assert "model" not in stub.last_to("/transcribe").query


@pytest.mark.asyncio
async def test_native_unknown_model_fails_final():
    with StrictDecoderStub(credential=SHARED, native_models=MODELS) as stub:
        client = DecodeClient(stub.url, SHARED, model="breeze-99")
        try:
            with pytest.raises(DecodeError):
                await client.decode(AUDIO, kind="final", prompt=None, beam=1)
        finally:
            await client.aclose()


@pytest.mark.asyncio
async def test_align_carries_model_to_native_and_hot_swaps():
    http = httpx.AsyncClient()
    try:
        c = align_decode_client(
            None, base_url="http://a", credential="t", protocol="native", openai_model="breeze-26"
        )
        assert isinstance(c, DecodeClient) and c.model == "breeze-26"
        same = align_decode_client(
            c, base_url="http://b", credential="t", protocol="native", openai_model="breeze-25"
        )
        assert same is c and c.model == "breeze-25"
        await c.aclose()
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_probe_native_sends_model_and_flags_unknown():
    with StrictDecoderStub(credential=SHARED, native_models=MODELS) as stub:
        ok = await probe_decode_target(stub.url, SHARED, protocol="native", openai_model="breeze-26")
        assert ok["reason"] == REASON_OK
        assert stub.last_to("/transcribe").query["model"] == ["breeze-26"]
        bad = await probe_decode_target(stub.url, SHARED, protocol="native", openai_model="breeze-99")
        assert bad["ok"] is False and "breeze-99" in bad["detail"]
        unset = await probe_decode_target(stub.url, SHARED, protocol="native", openai_model="whisper-1")
        assert unset["ok"] is True
        assert "model" not in stub.last_to("/transcribe").query
