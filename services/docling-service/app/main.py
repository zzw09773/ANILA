"""docling-service HTTP 介面。

契約(cingestion-worker / csp 是呼叫方):

    POST /parse
        header: X-Token: <DOCLING_SERVICE_TOKEN>
        multipart:
            file                文件位元組;副檔名驅動格式偵測
            ocr_langs           optional, comma-separated, default "ch_tra,en"
            table_structure     optional bool, default true
            picture_description optional bool, default false
        200: {"markdown","title","page_count","ocr_applied",
              "images":[{"id","page","caption","png_b64"}]}

        title 保證:**最多 255 字元**、**不含 C0(0x00–0x1F)與 DEL(0x7F)
        控制字元**、兩端已剝空白;非 str 時回 ""。**不保證**:已做 Unicode 正規化
        (NFC/NFKD 屬 exporter 領域,F2 會做);title 取自 uploader 控制的原始
        檔名(file.filename)、先於 document.title/name,故可能與文件內真標題不同。

    GET /health
        200 {"status":"ok","model_ready":bool}
        503 同上但 model_ready=false

⚠ 文件零落地於服務之外的存放:上傳 bytes 寫進暫存檔只因為 docling 的
``convert()`` 吃路徑不吃 bytes;轉完即刪。log 不記文件內容。
"""

from __future__ import annotations

import logging
import os
import secrets
import tempfile
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.config import Settings, settings as default_settings
from app.model import DoclingConverter

logger = logging.getLogger(__name__)

# client 端 DOCLING_SUPPORTED_EXTS(src/anila_core/ingestion/docling_parser.py)
# 就是這群;service 端自存一份,值要一致,否則 client 放行、service 拒收。
SUPPORTED_SUFFIXES = frozenset(
    {".pdf", ".docx", ".pptx", ".xlsx", ".html", ".htm", ".md"}
)

_TRUE_STRINGS = ("1", "true", "yes", "on")
_FALSE_STRINGS = ("0", "false", "no", "off")


def _configure_logging(app_settings: Settings) -> None:
    logging.basicConfig(
        level=app_settings.LOG_LEVEL.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("app").setLevel(app_settings.LOG_LEVEL.upper())


def _require_token(app_settings: Settings) -> None:
    """prod fail-loud(對齊 asr-decoder):缺密鑰直接停,不留 dev fallback。

    ⚠ 這段是**黑名單**,不是主防線。黑名單不會收斂——它接的是「有人把角括號
    拿掉但沒換值」,不是「有人用壞值」。真正的結構性防線在
    `services/docling-service/.env.example`:佔位值是根目錄慣例的角括號
    `<openssl rand -hex 32>`,原樣複製過去、`cp && up` 當場壞掉,不會安靜成功。
    這層只在替「角括號被手動刪掉、值又漏填」的情況墊底。
    ⚠ 2026-08-19 稽核量到的陷阱:舊佔位字串 `replace-with-openssl-rand-hex-32`
    **剛好 32 字元**,而 `openssl rand -hex 32` 真值 64 字元——「長度 < 32 拒
    收」這種直覺規則剛好抓不到它。所以那個字串必須**顯式列名**,不能靠長度。
    """
    token = app_settings.DOCLING_SERVICE_TOKEN.strip()
    if (
        not token
        or "<" in token
        or ">" in token
        or token == "replace-with-openssl-rand-hex-32"
        or len(token) < 32
    ):
        raise RuntimeError(
            "DOCLING_SERVICE_TOKEN must be a real secret (openssl rand -hex 32 = "
            "64 chars): empty, angle-bracket placeholder, legacy placeholder, or "
            "short values are refused — not the dev fallback, this is fail-loud."
        )


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    v = value.strip().lower()
    if v in _TRUE_STRINGS:
        return True
    if v in _FALSE_STRINGS:
        return False
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"invalid boolean value: {value!r}",
    )


def _split_langs(raw: str | None) -> list[str]:
    return [s.strip() for s in (raw or "").split(",") if s.strip()] or ["ch_tra", "en"]


def get_converter(request: Request) -> DoclingConverter:
    return request.app.state.converter


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def verify_token(
    request: Request,
    x_token: str | None = Header(default=None, alias="X-Token"),
) -> None:
    expected = request.app.state.settings.DOCLING_SERVICE_TOKEN
    # compare_digest 對 str 只收 ASCII;非 ASCII token(理論上 operator 可能設)
    # 會讓它拋 TypeError → 500 而非 401。先編碼再比,壞 token 一律得到 401。
    if not x_token or not secrets.compare_digest(x_token.encode(), expected.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token"
        )


