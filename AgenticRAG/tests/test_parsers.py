"""Tests for document parsers (PlainTextParser, MarkdownParser, ParserRegistry)."""

from __future__ import annotations

import pytest
from pathlib import Path

from agentic_rag.ingestion.parsers import (
    ImageParser,
    MarkdownParser,
    ParserRegistry,
    PlainTextParser,
    RtfParser,
)


# ---------------------------------------------------------------------------
# PlainTextParser
# ---------------------------------------------------------------------------

def test_plain_text_parser_basic(tmp_path: Path):
    f = tmp_path / "hello.txt"
    f.write_text("Hello, world!")
    result = PlainTextParser().parse(str(f))
    assert result.content == "Hello, world!"
    assert result.format == "txt"
    assert result.source_path == str(f)


def test_plain_text_parser_metadata_has_title(tmp_path: Path):
    f = tmp_path / "my_doc.txt"
    f.write_text("content")
    result = PlainTextParser().parse(str(f))
    assert result.metadata.get("title") == "my_doc"


# ---------------------------------------------------------------------------
# MarkdownParser
# ---------------------------------------------------------------------------

def test_markdown_parser_extracts_title(tmp_path: Path):
    f = tmp_path / "guide.md"
    f.write_text("# Getting Started\n\nWelcome!\n")
    result = MarkdownParser().parse(str(f))
    assert result.metadata.get("title") == "Getting Started"
    assert result.format == "md"


def test_markdown_parser_no_heading_uses_stem(tmp_path: Path):
    f = tmp_path / "notes.md"
    f.write_text("Just some notes without a heading.\n")
    result = MarkdownParser().parse(str(f))
    assert result.metadata.get("title") == "notes"


def test_markdown_parser_preserves_content(tmp_path: Path):
    content = "# Title\n\n## Section\n\nParagraph text.\n"
    f = tmp_path / "doc.md"
    f.write_text(content)
    result = MarkdownParser().parse(str(f))
    assert "## Section" in result.content


# ---------------------------------------------------------------------------
# ParserRegistry
# ---------------------------------------------------------------------------

def test_registry_selects_txt_parser(tmp_path: Path):
    f = tmp_path / "a.txt"
    f.write_text("text")
    result = ParserRegistry.parse(str(f))
    assert result.format == "txt"


def test_registry_selects_md_parser(tmp_path: Path):
    f = tmp_path / "a.md"
    f.write_text("# Title\n")
    result = ParserRegistry.parse(str(f))
    assert result.format == "md"


def test_registry_raises_for_unknown_extension(tmp_path: Path):
    f = tmp_path / "archive.xyz"
    f.write_text("data")
    with pytest.raises(ValueError, match="Unsupported file extension"):
        ParserRegistry.parse(str(f))


def test_registry_supported_extensions():
    exts = ParserRegistry.supported_extensions()
    for ext in [".txt", ".md", ".rtf", ".pdf", ".docx", ".doc", ".odt",
                ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"]:
        assert ext in exts, f"missing {ext}"


# ---------------------------------------------------------------------------
# RtfParser
# ---------------------------------------------------------------------------

def test_rtf_parser_extracts_text(tmp_path: Path):
    # Minimal RTF with plain text "Hello RTF"
    rtf = r"{\rtf1\ansi\deff0 Hello RTF.}"
    f = tmp_path / "doc.rtf"
    f.write_text(rtf)
    result = RtfParser().parse(str(f))
    assert result.format == "rtf"
    assert "Hello RTF" in result.content
    assert result.metadata.get("title") == "doc"


# ---------------------------------------------------------------------------
# ImageParser
# ---------------------------------------------------------------------------

def test_image_parser_registers_image_ref(tmp_path: Path):
    # Smallest possible PNG (1x1 transparent)
    png_bytes = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
        b"\x1f\x15\xc4\x89"
        b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    f = tmp_path / "pic.png"
    f.write_bytes(png_bytes)
    result = ImageParser().parse(str(f))
    assert result.format == "image"
    assert "[[IMAGE:" in result.content
    assert len(result.images) == 1
    ref = next(iter(result.images.values()))
    assert ref.mime == "image/png"
    assert ref.image_bytes == png_bytes
