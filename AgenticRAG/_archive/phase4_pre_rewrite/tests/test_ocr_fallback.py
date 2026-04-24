"""Tests for ingestion.ocr — trigger heuristic and env-driven backend dispatch.

The actual OCR engines (EasyOCR, Tesseract) are heavy optional deps; these
tests cover the parts that don't need a real model: the fallback trigger
heuristic and the env-driven backend selector.
"""
from __future__ import annotations

import pytest

from agentic_rag.ingestion.ocr import (
    EasyOcrBackend,
    TesseractOcrBackend,
    build_ocr_backend_from_env,
    needs_ocr_fallback,
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for k in (
        "PDF_OCR_FALLBACK",
        "PDF_OCR_BACKEND",
        "PDF_OCR_LANGS",
        "PDF_OCR_TESSERACT_LANG",
    ):
        monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------------------
# needs_ocr_fallback
# ---------------------------------------------------------------------------

def test_short_extraction_triggers_ocr():
    assert needs_ocr_fallback("") is True
    assert needs_ocr_fallback("   ") is True
    assert needs_ocr_fallback("hi") is True


def test_normal_extraction_does_not_trigger():
    body = "申誡之條件依陸海空軍懲罰法第八條規定如下，凡軍人違反軍紀者..." * 3
    assert needs_ocr_fallback(body) is False


def test_high_placeholder_density_triggers_ocr():
    # Mostly <?> sentinels — classic font-subsetted PDF symptom.
    junk = "<?>" * 50 + "abc"
    assert needs_ocr_fallback(junk) is True


def test_low_placeholder_density_does_not_trigger():
    body = "申誡之條件依陸海空軍懲罰法第八條規定如下" * 5 + "<?>"
    assert needs_ocr_fallback(body) is False


# ---------------------------------------------------------------------------
# build_ocr_backend_from_env
# ---------------------------------------------------------------------------

def test_disabled_by_default():
    assert build_ocr_backend_from_env() is None


def test_enabled_default_backend_is_easyocr(monkeypatch):
    monkeypatch.setenv("PDF_OCR_FALLBACK", "true")
    backend = build_ocr_backend_from_env()
    assert isinstance(backend, EasyOcrBackend)
    # default langs from env (ch_tra,en)
    assert backend._languages == ["ch_tra", "en"]


def test_enabled_easyocr_with_custom_langs(monkeypatch):
    monkeypatch.setenv("PDF_OCR_FALLBACK", "true")
    monkeypatch.setenv("PDF_OCR_BACKEND", "easyocr")
    monkeypatch.setenv("PDF_OCR_LANGS", "ch_tra, en, ja")
    backend = build_ocr_backend_from_env()
    assert isinstance(backend, EasyOcrBackend)
    assert backend._languages == ["ch_tra", "en", "ja"]


def test_enabled_tesseract_backend(monkeypatch):
    monkeypatch.setenv("PDF_OCR_FALLBACK", "true")
    monkeypatch.setenv("PDF_OCR_BACKEND", "tesseract")
    monkeypatch.setenv("PDF_OCR_TESSERACT_LANG", "chi_tra")
    backend = build_ocr_backend_from_env()
    assert isinstance(backend, TesseractOcrBackend)
    assert backend._lang == "chi_tra"


def test_unknown_backend_returns_none(monkeypatch):
    monkeypatch.setenv("PDF_OCR_FALLBACK", "true")
    monkeypatch.setenv("PDF_OCR_BACKEND", "magic")
    assert build_ocr_backend_from_env() is None


# ---------------------------------------------------------------------------
# Backend lazy-load behaviour: missing optional deps raise ImportError.
# ---------------------------------------------------------------------------

def test_easyocr_extract_without_dep_raises_importerror(monkeypatch):
    """When 'easyocr' isn't installed, the lazy load must surface a helpful error."""
    monkeypatch.setitem(__import__("sys").modules, "easyocr", None)
    backend = EasyOcrBackend()
    with pytest.raises(ImportError, match="agentic-rag\\[ocr\\]"):
        backend._ensure_loaded()


def test_tesseract_extract_without_dep_raises_importerror(monkeypatch, tmp_path):
    """Without pytesseract installed, the call must raise ImportError."""
    monkeypatch.setitem(__import__("sys").modules, "pytesseract", None)
    backend = TesseractOcrBackend()
    fake = tmp_path / "x.pdf"
    fake.write_bytes(b"not really a pdf")
    with pytest.raises(ImportError, match="ocr-tesseract"):
        backend.extract(str(fake))
