"""Bounded, offline inventory of this process's local ingestion prerequisites."""

from __future__ import annotations

import importlib.util
import os
import selectors
import shutil
import signal
import subprocess
import time
import threading
from copy import deepcopy
from datetime import datetime, timezone

PROBE_TIMEOUT_SECONDS = 2.0
PROBE_OUTPUT_LIMIT = 16 * 1024
CACHE_TTL_SECONDS = 5
_CACHE_LOCK = threading.Lock()
_CACHE_REPORT: dict | None = None
_CACHE_DEADLINE = 0.0
_TOOLS = {
    "tesseract": (("tesseract",), "--list-langs"),
    "libreoffice": (("libreoffice", "soffice"), "--version"),
}
_MODULES = {
    "pdf": ("PDF 文字解析", (("fitz", "pymupdf"), ("pymupdf4llm", "pymupdf4llm"))),
    "image": ("圖片與掃描頁載入", (("fitz", "pymupdf"),)),
    "docx": ("Word 文字與表格解析", (("docx", "python-docx"), ("lxml", "lxml"))),
    "odt": ("ODT 文字解析", (("odf", "odfpy"),)),
}


class _ProbeFailure(Exception):
    def __init__(self, kind: str):
        self.kind = kind


def _probe_tool(tool: str) -> str:
    """Only fixed inventory commands; return bounded output for local classification."""
    binaries, flag = _TOOLS[tool]
    binary = next((path for name in binaries if (path := shutil.which(name))), None)
    if binary is None:
        raise _ProbeFailure("missing")
    try:
        process = subprocess.Popen(
            [binary, flag],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=False,
            start_new_session=True,
        )
    except OSError as exc:
        raise _ProbeFailure("unavailable") from exc
    output = bytearray()
    deadline = time.monotonic() + PROBE_TIMEOUT_SECONDS
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            eof = False
            while not eof:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _ProbeFailure("timeout")
                for key, _ in selector.select(remaining):
                    chunk = os.read(
                        key.fd, min(4096, PROBE_OUTPUT_LIMIT + 1 - len(output))
                    )
                    if not chunk:
                        eof = True
                        break
                    output.extend(chunk)
                    if len(output) > PROBE_OUTPUT_LIMIT:
                        raise _ProbeFailure("probe_output_limit")
            try:
                exit_code = process.wait(
                    timeout=max(0.001, deadline - time.monotonic())
                )
            except subprocess.TimeoutExpired as exc:
                raise _ProbeFailure("timeout") from exc
        if exit_code:
            raise _ProbeFailure("probe_failed")
        return output.decode("utf-8", errors="replace")
    except OSError as exc:
        raise _ProbeFailure("unavailable") from exc
    finally:
        # The leader may have exited while a descendant still owns stdout.
        # start_new_session makes this PGID exclusive to this probe.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            if process.poll() is None:
                process.kill()
        process.wait(timeout=1)
        if process.stdout is not None:
            process.stdout.close()


def _check(check_id, label, *, kind="ready", message, action="", missing=()):
    return {
        "id": check_id,
        "label": label,
        "status": "ready" if kind == "ready" else "blocked",
        "type": kind,
        "message": message,
        "action": action,
        "missing": list(missing),
    }


def _module_checks() -> list[dict]:
    checks = []
    for check_id, (label, modules) in _MODULES.items():
        missing = []
        for module, package in modules:
            # All names are fixed and top-level; finding a spec cannot import a parent.
            try:
                present = importlib.util.find_spec(module) is not None
            except (ImportError, ValueError, AttributeError, OSError):
                present = False
            if not present:
                missing.append(package)
        checks.append(
            _check(
                check_id,
                label,
                kind="parser_dependency_missing" if missing else "ready",
                message="缺少必要的本機解析套件。" if missing else "已找到必要套件的中繼資料；尚未執行套件初始化或文件解析。",
                action="請管理員以經來源審核的離線交付套件補齊依賴，再重新檢查。" if missing else "",
                missing=missing,
            )
        )
    return checks


