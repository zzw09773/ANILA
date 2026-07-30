"""asr-decoder HTTP 介面。

契約(asr-gateway 是唯一的呼叫方):

    POST /transcribe?kind=partial|final&beam=<n>&prompt=<s>&language=zh
        header: X-Token: <ASR_DECODER_TOKEN>
        body:   application/octet-stream — Int16 mono PCM @16 kHz
        200:    {"text", "no_speech_prob", "avg_logprob", "decode_seconds"}

    GET /health
        200 {"status":"ok", model/device/compute_type/ready}   模型 ready
        503 同上但 ready=false                                  還在載入

⚠ 音訊零落地:PCM 只在記憶體流轉,不寫檔;log 不記辨識文字(對話內容機敏)。
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
from contextlib import asynccontextmanager

import numpy as np
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.config import Settings, settings as default_settings
from app.model import KIND_FINAL, KIND_PARTIAL, SAMPLE_RATE, WhisperDecoder

logger = logging.getLogger(__name__)

BYTES_PER_SAMPLE = 2  # Int16 mono


def _configure_logging(app_settings: Settings) -> None:
    """套用 LOG_LEVEL。

    uvicorn 的 --log-level 只管它自己的 logger;本服務模組的 logger 沒有
    handler 時會落到 root(預設 WARNING),INFO 全被丟掉 —— 模型載入進度與
    decode metadata 就此消失,而那正是 operator 判斷「還在暖機 vs 掛了」
    的唯一依據。basicConfig 對已設定過的 root 是 no-op,不會蓋掉 uvicorn。
    """
    logging.basicConfig(
        level=app_settings.LOG_LEVEL.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("app").setLevel(app_settings.LOG_LEVEL.upper())


def _require_token(app_settings: Settings) -> None:
    """prod fail-loud convention(對齊 csp / studio / router):缺密鑰直接停,
    不留 dev fallback。外部版部署時這是 decoder 唯一的一道防線。"""
    if not app_settings.ASR_DECODER_TOKEN.strip():
        raise RuntimeError(
            "ASR_DECODER_TOKEN must be set — asr-decoder refuses to start "
            "unauthenticated (it decodes for anyone who can reach the port)."
        )


def get_decoder(request: Request) -> WhisperDecoder:
    return request.app.state.decoder


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def verify_token(
    request: Request,
    x_token: str | None = Header(default=None, alias="X-Token"),
) -> None:
    expected = request.app.state.settings.ASR_DECODER_TOKEN
    # compare_digest:避免以字串比較的耗時差異洩漏密鑰前綴。
    if not x_token or not secrets.compare_digest(x_token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token"
        )


def create_app(
    *,
    decoder: WhisperDecoder | None = None,
    app_settings: Settings | None = None,
) -> FastAPI:
    """decoder 可注入 → 測試不必真的載入 faster-whisper 權重。"""
    app_settings = app_settings or default_settings
    _configure_logging(app_settings)
    _require_token(app_settings)
    injected = decoder is not None
    decoder = decoder or WhisperDecoder(app_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not injected:
            # 背景載入:權重動輒數 GB,同步載會讓 /health 在整個載入期間
            # 連 TCP 都不接,compose 分不出「還在暖機」與「掛了」。
            threading.Thread(
                target=_load_or_die, args=(app,), name="model-loader", daemon=True
            ).start()
        yield

    app = FastAPI(
        title=app_settings.APP_NAME,
        version=app_settings.APP_VERSION,
        lifespan=lifespan,
    )
    app.state.settings = app_settings
    app.state.decoder = decoder
    _register_routes(app)
    return app


def _load_or_die(app: FastAPI) -> None:
    try:
        app.state.decoder.load()
    except Exception:
        logger.exception("model load failed — exiting so restart policy retries")
        # ⚠ 名副其實地 die:讓整個 process 以非零碼結束,`restart:
        #   unless-stopped` 才會真的重試(暫時性 CUDA 故障就靠這個救回來)。
        # 早期版本只 log、process 續活,以為「health 一直 503 → compose 會判
        # unhealthy 重啟」——那是錯的:docker compose 沒有 autoheal,unhealthy
        # 容器不會被重啟,restart policy 只管 process 死亡。結果是永久 503 的
        # 殭屍。(規劃書 §2.1 記錄的 M7 待修。)
        #
        # 這是背景 daemon thread → raise 只會死在本 thread,uvicorn 主程序照樣
        # 活著。必須用 os._exit 從整個 process 出去;os._exit 而非 sys.exit,
        # 因為後者靠例外傳播,同樣被困在這條 thread 裡出不去。
        os._exit(1)


def _register_routes(app: FastAPI) -> None:
    @app.get("/health")
    def health(decoder: WhisperDecoder = Depends(get_decoder)) -> JSONResponse:
        info = decoder.info
        ok = bool(info.get("ready"))
        return JSONResponse(
            {"status": "ok" if ok else "loading", **info},
            status_code=status.HTTP_200_OK if ok else status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    @app.post("/transcribe", dependencies=[Depends(verify_token)])
    async def transcribe(
        request: Request,
        kind: str = Query(default=KIND_FINAL),
        beam: int = Query(default=5, ge=1, le=10),
        prompt: str = Query(default=""),
        language: str = Query(default="zh"),
        decoder: WhisperDecoder = Depends(get_decoder),
        app_settings: Settings = Depends(get_settings),
    ) -> dict:
        if kind not in (KIND_PARTIAL, KIND_FINAL):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"kind must be {KIND_PARTIAL!r} or {KIND_FINAL!r}",
            )
        if not decoder.ready:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="model still loading",
            )

        raw = await request.body()
        samples = _pcm_to_samples(raw, app_settings.ASR_MAX_AUDIO_SECONDS)

        # 解碼是 CPU/GPU 密集的同步呼叫 → 丟 threadpool,否則會卡住 event
        # loop,讓同一時間的 /health 探針超時、compose 誤判服務死掉。
        result = await run_in_threadpool(
            decoder.transcribe,
            samples,
            kind=kind,
            prompt=prompt or None,
            beam=beam,
            language=language,
        )
        # 只記 metadata,不記 result["text"] —— 辨識內容等同對話內容。
        logger.info(
            "decoded kind=%s samples=%d decode_seconds=%.3f",
            kind, len(samples), result["decode_seconds"],
        )
        return result


def _pcm_to_samples(raw: bytes, max_seconds: float) -> np.ndarray:
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="empty body"
        )
    if len(raw) % BYTES_PER_SAMPLE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="body length must be a whole number of Int16 samples",
        )
    max_bytes = int(max_seconds * SAMPLE_RATE) * BYTES_PER_SAMPLE
    if len(raw) > max_bytes:
        # 413 的 starlette 常數名在版本間改過(REQUEST_ENTITY_TOO_LARGE →
        # CONTENT_TOO_LARGE),用字面值免得綁死某個版本。
        raise HTTPException(
            status_code=413, detail=f"audio longer than {max_seconds}s"
        )
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


app = create_app()
