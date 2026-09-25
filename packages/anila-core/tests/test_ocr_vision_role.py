"""PDF OCR 的模型名來自視覺角色，不讀 VISION_MODEL。"""
from __future__ import annotations

import logging

import pytest

from anila_core.ingestion import ocr
from anila_core.ingestion.parser_registry import PdfParser


@pytest.fixture(autouse=True)
def _reset_provider():
    ocr.reset_ocr_model_provider()
    yield
    ocr.reset_ocr_model_provider()


def test_vision_model_env_is_ignored(monkeypatch, caplog):
    monkeypatch.setenv("PDF_OCR_FALLBACK", "true")
    monkeypatch.setenv("VISION_URL", "http://csp:8000/v1")
    monkeypatch.setenv("VISION_MODEL", "gemma26-nothink")
    with caplog.at_level(logging.WARNING):
        backend = ocr.build_ocr_backend_from_env()
    assert backend is None
    assert "視覺模型尚未在治理中心設定" in caplog.text


def test_injected_role_name_is_used(monkeypatch):
    monkeypatch.setenv("PDF_OCR_FALLBACK", "true")
    monkeypatch.setenv("VISION_URL", "http://csp:8000/v1")
    monkeypatch.setenv("VISION_MODEL", "should-not-use")
    ocr.set_ocr_model_provider(lambda: "see-llm")
    backend = ocr.build_ocr_backend_from_env()
    assert backend is not None
    assert backend._model == "see-llm"


def test_flag_off_does_not_build(monkeypatch):
    monkeypatch.delenv("PDF_OCR_FALLBACK", raising=False)
    monkeypatch.setenv("VISION_URL", "http://csp:8000/v1")
    monkeypatch.setenv("VISION_MODEL", "gemma4")
    ocr.set_ocr_model_provider(lambda: "see-llm")
    assert ocr.build_ocr_backend_from_env() is None


def test_parser_does_not_freeze_the_role_model(monkeypatch):
    monkeypatch.setattr(PdfParser, "_ocr_initialised", False, raising=False)
    monkeypatch.setenv("PDF_OCR_FALLBACK", "true")
    monkeypatch.setenv("VISION_URL", "http://csp:8000/v1")
    ocr.set_ocr_model_provider(lambda: "first-llm")
    first = PdfParser._get_ocr_backend()
    ocr.set_ocr_model_provider(lambda: "second-llm")
    second = PdfParser._get_ocr_backend()
    assert first._model == "first-llm"
    assert second._model == "second-llm"
