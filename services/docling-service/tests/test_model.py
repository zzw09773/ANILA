"""model.py helper tests — no docling weights loaded.

Covers the version-drift-tolerant helpers (safe export / title / page count /
picture extraction / ocr flag). These are the parts that must keep working
across docling data-model churn; the actual ``DocumentConverter`` is a thin
wrapper over docling's own surface and is not unit-tested here (it would pull
in the model weights).
"""

from __future__ import annotations

from app.model import (
    _safe_export_markdown,
    _safe_page_count,
    _safe_ocr_flag,
    _safe_title,
)


class _Doc:
    def __init__(self, *, title=None, name=None, pages=None, pictures=None):
        self.title = title
        self.name = name
        self.pages = pages
        self.pictures = pictures

    def export_to_markdown(self):
        return "  # hello  "


class _Result:
    def __init__(self, timings):
        self.timings = timings


def test_export_markdown_strips_and_stringifies():
    assert _safe_export_markdown(_Doc()) == "# hello"


def test_export_markdown_returns_empty_on_failure():
    class _Broken(_Doc):
        def export_to_markdown(self):
            raise RuntimeError("boom")

    assert _safe_export_markdown(_Broken()) == ""


def test_safe_title_prefers_document_title():
    assert _safe_title(_Doc(title="  Real Title  "), fallback="fallback") == "Real Title"


def test_safe_title_falls_back_when_no_title():
    assert _safe_title(_Doc(pages=[]), fallback="fallback") == "fallback"


def test_safe_page_count():
    assert _safe_page_count(_Doc(pages=[1, 2, 3])) == 3


def test_safe_page_count_none():
    assert _safe_page_count(_Doc()) is None


def test_safe_ocr_flag_detects_ocr_timing_key():
    assert _safe_ocr_flag(_Result({"ocr_pdf_pages": 1.2})) is True


def test_safe_ocr_flag_false_when_no_ocr_key():
    assert _safe_ocr_flag(_Result({"total": 1.2})) is False


def test_safe_ocr_flag_false_when_no_timings():
    assert _safe_ocr_flag(_Result(None)) is False
