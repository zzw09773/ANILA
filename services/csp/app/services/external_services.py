# -*- coding: utf-8 -*-
"""外部服務的讀寫與健康探測。

執行期位址以治理中心這張表為準。開機不再讀 ``DOC_PARSER`` /
``DOCLING_URL`` / ``ASR_DECODE_*``。日誌不記憑證。
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse

import httpx
from sqlalchemy.orm import Session

from anila_core.ingestion.docling_source import (
    DoclingEndpoint,
    invalidate_docling_source_cache,
    register_docling_source,
)
from anila_core.security.credential_crypto import unpack_credential_envelope
from anila_core.security.url_guard import (
    ENDPOINT_KIND_MODEL,
    UnsafeEndpointError,
    validate_outbound_url,
)

from app.models.external_service import (
    DOCUMENT_PARSER,
    SERVICE_KEYS,
    SPEECH,
    ExternalService,
)
from app.models.user import User
from app.services.audit_service import log_audit_event
from app.services.external_service_crypto import (
    LEGACY_PREFIX,
    encrypt_external_credential,
    open_external_credential,
)

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 30
# 背景探測的最短間隔。管理員按的探測不看這個間隔，請求內做完。
MIN_PROBE_INTERVAL_SECONDS = 30
# read/write 是兩次收到資料之間的空檔，不是整段請求的上限。慢速滴資料
# 可以一直重置這 8 秒。整段探測（含 DNS）共用 PROBE_DEADLINE_SECONDS。
PROBE_TIMEOUT = httpx.Timeout(8.0, connect=3.0)
PROBE_DEADLINE_SECONDS = 8.0
PROBE_TIMEOUT_DETAIL = "探測逾時"
REDIRECT_DETAIL = "健康檢查被重新導向（HTTP 3xx）"
_OPENAI_PATH = "/v1/audio/transcriptions"
_USERINFO_IN_TEXT = re.compile(r"(https?://)[^/@\s]+@")
# 與內部服務核發的系統帳號同名。身分是憑證檔裡的 sk-，不是 csk-。
WORKER_USERNAME = "ingestion-worker"
_probe_flight = threading.Lock()


class ExternalServiceUrlError(ValueError):
    """位址在外部服務這道邊界就被拒。訊息不含網址，避免把 userinfo 帶出去。"""

    def __init__(self, reason: str):
        self.reason = reason
        if reason == "userinfo":
            message = "位址不能帶帳號或密碼，請把憑證填在憑證欄"
        elif reason == "scheme":
            message = "外部服務只接受 http 或 https"
        else:
            message = "位址不正確"
        self.public_message = message
        super().__init__(message)


class ReaderDenied(Exception):
    """內部讀取憑證的拒絕。status_code 是 401 或 403。"""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def probe_flight_lock() -> threading.Lock:
    return _probe_flight


def probe_loop_enabled() -> bool:
    raw = os.environ.get("ANILA_EXTERNAL_SERVICE_PROBE")
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ensure_rows(db: Session) -> bool:
    """補上缺少的列。有新增才 flush，回傳這次是否插入了列。"""
    inserted = False
    for key in SERVICE_KEYS:
        if db.get(ExternalService, key) is None:
            db.add(
                ExternalService(
                    service_key=key,
                    enabled=False,
                    base_url="",
                    protocol="native",
                    openai_model="whisper-1",
                    health_status="unknown",
                    env_seeded=False,
                    updated_at=_utcnow(),
                )
            )
            inserted = True
    if inserted:
        db.flush()
    return inserted


def _row(db: Session, service_key: str) -> ExternalService:
    ensure_rows(db)
    row = db.get(ExternalService, service_key)
    if row is None:
        raise KeyError(service_key)
    return row


def credential_plaintext(row: ExternalService) -> str:
    """解開憑證。解不開就當沒有，不把別的服務的金鑰補進來。

    舊的 enc::v1:: 在這裡改寫成專用金鑰。呼叫端要 commit 才留得住。
    """
    stored = row.credential_envelope
    if not stored:
        return ""
    try:
        plain = (open_external_credential(stored) or "").strip()
    except Exception:
        logger.warning(
            "external_services[%s] 憑證解不開，當作沒有憑證",
            row.service_key,
        )
        return ""
    if stored.startswith(LEGACY_PREFIX):
        row.credential_envelope = (
            encrypt_external_credential(plain) if plain else None
        )
    return plain


def is_configured(row: ExternalService) -> bool:
    """啟用而且有位址。停用（即使還留著舊位址）不算已設定。"""
    return bool(row.enabled) and bool((row.base_url or "").strip())


def is_misconfigured(row: ExternalService) -> bool:
    """開了，但沒有位址。這種狀態不能假裝沒設定。"""
    return bool(row.enabled) and not (row.base_url or "").strip()


def _host_of(url: str) -> str:
    return urlparse(url).hostname or ""


def display_base_url(url: str) -> str:
    """拿掉 userinfo。序列化與出向請求都走這一個，帳密不進回應、也不進網址。"""
    raw = url or ""
    parsed = urlparse(raw)
    if parsed.username is None and parsed.password is None:
        return raw
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = host
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse(
        (parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment)
    )


def _scrub(text: str, secret: str) -> str:
    cleaned = text or ""
    if secret:
        cleaned = cleaned.replace(secret, "<redacted>")
    cleaned = _USERINFO_IN_TEXT.sub(r"\1", cleaned)
    return cleaned[:300]


def fallback_note(row: ExternalService) -> tuple[str | None, str]:
    """回 (fallback, 給主控台看的一句話)。

    文件解析只有「沒啟用」才是 native。啟用後，服務掛了或位址空著都不會退回。
    語音沒有內建辨識，沒啟用時麥克風不出現。
    """
    if row.service_key == DOCUMENT_PARSER:
        if not row.enabled:
            return (
                "native",
                "未啟用文件解析服務。擷取使用內建原生解析器。",
            )
        if not (row.base_url or "").strip():
            return (
                None,
                "已啟用文件解析，但沒有位址。擷取會失敗，不會改用內建原生解析器。",
            )
        return (
            None,
            "已設定 Docling。服務中斷時擷取工作會失敗，不會改用內建原生解析器。",
        )
    if not row.enabled or not (row.base_url or "").strip():
        return (None, "未設定語音辨識。麥克風不會出現。")
    return (None, "已設定語音辨識。服務不健康時麥克風不會出現。")


def public_view(row: ExternalService) -> dict:
    fallback, note = fallback_note(row)
    checked = row.health_checked_at
    updated = row.updated_at
    view = {
        "service_key": row.service_key,
        "enabled": bool(row.enabled),
        "base_url": display_base_url(row.base_url or ""),
        "has_credential": bool(row.credential_envelope),
        "protocol": row.protocol or "native",
        "openai_model": row.openai_model or "whisper-1",
        "health_status": row.health_status or "unknown",
        "health_checked_at": checked.isoformat() if checked else None,
        "health_detail": row.health_detail,
        "configured": is_configured(row),
        "misconfigured": is_misconfigured(row),
        "fallback": fallback,
        "fallback_note": note,
        "updated_at": updated.isoformat() if updated else None,
    }
    return view


def enforce_base_url(url: str, *, dns_timeout: float | None = None) -> None:
    """先限制 http/https、拒絕 userinfo，再走模型那道出向檢查。

    模型檢查接受 grpc/grpcs。文件解析與語音都是 HTTPX，那些 scheme 存進來
    只會等探測時才失敗。帳密必須走加密欄位，不能掛在網址上。
    ``dns_timeout`` 只在探測裡帶，與整段 deadline 同一段時間。
    """
    cleaned = (url or "").strip()
    if not cleaned:
        return
    parsed = urlparse(cleaned)
    if parsed.username is not None or parsed.password is not None:
        raise ExternalServiceUrlError("userinfo")
    if parsed.scheme not in ("http", "https"):
        raise ExternalServiceUrlError("scheme")
    validate_outbound_url(cleaned, ENDPOINT_KIND_MODEL, dns_timeout=dns_timeout)


def document_parser_endpoint(db: Session) -> DoclingEndpoint | None:
    """給本行程的 parser。沒啟用回 None。啟用但沒位址擲出 DoclingMisconfigured。"""
    from anila_core.ingestion.docling_source import DoclingMisconfigured

    row = _row(db, DOCUMENT_PARSER)
    if not row.enabled:
        return None
    url = (row.base_url or "").strip()
    if not url:
        raise DoclingMisconfigured("document_parser enabled without base_url")
    return DoclingEndpoint(base_url=url, token=credential_plaintext(row))


def register_document_parser_source() -> None:
    """CSP 行程註冊：之後 ParserRegistry 不再讀 DOC_PARSER。"""
    from app.database import SessionLocal

    def _provider() -> DoclingEndpoint | None:
        db = SessionLocal()
        try:
            return document_parser_endpoint(db)
        finally:
            db.close()

    register_docling_source(_provider, ttl_seconds=CACHE_TTL_SECONDS)


def _silence_wav() -> bytes:
    """100ms 16kHz 靜音。只給 OpenAI 相容探針用，不進辨識結果。"""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * 1600)
    return buffer.getvalue()


def _config_stamp(base_url: str | None, updated_at: datetime | None) -> tuple:
    """探測開始時的位址與更新時間。條件更新對不上就不寫。"""
    return ((base_url or "").strip(), updated_at)


def _perform_probe(target: dict, started: float, deadline: float) -> tuple:
    """出向檢查（含 DNS）與 HTTP 都在這個 worker。不碰資料庫，也不自己寫結果。"""
    url = target["url"]
    secret = target["secret"]

    def _finished(kind: str, payload) -> tuple:
        return (kind, payload, time.monotonic())

    try:
        remaining = deadline - (time.monotonic() - started)
        enforce_base_url(url, dns_timeout=remaining)
    except TimeoutError:
        return _finished("timeout", None)
    except UnsafeEndpointError as exc:
        return _finished("unsafe", exc)
    except ExternalServiceUrlError as exc:
        return _finished("url_error", exc)

    def _send(client: httpx.Client):
        headers: dict[str, str] = {}
        if target["service_key"] == SPEECH and target["protocol"] == "openai":
            if secret:
                headers["Authorization"] = f"Bearer {secret}"
            files = {"file": ("silence.wav", _silence_wav(), "audio/wav")}
            data = {"model": target["model"], "response_format": "json"}
            return client.post(
                f"{url}{_OPENAI_PATH}", headers=headers, files=files, data=data
            )
        if secret:
            headers["X-Token"] = secret
        return client.get(f"{url}/health", headers=headers)

    try:
        client = httpx.Client(timeout=PROBE_TIMEOUT, follow_redirects=False)
        with client:
            response = _send(client)
    except Exception as exc:
        return _finished("error", exc)
    finished = time.monotonic()
    if finished - started > deadline:
        return ("timeout", None, finished)
    return ("ok", response, finished)


def _probe_http(row: ExternalService) -> tuple[str, str]:
    """回 (health_status, detail)。detail 不含憑證，請求網址不含 userinfo。

    DNS 與 HTTP 共用同一段牆鐘。逾時後不再讀 worker 的回傳；worker 自己不寫庫。
    """
    url = display_base_url((row.base_url or "").strip()).rstrip("/")
    secret = credential_plaintext(row)
    target = {
        "url": url,
        "secret": secret,
        "service_key": row.service_key,
        "protocol": row.protocol or "native",
        "model": (row.openai_model or "whisper-1").strip() or "whisper-1",
    }
    if not url:
        return "unhealthy", "沒有位址"
    started = time.monotonic()
    deadline = PROBE_DEADLINE_SECONDS
    # 不用 with：它的 shutdown 會 wait=True，逾時之後又把 worker 等完。
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="external-service-probe")
    future = pool.submit(_perform_probe, target, started, deadline)
    try:
        try:
            outcome = future.result(timeout=deadline)
        except TimeoutError:
            return "unhealthy", PROBE_TIMEOUT_DETAIL
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    kind, payload, finished = outcome
    if (
        kind == "timeout"
        or finished - started > deadline
        or time.monotonic() - started > deadline
    ):
        return "unhealthy", PROBE_TIMEOUT_DETAIL
    if kind == "unsafe":
        return "unhealthy", f"位址未通過出向檢查（{payload.reason}）"
    if kind == "url_error":
        raise payload
    if kind == "error":
        exc = payload
        if isinstance(exc, (httpx.TimeoutException, TimeoutError)) and (
            finished - started
        ) >= deadline:
            return "unhealthy", PROBE_TIMEOUT_DETAIL
        return "unhealthy", _scrub(f"連線失敗（{type(exc).__name__}）", secret)

    response = payload
    if 300 <= response.status_code < 400:
        return "unhealthy", REDIRECT_DETAIL
    if response.status_code in (401, 403):
        return "unhealthy", "憑證被拒絕"
    if response.status_code == 200:
        return "healthy", ""
    return "unhealthy", _scrub(f"HTTP {response.status_code}", secret)


def _write_health_if_current(
    db: Session,
    service_key: str,
    stamp: tuple,
    now: datetime,
    status: str,
    detail: str,
) -> bool:
    """只在 service_key、位址、updated_at 都還是探測當時的值時寫健康。"""
    base_url, updated_at = stamp
    matched = (
        db.query(ExternalService)
        .filter(
            ExternalService.service_key == service_key,
            ExternalService.base_url == base_url,
            ExternalService.updated_at == updated_at,
        )
        .update(
            {
                ExternalService.health_status: status,
                ExternalService.health_detail: detail or None,
                ExternalService.health_checked_at: now,
            },
            synchronize_session=False,
        )
    )
    return matched == 1


def _probe_and_record(db: Session, row: ExternalService, now: datetime) -> None:
    """打一列並寫回。位址或 updated_at 對不上就不寫這次結果。

    呼叫端要已持有 probe flight lock。停用的列只改狀態，不送憑證。
    條件更新本身就是檢查，不再先查再寫。
    """
    if not row.enabled:
        _write_health_if_current(
            db,
            row.service_key,
            _config_stamp(row.base_url, row.updated_at),
            now,
            "disabled",
            "未啟用",
        )
        return
    # 舊外殼改寫會弄髒這列。先提交，戳記才對得上這次真正打出去的設定。
    credential_plaintext(row)
    if db.dirty or db.new or db.deleted:
        db.commit()
        db.refresh(row)
        if not row.enabled:
            _write_health_if_current(
                db,
                row.service_key,
                _config_stamp(row.base_url, row.updated_at),
                now,
                "disabled",
                "未啟用",
            )
            return
    stamp = _config_stamp(row.base_url, row.updated_at)
    status, detail = _probe_http(row)
    # 先結束探測前打開的交易，條件更新才看得到別的連線已提交的新位址。
    db.commit()
    if not _write_health_if_current(db, row.service_key, stamp, now, status, detail):
        logger.info(
            "external_services[%s] 探測期間設定已變更，丟棄這次結果",
            row.service_key,
        )


def probe_service_now(db: Session, service_key: str) -> ExternalService:
    """管理員按探測。與背景迴圈共用同一把鎖，避免兩次一起打上游。

    不看背景的最短間隔，否則剛存檔（健康被清成 unknown）按下去仍可能
    什麼都不打。鎖要等背景那輪放掉；寫回前若設定已變，結果作廢。
    """
    if service_key not in SERVICE_KEYS:
        raise KeyError(service_key)
    with _probe_flight:
        now = _utcnow()
        row = _row(db, service_key)
        _probe_and_record(db, row, now)
        db.commit()
        db.refresh(row)
        return row


def _within_probe_interval(row: ExternalService, now: datetime) -> bool:
    checked = row.health_checked_at
    if checked is None:
        return False
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    return now - checked < timedelta(seconds=MIN_PROBE_INTERVAL_SECONDS)


def run_probe_cycle(db: Session) -> bool:
    """背景探測一輪。鎖不住表示已經有一輪在跑（含管理員按的探測），這次不做。

    健康時間還在最短間隔內的列不打上游。停用的列只改狀態，不送憑證。
    """
    if not _probe_flight.acquire(blocking=False):
        return False
    try:
        now = _utcnow()
        ensure_rows(db)
        for key in SERVICE_KEYS:
            row = _row(db, key)
            if _within_probe_interval(row, now):
                continue
            _probe_and_record(db, row, now)
        db.commit()
        return True
    finally:
        _probe_flight.release()


def speech_status(db: Session) -> dict:
    """給頁面的快取。不探測、不把憑證送出去。"""
    row = _row(db, SPEECH)
    if not is_configured(row):
        return {"enabled": False, "healthy": False}
    return {
        "enabled": True,
        "healthy": row.health_status == "healthy",
    }


def update_service(
    db: Session,
    service_key: str,
    *,
    enabled: bool,
    base_url: str,
    credential_set: bool,
    credential: str | None,
    protocol: str | None,
    openai_model: str | None,
    actor: User,
    ip_address: str | None,
    commit: bool = True,
) -> ExternalService:
    if service_key not in SERVICE_KEYS:
        raise KeyError(service_key)
    row = _row(db, service_key)
    cleaned = (base_url or "").strip()
    try:
        enforce_base_url(cleaned)
    except UnsafeEndpointError:
        raise
    if service_key == SPEECH and protocol is not None:
        kind = protocol.strip().lower()
        if kind not in ("native", "openai"):
            raise ValueError("protocol")
        row.protocol = kind
    if service_key == SPEECH and openai_model is not None:
        model = openai_model.strip()
        if not model:
            raise ValueError("openai_model")
        row.openai_model = model
    row.enabled = bool(enabled)
    row.base_url = cleaned
    if credential_set:
        text = (credential or "").strip()
        row.credential_envelope = (
            encrypt_external_credential(text) if text else None
        )
    row.env_seeded = True
    row.updated_at = _utcnow()
    row.updated_by_user_id = actor.id
    if not row.enabled:
        row.health_status = "disabled"
        row.health_detail = "未啟用"
    else:
        row.health_status = "unknown"
        row.health_detail = None
    row.health_checked_at = None
    log_audit_event(
        db,
        actor=actor,
        action="external_service_update",
        resource_type="external_service",
        resource_id=service_key,
        detail=(
            f"更新外部服務 {service_key} enabled={bool(enabled)} "
            f"host={_host_of(cleaned) or '(empty)'} "
            f"credential={'set' if credential_set else 'unchanged'}"
        ),
        ip_address=ip_address,
    )
    if commit:
        db.commit()
        db.refresh(row)
        if service_key == DOCUMENT_PARSER:
            invalidate_docling_source_cache()
    else:
        db.flush()
    return row


def internal_payload(db: Session, service_key: str) -> dict:
    """服務權杖通道。帶明文憑證。人的請求不准走這裡。"""
    row = _row(db, service_key)
    payload = {
        "service_key": row.service_key,
        "enabled": bool(row.enabled),
        "configured": is_configured(row),
        "misconfigured": is_misconfigured(row),
        "base_url": display_base_url((row.base_url or "").strip()),
        "protocol": row.protocol or "native",
        "openai_model": row.openai_model or "whisper-1",
        "health_status": row.health_status or "unknown",
    }
    secret = credential_plaintext(row)
    if secret:
        payload["credential"] = secret
    return payload


def rewrap_legacy_credentials(db: Session) -> int:
    """把還停在 SECRET_KEY 外殼的憑證改成專用金鑰。明文不進日誌。"""
    ensure_rows(db)
    count = 0
    for key in SERVICE_KEYS:
        row = _row(db, key)
        stored = row.credential_envelope or ""
        if not stored.startswith(LEGACY_PREFIX):
            continue
        plain = (unpack_credential_envelope(stored) or "").strip()
        row.credential_envelope = (
            encrypt_external_credential(plain) if plain else None
        )
        count += 1
    if count:
        db.commit()
        logger.info("external_services: 已改寫 %d 筆舊憑證外殼", count)
    return count


def _bearer_token(authorization: str | None) -> str:
    raw = (authorization or "").strip()
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return ""


def authorize_internal_read(
    db: Session,
    service_key: str,
    *,
    service_token: str | None,
    authorization: str | None,
) -> None:
    """asr 的服務權杖只讀語音。ingestion-worker 的 sk- 只讀文件解析。

    其他服務權杖、對不上的 API key、以及無法確認類型的舊環境權杖都是 403。
    兩種憑證都沒帶是 401。有服務權杖時不再改看 Bearer。
    """
    token = (service_token or "").strip()
    if token:
        _authorize_service_token(db, service_key, token)
        return
    bearer = _bearer_token(authorization)
    if bearer:
        _authorize_worker_key(db, service_key, bearer)
        return
    raise ReaderDenied(401, "需要服務權杖")


def _authorize_service_token(db: Session, service_key: str, token: str) -> None:
    from app.services import agent_credential_service

    identity = agent_credential_service.verify_service_token(db, token=token)
    if identity is None:
        raise ReaderDenied(401, "服務權杖無效")
    if identity.kind != "service_client" or identity.service_client_id is None:
        raise ReaderDenied(403, "這個身分不能讀外部服務憑證")
    from app.models.service_client import ServiceClient

    row = (
        db.query(ServiceClient)
        .filter(ServiceClient.id == identity.service_client_id)
        .first()
    )
    if row is None or row.client_type != "asr" or service_key != SPEECH:
        raise ReaderDenied(403, "這個服務只能讀自己的外部服務")


def _authorize_worker_key(db: Session, service_key: str, bearer: str) -> None:
    from app.services.api_key_service import validate_api_key

    api_key = validate_api_key(db, bearer)
    if api_key is None or api_key.user is None:
        raise ReaderDenied(401, "服務權杖無效")
    user = api_key.user
    if user.username != WORKER_USERNAME or user.role != "system" or not user.is_active:
        raise ReaderDenied(403, "這個身分不能讀文件解析憑證")
    if service_key != DOCUMENT_PARSER:
        raise ReaderDenied(403, "ingestion-worker 只能讀文件解析")


def _run_probe_cycle_session() -> None:
    """開 session、跑一輪、關掉。給事件迴圈用執行緒呼叫，避免堵住 ASGI。"""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        run_probe_cycle(db)
    except Exception:
        logger.exception("external_services: 背景探測失敗")
    finally:
        db.close()


async def start_external_service_probe():
    """週期探測。測試把 ANILA_EXTERNAL_SERVICE_PROBE=0 時不啟動。

    一整輪（含 session 開關與同步的 httpx）都在工作執行緒，不佔事件迴圈。
    """
    if not probe_loop_enabled():
        return None

    async def _loop() -> None:
        while True:
            await asyncio.to_thread(_run_probe_cycle_session)
            await asyncio.sleep(MIN_PROBE_INTERVAL_SECONDS)

    logger.info("external_services: 背景健康探測已啟動")
    return asyncio.create_task(_loop())