def create_app(
    *,
    converter: DoclingConverter | None = None,
    app_settings: Settings | None = None,
) -> FastAPI:
    app_settings = app_settings or default_settings
    _configure_logging(app_settings)
    _require_token(app_settings)
    injected = converter is not None
    converter = converter or DoclingConverter(
        artifacts_dir=app_settings.DOCLING_ARTIFACTS_DIR,
        local_files_only=app_settings.DOCLING_LOCAL_FILES_ONLY,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not injected:
            # 背景 load:驗證 docling importable + artifacts 路徑(不真的載權重,
            # 權重攤在第一次 parse)。與 asr-decoder 同構:/health 在 load 完成
            # 前回 503。
            threading.Thread(
                target=_load_or_die, args=(app,), name="docling-loader", daemon=True
            ).start()
        yield

    app = FastAPI(
        title=app_settings.APP_NAME,
        version=app_settings.APP_VERSION,
        lifespan=lifespan,
    )
    app.state.settings = app_settings
    app.state.converter = converter
    _register_routes(app)
    return app


def _load_or_die(app: FastAPI) -> None:
    try:
        app.state.converter.load(
            _split_langs(app.state.settings.DOCLING_OCR_LANGS),
            app.state.settings.DOCLING_TABLE_STRUCTURE,
            app.state.settings.DOCLING_PICTURE_DESCRIPTION,
        )
    except Exception:
        logger.exception("docling load failed — exiting so restart policy retries")
        os._exit(1)


def _register_routes(app: FastAPI) -> None:
    @app.get("/health")
    def health(
        converter: DoclingConverter = Depends(get_converter),
        app_settings: Settings = Depends(get_settings),
    ) -> JSONResponse:
        ready = converter.is_ready(
            _split_langs(app_settings.DOCLING_OCR_LANGS),
            app_settings.DOCLING_TABLE_STRUCTURE,
            app_settings.DOCLING_PICTURE_DESCRIPTION,
        )
        return JSONResponse(
            {"status": "ok" if ready else "loading", "model_ready": ready},
            status_code=(
                status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE
            ),
        )

    @app.post("/parse", dependencies=[Depends(verify_token)])
    async def parse(
        request: Request,
        file: UploadFile,
        ocr_langs: str | None = Form(default=None),
        table_structure: str | None = Form(default=None),
        picture_description: str | None = Form(default=None),
        converter: DoclingConverter = Depends(get_converter),
        app_settings: Settings = Depends(get_settings),
    ) -> dict:
        # 表單沒帶 ocr_langs 才回落到設定預設;原寫法用 `or`,但 _split_langs
        # 空輸入回硬寫的預設清單(永遠非 falsy),右運算元不可達 → 設定檔的
        # DOCLING_OCR_LANGS 永遠沒被讀。explicit-else 才真的讓設定值生效。
        if ocr_langs:
            langs = _split_langs(ocr_langs)
        else:
            langs = _split_langs(app_settings.DOCLING_OCR_LANGS)
        ts = _parse_bool(table_structure, app_settings.DOCLING_TABLE_STRUCTURE)
        pd = _parse_bool(picture_description, app_settings.DOCLING_PICTURE_DESCRIPTION)

        # 串流寫進暫存檔:上限在 write 之前擋,不堆任何 buffer——記憶體峰值
        # 只有一個 1 MiB chunk,不隨檔案大小線性成長。(舊寫法 chunks.append
        # 後 b"".join,等於先把整份放進記憶體、再複製一份,2 GB 上限可衝 ~4 GB
        # RSS;GPU 主機上還放著模型權重,OOM 會表現成「端點間歇性故障」。)
        # (multipart 的 Content-Length 是整個 body 不是單檔,不能拿來當單檔上限。)
        suffix = Path(file.filename or "").suffix.lower()
        if not suffix or suffix not in SUPPORTED_SUFFIXES:
            # ⚠ 這個 detail 的子字串 "unsupported extension" 是**跨服務的分類
            # 依據**:anila-core 的 docling_parser._raise_for_status 靠它在 400
            # 裡認出「不支援格式」→ E_PARSE_FORMAT_UNSUPPORTED。它一改寫或在地
            # 化,客戶端會**靜默**退回 corrupt(對使用者說「檔案可能損毀」)。
            # 守衛測試 test_parse.py::test_unsupported_extension_detail_contract
            # 釘住它——除非兩邊一起改(那是更大的 wire 契約變更,該走正式管道),
            # 否則這個子字串不能動。
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unsupported extension: {file.filename!r}",
            )

        total = 0
        max_bytes = app_settings.DOCLING_MAX_FILE_BYTES
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tmp:
            while True:
                chunk = await file.read(1024 * 1024)  # 1 MiB per chunk
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    # 413 的 starlette 常數名在版本間改過(REQUEST_ENTITY_TOO_LARGE →
                    # CONTENT_TOO_LARGE),用字面值免得綁死某個版本(同 asr-decoder)。
                    raise HTTPException(status_code=413, detail="file too large")
                tmp.write(chunk)
            if total == 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail="empty file"
                )
            tmp.flush()
            try:
                return await run_in_threadpool(
                    converter.convert,
                    tmp.name,
                    ocr_langs=langs,
                    table_structure=ts,
                    picture_description=pd,
                    original_name=file.filename,
                )
            except Exception as exc:
                # HIGH-B(R9):分開「pipeline 初始化失敗」與「這一份文件轉不動」。
                # docling 把模型載入攤到第一次 convert——所以第一次 convert 前
                # 的失敗幾乎都是基礎設施(缺權重/缺原生 lib/CUDA OOM),同樣的
                # deploy 換任何一份文件都會再死,重試才有意義 → 回 5xx。只要
                # 成功轉過一次,之後的失敗才是「這一份文件」自身的問題 → 422。
                # 用機器欄位 kind(不靠 detail 子字串分類),client 依 status
                # 分域:5xx→可重試、422→檔案錯。
                logger.exception("docling convert failed (completed_before=%s)",
                                converter.has_completed_conversion(langs, ts, pd))
                if converter.has_completed_conversion(langs, ts, pd):
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail={"kind": "convert", "message": type(exc).__name__},
                    ) from exc
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail={"kind": "init", "message": type(exc).__name__},
                ) from exc


# 出貨啟動字串(Dockerfile CMD)是 `uvicorn app.main:app`——它要的是模組層級
# 的 `app`,不是 factory 函式。與 asr-decoder/app/main.py:206 同構:缺 token 時
# 這一行在 import 期就 fail-loud(RuntimeError),容器起不來、重啟策略接手。
# 守衛測試 tests/test_dockerfile_cmd.py 從 Dockerfile 讀出這個字串再載入,
# 把 CMD 與模組綁在一起。
app = create_app()
