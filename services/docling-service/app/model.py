"""Docling 轉換器,包 IBM docling(佈局感知文件解析)。

Layer 職責與 asr-decoder 的 model.py 相同:這是唯一 import docling 的地方,
import 全部延後到 ``load()``——沒有 docling 套件也能 import 本模組,且
platform host 上被誤載時不會在 import 期就拖半個 torch 進來。

轉換器以 ``(ocr_langs, table_structure, picture_description)`` 為鍵做快取,
因為 docling 的 PdfPipelineOptions 在建構時就釘死這三項,而 wire contract
每次請求都帶它們──改選項只能重建 converter(與 asr-decoder 換模型同理,
是「貴,但罕見」的那種)。每次 parse 仍只重建有異的鍵。
"""

from __future__ import annotations

import base64
import io
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def apply_offline_environment(local_files_only: bool) -> None:
    """把「離線」宣稱接上事實——設進 process env,蓋掉 docker 層的隱性下載。

    只靠 Dockerfile 的 ``HF_HUB_OFFLINE``/``TRANSFORMERS_OFFLINE`` 蓋不住
    EasyOCR:它以 url 直抓權重、不吃這兩個 HF env(實測 docling 2.120.1,
    artifacts_path=None + HF_HUB_OFFLINE=1 時 easyocr.Reader 仍收到
    download_enabled=True)。所以真正的離線開關是 ``local_files_only``:
    這裡把它寫進 env(管 layout model / tableformer 的 huggingface_hub 路徑),
    ``_build`` 再把它寫進 EasyOcrOptions.download_enabled(管 EasyOCR 路徑)。

    ⚠ 雙向對稱,不是「只有 True 才設」:Dockerfile 把這兩個 env 烘死成 1,
    若 False 分支什麼都不做,``local_files_only=0`` 會只開 EasyOCR 那一半、
    HF 端(layout/tableformer)仍被烘死的 1 鎖住——半線上狀態。所以 False 要
    明確寫 0,覆寫掉映像層的任何起始值。
    """
    value = "1" if local_files_only else "0"
    os.environ["HF_HUB_OFFLINE"] = value
    os.environ["TRANSFORMERS_OFFLINE"] = value


class _CacheEntry:
    def __init__(self, converter: Any, ready: bool = False) -> None:
        self.converter = converter
        self.ready = ready
        # 兩件事不能共用 ready 一個旗標(2026-08-18 實測抓出):
        #   ready          =「converter 建構且 _verify_ready 通過」→ 供 /health。
        #   converted_once =「真的轉出過一份文件」→ 供 has_completed_conversion。
        # load() 成功就把 ready=True;若 has_completed_conversion 也讀 ready,
        # 那「第一次 convert 的基礎設施失敗」會被誤判成「成功過一次之後的
        # 文件失敗」→ 503 vs 422 分錯。所以拆成兩個。
        self.converted_once = False


