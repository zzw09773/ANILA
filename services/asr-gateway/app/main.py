"""asr-gateway HTTP/WS 介面。

⚠ **路由一律自帶 `/asr` 前綴**:nginx 的 `location /asr/` 不 strip prefix,
所以對外是 `/asr/stream`、`/asr/health`。compose healthcheck 與 M4 驗證都要
打 `/asr/health` —— 打 `/health` 得到 404 是路徑錯,不是服務掛了。

WS 協定見規劃書 §2.2。close code:
  4401 權杖無效/過期/被撤銷(重新登入可解)
  4408 session 逾時(先 flush 再關,不會丟掉最後一句)
  4409 併發:同一 user 開了新連線,舊的被踢掉
  4503 驗證基礎設施不可用(JWKS 拉不到 / 撤銷清單未就緒)——
       fail-closed,重新登入無用,是 Redis/csp 的問題

⚠ **close code 要送得到前端就必須先 accept()**:Starlette 對「未 accept 就
close」的 WS 回 HTTP 403 拒絕握手,前端 onclose 只看得到 1006,永遠讀不到
自訂 code。所以下面一律先 accept、再 close(code)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from starlette.websockets import WebSocketState

from app import auth as auth_mod
from app.config import Settings, settings as default_settings
from app.decode_client import DecodeClient
from app.decode_endpoint import (
    current_decode_url,
    decode_url_refresh_meta,
    decode_url_source,
    refresh_decode_endpoint,
    reset_decode_endpoint_cache,
)
from app.decode_probe import (
    REASON_OK,
    probe_decode_target,
    strip_url_userinfo,
)
from app.services import jwks_client, revocation_cache as revocation_cache_mod
from app.session import AsrSession, make_text_filter
from app.transcriber import VadSegmenter

logger = logging.getLogger(__name__)

CLOSE_AUTH_FAILED = 4401
CLOSE_SESSION_TIMEOUT = 4408
CLOSE_CONCURRENCY = 4409
CLOSE_AUTH_UNAVAILABLE = 4503


def _configure_logging(app_settings: Settings) -> None:
    """uvicorn 的 --log-level 只管它自己的 logger;本服務模組的 logger 沒有
    handler 會落到 root(預設 WARNING),INFO 全被丟掉。"""
    logging.basicConfig(
        level=app_settings.LOG_LEVEL.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("app").setLevel(app_settings.LOG_LEVEL.upper())


def _validate_settings(s: Settings) -> None:
    """prod fail-loud(對齊 csp / studio / router):缺值直接停,不留 fallback。"""
    if not s.ASR_DECODE_URL.strip():
        raise RuntimeError("ASR_DECODE_URL must be set")
    if not s.ASR_DECODER_TOKEN.strip():
        raise RuntimeError("ASR_DECODER_TOKEN must be set")
    url = s.ASR_DECODE_URL.strip()
    if not url.startswith(("http://", "https://")):
        raise RuntimeError(f"ASR_DECODE_URL must be http(s): {url!r}")
    # Pure http is accepted here the same way the governance console accepts
    # model endpoints under ANILA_ALLOW_HTTP_ENDPOINT (P0.2). The two doors
    # must agree; refusing env while accepting CSP was confusing, not safer
    # on an air-gapped network.


class SessionRegistry:
    """per-user 併發控制。**踢舊留新**。

    為什麼不是「拒絕新連線」:筆電闔蓋、網路斷線留下的死 TCP,gateway 察覺
    不到(要等 ping/pong 或 session 上限才回收)。拒新的話使用者會被自己的
    殭屍連線鎖在門外,而且他什麼都做不了。踢舊在語意上也對 —— 使用者顯然
    想用新開的那個。

    ⚠ **單一 process 假設**:計數在記憶體,uvicorn 必須 `--workers 1`。
    gateway 無狀態、CPU 輕,單 worker 夠用;要 scale 再議 Redis 計數,不要
    默默開多 worker 讓限制失效。
    """

    def __init__(self) -> None:
        self._by_user: dict[int, WebSocket] = {}

    async def register(self, user_id: int, ws: WebSocket) -> None:
        old = self._by_user.get(user_id)
        self._by_user[user_id] = ws
        if old is not None and old is not ws:
            logger.info("kicking previous session for user_id=%s", user_id)
            await _safe_close(old, CLOSE_CONCURRENCY, "已在其他地方開啟新的語音工作階段")

    def unregister(self, user_id: int, ws: WebSocket) -> None:
        # 只有「自己還是當前登記者」時才移除 —— 被踢掉的舊連線收尾時,
        # 登記的已經是新連線,不能把它清掉。
        if self._by_user.get(user_id) is ws:
            del self._by_user[user_id]


async def _safe_close(ws: WebSocket, code: int, reason: str = "") -> None:
    try:
        if ws.client_state is WebSocketState.CONNECTED:
            await ws.close(code=code, reason=reason)
    except Exception:
        logger.debug("close failed (peer already gone)", exc_info=True)


def create_app(
    *,
    app_settings: Settings | None = None,
    decode_client: DecodeClient | None = None,
    skip_upstreams: bool = False,
) -> FastAPI:
    """`decode_client` / `skip_upstreams` 可注入 → 測試不需要真的 decoder、
    Redis、csp。"""
    app_settings = app_settings or default_settings
    _configure_logging(app_settings)
    if not skip_upstreams:
        _validate_settings(app_settings)
    # Each app build starts with a clean decoder-URL cache (tests rebuild
    # per case; production builds once).
    reset_decode_endpoint_cache()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not skip_upstreams:
            await jwks_client.start(app)
            cache = revocation_cache_mod.get_revocation_cache()
            await cache.start(app)
            # Read CSP's asr-primary at boot so the first session already
            # uses the governance-selected decoder (no-op without a token).
            await refresh_decode_endpoint(
                app.state.settings, decode_client=app.state.decode_client
            )
            logger.info(
                "ASR decode URL = %s (source=%s)",
                current_decode_url(app.state.settings),
                decode_url_source(),
            )
        try:
            yield
        finally:
            if not skip_upstreams:
                await revocation_cache_mod.get_revocation_cache().stop(app)
                await jwks_client.stop(app)
            client = app.state.decode_client
            if client is not None:
                await client.aclose()

    app = FastAPI(
        title=app_settings.APP_NAME,
        version=app_settings.APP_VERSION,
        lifespan=lifespan,
    )
    app.state.settings = app_settings
    app.state.skip_upstreams = skip_upstreams
    # Unit tests that skip Redis/JWKS also skip the live decoder probe by
    # default; health tests that assert probe behaviour flip this False and
    # mock the decoder with respx.
    app.state.skip_decoder_probe = skip_upstreams
    app.state.decode_client = decode_client or (
        None if skip_upstreams
        else DecodeClient(app_settings.ASR_DECODE_URL, app_settings.ASR_DECODER_TOKEN)
    )
    app.state.registry = SessionRegistry()
    app.state.text_filter = make_text_filter(app_settings.ASR_OPENCC_MODE)
    _register_routes(app)
    return app


async def _compose_health_status(
    *,
    revocation_ready: bool,
    source: str,
    refresh_error: str | None,
    decode_url: str,
    decoder_token: str,
    service_token_configured: bool,
    skip_decoder_probe: bool = False,
) -> tuple[str, str, str | None, dict | None]:
    """Decide top-level health status + operator-facing reason.

    Returns ``(status, reason, detail, probe_dict_or_None)``.

    * ``ok`` — microphone may be shown; speech can be transcribed.
    * ``unavailable`` — voice is switched off (we will not point at a
      machine the operator may not have chosen).
    * ``degraded`` — voice is configured but something is broken.
    """
    if not revocation_ready:
        return (
            "degraded",
            "revocation_not_ready",
            "revocation cache not ready; WebSocket auth is fail-closed",
            None,
        )

    # CSP transport failed and we never confirmed a designation → using the
    # env fallback may be a different machine than the console selected.
    # Prefer voice off over a green mic aimed at the wrong box.
    if (
        service_token_configured
        and source == "env"
        and refresh_error
        and "CSP_SERVICE_TOKEN unset" not in refresh_error
        and (
            "lookup errored" in refresh_error
            or "lookup failed" in refresh_error
            or "unusable endpoint_url" in refresh_error
        )
    ):
        return (
            "unavailable",
            "csp_unreachable",
            (
                "CSP asr-primary unreachable; refusing env fallback so the "
                "microphone is not aimed at a machine the operator did not choose. "
                f"refresh_error={refresh_error}"
            ),
            None,
        )

    if source == "csp_registry_stale":
        # Still probe so detail shows whether the last-known box is up, but
        # overall status stays degraded — designation may have changed.
        probe = None
        if not skip_decoder_probe:
            probe = await probe_decode_target(decode_url, decoder_token)
        detail = (
            "CSP asr-primary refresh failed; still using last known address. "
            "Voice stays off until CSP is reachable again."
        )
        if probe and not probe["ok"]:
            detail = f"{detail} decoder also: {probe.get('detail') or probe['reason']}"
        return ("degraded", "csp_stale", detail, probe)

    if skip_decoder_probe:
        return ("ok", REASON_OK, None, {"ok": True, "reason": REASON_OK, "detail": None})

    probe = await probe_decode_target(decode_url, decoder_token)
    if probe["ok"]:
        return ("ok", REASON_OK, None, probe)
    return ("degraded", probe["reason"], probe.get("detail"), probe)


def _register_routes(app: FastAPI) -> None:
    @app.get("/asr/health")
    async def health() -> JSONResponse:
        """撤銷是 fail-closed → cache 沒 ready 時 ASR 其實不能用,health 必須
        誠實反映,否則 operator 看到綠燈卻連不上,只會查錯方向。

        Also refreshes the CSP-designated decoder URL (TTL), probes that
        decoder, and folds the result into the top-level ``status`` — the
        frontend microphone probe and compose healthcheck both key on HTTP
        200 / status==ok. A buried field is not enough.
        """
        s: Settings = app.state.settings
        await refresh_decode_endpoint(s, decode_client=app.state.decode_client)
        if app.state.skip_upstreams:
            ready = True
        else:
            ready = revocation_cache_mod.get_revocation_cache().ready
        meta = decode_url_refresh_meta()
        source = decode_url_source()
        decode_url = current_decode_url(s)

        status, reason, detail, probe = await _compose_health_status(
            revocation_ready=ready,
            source=source,
            refresh_error=meta["last_refresh_error"],
            decode_url=decode_url,
            decoder_token=s.ASR_DECODER_TOKEN,
            service_token_configured=bool((s.CSP_SERVICE_TOKEN or "").strip()),
            skip_decoder_probe=bool(
                getattr(app.state, "skip_decoder_probe", False)
            ),
        )
        body = {
            "status": status,
            "reason": reason,
            "detail": detail,
            "revocation_cache": ready,
            "version": s.APP_VERSION,
            "decode_url": strip_url_userinfo(decode_url),
            "decode_url_source": source,
            "decode_url_last_refresh_error": meta["last_refresh_error"],
            "decode_url_last_refresh_at": meta["last_refresh_at"],
            "decoder_probe": probe,
        }
        return JSONResponse(body, status_code=200 if status == "ok" else 503)

    @app.websocket("/asr/stream")
    async def stream(websocket: WebSocket) -> None:
        s: Settings = app.state.settings
        # 先 accept,否則自訂 close code 送不到前端(見檔頭)。
        await websocket.accept()
        # TTL refresh so a governance change of decoder address takes effect
        # on the next session without restarting the gateway.
        await refresh_decode_endpoint(s, decode_client=app.state.decode_client)

        token = auth_mod.extract_token(websocket)
        if not token:
            await _safe_close(websocket, CLOSE_AUTH_FAILED, "未登入")
            return
        try:
            identity = await auth_mod.authenticate(token)
        except auth_mod.AuthError as exc:
            await _safe_close(websocket, CLOSE_AUTH_FAILED, str(exc))
            return
        except auth_mod.AuthUnavailable as exc:
            logger.error("auth infrastructure unavailable: %s", exc)
            await _safe_close(websocket, CLOSE_AUTH_UNAVAILABLE, "認證服務暫時不可用")
            return

        await app.state.registry.register(identity.id, websocket)
        session = AsrSession(
            decode=app.state.decode_client.decode,
            send=websocket.send_json,
            segmenter=VadSegmenter(
                vad_aggressiveness=s.ASR_VAD_AGGRESSIVENESS,
                partials_enabled=s.ASR_PARTIALS_ENABLED,
            ),
            initial_prompt=s.ASR_INITIAL_PROMPT or None,
            beam_size=s.ASR_BEAM_SIZE,
            partials_enabled=s.ASR_PARTIALS_ENABLED,
            text_filter=app.state.text_filter,
        )
        await session.start()
        guard = asyncio.create_task(
            _guard(websocket, session, identity, s), name="asr-session-guard"
        )
        try:
            await websocket.send_json({"type": "status", "state": "listening",
                                       "msg": "聆聽中…"})
            await _pump(websocket, session, s)
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("session loop failed")
        finally:
            # 規則 5:任何路徑都要釋放,否則一次未捕捉例外就把該 user 鎖死。
            guard.cancel()
            await session.aclose()
            app.state.registry.unregister(identity.id, websocket)
            await _safe_close(websocket, 1000)


async def _pump(websocket: WebSocket, session: AsrSession, s: Settings) -> None:
    """WS 接收迴圈。只做「收 → 餵」,解碼在 session 的 worker task 裡跑,
    所以解碼再慢也不會擋住收音。"""
    window_start = time.monotonic()
    window_bytes = 0
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        if (data := message.get("bytes")) is not None:
            # 進站速率上限:正常是 32 KB/s(16kHz×2bytes),超標代表對方在灌。
            window_bytes += len(data)
            elapsed = time.monotonic() - window_start
            if elapsed >= 1.0:
                window_start, window_bytes = time.monotonic(), 0
            elif window_bytes > s.ASR_MAX_INGRESS_BYTES_PER_SEC:
                logger.warning("ingress rate exceeded; closing session")
                await _safe_close(websocket, CLOSE_CONCURRENCY, "音訊速率超出上限")
                return
            await session.feed(data)
        elif (text := message.get("text")) is not None:
            await _handle_control(text, session)


async def _handle_control(text: str, session: AsrSession) -> None:
    """控制訊息目前只有 `{"type":"flush"}`(使用者按停)。

    壞掉的 JSON 靜默忽略 —— 這是 client 的 bug,斷線只會讓使用者的錄音無故
    中斷,幫不上任何人。
    """
    try:
        message = json.loads(text)
    except (ValueError, TypeError):
        logger.debug("ignoring non-JSON control frame")
        return
    if isinstance(message, dict) and message.get("type") == "flush":
        await session.flush()


async def _guard(
    websocket: WebSocket,
    session: AsrSession,
    identity: auth_mod.CurrentUserIdentity,
    s: Settings,
) -> None:
    """背景守衛:session 上限與撤銷重查。

    與 `_pump` 分開跑,因為 `_pump` 阻塞在 `receive()` —— 使用者不講話時它
    什麼都不會做,逾時與撤銷不能靠它推進。

    ping/pong **不在這裡做**:uvicorn 的 websockets 實作本來就會自動送
    keepalive ping(`--ws-ping-interval`,預設 20s)並在對方沒回 pong 時斷線,
    死連線由它回收。自己再送一份只是重複。`ASR_PING_INTERVAL_SECONDS` 是給
    部署設 uvicorn 旗標用的,不是這個迴圈在讀。
    """
    deadline = time.monotonic() + s.ASR_MAX_SESSION_SECONDS
    next_recheck = time.monotonic() + s.REVOCATION_RECHECK_SECONDS
    try:
        while True:
            # tick 要比兩個期限都密,否則逾時/撤銷會晚一個 tick 才生效。
            await asyncio.sleep(1.0)
            now = time.monotonic()

            if now >= deadline:
                # 規則 6:先 flush、等 final 回來,再關 —— 直接斷會默默丟掉
                # 使用者最後一句話。
                await session.flush()
                await session.drain(s.ASR_FLUSH_GRACE_SECONDS)
                await _safe_close(websocket, CLOSE_SESSION_TIMEOUT,
                                  "語音工作階段已達時間上限")
                return

            if now >= next_recheck:
                next_recheck = now + s.REVOCATION_RECHECK_SECONDS
                if not await auth_mod.is_still_valid(identity):
                    logger.info("token revoked mid-session; user_id=%s", identity.id)
                    await _safe_close(websocket, CLOSE_AUTH_FAILED, "權杖已失效")
                    return
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("session guard failed")


app = create_app()
