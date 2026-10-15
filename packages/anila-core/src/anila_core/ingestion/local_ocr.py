"""Independent offline OCR adapter for a pre-provisioned Tesseract binary."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .errors import ParseError


def _binary() -> str:
    binary = shutil.which("tesseract")
    if binary is None:
        raise ParseError.bad_config(
            "目前沒有可用的離線文字辨識工具。請管理員提供經來源審核的 Tesseract 與繁體中文 chi_tra 語料，或恢復 Docling 服務後重試。",
            details={
                "reason": "offline_ocr_missing",
                "required_binary": "tesseract",
                "required_languages": ["chi_tra", "eng"],
            },
        )
    try:
        result = subprocess.run(
            [binary, "--list-langs"], capture_output=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ParseError.bad_config(
            "離線文字辨識工具無法啟動，請通知管理員。",
            details={"reason": "offline_ocr_unavailable"},
        ) from exc
    languages = result.stdout.decode("utf-8", errors="replace").split()
    if result.returncode or not {"chi_tra", "eng"}.issubset(languages):
        raise ParseError.bad_config(
            "離線文字辨識缺少繁體中文 chi_tra 或英文 eng 語料，請通知管理員補齊已審核的語料。",
            details={
                "reason": "offline_ocr_languages_missing",
                "required_languages": ["chi_tra", "eng"],
            },
        )
    return binary


def _recognize(binary: str, image: Path, output: Path, timeout: float) -> str:
    try:
        result = subprocess.run(
            [binary, str(image), str(output), "-l", "chi_tra+eng", "--psm", "3"],
            capture_output=True,
            timeout=max(0.1, min(45, timeout)),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ParseError.bad_config(
            "離線文字辨識超過時間限制，請分成較小的檔案重試。",
            details={"reason": "offline_ocr_timeout"},
        ) from exc
    except OSError as exc:
        raise ParseError.bad_config(
            "離線文字辨識工具無法執行，請通知管理員。",
            details={"reason": "offline_ocr_unavailable"},
        ) from exc
    result_path = output.with_suffix(".txt")
    if result.returncode or not result_path.is_file():
        raise ParseError.corrupt(
            "離線文字辨識無法讀取此圖片，請確認檔案可正常開啟。",
            details={"reason": "offline_ocr_failed"},
        )
    return result_path.read_text(encoding="utf-8").replace("\f", "\n").strip()


def image_text(image_bytes: bytes) -> str:
    import fitz

    binary = _binary()
    with tempfile.TemporaryDirectory(prefix="anila-local-ocr-") as work:
        root = Path(work)
        with fitz.open(stream=image_bytes) as image:
            with fitz.open("pdf", image.convert_to_pdf()) as pdf:
                page = pdf[0]
                scale = min(2, 8000 / max(page.rect.width, page.rect.height, 1))
                pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                pix.save(str(root / "image.png"))
        return _recognize(binary, root / "image.png", root / "recognized", 45)


def pdf_document(file_path: str) -> Any:
    import fitz
    from .parser_registry import ParsedDocument

    binary = _binary()
    deadline = time.monotonic() + 180
    pages: list[str] = []
    with fitz.open(file_path) as pdf, tempfile.TemporaryDirectory(prefix="anila-pdf-ocr-") as work:
        if pdf.is_encrypted:
            raise ParseError.corrupt(
                "此 PDF 受到密碼保護，請先解除密碼後再上傳。", details={"reason": "encrypted_pdf"}
            )
        if pdf.page_count > 500:
            raise ParseError.too_large("掃描文件超過 500 頁，請分成較小的檔案上傳。")
        for index, page in enumerate(pdf):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ParseError.bad_config(
                    "離線文字辨識超過時間限制，請分成較小的檔案重試。",
                    details={"reason": "offline_ocr_timeout"},
                )
            root = Path(work)
            scale = min(200 / 72, 8000 / max(page.rect.width, page.rect.height, 1))
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            image = root / f"page-{index}.png"
            pix.save(str(image))
            pages.append(_recognize(binary, image, root / f"text-{index}", remaining))
            image.unlink()
    return ParsedDocument(
        content="\f".join(pages),
        source_path=file_path,
        format="pdf",
        metadata={
            "title": Path(file_path).stem,
            "pages": len(pages),
            "ocr_used": True,
            "parser": "tesseract-local",
            "actual_parser": "tesseract-local",
            "ocr_languages": ["chi_tra", "eng"],
            "quality": "ocr_text_only",
            "quality_notice": "離線文字辨識保留頁碼；複雜版面與表格可能需要核對原檔。",
        },
    )