class DoclingConverter:
    """Layout-aware converter with per-options caching + lazy model load."""

    def __init__(
        self,
        *,
        artifacts_dir: str = "/var/anila/docling-artifacts",
        local_files_only: bool = True,
    ) -> None:
        self._artifacts_dir = artifacts_dir
        self._local_files_only = local_files_only
        self._lock = threading.Lock()
        self._cache: dict[tuple, _CacheEntry] = {}

    # -- lifecycle -----------------------------------------------------------------

    def load(self, ocr_langs: list[str], table_structure: bool, picture_description: bool) -> None:
        """建(或抓)對應選項的 converter。背景 thread 呼叫;重複呼叫 idempotent。

        ⚠ HIGH-A(R9):docling 的 DocumentConverter 建構本身**不**載權重、也不
        驗證 artifacts 路徑裡真有東西;真正載是第一次 convert()。所以 load()
        光「converter 建得起來」就標 ready,會讓 healthcheck 在權重目錄是空的
        情況下也回「model_ready: true」——那是 nginx -t 形狀的假綠(驗的是設定
        語法,不是「它做得到下一次轉換」)。所以 load() 額外呼叫 _verify_ready():
        - import easyocr(抓到缺 libxcb/libGL 這類 import 期失敗);
        - local_files_only 時,assert artifacts 路徑非空(抓到「空權重目錄」假綠)。
        任一步 raise → load 失敗 → _load_or_die 讓容器 exit,重啟策略接手,
        health 不會假綠。
        """
        key = self._key(ocr_langs, table_structure, picture_description)
        with self._lock:
            if key in self._cache:
                return
            try:
                converter = self._build(ocr_langs, table_structure, picture_description)
                self._verify_ready()
            except Exception:
                raise
            self._cache[key] = _CacheEntry(converter, ready=True)

    def _verify_ready(self) -> None:
        """開機檢查,只保證它明講的那兩件事——**不**宣稱「下一次 convert 做得到」
        (2026-08-18 的 InvalidCxxCompiler 是第一次 convert 才發生、這裡抓不到)。
        命中兩個已知假綠根因:
        1. easyocr import 失敗(缺 libxcb.so.1 / libGL / libglib 原生函式庫)。
        2. offline 模式下 artifacts 路徑不存在或為空(權重沒 fetch 就起服務)。
        """
        try:
            import easyocr  # noqa: F401  # import 它就驗證原生 lib 齊了
        except ImportError as exc:
            raise RuntimeError(
                f"easyocr import failed (missing native lib?): {exc}"
            ) from exc
        if self._local_files_only:
            artifacts = Path(self._artifacts_dir)
            if not artifacts.is_dir() or not any(artifacts.iterdir()):
                raise RuntimeError(
                    "DOCLING_LOCAL_FILES_ONLY=1 but the artifacts directory "
                    f"is empty or missing: {self._artifacts_dir}. "
                    "Run fetch-docling-weights.sh before starting."
                )

    def is_ready(self, ocr_langs: list[str], table_structure: bool, picture_description: bool) -> bool:
        key = self._key(ocr_langs, table_structure, picture_description)
        with self._lock:
            entry = self._cache.get(key)
        return bool(entry and entry.ready)

    def has_completed_conversion(
        self, ocr_langs: list[str], table_structure: bool, picture_description: bool
    ) -> bool:
        """**同一個 option-key** 曾成功轉出過一份文件?

        HIGH-B(R9):第一次 convert 前,docling 還沒真的載入 layout/tableformer/
        EasyOCR 模型——那階段的失敗幾乎都是基礎設施(缺權重、缺原生 lib、CUDA
        OOM),歸 5xx/可重試;一旦同 key 成功轉過一次,之後的失敗才是「這一份
        文件轉不動」,歸 422。main.py 靠這個旗標分開兩者。

        ⚠ 範圍必須跟失敗的範圍一致:判斷「這次失敗是基礎設施還是文件」時,
        問的要是**同一個 key**。(2026-08-18 稽核抓到此前用 any(...) 問全域,
        而 is_ready 是逐 key——picture_description=true 第一次進場、要另一份
        氣隙裡沒有的權重時,會因為前面另一把 key 被誤判「轉成功過」→ 422 →
        corrupt → 對使用者說「檔案可能損毀」。)
        """
        key = self._key(ocr_langs, table_structure, picture_description)
        with self._lock:
            entry = self._cache.get(key)
            return bool(entry and entry.converted_once)

    # -- conversion -----------------------------------------------------------------

    def convert(
        self,
        file_path: str,
        *,
        ocr_langs: list[str],
        table_structure: bool,
        picture_description: bool,
    ) -> dict:
        """回 wire contract 的 dict(markdown/title/page_count/ocr_applied/images)。"""
        key = self._key(ocr_langs, table_structure, picture_description)
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                entry = _CacheEntry(
                    self._build(ocr_langs, table_structure, picture_description),
                    ready=False,
                )
                self._cache[key] = entry
            converter = entry.converter

        result = converter.convert(file_path)
        document = result.document

        markdown = _safe_export_markdown(document)
        image_objs = _collect_pictures(document)
        images = [self._image_to_wire(img_id, png_bytes, page, caption)
                  for img_id, png_bytes, page, caption in image_objs]

        with self._lock:
            self._cache[key].ready = True
            self._cache[key].converted_once = True

        return {
            "markdown": markdown,
            "title": _safe_title(document, fallback=Path(file_path).stem),
            "page_count": _safe_page_count(document),
            "ocr_applied": _safe_ocr_flag(result),
            "images": images,
        }

    @staticmethod
    def _image_to_wire(img_id: str, png_bytes: bytes, page: int | None, caption: str) -> dict:
        return {
            "id": img_id,
            "page": page,
            "caption": caption,
            "png_b64": base64.b64encode(png_bytes).decode("ascii"),
        }

    # -- internals -----------------------------------------------------------------

    @staticmethod
    def _key(ocr_langs: list[str], table_structure: bool, picture_description: bool) -> tuple:
        return (
            tuple(sorted(lang for lang in ocr_langs)),
            bool(table_structure),
            bool(picture_description),
        )

    def _build(self, ocr_langs: list[str], table_structure: bool, picture_description: bool) -> Any:
        apply_offline_environment(self._local_files_only)
        try:
            from docling.datamodel.base_models import InputFormat
            from docling.datamodel.pipeline_options import EasyOcrOptions, PdfPipelineOptions
            from docling.document_converter import DocumentConverter, PdfFormatOption
        except ImportError as exc:
            raise ImportError(
                "docling-service needs the 'docling' package. "
                "Install it on the GPU host, not in the platform image."
            ) from exc

        # ⚠ artifacts_path 只在離線模式才設。docling 的 EasyOcrModel 見到
        # artifacts_path 就會把 download_enabled 壓成 False、model_storage_directory
        # 指到 <artifacts>/EasyOcr(easyocr_model.py 的
        # `if artifacts_path is not None and model_storage_directory is None:
        #   download_enabled = False`)——比 EasyOcrOptions.download_enabled 晚設。
        # 線上模式(local_files_only=False)若仍塞 artifacts_path,會讓這裡明示的
        # `download_enabled=not local_files_only`(True)被那個分支蓋掉:線上想
        # 下載、實際得到「downloads disabled」——正是 2026-08-17 實測到的那件。
        # 離線才給 artifacts_path(讀掛載的權重)、線上給 None(回 docling 預設下載)。
        pdf_pipeline = PdfPipelineOptions(
            artifacts_path=(self._artifacts_dir if self._local_files_only else None),
            do_ocr=True,
            do_table_structure=table_structure,
            do_picture_description=picture_description,
            # local_files_only → EasyOCR 不准自動下載。artifacts_path 存在時 docling
            # 已會 auto-off,但那是「副作用」不是「宣稱」——明示在選項上,宣稱與
            # 行為才一致(見 apply_offline_environment 的實測)。
            ocr_options=EasyOcrOptions(
                lang=ocr_langs,
                download_enabled=not self._local_files_only,
            ),
        )
        if picture_description:
            # IBM Granite-Vision-3.2-2B for non-China image captions.
            try:
                from docling.datamodel.pipeline_options import granite_picture_description
                pdf_pipeline.picture_description_options = granite_picture_description
            except ImportError:
                logger.warning(
                    "docling installed without granite_picture_description preset — "
                    "falling back to docling default. Update docling to ≥2.0 for Granite."
                )

        return DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pdf_pipeline),
            },
        )


