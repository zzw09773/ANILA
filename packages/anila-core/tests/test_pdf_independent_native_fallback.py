"""Real PDF text-layer recovery when remote and Markdown parsers are unavailable."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from anila_core.ingestion.chunking_plugins import get_chunker
from anila_core.ingestion.errors import ParseError
from anila_core.ingestion.parser_registry import ParserRegistry, PdfParser
from anila_core.ingestion.parsers import extract_text

fitz = pytest.importorskip("fitz")

PAGE_TEXT = [
    "Alpha travel policy requires each employee to retain receipts for transport and accommodation. "
    "The department reviews a complete application before reimbursement is authorized.",
    "Bravo procurement rules require competitive quotations and a signed specification before purchase. "
    "The receiving officer checks delivered equipment against the approved technical requirements.",
]


def digital_pdf() -> bytes:
    with fitz.open() as pdf:
        for body in PAGE_TEXT:
            page = pdf.new_page()
            page.insert_textbox(fitz.Rect(50, 50, 530, 500), body, fontsize=12)
        return pdf.tobytes()


def fail_markdown(*args, **kwargs):
    raise RuntimeError("synthetic Markdown extraction failure")


@pytest.mark.parametrize("docling_fault", ["disabled", "initialization", "timeout", "parse"])
def test_real_text_layer_survives_both_parser_failures(monkeypatch, docling_fault):
    class BrokenRemote:
        def parse(self, path):
            if docling_fault == "timeout":
                raise TimeoutError("synthetic remote timeout")
            raise ValueError("synthetic remote parse failure")

    def selected(cls):
        if docling_fault == "initialization":
            raise RuntimeError("synthetic remote initialization failure")
        return None if docling_fault == "disabled" else BrokenRemote()

    monkeypatch.setattr(ParserRegistry, "_get_docling_parser", classmethod(selected))
    monkeypatch.setitem(sys.modules, "pymupdf4llm", SimpleNamespace(to_markdown=fail_markdown))
    text, metadata, images = extract_text("synthetic.pdf", digital_pdf())
    assert all(body.split()[0] in text for body in PAGE_TEXT)
    assert metadata["page_count"] == 2 and metadata["has_page_boundaries"] is True
    assert metadata["actual_parser"] == "pymupdf-native-text"
    assert metadata["quality"] == "native_text_only"
    assert metadata["pdf_text_fallback_reason"] == "markdown_extraction_failed"
    assert metadata["quality_notice"] and metadata["ocr_used"] is False
    assert images == {}
    chunks = get_chunker("pdf-page").chunk(text, metadata, {"max_tokens": 500})
    assert [chunk.metadata["page"] for chunk in chunks] == [1, 2]
    assert all(chunk.metadata["quality"] == "native_text_only" for chunk in chunks)
    assert all(chunk.metadata["actual_parser"] == "pymupdf-native-text" for chunk in chunks)
    assert "Alpha" in chunks[0].content and "Bravo" in chunks[1].content


def test_missing_markdown_library_uses_installed_native_text_reader(monkeypatch):
    monkeypatch.setattr(ParserRegistry, "_get_docling_parser", classmethod(lambda cls: None))
    monkeypatch.setitem(sys.modules, "pymupdf4llm", None)
    text, metadata, _ = extract_text("synthetic.pdf", digital_pdf())
    assert "Alpha" in text and "Bravo" in text
    assert metadata["actual_parser"] == "pymupdf-native-text"
    assert metadata["pdf_text_fallback_reason"] == "markdown_extractor_unavailable"
    assert metadata["page_count"] == 2


@pytest.mark.parametrize("entries", [[{"text": "Alpha"}], [{"text": "Alpha"}] * 3])
def test_markdown_page_mismatch_recovers_real_physical_pages(monkeypatch, entries):
    monkeypatch.setattr(ParserRegistry, "_get_docling_parser", classmethod(lambda cls: None))
    monkeypatch.setitem(sys.modules, "pymupdf4llm", SimpleNamespace(to_markdown=lambda *a, **k: entries))
    text, metadata, _ = extract_text("synthetic.pdf", digital_pdf())
    assert "Alpha" in text and "Bravo" in text
    assert metadata["page_count"] == 2
    assert metadata["actual_parser"] == "pymupdf-native-text"
    assert metadata["pdf_text_fallback_reason"] == "markdown_page_count_mismatch"


def test_blank_native_recovery_still_requires_real_ocr(monkeypatch):
    monkeypatch.setattr(ParserRegistry, "_get_docling_parser", classmethod(lambda cls: None))
    monkeypatch.setitem(sys.modules, "pymupdf4llm", SimpleNamespace(to_markdown=fail_markdown))
    from anila_core.ingestion import local_ocr

    monkeypatch.setattr(local_ocr.shutil, "which", lambda name: None)
    with fitz.open() as source:
        source.new_page().insert_text((40, 50), "Synthetic raster only")
        pixels = source[0].get_pixmap().tobytes("png")
    with fitz.open() as scanned:
        page = scanned.new_page()
        page.insert_image(page.rect, stream=pixels)
        data = scanned.tobytes()
    with pytest.raises(ParseError) as exc:
        extract_text("synthetic-scan.pdf", data)
    assert exc.value.details["reason"] == "offline_ocr_missing"


@pytest.mark.parametrize("missing_page", ["blank", "marker"])
def test_partial_text_layer_cannot_report_complete_native_recovery(monkeypatch, missing_page):
    monkeypatch.setattr(ParserRegistry, "_get_docling_parser", classmethod(lambda cls: None))
    monkeypatch.setitem(sys.modules, "pymupdf4llm", SimpleNamespace(to_markdown=fail_markdown))
    from anila_core.ingestion import local_ocr

    monkeypatch.setattr(local_ocr.shutil, "which", lambda name: None)
    with fitz.open(stream=digital_pdf(), filetype="pdf") as pdf:
        page = pdf.new_page()
        if missing_page == "marker":
            page.insert_text((40, 50), "Page 3 of 3")
        data = pdf.tobytes()
    with pytest.raises(ParseError) as exc:
        extract_text("partial-text-layer.pdf", data)
    assert exc.value.details["reason"] == "offline_ocr_missing"


def test_native_page_limit_refuses_before_page_iteration():
    class Oversize:
        page_count = PdfParser._NATIVE_MAX_PAGES + 1

        def __iter__(self):
            raise AssertionError("oversize document must not iterate pages")

    with pytest.raises(ParseError) as exc:
        PdfParser._native_text_pages(Oversize())
    assert exc.value.code == "E_PARSE_TOO_LARGE"
    assert exc.value.details["reason"] == "native_pdf_page_limit"


@pytest.mark.parametrize("cap", ["_NATIVE_MAX_PAGE_BYTES", "_NATIVE_MAX_TEXT_BYTES"])
def test_native_text_limits_stop_before_reading_subsequent_page(monkeypatch, cap):
    calls = []

    class Page:
        def get_text(self, **kwargs):
            calls.append(True)
            return "界" * 5  # 15 UTF-8 bytes, not five bytes.

    class Document:
        page_count = 2

        def __iter__(self):
            return iter([Page(), Page()])

    monkeypatch.setattr(PdfParser, cap, 14)
    with pytest.raises(ParseError) as exc:
        PdfParser._native_text_pages(Document())
    assert exc.value.code == "E_PARSE_TOO_LARGE"
    assert exc.value.details["reason"] == "native_pdf_text_limit"
    assert exc.value.details["page"] == 1
    assert len(calls) == 1


def test_invalid_markdown_output_uses_native_text(monkeypatch):
    monkeypatch.setattr(ParserRegistry, "_get_docling_parser", classmethod(lambda cls: None))
    monkeypatch.setitem(sys.modules, "pymupdf4llm", SimpleNamespace(to_markdown=lambda *a, **k: None))
    text, metadata, _ = extract_text("synthetic.pdf", digital_pdf())
    assert "Alpha" in text and "Bravo" in text
    assert metadata["pdf_text_fallback_reason"] == "markdown_invalid_output"


@pytest.mark.parametrize("body", ["", "[[IMAGE:unreadable]]\nPage 1"])
def test_marker_only_markdown_cannot_replace_real_native_text(monkeypatch, body):
    monkeypatch.setattr(ParserRegistry, "_get_docling_parser", classmethod(lambda cls: None))
    monkeypatch.setitem(sys.modules, "pymupdf4llm", SimpleNamespace(
        to_markdown=lambda *a, **k: [{"text": body}] * 2,
    ))
    text, metadata, _ = extract_text("synthetic.pdf", digital_pdf())
    assert "Alpha" in text and "Bravo" in text
    assert metadata["pdf_text_fallback_reason"] == "markdown_empty_output"


def test_real_traditional_chinese_text_layer_is_recovered_without_ocr(monkeypatch):
    monkeypatch.setattr(ParserRegistry, "_get_docling_parser", classmethod(lambda cls: None))
    monkeypatch.setitem(sys.modules, "pymupdf4llm", None)
    body = (
        "差旅核銷規定：交通費及住宿費應附收據，核銷申請由單位主管審核。"
        "採購程序須保留簽核文件及驗收紀錄，檢查設備規格及交貨項目。"
    ) * 3
    with fitz.open() as pdf:
        # The existing PyMuPDF distribution supplies this built-in CJK font.
        result = pdf.new_page().insert_textbox(
            fitz.Rect(45, 45, 540, 700), body, fontsize=12, fontname="china-t",
        )
        assert result >= 0
        data = pdf.tobytes()
    text, metadata, _ = extract_text("synthetic-traditional-chinese.pdf", data)
    assert "差旅核銷規定" in text and "單位主管審核" in text
    assert "驗收紀錄" in text and "交貨項目" in text
    assert metadata["actual_parser"] == "pymupdf-native-text"
    assert metadata["ocr_used"] is False
