"""Docling parser backend (IBM Research, Apache-2.0, US origin).

Docling is a layout-aware document converter that handles PDF / DOCX /
PPTX / XLSX / HTML in one pipeline. Compared to the ``native`` parsers
(pymupdf4llm + python-docx + odfpy), Docling adds:

* Layout-aware reading order (multi-column scientific PDFs)
* Table-structure recovery (header rows, merged cells)
* Built-in EasyOCR for scanned / font-subsetted PDFs
* Optional picture description via IBM Granite-Vision-3.2-2B

⚠ **2026-08-17 擁有者裁決:docling 一律跑在遠端 GPU 端點,不進平台映像。**

平台主機永遠是 CPU-only;所有 GPU 工作(含 docling 的 layout model、
EasyOCR、Granite picture description)都以 HTTP 端點抵達,像 ASR 一樣。
所以 ``build_docling_parser_from_env()`` 在 ``DOC_PARSER=docling`` 時回傳
:class:`RemoteDoclingParser`——一個只把文件位元組 POST 過去、收回 markdown
的 httpx 客戶端。平台映像因此**不帶 docling / torch / easyocr**,只有 httpx。

下面的 :class:`DoclingParser` 是本機(in-process)實作,留作參考。它
刻意把 ``docling`` import 延後,所以沒裝 ``[docling]`` extra 也能 import
本模組;**但 ``DOC_PARSER=docling`` 缺套件時它不是「靜默退回 native」,而是
raise ImportError**——遠端版延續這個 fail-loud,而且更嚴:非 2xx 一律大聲錯,
絕不降級到 native parser(靜默拿較差的解析結果換一個綠燈,是這專案反覆踩的洞)。
"""
from __future__ import annotations

import base64
import io
import logging
import math
import os
import re
from pathlib import Path
from typing import Any, Optional

import httpx

from anila_core.security.upstream_urls import join_upstream_path
from anila_core.security.url_guard import (
    ENDPOINT_KIND_MODEL,
    UnsafeEndpointError,
    validate_outbound_url,
)

from .parser_registry import ImageRef, ParsedDocument, _new_image_id
from .errors import ParseError

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


_TITLE_MAX_CHARS = 255
# C0(0x00–0x1F)加 DEL(0x7F):title 回給下游後會落 metadata、進 UI、日誌等,
# 控制字元若不剝,一回傳就可能污染窗格/專案標題/日誌。全部 C0 一起剝(不只是
# \r\n\t)是最不帶漏的邊界。與 docling-service 端同源同形。
_TITLE_CONTROL_TRANS = str.maketrans(
    "", "", "".join(chr(c) for c in range(32)) + chr(127)
)


def _normalize_title(raw: str) -> str:
    """wire 的 title 單一常化:非 str→"",剝 C0+DEL 控制字元,剝兩端空白,
    再 cap 到 255 字元。

    這層在 client 側鏡射 docling-service 的邊界——不假設遠端一定清乾淨
    (防禦性:上游若是另一版 service 或少跑了一層,這裡仍守住下游不拿到
    控制字元/超長字串)。

    不做事:Unicode 正常化(NFC/NFKD)不做——檔名正規化屬 exporter 領域,
    F2 會做;這裡只做「進回傳前一定成立的」邊界。
    """
    if not isinstance(raw, str):
        return ""
    clean = raw.translate(_TITLE_CONTROL_TRANS).strip()
    return clean[:_TITLE_MAX_CHARS]


def _safe_title(document: Any, fallback: str) -> str:
    # 與 services/docling-service/app/model.py 的 _safe_title 同源同形(wire
    # 雙側一次改齊,批次①)。prefer real document.title(不設才常有);docling
    # 幾乎不設 title、卻把 document.name 填成 input stem(如暫存名)——
    # 所以 fallback(原始檔名)要優先於 name,不是排在 name 之後。
    title = getattr(document, "title", None)
    if isinstance(title, str) and title.strip():
        return _normalize_title(title)
    if isinstance(fallback, str) and fallback.strip():
        return _normalize_title(fallback)
    name = getattr(document, "name", None)
    if isinstance(name, str) and name.strip():
        return _normalize_title(name)
    return _normalize_title(fallback)


