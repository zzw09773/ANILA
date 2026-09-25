"""視覺角色：問 CSP，不猜模型名。"""
from __future__ import annotations

import httpx
import pytest
import respx

from ingestion_worker import vision_role


@pytest.fixture(autouse=True)
def _reset():
    vision_role.reset_cache()
    yield
    vision_role.reset_cache()


def test_csp_origin_strips_v1():
    assert vision_role.csp_origin("http://csp:8000/v1") == "http://csp:8000"
    assert vision_role.csp_origin("http://csp:8000/v1/") == "http://csp:8000"
    assert vision_role.csp_origin("") == ""


@pytest.mark.asyncio
async def test_unset_role_returns_the_message_and_no_name():
    with respx.mock:
        respx.get("http://csp:8000/api/models/roles/vision").mock(
            return_value=httpx.Response(
                404, json={"detail": "視覺模型尚未在治理中心設定"}
            )
        )
        name, message = await vision_role.resolve_vision_model(
            vision_url="http://csp:8000/v1", api_key="sk-worker"
        )
    assert name is None
    assert message == "視覺模型尚未在治理中心設定"


@pytest.mark.asyncio
async def test_active_role_is_cached():
    with respx.mock:
        route = respx.get("http://csp:8000/api/models/roles/vision").mock(
            return_value=httpx.Response(200, json={"name": "see-llm"})
        )
        first = await vision_role.resolve_vision_model(
            vision_url="http://csp:8000/v1", api_key="sk-worker"
        )
        second = await vision_role.resolve_vision_model(
            vision_url="http://csp:8000/v1", api_key="sk-worker"
        )
    assert first == ("see-llm", "")
    assert second == ("see-llm", "")
    assert route.call_count == 1
    assert route.calls[0].request.headers["authorization"] == "Bearer sk-worker"


@pytest.mark.asyncio
async def test_pdf_ocr_binds_the_role_and_ignores_vision_model_env(monkeypatch):
    monkeypatch.setenv("VISION_MODEL", "gemma26-nothink")
    monkeypatch.setenv("PDF_OCR_FALLBACK", "true")
    monkeypatch.setenv("VISION_URL", "http://csp:8000/v1")
    with respx.mock:
        respx.get("http://csp:8000/api/models/roles/vision").mock(
            return_value=httpx.Response(200, json={"name": "see-llm"})
        )
        await vision_role.bind_pdf_ocr_model(
            vision_url="http://csp:8000/v1", api_key="sk-worker"
        )
    from anila_core.ingestion import ocr

    try:
        backend = ocr.build_ocr_backend_from_env()
        assert backend is not None
        assert backend._model == "see-llm"
    finally:
        ocr.reset_ocr_model_provider()


@pytest.mark.asyncio
async def test_pdf_ocr_bind_skips_when_role_unset(caplog):
    with respx.mock:
        respx.get("http://csp:8000/api/models/roles/vision").mock(
            return_value=httpx.Response(
                404, json={"detail": "視覺模型尚未在治理中心設定"}
            )
        )
        await vision_role.bind_pdf_ocr_model(
            vision_url="http://csp:8000/v1", api_key="sk-worker"
        )
    assert "略過 PDF OCR" in caplog.text
    assert "視覺模型尚未在治理中心設定" in caplog.text
    assert vision_role.cached_vision_model_name() == ""
