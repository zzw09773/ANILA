"""Tests for ingestion.docling_parser — env switch, registry routing,
and DoclingDocument → ParsedDocument conversion.

Real Docling is heavy (~1 GB model download) so the real ``DocumentConverter``
is faked. We exercise:

* ``build_docling_parser_from_env`` honours ``DOC_PARSER``
* ``ParserRegistry.get`` swaps native parsers for Docling on supported exts
* ``ImportError`` surfaces when the extra is missing
* The ``DoclingDocument`` → ``ParsedDocument`` shim copies content,
  pictures, captions, and metadata into the existing schema.
"""
from __future__ import annotations

import io
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image

from agentic_rag.ingestion.docling_parser import (
    DOCLING_SUPPORTED_EXTS,
    DoclingParser,
    build_docling_parser_from_env,
)
from agentic_rag.ingestion.parsers import (
    DocxParser,
    ParserRegistry,
    PdfParser,
)


# ---------------------------------------------------------------------------
# Test isolation
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for k in (
        "DOC_PARSER",
        "DOCLING_OCR_LANGS",
        "DOCLING_PICTURE_DESCRIPTION",
        "DOCLING_TABLE_STRUCTURE",
    ):
        monkeypatch.delenv(k, raising=False)
    # Always start with a clean registry cache so previous tests don't bleed.
    ParserRegistry._reset_docling_cache()
    yield
    ParserRegistry._reset_docling_cache()


# ---------------------------------------------------------------------------
# build_docling_parser_from_env
# ---------------------------------------------------------------------------

def test_disabled_by_default():
    assert build_docling_parser_from_env() is None


def test_native_mode_explicit_returns_none(monkeypatch):
    monkeypatch.setenv("DOC_PARSER", "native")
    assert build_docling_parser_from_env() is None


def test_enabled_returns_docling_parser_with_default_langs(monkeypatch):
    monkeypatch.setenv("DOC_PARSER", "docling")
    parser = build_docling_parser_from_env()
    assert isinstance(parser, DoclingParser)
    assert parser._ocr_languages == ["ch_tra", "en"]
    assert parser._enable_pic_desc is False
    assert parser._do_table_structure is True


def test_enabled_with_custom_langs(monkeypatch):
    monkeypatch.setenv("DOC_PARSER", "docling")
    monkeypatch.setenv("DOCLING_OCR_LANGS", "ch_tra, en, ja")
    monkeypatch.setenv("DOCLING_PICTURE_DESCRIPTION", "true")
    monkeypatch.setenv("DOCLING_TABLE_STRUCTURE", "false")
    parser = build_docling_parser_from_env()
    assert parser._ocr_languages == ["ch_tra", "en", "ja"]
    assert parser._enable_pic_desc is True
    assert parser._do_table_structure is False


# ---------------------------------------------------------------------------
# Lazy converter construction surfaces a helpful ImportError
# ---------------------------------------------------------------------------

def test_ensure_converter_without_docling_raises_importerror(monkeypatch):
    """Without 'docling' installed, the lazy load must point at the extra."""
    # Simulate missing 'docling' package by stubbing it out of sys.modules.
    # `monkeypatch.setitem(... None)` makes Python raise ModuleNotFoundError
    # when the import statement runs.
    for name in (
        "docling",
        "docling.datamodel",
        "docling.datamodel.base_models",
        "docling.datamodel.pipeline_options",
        "docling.document_converter",
    ):
        monkeypatch.setitem(sys.modules, name, None)
    parser = DoclingParser()
    with pytest.raises(ImportError, match="agentic-rag\\[docling\\]"):
        parser._ensure_converter()


# ---------------------------------------------------------------------------
# ParserRegistry routing — Docling overrides native parsers when enabled
# ---------------------------------------------------------------------------

class _StubDocling:
    """Stand-in DoclingParser used to verify registry routing."""

    def __init__(self):
        self.parsed: list[str] = []

    def parse(self, file_path: str):  # pragma: no cover - never invoked here
        self.parsed.append(file_path)
        raise AssertionError("not expected to run during routing test")


def test_registry_uses_native_when_doc_parser_unset():
    assert isinstance(ParserRegistry.get("foo.pdf"), PdfParser)
    assert isinstance(ParserRegistry.get("foo.docx"), DocxParser)


def test_registry_routes_supported_exts_to_docling(monkeypatch):
    monkeypatch.setenv("DOC_PARSER", "docling")
    stub = _StubDocling()
    monkeypatch.setattr(
        "agentic_rag.ingestion.parsers.ParserRegistry._docling_parser", stub,
        raising=False,
    )
    monkeypatch.setattr(
        "agentic_rag.ingestion.parsers.ParserRegistry._docling_initialised", True,
        raising=False,
    )

    for ext in (".pdf", ".docx", ".html", ".md"):
        chosen = ParserRegistry.get(f"sample{ext}")
        assert chosen is stub, f"ext {ext} should route to Docling"


