"""Tests for document parsers (PlainTextParser, MarkdownParser, ParserRegistry)."""

from __future__ import annotations

import pytest
from pathlib import Path

from anila_core.ingestion.parsers import (
    MarkdownParser,
    ParserRegistry,
    PlainTextParser,
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
    assert ".txt" in exts
    assert ".md" in exts
    assert ".pdf" in exts
    assert ".docx" in exts
