"""Render already approved text through Studio's fixed report templates; no LLM/RAG."""

from __future__ import annotations

import asyncio
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from app.schemas.quick_document import RenderQuickDocument
from app.schemas.report import ReportSection, ReportSpec
from app.services import report_renderer

MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
MEDIA_TYPES = {
    "html": "text/html",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
_MARKDOWN = re.compile(r"([\\`*_{}\[\]<>()!#+\-.|])")


async def render_document_bytes(request: RenderQuickDocument) -> bytes:
    # Content is data, not Markdown instructions or a Jinja template. Escaping
    # also stops Markdown images from causing Chromium/pandoc network fetches.
    body = _MARKDOWN.sub(r"\\\1", request.text)
    spec = ReportSpec(
        title=request.title.strip() or "助手草稿",
        preset=request.preset,
        tldr="本文件為依既有文件範本排版的助手草稿，內容由已核准的助手流程提供。",
        sections=[ReportSection(heading="草稿內容", content_markdown=body)],
    )
    html = report_renderer.render_html(spec)
    if request.format == "html":
        content = html.encode("utf-8")
    else:
        with TemporaryDirectory(prefix="anila-quick-document-") as directory:
            path = Path(directory) / ("document." + request.format)
            if request.format == "pdf":
                await report_renderer.write_pdf_file(html, path, block_network=True)
            else:
                await report_renderer.write_docx_file(html, path)
            if not path.is_file() or path.stat().st_size > MAX_DOCUMENT_BYTES:
                raise ValueError("文件未產生或超過大小上限")
            content = await asyncio.to_thread(path.read_bytes)
    if not content or len(content) > MAX_DOCUMENT_BYTES:
        raise ValueError("文件未產生或超過大小上限")
    return content
