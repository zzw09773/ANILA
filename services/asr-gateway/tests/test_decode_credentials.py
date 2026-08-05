"""治理中心指派的解碼端可以自帶金鑰;金鑰不准外流。

以前 `decode_endpoint.py` 的檔頭明文寫著「刻意不從 CSP 拿憑證」。那個自我
設限在解碼端還在同一台機器時成立;一旦端點在算力中心後面,它就等於「沒有
地方可以放金鑰」→ 每一句話 401。這一份守著解除設限之後的兩條線:

1. CSP 給了金鑰就要**真的用上**(不是收下來放著)。
2. 金鑰**不進** /asr/health、不進 log、不進錯誤訊息。repo 是 PUBLIC。
"""

from __future__ import annotations

import json

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

from app.config import Settings
from app.decode_client import DecodeClient, OpenAIDecodeClient
from app.decode_endpoint import (
    current_decode_credential,
    refresh_decode_endpoint,
    reset_decode_endpoint_cache,
)
from app.main import create_app

ENV_URL = "https://env-decoder.example.test:9000"
CSP_URL = "https://csp-decoder.example.test:9000"
CSP_KEY = "sk-designated-by-governance-console"
ENV_KEY = "sk-from-environment-variable"


@pytest.fixture(autouse=True)
def _clean_cache():
    reset_decode_endpoint_cache()
    yield
    reset_decode_endpoint_cache()


def _settings(**overrides) -> Settings:
    base = dict(
        ASR_DECODE_URL=ENV_URL,
        ASR_DECODER_TOKEN="shared-token",
        CSP_BASE_URL="http://csp.test",
        CSP_SERVICE_TOKEN="csk-test",
    )
    base.update(overrides)
    return Settings(**base)


def _mock_primary(*, api_key: str | None) -> None:
    payload = {
        "id": 1,
        "name": "asr-remote",
        "display_name": "算力中心語音",
        "model_type": "asr",
        "endpoint_url": CSP_URL,
        "api_version": "v1",
        "health_status": "unknown",
    }
    if api_key is not None:
        payload["api_key"] = api_key
    respx.get("http://csp.test/api/models/asr-primary").mock(
        return_value=Response(200, json=payload)
    )


@pytest.mark.asyncio
@respx.mock
async def test_csp_designated_key_reaches_the_wire():
    """PROVE RED:在 decode_endpoint 的 `designated is True` 分支不要設
    `_state["api_key"]` → Bearer 會退回 ENV_KEY,這條紅。
    """
    _mock_primary(api_key=CSP_KEY)
    route = respx.post(f"{CSP_URL}/v1/audio/transcriptions").mock(
        return_value=Response(200, json={"text": "ok"})
    )
    s = _settings(
        ASR_DECODE_PROTOCOL="openai", ASR_DECODE_API_KEY=ENV_KEY, ASR_DECODE_URL=ENV_URL
    )
    client = OpenAIDecodeClient(ENV_URL, ENV_KEY, model="whisper-1")
    await refresh_decode_endpoint(s, decode_client=client, force=True)

    assert current_decode_credential(s) == CSP_KEY
    import numpy as np

    await client.decode(np.zeros(160, dtype=np.float32), kind="final", prompt=None, beam=1)
    await client.aclose()
    assert route.calls.last.request.headers["authorization"] == f"Bearer {CSP_KEY}"


@pytest.mark.asyncio
@respx.mock
async def test_env_key_used_when_csp_designates_no_key():
    """本地 decoder 的那筆通常沒掛金鑰 —— 不可以因此把憑證清空。"""
    _mock_primary(api_key=None)
    s = _settings(ASR_DECODE_PROTOCOL="openai", ASR_DECODE_API_KEY=ENV_KEY)
    client = OpenAIDecodeClient(ENV_URL, ENV_KEY, model="whisper-1")
    await refresh_decode_endpoint(s, decode_client=client, force=True)
    assert current_decode_credential(s) == ENV_KEY
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_native_transport_also_accepts_a_designated_secret():
    """native 那條也要能被治理中心換祕密(遠端原生 decoder 的情境)。"""
    _mock_primary(api_key="designated-shared-secret")
    route = respx.post(f"{CSP_URL}/transcribe").mock(
        return_value=Response(200, json={"text": "ok", "no_speech_prob": 0,
                                         "avg_logprob": 0, "decode_seconds": 0})
    )
    s = _settings()
    client = DecodeClient(ENV_URL, "shared-token")
    await refresh_decode_endpoint(s, decode_client=client, force=True)
    import numpy as np

    await client.decode(np.zeros(160, dtype=np.float32), kind="final", prompt=None, beam=1)
    await client.aclose()
    assert route.calls.last.request.headers["x-token"] == "designated-shared-secret"