def _binary_check(tool: str) -> dict:
    ocr = tool == "tesseract"
    label = "離線 OCR 與繁中／英文語料" if ocr else "舊版 Office 離線轉檔"
    prefix = "offline_ocr" if ocr else "office_converter"
    action = (
        "請管理員提供經來源審核的 Tesseract、chi_tra 與 eng 離線語料，再重新檢查。"
        if ocr
        else "請管理員提供經來源審核的 LibreOffice 離線工具；也可先另存 DOCX／PPTX 後上傳。"
    )
    try:
        output = _probe_tool(tool)
    except _ProbeFailure as exc:
        messages = {
            "missing": "本機尚未提供 Tesseract 離線 OCR。"
            if ocr
            else "本機尚未提供 LibreOffice 離線轉檔工具。",
            "unavailable": "本機工具目前無法啟動。",
            "timeout": "本機工具檢查逾時，請通知管理員。",
            "probe_failed": "本機工具檢查失敗，請通知管理員確認工具與語料。",
            "probe_output_limit": "本機工具回覆超過檢查上限，請通知管理員確認工具。",
        }
        return _check(
            tool,
            label,
            kind=f"{prefix}_{exc.kind}",
            message=messages[exc.kind],
            action=action,
            missing=[tool] if exc.kind == "missing" else [],
        )
    if ocr:
        # Tesseract lists one language per line; do not expose the header's path
        # or additional installed language names.
        languages = {line.strip() for line in output.splitlines()}
        missing = sorted({"chi_tra", "eng"} - languages)
        if missing:
            return _check(
                tool,
                label,
                kind="offline_ocr_languages_missing",
                message="離線 OCR 缺少必要語料：" + "、".join(missing) + "。",
                action=action,
                missing=missing,
            )
        message = "本機 Tesseract 可列出 chi_tra 與 eng 語料；尚未執行圖片辨識。"
    else:
        if not any(name in output.lower() for name in ("libreoffice", "lodev")):
            return _check(
                tool,
                label,
                kind="office_converter_probe_failed",
                message="本機轉檔工具未回覆可辨識的檢查結果。",
                action=action,
            )
        message = "本機 LibreOffice 可回覆工具檢查；尚未執行文件轉檔。"
    return _check(tool, label, message=message)


_FORMATS = (
    (
        "text",
        "純文字與 Markdown",
        ("txt", "md", "markdown", "csv", "json", "html", "htm"),
        ("source_code",),
        "依檔案內容檢查文字編碼；未檢查個別檔案。",
    ),
    (
        "source_code",
        "Python 與 C／C++ 原始碼",
        ("py", "c", "cpp", "cc", "cxx", "c++", "h", "hpp"),
        ("source_code",),
        "以靜態文字解析，不執行原始碼。",
    ),
    ("pdf_text", "PDF 文字", ("pdf",), ("pdf",), "僅檢查本機文字解析依賴；複雜版面仍需核對原檔。"),
    (
        "pdf_scanned",
        "掃描 PDF 離線辨識",
        ("pdf",),
        ("pdf", "image", "tesseract"),
        "需本機 OCR 與繁中／英文語料；未測辨識品質。",
    ),
    ("docx", "Word 文字與表格", ("docx",), ("docx",), "文件中的圖片辨識另需圖片與 OCR 檢查通過。"),
    (
        "pptx",
        "PowerPoint 文字與表格",
        ("pptx",),
        ("presentation",),
        "圖表、SmartArt 與圖片復原不在此文字能力保證內。",
    ),
    (
        "legacy_word",
        "舊版 Word 與 RTF",
        ("doc", "rtf"),
        ("libreoffice", "docx"),
        "轉檔後仍須個別檔案驗證；未開啟文件或巨集。",
    ),
    (
        "legacy_presentation",
        "舊版 PowerPoint",
        ("ppt",),
        ("libreoffice", "presentation"),
        "轉檔後使用本機 PPTX 解析；未驗證圖表或 SmartArt。",
    ),
    ("odt", "ODT 文字", ("odt",), ("odt",), "僅檢查本機解析套件中繼資料。"),
    (
        "image_ocr",
        "圖片文字離線辨識",
        ("png", "jpg", "jpeg", "webp", "gif", "bmp"),
        ("image", "tesseract"),
        "僅檢查必要工具與語料；未測圖片辨識品質。",
    ),
)


def _diagnose_local_ingestion() -> dict:
    """Inspect fixed prerequisites only; no uploads, URLs, installation, or remote calls."""
    checks = _module_checks()
    checks.extend(
        [
            _check(
                "presentation", "簡報內建文字解析", message="使用平台內建 ZIP／XML 文字解析；尚未讀取簡報或驗證複雜物件。"
            ),
            _check("source_code", "文字與原始碼靜態解析", message="使用平台內建靜態解析；不執行任何上傳程式碼。"),
            _binary_check("libreoffice"),
            _binary_check("tesseract"),
        ]
    )
    ready = {check["id"]: check["status"] == "ready" for check in checks}
    return {
        "schema_version": 1,
        "scope": "current_process",
        "status": "ready" if all(ready.values()) else "attention",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
        "message": "僅診斷目前處理程序的本機依賴；未檢查 ingestion worker、遠端 Docling、正式映像或實際文件品質。",
        "checks": checks,
        "formats": [
            {
                "id": check_id,
                "label": label,
                "extensions": list(extensions),
                "status": "ready"
                if all(ready[item] for item in requires)
                else "blocked",
                "requires": list(requires),
                "message": message,
            }
            for check_id, label, extensions, requires, message in _FORMATS
        ],
        "not_checked": ["ingestion_worker", "remote_docling", "production_image"],
    }


def diagnose_local_ingestion() -> dict:
    """Single-flight per process; a short cache bounds repeated admin probes."""
    global _CACHE_REPORT, _CACHE_DEADLINE
    with _CACHE_LOCK:
        if _CACHE_REPORT is None or time.monotonic() >= _CACHE_DEADLINE:
            report = _diagnose_local_ingestion()
            _CACHE_REPORT = deepcopy(report)
            _CACHE_DEADLINE = time.monotonic() + CACHE_TTL_SECONDS
        return deepcopy(_CACHE_REPORT)