# ──────────────────────────────────────────────────────────────────────
# DoclingDocument → wire helpers(與 in-process parser 同形;service 端
# 不回 ImageRef,直接出 wire 的 base64 image)。
# 全數吞掉 AttributeError / 版本差異,回 sensible 預設——docling data
# model 跨版 churn 過,只依賴最穩的表面。
# ──────────────────────────────────────────────────────────────────────


def _safe_export_markdown(document: Any) -> str:
    try:
        markdown = document.export_to_markdown()
    except Exception as exc:  # pragma: no cover - safety net for API drift
        logger.warning("DoclingDocument.export_to_markdown failed: %s", exc)
        return ""
    return markdown.strip() if isinstance(markdown, str) else ""


def _collect_pictures(document: Any) -> list[tuple[str, bytes, int | None, str]]:
    """Walk ``document.pictures`` → list of (id, png_bytes, page, caption)."""
    out: list[tuple[str, bytes, int | None, str]] = []
    pictures = getattr(document, "pictures", None) or []
    for idx, picture in enumerate(pictures):
        try:
            png_bytes = _picture_to_png(picture, document)
        except Exception as exc:
            logger.warning("Skip docling picture: %s", exc)
            continue
        if not png_bytes:
            continue
        img_id = f"img_{idx:04d}"
        out.append((img_id, png_bytes, _picture_page(picture), _picture_caption(picture, document)))
    return out


def _picture_to_png(picture: Any, document: Any) -> bytes | None:
    pil_image = None
    image_obj = getattr(picture, "image", None)
    if image_obj is not None:
        pil_image = getattr(image_obj, "pil_image", None) or image_obj
    if pil_image is None and hasattr(picture, "get_image"):
        try:
            pil_image = picture.get_image(document)
        except TypeError:
            pil_image = picture.get_image()
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
            text = caption()
        except Exception:
            text = ""
        return (text or "").strip()
    return ""


def _safe_title(document: Any, fallback: str) -> str:
    title = getattr(document, "title", None) or getattr(document, "name", None)
    if isinstance(title, str) and title.strip():
        return title.strip()
    return fallback


def _safe_page_count(document: Any) -> int | None:
    pages = getattr(document, "pages", None)
    if pages is None:
        return None
    try:
        return len(pages)
    except TypeError:
        return None


def _safe_ocr_flag(result: Any) -> bool:
    timings = getattr(result, "timings", None) or {}
    if isinstance(timings, dict):
        for key in timings:
            if "ocr" in str(key).lower():
                return True
    return False