def _safe_page_count(document: Any) -> int:
    pages = getattr(document, "pages", None)
    if pages is None:
        return 0
    try:
        return len(pages)
    except TypeError:
        return 0


def _safe_ocr_flag(result: Any) -> bool:
    """OCR 有沒有真的跑過（非「OCR 被啟用」）。

    與 services/docling-service/app/model.py 的 _safe_ocr_flag 同源同形
    （鏡射修的缺陷,wire 雙側一次改齊）。舊寫法掃 timings 找 "ocr" 鍵,但
    docling 不開 profiling 時 timings 恆空。決定性判準:confidence.pages[*]
    的 ocr_score 只在「OCR 真產生 cell」時才被設(post_process_cells),否則
    維持預設 np.nan。任一頁非 NaN = OCR 真跑。
    """
    confidence = getattr(result, "confidence", None)
    pages = getattr(confidence, "pages", None) or {}
    if isinstance(pages, dict):
        for page_score in pages.values():
            score = getattr(page_score, "ocr_score", None)
            if isinstance(score, float) and not math.isnan(score):
                return True
    return False


# ──────────────────────────────────────────────────────────────────────
# RemoteDoclingParser
# ──────────────────────────────────────────────────────────────────────

# Wire contract (both sides implement it, see services/docling-service):
#   POST /parse   multipart: file / ocr_langs / table_structure /
#                            picture_description; X-Token shared secret.
#   200 -> {"markdown", "title", "page_count", "ocr_applied",
#           "images": [{"id","page","caption","png_b64"}]}
#   The response maps 1:1 onto ParsedDocument / ImageRef below.
#   title is normalized on BOTH sides (see _normalize_title): ≤255 chars,
#   no C0(0x00-0x1F)/DEL(0x7F) control chars, stripped; "" when not a str.
#   NOT guaranteed: spreadsheet formula neutralisation (=/+/-/@/\t/\r) — exporters must call csv_formula_safe. Also not guaranteed: Unicode normalization (NFC/NFKD).

# 預設 OCR 語言(與 in-process 版一致)。語意是「文件的語言」,由呼叫端
# DOCLING_OCR_LANGS 覆寫。
DEFAULT_OCR_LANGS = "ch_tra,en"


