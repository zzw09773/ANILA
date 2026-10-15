"""Send image text recognition through the registered Docling service."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from .errors import ParseError
from .text_quality import has_recovered_text


def recognize_image(image_bytes: bytes, parser: Any, metadata: dict[str, Any] | None = None) -> str:
    """Wrap image pixels as a local PDF supported by the existing wire API."""
    if metadata is None:
        metadata = {}
    if parser is None:
        return _local_image(image_bytes, metadata)
    try:
        content = _docling_image(image_bytes, parser)
        if not has_recovered_text(content):
            raise ValueError("empty image OCR")
        metadata["actual_parser"] = "docling-image"
        return content
    except Exception:
        return _local_image(image_bytes, metadata)


def _local_image(image_bytes: bytes, metadata: dict[str, Any]) -> str:
    from .local_ocr import image_text

    content = image_text(image_bytes)
    if not has_recovered_text(content):
        raise ParseError.corrupt(
            "圖片辨識後沒有可用文字，請確認圖片清晰且包含文字。",
            details={"reason": "empty_image_ocr_output"},
        )
    metadata["actual_parser"] = "tesseract-local"
    return content


def _docling_image(image_bytes: bytes, parser: Any) -> str:
    import fitz

    with fitz.open(stream=image_bytes) as image:
        pdf_bytes = image.convert_to_pdf()
    with tempfile.TemporaryDirectory(prefix="anila-image-ocr-") as work:
        pdf = Path(work) / "image.pdf"
        pdf.write_bytes(pdf_bytes)
        return str(parser.parse(str(pdf)).content).strip()


def require_image_ocr() -> Any:
    from .parser_registry import ParserRegistry

    try:
        return ParserRegistry._get_docling_parser()
    except Exception:
        return None
