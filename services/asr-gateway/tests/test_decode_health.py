"""Health status must hide the microphone unless speech can be transcribed.

Frontend ``probeAsrAvailable`` keys on HTTP 200. These tests drive the real
``/asr/health`` path with respx at the HTTP boundary.
"""

from __future__ import annotations

import numpy as np
import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

from app.config import Settings
from app.decode_client import DecodeClient
from app.decode_endpoint import refresh_decode_endpoint, reset_decode_endpoint_cache
from app.decode_probe import strip_url_userinfo
from app.main import create_app

ENV_URL = "https://env-decoder.example.test:9000"
CSP_URL = "https://csp-decoder.example.test:9000"
TOKEN = "csk-test-asr-health"


def _settings(**overrides) -> Settings:
    base = dict(
        ASR_DECODE_URL=ENV_URL,
        ASR_DECODER_TOKEN="shared-token",
        CSP_BASE_URL="http://csp.test",
        CSP_SERVICE_TOKEN=TOKEN,
        ASR_DECODE_URL_TTL=60.0,
    )
    base.update(overrides)
    return Settings(**base)


def _mock_decoder_ok(base: str, *, token: str = "shared-token") -> None:
    respx.get(f"{base}/health").mock(
        return_value=Response(200, json={"status": "ok", "ready": True})
    )
    respx.post(f"{base}/transcribe").mock(
        return_value=Response(400, json={"detail": "audio too short"})
    )


def _mock_decoder_down(base: str) -> None:
    respx.get(f"{base}/health").mock(side_effect=ConnectionError("decoder down"))


def _mock_csp_primary(url: str) -> None:
    respx.get("http://csp.test/api/internal/external-services/speech").mock(
        return_value=Response(
            200,
            json={
                "id": 1,
                "name": "asr-gpu",
                "display_name": "asr-gpu",
                "model_type": "asr",
                "endpoint_url": url,
                "api_version": "v1",
                "health_status": "unknown",
            },
        )
    )


@pytest.fixture(autouse=True)
def _clean_cache():
    reset_decode_endpoint_cache()
    yield
    reset_decode_endpoint_cache()


def _app(s: Settings, client: DecodeClient | None = None):
    app = create_app(
        app_settings=s,
        decode_client=client or DecodeClient(s.ASR_DECODE_URL, s.ASR_DECODER_TOKEN),
        skip_upstreams=True,
    )
    # Exercise the real probe; respx supplies the decoder.
    app.state.skip_decoder_probe = False
    return app


# ── AC1: decoder unreachable → status not ok, mic would hide ───────────────


@respx.mock
def test_health_degraded_when_decoder_unreachable():
    """PROVE RED: hardcode status='ok' in health → this fails (expects 503)."""
    _mock_csp_primary(CSP_URL)
    _mock_decoder_down(CSP_URL)
    s = _settings()
    with TestClient(_app(s)) as client:
        resp = client.get("/asr/health")
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["reason"] == "decoder_unreachable"
    assert body["status"] != "ok"


@respx.mock
def test_health_degraded_when_decoder_token_rejected():
    _mock_csp_primary(CSP_URL)
    respx.get(f"{CSP_URL}/health").mock(
        return_value=Response(200, json={"status": "ok", "ready": True})
    )
    respx.post(f"{CSP_URL}/transcribe").mock(
        return_value=Response(401, json={"detail": "invalid token"})
    )
    s = _settings()
    with TestClient(_app(s)) as client:
        resp = client.get("/asr/health")
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["reason"] == "decoder_unauthorized"
    assert "ASR_DECODER_TOKEN" in (body.get("detail") or "")


# ── AC2: CSP unreachable on env fallback → status unavailable (voice off) ──


@respx.mock
def test_health_unavailable_when_csp_down_and_on_env_fallback():
    """Console may have designated another machine; env is not trusted here."""
    respx.get("http://csp.test/api/internal/external-services/speech").mock(
        side_effect=ConnectionError("csp down")
    )
    _mock_decoder_ok(ENV_URL)  # env decoder would work — still refuse
    s = _settings()
    with TestClient(_app(s)) as client:
        resp = client.get("/asr/health")
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["status"] == "unavailable"
    assert body["reason"] == "csp_unreachable"
    assert body["decode_url_source"] == "unconfigured"
    assert body["decode_url_last_refresh_error"]