class RemoteDoclingError(RuntimeError):
    """遠端 docling 端點失敗(基礎設施)——fail-loud,不降級。

    兩條訊息分開(M2/M3 不變式,使用者面與維運面不能是同一句話):
      - ``usersafe``:上到使用者畫面的通用句子,**不含**內部主機名、連接埠、
        上游 body 片段。經 token redaction。
      - ``details``:給維運 log 的具體線索(endpoint、status、upstream detail)。

    ``str(err)`` 回 ``usersafe``——歷史相依的接縫若仍拿 ``str(e)`` 當訊息,
    不會因此洩漏內部組態。
    """

    def __init__(self, usersafe: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(usersafe)
        self.usersafe = usersafe
        self.details = details or {}


def _redact_token(text: str, token: str) -> str:
    """把共享祕密從要外流的字串裡抹掉(token 可能被上游回音在 body/URL)。"""
    secret = (token or "").strip()
    if not secret:
        return text
    return text.replace(secret, "<redacted>")


def _strip_userinfo(url: str) -> str:
    """剝掉 URL 裡的 ``user:pass@``——operator 把憑證貼進位址時,base_url 本身
    就是一條祕密,而它會被接進「請檢查 DOCLING_URL=...」這類錯誤訊息。"""
    if not url or "@" not in url:
        return url
    match = re.search(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*://)[^/?#\s@]*@", url)
    if not match:
        return url
    return url[: match.start()] + match.group("scheme") + url[match.end():]


class RemoteDoclingParser:
    """HTTP client for the remote docling service (ASR remote-compute pattern).

    - URL guarded with ``validate_outbound_url(..., ENDPOINT_KIND_MODEL)`` —
      同 asr-gateway 的 guard_decode_url:http 由 ANILA_ALLOW_HTTP_ENDPOINT
      決定、單標籤 docker 服務名由 ANILA_TRUSTED_HOSTS 點名。**不放寬 guard**;
      檔掉了就把 host 加進 ANILA_TRUSTED_HOSTS。
    - Path built with ``join_upstream_path``(端點帶不帶 /v1 都正確)。
    - Token 在錯誤訊息、log、任何 raise 之前一律 redact。
    """

    # docling 不支援 .doc/.odt,與 in-process 版同一個集合(parser_registry
    # 靠同一顆 DOCLING_SUPPORTED_EXTS 路由,這裡不必重複)。
    POST_PATH = "/parse"

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        ocr_langs: Optional[list[str]] = None,
        enable_picture_description: bool = False,
        do_table_structure: bool = True,
        timeout: float = 120.0,
        connect_timeout: float = 5.0,
        client: Optional[httpx.Client] = None,
    ) -> None:
        self._base_url = (base_url or "").rstrip("/")
        self._token = token or ""
        self._ocr_langs = ocr_langs or ["ch_tra", "en"]
        self._enable_pic_desc = enable_picture_description
        self._do_table_structure = do_table_structure
        self._timeout = httpx.Timeout(timeout, connect=connect_timeout)
        # 單一 client,延遲到第一次 parse 才建、之後重用。registry 快取 parser
        # 實例 = 一個 process 一個 client(與 asr-gateway decode_client 同構),
        # 不每份文件重做 TCP+TLS 交握。測試注入的 client 一律優先用。
        self._client = client  # None → 第一次 parse 才建

    def _guard(self) -> None:
        # M1:這兩個都是**設定錯**(DOCLING_URL 未設 / 被 SSRF guard 擋),不是
        # 基礎設施——重試一萬次也不會變對。走 bad_config(severity=error、
        # 不可重試),不進 RemoteDoclingError(可重試)桶。維運線索進 details,
        # 使用者面通用句子不帶內部組態名/端點。
        if not self._base_url:
            raise ParseError.bad_config(
                "文件解析服務尚未設定解析端點,請聯絡平台管理員。",
                details={"setting": "DOCLING_URL", "reason": "unset"},
            )
        try:
            validate_outbound_url(self._base_url, ENDPOINT_KIND_MODEL)
        except UnsafeEndpointError as exc:
            hint = ""
            if exc.fixable_by_trust_host:
                hint = (
                    f";若 {exc.host!r} 是本站台刻意要連的 docling 端點,"
                    "把它加進 ANILA_TRUSTED_HOSTS"
                )
            raise ParseError.bad_config(
                "文件解析服務的解析端點未通過安全檢查,請聯絡平台管理員。",
                details={
                    "setting": "DOCLING_URL",
                    "reason": str(exc),
                    "hint": hint,
                },
            ) from exc

    def parse(self, file_path: str) -> ParsedDocument:
        path = Path(file_path)
        ext = path.suffix.lower()
        if ext not in DOCLING_SUPPORTED_EXTS:
            raise ValueError(
                f"RemoteDoclingParser does not support extension '{ext}'. "
                f"Supported: {sorted(DOCLING_SUPPORTED_EXTS)}"
            )

        if not path.is_file():
            raise FileNotFoundError(file_path)

        self._guard()

        # 非 ASCII token 會在 httpx 組 X-Token header 時拋 UnicodeEncodeError,
        # 然後被下面 `except Exception` 包成「遠端無回應」——把組態錯誤指向
        # 網路,是錯的方向。在建 header 前就攔下,導到 bad_config(組態問題、
        # 不可重試)。不 echo 值(是祕密);設定名只進 details(=log),不進 body。
        if self._token:
            try:
                self._token.encode("ascii")
            except UnicodeEncodeError:
                raise ParseError.bad_config(
                    user_message=(
                        "文件解析服務的憑證設定不合法,請聯絡平台管理員。"
                    ),
                    details={
                        "setting": "DOCLING_SERVICE_TOKEN",
                        "reason": "non-ascii",
                    },
                )

        data = {
            "ocr_langs": ",".join(self._ocr_langs),
            "table_structure": "true" if self._do_table_structure else "false",
            "picture_description": "true" if self._enable_pic_desc else "false",
        }
        headers = {}
        if self._token:
            headers["X-Token"] = self._token

        url = join_upstream_path(self._base_url, self.POST_PATH)
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        client = self._client
        try:
            # 串流上傳:傳 file object 給 httpx,讓它分塊送。⚠ 這不表示「整份不進
            # 記憶體」——extract_text(filename, content: bytes) 是 bytes 介面,呼叫端
            # 早就 f.read() 整份持有。這裡達成的是:parser 自己不再做第二份整份
            # 複本(不 fh.read 再塞 httpx),避免同一文件在記憶體有兩份完整拷貝。
            with path.open("rb") as fh:
                resp = client.post(
                    url,
                    files={
                        "file": (path.name, fh, "application/octet-stream"),
                    },
                    data=data,
                    headers=headers,
                )
        except Exception as exc:  # httpx.TimeoutException / ConnectError / ...
            # 基礎設施分域:維運線索(endpoint)進 details,不在 user 面字串。
            raise RemoteDoclingError(
                "文件解析服務暫時無法連線,請稍後重試。",
                details={
                    "kind": type(exc).__name__,
                    "endpoint": self._display_url(),
                    "upstream": _redact_token(str(exc)[:300], self._token),
                },
            ) from exc

        if not (200 <= resp.status_code < 300):
            self._raise_for_status(resp)

        try:
            payload = resp.json()
        except Exception as exc:
            # 200 但 body 不是 JSON ＝ 端點指到別的東西(沒誤判成檔案錯,
            # 是基礎設施/協定錯),可重試;上游片段只進 details。
            raise RemoteDoclingError(
                "文件解析服務回傳格式異常,請稍後重試。",
                details={
                    "kind": type(exc).__name__,
                    "endpoint": self._display_url(),
                    "upstream": _redact_token(resp.text[:200], self._token),
                },
            ) from exc

        return self._reconstruct(payload, file_path, ext)

    def _display_url(self) -> str:
        """錯誤訊息要放進去的網址,userinfo 已剝掉。"""
        return _strip_userinfo(self._base_url or "(未設定)")

    def _raise_for_status(self, resp: httpx.Response) -> None:
        """把非 2xx 依狀態碼分域到三類(文件／組態／基礎設施),不塌成一類。

        ⚠ 已知限制:分類做不到**完備**。代理(nginx 等)「路上」也可能發 400／
        422——客戶端在此無法分辨碼是 docling 服務發的、還是路上代理發的,後者
        會被歸成「檔案的問題」。這是本分域表的已知殘餘,不是「分類完備」。
        """
        if 300 <= resp.status_code < 400:
            # 3xx = 重導向(如 http 打在 TLS 結尾前、路徑少斜線)——永久組態問題,
            # 不可重試。httpx 的 follow_redirects 預設 False,所以 3xx 會到這。
            raise ParseError.bad_config(
                "文件解析服務的端點設定有誤,請聯絡平台管理員。",
                details={
                    "setting": "DOCLING_URL",
                    "status_code": resp.status_code,
                    "location": resp.headers.get("location"),
                },
            )
        if resp.status_code in (401, 403, 404, 405, 407):
            # 組態問題(token 不符 / URL 路徑錯打 → 404/405 / auth 代理 407)。
            # ⚠ 404/405 常是「DOCLING_URL 多了一個 /v1 或少一個斜線」造成——
            # join_upstream_path 會把 path 接到 base 之後,例如 URL 填 .../parse
            # 就打成 /parse/parse → 404。對維運,不是對使用者說「檔案壞了」。
            raise ParseError.bad_config(
                "文件解析服務的連線設定有誤,請聯絡平台管理員。",
                details={
                    "setting": (
                        "DOCLING_SERVICE_TOKEN"
                        if resp.status_code in (401, 403)
                        else "DOCLING_URL"
                    ),
                    "status_code": resp.status_code,
                    "upstream": _redact_token(resp.text[:200], self._token),
                },
            )
        if resp.status_code in (400, 413, 422):
            # 檔案的錯。413 用 too_large,文案不能叫人去猜「純圖片/損毀/密碼保護」。
            if resp.status_code == 413:
                raise ParseError.too_large(
                    "檔案超過解析服務的大小上限,請縮小後重試。",
                    details={"status_code": 413},
                )
            # service 回 400 "unsupported extension" = 兩端 extension 集合漂移
            # (client DOCLING_SUPPORTED_EXTS 與 service SUPPORTED_SUFFIXES 不同步),
            # 歸 format_unsupported 而非 corrupt——「不支援格式」不是「檔案壞」。
            # ⚠ 但這個子字串是**跨服務的無守衛分類依據**:它靠 services/docling-service
            # 端的守衛測試(tests/test_parse.py::test_unsupported_extension_detail_contract)
            # 釘住——service 若單方面改寫/在地化,這裡就靜默退回 corrupt。改動時
            # 兩端要一起改,或換成 wire contract 的機器欄位。
            if "unsupported extension" in resp.text:
                raise ParseError.format_unsupported(
                    user_message="此檔案格式不受文件解析服務支援。",
                    details={
                        "status_code": 400,
                        "upstream": _redact_token(resp.text[:200], self._token),
                    },
                )
            raise ParseError.corrupt(
                user_message=(
                    "檔案無法解析,如果是 PDF 可能是純圖片、損毀或密碼保護;"
                    "如果是 DOCX/PPTX/XLSX 請確認非密碼保護。"
                ),
                details={
                    "status_code": resp.status_code,
                    "upstream": _redact_token(resp.text[:200], self._token),
                },
            )
        if resp.status_code >= 500 or resp.status_code in (408, 429):
            # 基礎設施 & 可重試:5xx、408、429。
            raise RemoteDoclingError(
                "文件解析服務暫時無法處理,請稍後重試。",
                details={
                    "status_code": resp.status_code,
                    "endpoint": self._display_url(),
                    "upstream": _redact_token(resp.text[:200], self._token),
                },
            )
        # 剩餘未分類的 4xx(如 409/410…):保守歸組態,不可重試。
        raise ParseError.bad_config(
            "文件解析服務的連線設定有誤,請聯絡平台管理員。",
            details={
                # LOW-2 R7:維運面拿得到「setting 有誤」卻沒有變數名,是 R6 死信的
                # 殘留形狀——補「未分類」標記,維運至少知道這一格未被指向任一變數。
                "setting": "(unclassified)",
                "status_code": resp.status_code,
                "upstream": _redact_token(resp.text[:200], self._token),
            },
        )

    def _reconstruct(
        self, payload: dict, file_path: str, ext: str
    ) -> ParsedDocument:
        # LOW#2:200 + 合法 JSON 但沒有 markdown 鍵 = 端點指到別的服務(不是
        # docling 的回應),「別人回 200 不代表別人收下了」。這是組態錯,不是
        # 「檔案抽取後沒有文字」。
        if "markdown" not in payload:
            raise ParseError.bad_config(
                "文件解析服務的回應與預期不符,請聯絡平台管理員。",
                details={
                    "setting": "DOCLING_URL",
                    "reason": "missing markdown key",
                },
            )
        markdown = payload.get("markdown") or ""
        # 鏡射 service 端的邊界:不假設遠端一定清乾淨(另一版 service 或少跑
        # 一層時,這裡仍守下游不拿到控制字元/超長字串)。
        title = _normalize_title(payload.get("title"))
        page_count = payload.get("page_count")
        ocr_applied = bool(payload.get("ocr_applied"))
        images: dict[str, ImageRef] = {}

        for raw in payload.get("images") or []:
            # images 是 list[dict],但手衛防一下 shape 漂移。
            if not isinstance(raw, dict):
                continue
            png_b64 = raw.get("png_b64")
            if not isinstance(png_b64, str) or not png_b64:
                continue
            try:
                png_bytes = base64.b64decode(png_b64)
            except Exception:
                logger.warning("docling remote: skip undecodable image png_b64")
                continue
            img_id = str(raw.get("id") or _new_image_id())
            page = raw.get("page")
            caption = raw.get("caption") or ""
            images[img_id] = ImageRef(
                image_id=img_id,
                image_bytes=png_bytes,
                mime="image/png",
                page=int(page) if isinstance(page, int) or (
                    isinstance(page, str) and page.isdigit()
                ) else None,
                caption=str(caption),
            )

        # 與 in-process 版對齊:markdown 尾串 image placeholder(parser_registry 會
        # 解析 [[IMAGE:id]] 再切塊)。
        if images:
            placeholders = "\n\n".join(
                f"[[IMAGE:{img_id}]]" for img_id in images
            )
            content = f"{markdown}\n\n{placeholders}".strip()
        else:
            content = markdown.strip() if isinstance(markdown, str) else ""

        captions = {
            img_id: ref.caption for img_id, ref in images.items() if ref.caption
        }

        return ParsedDocument(
            content=content,
            metadata={
                "title": title,
                "pages": page_count or 0,
                "embedded_images": len(images),
                "ocr_used": ocr_applied,
                "parser": "docling-remote",
                "picture_descriptions": captions,
            },
            source_path=file_path,
            format=ext.lstrip("."),
            images=images,
        )


# ──────────────────────────────────────────────────────────────────────
# Env-driven factory
# ──────────────────────────────────────────────────────────────────────

def build_docling_parser_from_env() -> Optional[RemoteDoclingParser]:
    """Construct a remote Docling parser when ``DOC_PARSER=docling``, else None.

    ⚠ 2026-08-17 起,``DOC_PARSER=docling`` 回 :class:`RemoteDoclingParser`
    (遠端 GPU 端點),不再是 in-process DoclingParser 的觸發。平台映像不帶
    docling / torch / easyocr。

    Env:
      DOC_PARSER                = "native" | "docling"        (default: native)
      DOCLING_URL               = 遠端 parse 端點 base(如 http://docling:9100)
      DOCLING_SERVICE_TOKEN     = 共享祕密(X-Token),可空=不送
      DOCLING_TIMEOUT_SECONDS   = 每通 parse 總秒數(default 120)
      DOCLING_CONNECT_TIMEOUT_SECONDS = 連線秒數(default 5)
      DOCLING_OCR_LANGS         = comma-separated OCR lang codes
                                  (default: "ch_tra,en")
      DOCLING_PICTURE_DESCRIPTION = "true" | "false"          (default: false)
      DOCLING_TABLE_STRUCTURE   = "true" | "false"            (default: true)
    """
    if os.getenv("DOC_PARSER", "native").lower() != "docling":
        return None
    langs = [s.strip() for s in DEFAULT_OCR_LANGS.split(",") if s.strip()]
    raw_langs = os.getenv("DOCLING_OCR_LANGS", "").strip()
    if raw_langs:
        langs = [s.strip() for s in raw_langs.split(",") if s.strip()] or langs

    def _env_float(name: str, default: str) -> float:
        raw = os.getenv(name, default)
        try:
            return float(raw)
        except (ValueError, TypeError):
            # 設定手誤(如 DOCLING_TIMEOUT_SECONDS=abc)不能讓裸 ValueError 掉進
            # parsers.py 的「不支援副檔名」分支,把組態錯說成檔案格式錯。
            # 結構化、terminal;body 不帶內部變數名與值(使用者不會去修 .env、
            # 也不該看到內部組態名)——設定名只進 details(=log),維運看得到。
            raise ParseError.bad_config(
                user_message=(
                    "文件解析服務的設定值不合法,請聯絡平台管理員。"
                ),
                details={"setting": name, "value": raw},
            ) from None

    return RemoteDoclingParser(
        base_url=os.getenv("DOCLING_URL", ""),
        token=os.getenv("DOCLING_SERVICE_TOKEN", ""),
        ocr_langs=langs,
        enable_picture_description=os.getenv(
            "DOCLING_PICTURE_DESCRIPTION", "false"
        ).lower() == "true",
        do_table_structure=os.getenv(
            "DOCLING_TABLE_STRUCTURE", "true"
        ).lower() == "true",
        timeout=_env_float("DOCLING_TIMEOUT_SECONDS", "120"),
        connect_timeout=_env_float("DOCLING_CONNECT_TIMEOUT_SECONDS", "5"),
    )
