"""Docling parser backend (IBM Research, Apache-2.0, US origin).

Docling is a layout-aware document converter that handles PDF / DOCX /
PPTX / XLSX / HTML in one pipeline. Compared to the ``native`` parsers
(pymupdf4llm + python-docx + odfpy), Docling adds:

* Layout-aware reading order (multi-column scientific PDFs)
* Table-structure recovery (header rows, merged cells)
* Built-in EasyOCR for scanned / font-subsetted PDFs
* Optional picture description via IBM Granite-Vision-3.2-2B
  (``do_picture_description=True``)

Trade-offs:

* First run downloads ~1 GB of model weights (layout model + EasyOCR;
  Granite Vision adds ~3 GB more if enabled).
* CPU inference is noticeably slower than the native parsers.
* Output goes through ``DoclingDocument.export_to_markdown()`` so the
  exact whitespace / heading shape will differ from native output.

This file deliberately keeps the heavy ``docling`` import lazy so the
module is safe to import even when the optional ``[docling]`` extra is
not installed. ``build_docling_parser_from_env`` returns ``None`` when
``DOC_PARSER != docling`` so the registry can quietly fall back to the
native parser stack.
"""
from __future__ import annotations

import io
import logging
import os
from pathlib import Path
from typing import Any, Optional

from .parser_registry import ImageRef, ParsedDocument, _new_image_id

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# DoclingParser
# ──────────────────────────────────────────────────────────────────────

# Extensions we let Docling handle when DOC_PARSER=docling. .doc and .odt
# fall through to the native parsers because Docling does not support them.
DOCLING_SUPPORTED_EXTS: frozenset[str] = frozenset(
    {".pdf", ".docx", ".pptx", ".xlsx", ".html", ".htm", ".md"}
)


class DoclingParser:
    """Layout-aware parser backed by Docling.

    Constructed once and reused — the underlying ``DocumentConverter``
    is itself lazy: it only loads model weights on the first call to
    ``convert()``. This means importing the parser does not pay any
    model-loading cost; the first parsed document does.
    """

    def __init__(
        self,
        *,
        ocr_languages: Optional[list[str]] = None,
        enable_picture_description: bool = False,
        do_table_structure: bool = True,
    ) -> None:
        self._ocr_languages: list[str] = ocr_languages or ["ch_tra", "en"]
        self._enable_pic_desc: bool = enable_picture_description
        self._do_table_structure: bool = do_table_structure
        self._converter: Any = None  # docling.DocumentConverter, lazy

    # -- lazy converter construction -------------------------------------------------

    def _ensure_converter(self) -> Any:
        if self._converter is not None:
            return self._converter
        try:
            from docling.datamodel.base_models import InputFormat  # type: ignore[import]
            from docling.datamodel.pipeline_options import (  # type: ignore[import]
                EasyOcrOptions,
                PdfPipelineOptions,
            )
            from docling.document_converter import (  # type: ignore[import]
                DocumentConverter,
                PdfFormatOption,
            )
        except ImportError as exc:
            raise ImportError(
                "Docling backend needs the 'docling' package. "
                "Install with: pip install 'agentic-rag[docling]'"
            ) from exc

        pdf_pipeline = PdfPipelineOptions(
            do_ocr=True,
            do_table_structure=self._do_table_structure,
            do_picture_description=self._enable_pic_desc,
            ocr_options=EasyOcrOptions(lang=self._ocr_languages),
        )
        if self._enable_pic_desc:
            # IBM Granite-Vision-3.2-2B for non-China image captions.
            try:
                from docling.datamodel.pipeline_options import (  # type: ignore[import]
                    granite_picture_description,
                )
                pdf_pipeline.picture_description_options = granite_picture_description
            except ImportError:
                logger.warning(
                    "Docling installed without granite_picture_description preset — "
                    "falling back to docling default. Update docling to ≥2.0 for Granite."
                )

        self._converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_pipeline),
            },
        )
        logger.info(
            "Docling DocumentConverter ready (ocr_langs=%s, pic_desc=%s, tables=%s)",
            self._ocr_languages, self._enable_pic_desc, self._do_table_structure,
        )
        return self._converter

    # -- DocumentParser protocol -----------------------------------------------------

    def parse(self, file_path: str) -> ParsedDocument:
        path = Path(file_path)
        ext = path.suffix.lower()
        if ext not in DOCLING_SUPPORTED_EXTS:
            raise ValueError(
                f"DoclingParser does not support extension '{ext}'. "
                f"Supported: {sorted(DOCLING_SUPPORTED_EXTS)}"
            )

        converter = self._ensure_converter()
        result = converter.convert(file_path)
        document = result.document

        content = _safe_export_markdown(document)
        images, captions = _collect_pictures(document)
        if images:
            placeholders = "\n\n".join(f"[[IMAGE:{img_id}]]" for img_id in images)
            content = f"{content}\n\n{placeholders}".strip()

        title = _safe_title(document, fallback=path.stem)
        page_count = _safe_page_count(document)
        ocr_used = _safe_ocr_flag(result)

        return ParsedDocument(
            content=content,
            metadata={
                "title": title,
                "pages": page_count,
                "embedded_images": len(images),
                "ocr_used": ocr_used,
                "parser": "docling",
                "picture_descriptions": captions,
            },
            source_path=file_path,
            format=ext.lstrip("."),
            images=images,
        )