@respx.mock
def test_health_degraded_when_csp_stale():
    # create_app resets the decode-URL cache — build the app first, then adopt.
    s = _settings(ASR_DECODE_URL_TTL=0.0)
    decode = DecodeClient(ENV_URL, "shared-token")
    app = _app(s, decode)
    _mock_csp_primary(CSP_URL)
    _mock_decoder_ok(CSP_URL)
    with TestClient(app) as client:
        ok = client.get("/asr/health")
        assert ok.status_code == 200, ok.text
        assert ok.json()["decode_url_source"] == "csp_registry"

        respx.get("http://csp.test/api/internal/external-services/speech").mock(
            side_effect=ConnectionError("csp down")
        )
        resp = client.get("/asr/health")
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["reason"] == "csp_stale"
    assert body["decode_url_source"] == "csp_registry_stale"


# ── AC3: distinguish voice-off vs pointed-at-broken ─────────────────────────


@respx.mock
def test_operator_can_tell_voice_off_from_decoder_broken():
    # voice off
    respx.get("http://csp.test/api/internal/external-services/speech").mock(
        side_effect=ConnectionError("csp down")
    )
    s = _settings()
    with TestClient(_app(s)) as client:
        off = client.get("/asr/health").json()
    assert off["status"] == "unavailable"
    assert off["reason"] == "csp_unreachable"

    reset_decode_endpoint_cache()
    # pointed at broken
    _mock_csp_primary(CSP_URL)
    _mock_decoder_down(CSP_URL)
    with TestClient(_app(s)) as client:
        broken = client.get("/asr/health").json()
    assert broken["status"] == "degraded"
    assert broken["reason"] == "decoder_unreachable"
    assert off["reason"] != broken["reason"]