def test_registry_keeps_native_for_unsupported_exts(monkeypatch):
    """Docling does not handle .doc / .odt / images — those keep native."""
    monkeypatch.setenv("DOC_PARSER", "docling")
    stub = _StubDocling()
    monkeypatch.setattr(
        "agentic_rag.ingestion.parsers.ParserRegistry._docling_parser", stub,
        raising=False,
    )
    monkeypatch.setattr(
        "agentic_rag.ingestion.parsers.ParserRegistry._docling_initialised", True,
        raising=False,
    )

    # .doc + .odt are not in DOCLING_SUPPORTED_EXTS — must stay native.
    for ext in (".doc", ".odt", ".png", ".jpg", ".txt"):
        chosen = ParserRegistry.get(f"sample{ext}")
        assert chosen is not stub, f"ext {ext} must NOT route to Docling"


def test_registry_falls_back_when_docling_factory_returns_none(monkeypatch):
    """If DOC_PARSER=docling but the factory returns None, native is used."""
    monkeypatch.setenv("DOC_PARSER", "docling")

    def _none_factory():
        return None

    monkeypatch.setattr(
        "agentic_rag.ingestion.docling_parser.build_docling_parser_from_env",
        _none_factory,
    )
    chosen = ParserRegistry.get("foo.pdf")
    assert isinstance(chosen, PdfParser)


# ---------------------------------------------------------------------------
# DoclingDocument → ParsedDocument conversion (faked converter)
# ---------------------------------------------------------------------------

def _make_png_bytes(color: tuple[int, int, int] = (255, 0, 0)) -> bytes:
    img = Image.new("RGB", (4, 4), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _png_to_pil(png_bytes: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png_bytes))


def _fake_picture(pil_image: Image.Image, page: int = 1, caption: str = ""):
    """Build a stand-in for a docling PictureItem."""
    image_holder = SimpleNamespace(pil_image=pil_image)
    prov = [SimpleNamespace(page_no=page)]
    return SimpleNamespace(
        image=image_holder,
        prov=prov,
        caption_text=lambda doc: caption,
    )


def _fake_document(markdown: str, pictures: list[Any], pages: int = 2, title: str = ""):
    return SimpleNamespace(
        export_to_markdown=lambda: markdown,
        pictures=pictures,
        pages=list(range(pages)),
        title=title,
        name=None,
    )


def _fake_result(document: Any, ocr: bool = False) -> Any:
    timings = {"ocr": 1.23} if ocr else {}
    return SimpleNamespace(document=document, timings=timings)


def test_parse_converts_docling_document_to_parseddocument(tmp_path):
    parser = DoclingParser()
    pil = _png_to_pil(_make_png_bytes((10, 20, 30)))
    document = _fake_document(
        markdown="# Title\n\nBody paragraph.",
        pictures=[_fake_picture(pil, page=1, caption="diagram caption")],
        pages=3,
        title="Real Title",
    )
    converter = SimpleNamespace(convert=lambda fp: _fake_result(document, ocr=True))
    parser._converter = converter

    target = tmp_path / "doc.pdf"
    target.write_bytes(b"placeholder")

    result = parser.parse(str(target))

    assert "# Title" in result.content
    assert "Body paragraph." in result.content
    # Image placeholder appended to content
    assert result.content.count("[[IMAGE:") == 1
    # Single image extracted, caption stored
    assert len(result.images) == 1
    only_id = next(iter(result.images))
    assert result.images[only_id].caption == "diagram caption"
    assert result.images[only_id].page == 1
    assert result.images[only_id].mime == "image/png"
    # Metadata
    assert result.metadata["title"] == "Real Title"
    assert result.metadata["pages"] == 3
    assert result.metadata["embedded_images"] == 1
    assert result.metadata["ocr_used"] is True
    assert result.metadata["parser"] == "docling"
    assert result.metadata["picture_descriptions"] == {only_id: "diagram caption"}
    assert result.format == "pdf"


def test_parse_with_no_pictures(tmp_path):
    parser = DoclingParser()
    document = _fake_document("plain content", pictures=[])
    parser._converter = SimpleNamespace(convert=lambda fp: _fake_result(document))

    target = tmp_path / "x.docx"
    target.write_bytes(b"placeholder")
    result = parser.parse(str(target))

    assert result.content == "plain content"
    assert "[[IMAGE:" not in result.content
    assert result.images == {}
    assert result.metadata["embedded_images"] == 0
    assert result.metadata["ocr_used"] is False


def test_parse_rejects_unsupported_extension(tmp_path):
    parser = DoclingParser()
    target = tmp_path / "legacy.doc"
    target.write_bytes(b"x")
    with pytest.raises(ValueError, match="DoclingParser does not support"):
        parser.parse(str(target))


def test_supported_exts_constant_matches_registry_routing():
    """Sanity: the routing decision uses the same allow-list as the parser."""
    assert ".pdf" in DOCLING_SUPPORTED_EXTS
    assert ".docx" in DOCLING_SUPPORTED_EXTS
    assert ".doc" not in DOCLING_SUPPORTED_EXTS
    assert ".odt" not in DOCLING_SUPPORTED_EXTS
    assert ".png" not in DOCLING_SUPPORTED_EXTS
