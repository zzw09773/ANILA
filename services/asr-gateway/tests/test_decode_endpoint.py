"""ASR decoder URL resolution from CSP — mirrors router-primary behaviour.

These tests exercise the real ``refresh_decode_endpoint`` + ``DecodeClient``
path. They mock the HTTP boundary (respx → CSP), never the function under
test. Prove-red: each acceptance case names the one-line production edit
that turns it red.
"""

from __future__ import annotations

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

from app.config import Settings
from app.decode_client import DecodeClient
from app.decode_endpoint import (
    current_decode_url,
    decode_url_source,
    refresh_decode_endpoint,
    reset_decode_endpoint_cache,
)
from app.main import create_app

ENV_URL = "https://env-decoder.example.test:9000"
CSP_URL = "https://csp-decoder.example.test:9000"
CSP_URL_B = "https://csp-decoder-b.example.test:9000"
TOKEN = "csk-test-asr-decode"


def _settings(**overrides) -> Settings:
    base = dict(
        ASR_DECODE_URL=ENV_URL,
        ASR_DECODER_TOKEN="t",
        CSP_BASE_URL="http://csp.test",
        CSP_SERVICE_TOKEN=TOKEN,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture(autouse=True)
def _clean_cache():
    reset_decode_endpoint_cache()
    yield
    reset_decode_endpoint_cache()


# ── AC1: CSP address when set; env when CSP has none ─────────────────────────


@pytest.mark.asyncio
@respx.mock
async def test_uses_csp_address_when_asr_primary_set():
    """PROVE RED: in refresh_decode_endpoint, after a 200, set
    ``_state["url"] = None`` (ignore csp_url) → this fails because
    current_decode_url would stay on ENV_URL.
    """
    route = respx.get("http://csp.test/api/models/asr-primary").mock(
        return_value=Response(
            200,
            json={
                "id": 1,
                "name": "asr-gpu",
                "display_name": "asr-gpu",
                "model_type": "asr",
                "endpoint_url": CSP_URL,
                "api_version": "v1",
                "health_status": "unknown",
            },
        )
    )
    s = _settings()
    client = DecodeClient(ENV_URL, "t")
    await refresh_decode_endpoint(s, decode_client=client, force=True)

    assert route.called
    assert current_decode_url(s) == CSP_URL.rstrip("/")
    assert client.base_url == CSP_URL.rstrip("/")
    assert decode_url_source() == "csp_registry"
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_uses_env_when_csp_has_no_asr_primary():
    """PROVE RED: on 404, set ``_state["url"] = CSP_URL`` instead of None →
    current_decode_url would not equal ENV_URL.
    """
    respx.get("http://csp.test/api/models/asr-primary").mock(
        return_value=Response(404, json={"detail": "尚未指定主語音辨識模型"})
    )
    s = _settings()
    client = DecodeClient(ENV_URL, "t")
    await refresh_decode_endpoint(s, decode_client=client, force=True)

    assert current_decode_url(s) == ENV_URL.rstrip("/")
    assert client.base_url == ENV_URL.rstrip("/")
    assert decode_url_source() == "env"
    await client.aclose()


# ── AC2: CSP unreachable → keep last known / env; state visible ──────────────


@pytest.mark.asyncio
@respx.mock
async def test_csp_unreachable_keeps_last_known_and_marks_stale():
    """PROVE RED: in the ``designated is None`` branch, set
    ``_state["url"] = None`` and ``source = "env"`` → silently falls back
    to the env decoder and this fails (source would be env, url ENV).
    """
    s = _settings()
    client = DecodeClient(ENV_URL, "t")
    respx.get("http://csp.test/api/models/asr-primary").mock(
        return_value=Response(
            200,
            json={"endpoint_url": CSP_URL, "name": "asr-gpu", "id": 1,
                  "display_name": "a", "model_type": "asr",
                  "api_version": "v1", "health_status": "unknown"},
        )
    )
    await refresh_decode_endpoint(s, decode_client=client, force=True)
    assert decode_url_source() == "csp_registry"

    respx.get("http://csp.test/api/models/asr-primary").mock(
        side_effect=ConnectionError("csp down")
    )
    await refresh_decode_endpoint(s, decode_client=client, force=True)

    assert current_decode_url(s) == CSP_URL.rstrip("/")
    assert client.base_url == CSP_URL.rstrip("/")
    assert decode_url_source() == "csp_registry_stale"
    from app.decode_endpoint import decode_url_refresh_meta
    meta = decode_url_refresh_meta()
    assert meta["last_refresh_error"]
    assert "ConnectionError" in meta["last_refresh_error"]
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_csp_unreachable_at_boot_falls_back_to_env_visibly():
    """Never had a CSP URL → keep env, but error must be visible (not silent)."""
    respx.get("http://csp.test/api/models/asr-primary").mock(
        side_effect=ConnectionError("csp down")
    )
    s = _settings()
    client = DecodeClient(ENV_URL, "t")
    await refresh_decode_endpoint(s, decode_client=client, force=True)

    assert current_decode_url(s) == ENV_URL.rstrip("/")
    assert decode_url_source() == "env"
    from app.decode_endpoint import decode_url_refresh_meta
    assert decode_url_refresh_meta()["last_refresh_error"]
    await client.aclose()


# ── AC3: operator can see which source is in force ───────────────────────────


@respx.mock
def test_health_reports_decode_url_source():
    """PROVE RED: remove ``decode_url_source`` from the health JSON → KeyError.
    """
    respx.get("http://csp.test/api/models/asr-primary").mock(
        return_value=Response(
            200,
            json={"endpoint_url": CSP_URL, "name": "asr-gpu", "id": 1,
                  "display_name": "a", "model_type": "asr",
                  "api_version": "v1", "health_status": "unknown"},
        )
    )
    s = _settings()
    app = create_app(
        app_settings=s,
        decode_client=DecodeClient(ENV_URL, "t"),
        skip_upstreams=True,
    )
    with TestClient(app) as client:
        resp = client.get("/asr/health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["decode_url"] == CSP_URL.rstrip("/")
    assert body["decode_url_source"] == "csp_registry"
    assert "decode_url_last_refresh_error" in body
    assert "decode_url_last_refresh_at" in body


# ── AC4: address change takes effect within refresh window, no restart ───────


@pytest.mark.asyncio
@respx.mock
async def test_address_change_takes_effect_on_force_refresh():
    """PROVE RED: skip ``_apply_url`` (or ``set_base_url``) after adopting
    the new CSP URL → client.base_url stays on CSP_URL and this fails.
    """
    s = _settings()
    client = DecodeClient(ENV_URL, "t")
    respx.get("http://csp.test/api/models/asr-primary").mock(
        return_value=Response(
            200,
            json={"endpoint_url": CSP_URL, "name": "asr-a", "id": 1,
                  "display_name": "a", "model_type": "asr",
                  "api_version": "v1", "health_status": "unknown"},
        )
    )
    await refresh_decode_endpoint(s, decode_client=client, force=True)
    assert client.base_url == CSP_URL.rstrip("/")

    respx.get("http://csp.test/api/models/asr-primary").mock(
        return_value=Response(
            200,
            json={"endpoint_url": CSP_URL_B, "name": "asr-b", "id": 2,
                  "display_name": "b", "model_type": "asr",
                  "api_version": "v1", "health_status": "unknown"},
        )
    )
    await refresh_decode_endpoint(s, decode_client=client, force=True)

    assert current_decode_url(s) == CSP_URL_B.rstrip("/")
    assert client.base_url == CSP_URL_B.rstrip("/")
    assert decode_url_source() == "csp_registry"
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_does_not_silently_fall_back_to_env_while_csp_designation_stands():
    """Once CSP named a decoder, a later 500 must NOT switch to ENV_URL."""
    s = _settings()
    client = DecodeClient(ENV_URL, "t")
    respx.get("http://csp.test/api/models/asr-primary").mock(
        return_value=Response(
            200,
            json={"endpoint_url": CSP_URL, "name": "asr-gpu", "id": 1,
                  "display_name": "a", "model_type": "asr",
                  "api_version": "v1", "health_status": "unknown"},
        )
    )
    await refresh_decode_endpoint(s, decode_client=client, force=True)

    respx.get("http://csp.test/api/models/asr-primary").mock(
        return_value=Response(500, json={"detail": "boom"})
    )
    await refresh_decode_endpoint(s, decode_client=client, force=True)

    assert current_decode_url(s) == CSP_URL.rstrip("/")
    assert current_decode_url(s) != ENV_URL.rstrip("/")
    assert decode_url_source() == "csp_registry_stale"
    await client.aclose()