@respx.mock
def test_health_ok_when_csp_designation_and_decoder_ready():
    _mock_csp_primary(CSP_URL)
    _mock_decoder_ok(CSP_URL)
    s = _settings()
    with TestClient(_app(s)) as client:
        resp = client.get("/asr/health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["reason"] == "ok"
    assert body["decode_url_source"] == "csp_registry"


@respx.mock
def test_health_not_configured_when_speech_is_off():
    """沒啟用語音時行程仍健康，但不把麥克風指向環境變數裡的解碼器。"""
    respx.get("http://csp.test/api/internal/external-services/speech").mock(
        return_value=Response(200, json={"configured": False, "enabled": False})
    )
    _mock_decoder_ok(ENV_URL)
    s = _settings()
    with TestClient(_app(s)) as client:
        resp = client.get("/asr/health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "unavailable"
    assert body["reason"] == "not_configured"
    assert body["decode_url_source"] == "unconfigured"
    assert body["decode_url"] == ""


# ── userinfo stripped; host kept ────────────────────────────────────────────


def test_strip_url_userinfo_keeps_host():
    assert (
        strip_url_userinfo("http://user:s3cret@decoder.example.test:9000")
        == "http://decoder.example.test:9000"
    )
    assert strip_url_userinfo("https://decoder.example.test:9000") == (
        "https://decoder.example.test:9000"
    )


@respx.mock
def test_health_strips_userinfo_from_decode_url(intranet_guard_env):
    dirty = "http://user:s3cret@csp-decoder.example.test:9000"
    _mock_csp_primary(dirty)
    s = _settings()
    # Probe skipped: this case only asserts the echoed address redacts userinfo.
    app = create_app(
        app_settings=s,
        decode_client=DecodeClient(ENV_URL, "shared-token"),
        skip_upstreams=True,
    )
    with TestClient(app) as client:
        body = client.get("/asr/health").json()
    assert "s3cret" not in body["decode_url"]
    assert "user:" not in body["decode_url"]
    assert "csp-decoder.example.test" in body["decode_url"]


# ── Mutation killers ────────────────────────────────────────────────────────


@respx.mock
def test_websocket_path_refreshes_decode_url():
    """PROVE RED: delete ``await refresh_decode_endpoint`` in the WS handler
    → client.base_url stays on ENV after connecting.
    """
    from app import auth as auth_mod
    from app.auth import CurrentUserIdentity

    s = _settings(ASR_DECODE_URL_TTL=0.0)
    decode = DecodeClient(ENV_URL, "shared-token")
    # First: no primary → env
    respx.get("http://csp.test/api/internal/external-services/speech").mock(
        return_value=Response(404, json={"detail": "none"})
    )
    app = create_app(app_settings=s, decode_client=decode, skip_upstreams=True)
    app.state.skip_decoder_probe = True

    async def fake_auth(token: str):
        return CurrentUserIdentity(id=1, username="t", role="user", token_version=1)

    async def fake_still(ident):
        return True

    # Patch via attribute on module used by main
    import app.main as main_mod

    orig_auth = auth_mod.authenticate
    orig_still = auth_mod.is_still_valid
    auth_mod.authenticate = fake_auth
    auth_mod.is_still_valid = fake_still
    try:
        with TestClient(app) as client:
            # Prime cache on env
            client.get("/asr/health")
            assert decode.base_url == ""

            # Operator designates a new decoder; TTL=0 so next refresh adopts it
            _mock_csp_primary(CSP_URL)
            client.cookies.set(auth_mod.ACCESS_COOKIE_NAME, "tok")
            with client.websocket_connect("/asr/stream") as ws:
                ws.receive_json()
            assert decode.base_url == CSP_URL.rstrip("/"), (
                "WS path must refresh decode URL; mutation that removes the "
                f"refresh leaves base_url={decode.base_url!r}"
            )
    finally:
        auth_mod.authenticate = orig_auth
        auth_mod.is_still_valid = orig_still


@pytest.mark.asyncio
@respx.mock
async def test_ttl_comes_from_settings_not_year_long_default():
    """PROVE RED: hardcode TTL to 365 days (or read os.environ at import with
    a year default) → second refresh within ~0.05s would NOT hit CSP.
    """
    import asyncio

    route = respx.get("http://csp.test/api/internal/external-services/speech").mock(
        return_value=Response(404, json={"detail": "none"})
    )
    s = _settings(ASR_DECODE_URL_TTL=0.05)
    decode = DecodeClient(ENV_URL, "t")
    await refresh_decode_endpoint(s, decode_client=decode, force=True)
    assert route.call_count == 1
    await refresh_decode_endpoint(s, decode_client=decode, force=False)
    assert route.call_count == 1, "within TTL must not re-hit CSP"
    await asyncio.sleep(0.08)
    await refresh_decode_endpoint(s, decode_client=decode, force=False)
    assert route.call_count == 2, (
        "after Settings.ASR_DECODE_URL_TTL elapses must re-hit CSP; "
        "a year-long hardcoded TTL leaves call_count=1"
    )
    await decode.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_decode_request_lands_on_refreshed_address_at_transport():
    """PROVE RED: freeze transcribe URL at __init__ while set_base_url only
    updates the ``base_url`` property → POST still hits ENV_URL and this fails.
    """
    _mock_csp_primary(CSP_URL)
    env_post = respx.post(f"{ENV_URL}/transcribe").mock(
        return_value=Response(200, json={"text": "env", "no_speech_prob": 0,
                                         "avg_logprob": 0, "decode_seconds": 0})
    )
    csp_post = respx.post(f"{CSP_URL}/transcribe").mock(
        return_value=Response(200, json={"text": "csp", "no_speech_prob": 0,
                                         "avg_logprob": 0, "decode_seconds": 0})
    )
    s = _settings()
    client = DecodeClient(ENV_URL, "shared-token")
    await refresh_decode_endpoint(s, decode_client=client, force=True)
    assert client.base_url == CSP_URL.rstrip("/")

    samples = np.zeros(1600, dtype=np.float32)
    result = await client.decode(
        samples, kind="final", prompt=None, beam=1, language="zh"
    )
    assert result["text"] == "csp"
    assert csp_post.called, "decode must POST to the refreshed CSP address"
    assert not env_post.called, (
        "decode must not still POST to the start-up env URL after refresh"
    )
    await client.aclose()
