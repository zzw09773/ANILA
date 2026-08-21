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
import math
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
        original_name: str | None = None,
    ) -> dict:
        """回 wire contract 的 dict(markdown/title/page_count/ocr_applied/images)。

        ``original_name`` = 呼叫端手上的原始檔名。docling 的 document.title 對
        PDF 幾乎不會設（除非 PDF outline 有 Title），fallback 原本是
        ``Path(file_path).stem`` = 服務端暫存檔名（如 tmp2duc_n2r），既垃圾又是
        「洩內部暫存名」的形狀。改收原始檔名當 fallback：有意義、且不洩內部名。
        """
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

        # title 的 fallback 優先原始檔名(呼叫端手上),掉到 Path(file_path).stem
        # 只是「連原始名都沒傳」時的保底,不再是常態。
        fallback = original_name or Path(file_path).stem
        return {
            "markdown": markdown,
            "title": _safe_title(document, fallback=fallback),
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


_TITLE_MAX_CHARS = 255
# C0(0x00–0x1F)加 DEL(0x7F):title 回給下游後會落 metadata、進 UI、`logs` 等,
# 控制字元若不剝,一回傳就可能污染窗格/專案標題/日誌。全部 C0 一起剝(不只是
# \r\n\t)是最不帶漏的邊界。
_TITLE_CONTROL_TRANS = str.maketrans(
    "", "", "".join(chr(c) for c in range(32)) + chr(127)
)


def _normalize_title(raw: str) -> str:
    """wire 的 title 單一常化:非 str→"",剝 C0+DEL 控制字元,剝兩端空白,
    再 cap 到 255 字元。

    title 由 uploader 控制(``main.py`` 用 ``file.filename``),無長度/字元集
    約束時,一個長檔名或塞了控制字元的檔名會原封進回傳。這層是服務的邊界
    (與 client 端 ``anila-core`` 鏡射同源);保證連帶的字串會爽明在
    ``main.py`` 的 wire contract docstring 裡。

    不做事:**試算表公式中性化(=/+/-/@/\t/\r)不做**——title 進匯出時,
    匯出器必須自己過 ``csv_formula_safe``(F2 的 helper),這裡不替它擋。
    Unicode 正規化(NFC/NFKD)也不做。這裡只做「進回傳前一定成立的」邊界。
    """
    if not isinstance(raw, str):
        return ""
    clean = raw.translate(_TITLE_CONTROL_TRANS).strip()
    return clean[:_TITLE_MAX_CHARS]


def _safe_title(document: Any, fallback: str) -> str:
    # 優先:呼叫端手上真正的原始檔名(fallback)。docling 對 PDF 幾乎不設
    # document.title(實測 None),但會把 document.name 填成 input stem(如它自己
    # 抽的 'L312' / 服務端暫存名)——那不是「有意義的標題」,是 docling 的內部
    # input 名。所以 fallback 優先;只有 docling 真的給出 document.title 才用。
    # (格式若是 DOCX 等、docling 從文件元資料讀出真標題時,document.title 才有值,
    # 那比檔名更有義 → 那時 title 優先。)
    title = getattr(document, "title", None)
    if isinstance(title, str) and title.strip():
        return _normalize_title(title)
    if isinstance(fallback, str) and fallback.strip():
        return _normalize_title(fallback)
    name = getattr(document, "name", None)
    if isinstance(name, str) and name.strip():
        return _normalize_title(name)
    return ""


def _safe_page_count(document: Any) -> int | None:
    pages = getattr(document, "pages", None)
    if pages is None:
        return None
    try:
        return len(pages)
    except TypeError:
        return None


def _safe_ocr_flag(result: Any) -> bool:
    """OCR 有沒有真的跑過（非「OCR 被開啟/啟用」）。

    舊寫法掃 ``result.timings`` 找 "ocr" 鍵——但 docling 只在
    ``settings.debug.profile_pipeline_timings`` 開著時才把 timing 寫進
    ``timings``（profiling.py:49），而本服務不開 profiling → ``timings`` 恆空
    → 恆 false。那是「OCR 有啟用但零實據」的假判準，抄進新碼的既有缺陷
    （2026-08-19 稽核 v14）。

    決定性判準改看 ``result.confidence.pages[*].ocr_score``：
    docling 的 :func:`BaseOcrModel.post_process_cells` 只在**真的產出 from_ocr
    cell** 時才把該頁的 ocr_score 從預設 np.nan 設成 float 均值
    （base_ocr_model.py:268-272）；OCR 矩形為空（有文字層的 PDF）時它維持 np.nan。
    所以「有任何一頁 ocr_score 非 NaN」=「OCR 真跑過」。
    實測（docling 2.120.3，本機 CPU，關 profiling）：點陣化純圖片 PDF → 1 頁
    ocr_score=0.733；L312（有文字層）→ 7 頁全 NaN。與下游
    chunking_plugins/builtins.py:580 的「OCR 過 → 頁碼不可信」消費者對齊。
    """
    confidence = getattr(result, "confidence", None)
    pages = getattr(confidence, "pages", None) or {}
    if isinstance(pages, dict):
        for score in pages.values():
            ocr_score = getattr(score, "ocr_score", None)
            if isinstance(ocr_score, float) and not (
                hasattr(math, "isnan") and math.isnan(ocr_score)
            ):
                return True
    return False