# ──────────────────────────────────────────────────────────────────────
# DoclingDocument → ParsedDocument helpers
#
# All helpers swallow AttributeError + version-specific surprises and
# return a sensible default. Docling's data model has churned across
# versions so we depend only on the most stable surface area.
# ──────────────────────────────────────────────────────────────────────

def _safe_export_markdown(document: Any) -> str:
    try:
        markdown = document.export_to_markdown()
    except Exception as exc:  # pragma: no cover - safety net for API drift
        logger.warning("DoclingDocument.export_to_markdown failed: %s", exc)
        return ""
    return markdown.strip() if isinstance(markdown, str) else ""


def _collect_pictures(document: Any) -> tuple[dict[str, ImageRef], dict[str, str]]:
    """Walk ``document.pictures`` and copy bytes + captions into our shape."""
    images: dict[str, ImageRef] = {}
    captions: dict[str, str] = {}
    pictures = getattr(document, "pictures", None) or []
    for picture in pictures:
        try:
            png_bytes = _picture_to_png(picture, document)
        except Exception as exc:
            logger.warning("Skip docling picture: %s", exc)
            continue
        if not png_bytes:
            continue
        img_id = _new_image_id()
        page = _picture_page(picture)
        caption = _picture_caption(picture, document)
        images[img_id] = ImageRef(
            image_id=img_id,
            image_bytes=png_bytes,
            mime="image/png",
            page=page,
            caption=caption,
        )
        if caption:
            captions[img_id] = caption
    return images, captions


def _picture_to_png(picture: Any, document: Any) -> bytes | None:
    """Extract PNG bytes from a Docling picture across API variants."""
    pil_image = None
    # Newer API: picture.image.pil_image
    image_obj = getattr(picture, "image", None)
    if image_obj is not None:
        pil_image = getattr(image_obj, "pil_image", None) or image_obj
    # Older API: picture.get_image(document)
    if pil_image is None and hasattr(picture, "get_image"):
        try:
            pil_image = picture.get_image(document)
        except TypeError:
            pil_image = picture.get_image()  # type: ignore[call-arg]
    if pil_image is None:
        return None

    buffer = io.BytesIO()
    pil_image.save(buffer, format="PNG")
    return buffer.getvalue()


def _picture_page(picture: Any) -> int | None:
    prov = getattr(picture, "prov", None) or []
    if not prov:
        return None
    first = prov[0]
    return getattr(first, "page_no", None) or getattr(first, "page", None)


def _picture_caption(picture: Any, document: Any) -> str:
    caption = getattr(picture, "caption_text", None)
    if callable(caption):
        try:
            text = caption(document)
        except TypeError:
            text = caption()  # type: ignore[call-arg]
        except Exception:
            text = ""
        return (text or "").strip()
    return ""


def _safe_title(document: Any, fallback: str) -> str:
    title = getattr(document, "title", None) or getattr(document, "name", None)
    if isinstance(title, str) and title.strip():
        return title.strip()
    return fallback


def _safe_page_count(document: Any) -> int:
    pages = getattr(document, "pages", None)
    if pages is None:
        return 0
    try:
        return len(pages)
    except TypeError:
        return 0


def _safe_ocr_flag(result: Any) -> bool:
    """Best-effort detection that OCR fired during conversion."""
    timings = getattr(result, "timings", None) or {}
    if isinstance(timings, dict):
        for key in timings:
            if "ocr" in str(key).lower():
                return True
    return False


# ──────────────────────────────────────────────────────────────────────
# Env-driven factory
# ──────────────────────────────────────────────────────────────────────

def build_docling_parser_from_env() -> Optional[DoclingParser]:
    """Construct a DoclingParser when ``DOC_PARSER=docling``, else None.

    Env:
      DOC_PARSER                = "native" | "docling"        (default: native)
      DOCLING_OCR_LANGS         = comma-separated EasyOCR lang codes
                                  (default: "ch_tra,en")
      DOCLING_PICTURE_DESCRIPTION = "true" | "false"          (default: false)
                                  Pulls ~3 GB extra weights on first use.
      DOCLING_TABLE_STRUCTURE   = "true" | "false"            (default: true)
    """
    if os.getenv("DOC_PARSER", "native").lower() != "docling":
        return None
    langs = [
        s.strip()
        for s in os.getenv("DOCLING_OCR_LANGS", "ch_tra,en").split(",")
        if s.strip()
    ]
    return DoclingParser(
        ocr_languages=langs,
        enable_picture_description=os.getenv(
            "DOCLING_PICTURE_DESCRIPTION", "false"
        ).lower() == "true",
        do_table_structure=os.getenv(
            "DOCLING_TABLE_STRUCTURE", "true"
        ).lower() == "true",
    )