@pytest.mark.asyncio
@respx.mock
async def test_designated_key_is_dropped_when_designation_goes_away():
    """CSP 說「沒有主 ASR」之後,前一個端點的金鑰不能留在身上。"""
    _mock_primary(api_key=CSP_KEY)
    s = _settings(ASR_DECODE_PROTOCOL="openai", ASR_DECODE_API_KEY=ENV_KEY)
    client = OpenAIDecodeClient(ENV_URL, ENV_KEY, model="whisper-1")
    await refresh_decode_endpoint(s, decode_client=client, force=True)
    assert current_decode_credential(s) == CSP_KEY

    respx.get("http://csp.test/api/models/asr-primary").mock(
        return_value=Response(404, json={"detail": "尚未指定主語音辨識模型"})
    )
    await refresh_decode_endpoint(s, decode_client=client, force=True)
    assert current_decode_credential(s) == ENV_KEY
    await client.aclose()


@respx.mock
def test_health_never_echoes_the_credential():
    """PROVE RED:在 health body 加一個 "api_key" 欄位 → 這條紅。"""
    _mock_primary(api_key=CSP_KEY)
    respx.post(f"{CSP_URL}/v1/audio/transcriptions").mock(
        return_value=Response(200, json={"text": ""})
    )
    s = _settings(ASR_DECODE_PROTOCOL="openai", ASR_DECODE_API_KEY=ENV_KEY)
    app = create_app(
        app_settings=s,
        decode_client=OpenAIDecodeClient(ENV_URL, ENV_KEY, model="whisper-1"),
        skip_upstreams=True,
    )
    app.state.skip_decoder_probe = False
    with TestClient(app) as client:
        resp = client.get("/asr/health")
    raw = resp.text
    assert CSP_KEY not in raw
    assert ENV_KEY not in raw
    assert "shared-token" not in raw
    body = json.loads(raw)
    # 只說來源,不說值。
    assert body["decode_credential_source"] == "csp_registry"
    assert body["decode_protocol"] == "openai"


@respx.mock
def test_health_reports_the_protocol_in_force():
    """協定要看得見 —— 選錯的症狀在別的欄位上長得像網路問題。"""
    respx.get("http://csp.test/api/models/asr-primary").mock(
        return_value=Response(404, json={"detail": "none"})
    )
    respx.get(f"{ENV_URL}/health").mock(return_value=Response(200, json={"ok": True}))
    respx.post(f"{ENV_URL}/transcribe").mock(return_value=Response(400, json={"d": "short"}))
    s = _settings()
    app = create_app(
        app_settings=s,
        decode_client=DecodeClient(ENV_URL, "shared-token"),
        skip_upstreams=True,
    )
    app.state.skip_decoder_probe = False
    with TestClient(app) as client:
        body = client.get("/asr/health").json()
    assert body["decode_protocol"] == "native"
    assert body["decode_credential_source"] == "env"


@pytest.mark.asyncio
@respx.mock
async def test_csp_endpoint_failing_the_guard_is_not_adopted():
    """治理中心的資料庫值可能在登記之後被改動 —— 採用點自己也要驗。

    PROVE RED:拿掉 decode_endpoint 裡的 guard_decode_url(raw) → gateway 會
    把麥克風指向 169.254.169.254,這條紅。
    """
    respx.get("http://csp.test/api/models/asr-primary").mock(
        return_value=Response(
            200,
            json={"endpoint_url": "https://169.254.169.254/latest",
                  "id": 1, "name": "x", "display_name": "x",
                  "model_type": "asr", "api_version": "v1",
                  "health_status": "unknown"},
        )
    )
    s = _settings()
    client = DecodeClient(ENV_URL, "shared-token")
    await refresh_decode_endpoint(s, decode_client=client, force=True)
    assert "169.254.169.254" not in client.base_url

    from app.decode_endpoint import decode_url_refresh_meta

    assert "未通過出向檢查" in (decode_url_refresh_meta()["last_refresh_error"] or "")
    await client.aclose()
