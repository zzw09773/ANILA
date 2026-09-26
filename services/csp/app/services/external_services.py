# -*- coding: utf-8 -*-
"""外部服務的讀寫、健康探測、以及舊環境變數的一次匯入。

執行期位址以這張表為準。``DOC_PARSER`` / ``DOCLING_*`` / ``ASR_DECODE_*``
只在列還空著、而且還沒匯入過時讀一次。日誌只記主機名，不記憑證。
"""
from __future__ import annotations

import asyncio
import hmac
import io
import logging
import os
import re
import threading
import wave
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
    RETIRED_LOCAL_ASR_HOST,
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
# 背景探測的最短間隔。使用者的請求不會另開一輪。
MIN_PROBE_INTERVAL_SECONDS = 30
PROBE_TIMEOUT = httpx.Timeout(8.0, connect=3.0)
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


def ensure_rows(db: Session) -> None:
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
    db.flush()


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
    return {
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


def enforce_base_url(url: str) -> None:
    """先限制 http/https、拒絕 userinfo，再走模型那道出向檢查。

    模型檢查接受 grpc/grpcs。文件解析與語音都是 HTTPX，那些 scheme 存進來
    只會等探測時才失敗。帳密必須走加密欄位，不能掛在網址上。
    """
    cleaned = (url or "").strip()
    if not cleaned:
        return
    parsed = urlparse(cleaned)
    if parsed.username is not None or parsed.password is not None:
        raise ExternalServiceUrlError("userinfo")
    if parsed.scheme not in ("http", "https"):
        raise ExternalServiceUrlError("scheme")
    validate_outbound_url(cleaned, ENDPOINT_KIND_MODEL)


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


def _probe_http(row: ExternalService) -> tuple[str, str]:
    """回 (health_status, detail)。detail 不含憑證，請求網址不含 userinfo。"""
    url = display_base_url((row.base_url or "").strip()).rstrip("/")
    secret = credential_plaintext(row)
    if not url:
        return "unhealthy", "沒有位址"
    try:
        enforce_base_url(url)
    except UnsafeEndpointError as exc:
        return "unhealthy", f"位址未通過出向檢查（{exc.reason}）"

    headers: dict[str, str] = {}
    try:
        with httpx.Client(timeout=PROBE_TIMEOUT, follow_redirects=False) as client:
            if row.service_key == SPEECH and (row.protocol or "native") == "openai":
                if secret:
                    headers["Authorization"] = f"Bearer {secret}"
                files = {"file": ("silence.wav", _silence_wav(), "audio/wav")}
                data = {
                    "model": (row.openai_model or "whisper-1").strip() or "whisper-1",
                    "response_format": "json",
                }
                response = client.post(
                    f"{url}{_OPENAI_PATH}", headers=headers, files=files, data=data
                )
            else:
                if secret:
                    headers["X-Token"] = secret
                response = client.get(f"{url}/health", headers=headers)
    except Exception as exc:
        return "unhealthy", _scrub(f"連線失敗（{type(exc).__name__}）", secret)

    if response.status_code in (401, 403):
        return "unhealthy", "憑證被拒絕"
    if response.status_code == 200:
        return "healthy", ""
    return "unhealthy", _scrub(f"HTTP {response.status_code}", secret)


def _within_probe_interval(row: ExternalService, now: datetime) -> bool:
    checked = row.health_checked_at
    if checked is None:
        return False
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    return now - checked < timedelta(seconds=MIN_PROBE_INTERVAL_SECONDS)


def run_probe_cycle(db: Session) -> bool:
    """背景探測一輪。鎖不住表示已經有一輪在跑，這次不做。

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
            if not row.enabled:
                row.health_status = "disabled"
                row.health_detail = "未啟用"
                row.health_checked_at = now
                continue
            status, detail = _probe_http(row)
            row.health_status = status
            row.health_detail = detail or None
            row.health_checked_at = now
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
    db.commit()
    db.refresh(row)
    if service_key == DOCUMENT_PARSER:
        invalidate_docling_source_cache()
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


def _mark_seeded(row: ExternalService) -> None:
    row.env_seeded = True
    row.updated_at = _utcnow()


def import_legacy_env_once(db: Session) -> None:
    """列是空的、而且還沒匯入過，才把舊環境變數抄進來一次。

    管理員清掉位址之後 ``env_seeded`` 已是 true，重啟不會把 .env 寫回來。
    """
    ensure_rows(db)
    _import_document_parser(_row(db, DOCUMENT_PARSER))
    _import_speech(_row(db, SPEECH))
    db.commit()


def _import_document_parser(row: ExternalService) -> None:
    if row.env_seeded or (row.base_url or "").strip() or row.enabled:
        if not row.env_seeded and ((row.base_url or "").strip() or row.enabled):
            _mark_seeded(row)
        return
    parser = os.environ.get("DOC_PARSER", "").strip().lower()
    url = os.environ.get("DOCLING_URL", "").strip()
    token = os.environ.get("DOCLING_SERVICE_TOKEN", "").strip()
    if parser != "docling" or not url:
        return
    try:
        enforce_base_url(url)
    except (UnsafeEndpointError, ExternalServiceUrlError) as exc:
        logger.error(
            "external_services: 略過文件解析的環境變數匯入（位址未通過出向檢查：%s）",
            exc.reason,
        )
        return
    row.base_url = display_base_url(url)
    row.enabled = True
    if token:
        row.credential_envelope = encrypt_external_credential(token)
    row.health_status = "unknown"
    _mark_seeded(row)
    logger.info(
        "external_services: 已從環境變數匯入文件解析位址 host=%s 憑證%s",
        _host_of(url) or "(no host)",
        "已寫入" if token else "未設定",
    )


def _import_speech(row: ExternalService) -> None:
    if row.env_seeded or (row.base_url or "").strip() or row.enabled:
        if not row.env_seeded and ((row.base_url or "").strip() or row.enabled):
            _mark_seeded(row)
        return
    url = os.environ.get("ASR_DECODE_URL", "").strip()
    if not url:
        return
    host = _host_of(url)
    if host == RETIRED_LOCAL_ASR_HOST:
        logger.info(
            "external_services: 略過已退役的本機語音解碼器 host=%s",
            host,
        )
        return
    protocol = os.environ.get("ASR_DECODE_PROTOCOL", "native").strip().lower() or "native"
    if protocol not in ("native", "openai"):
        logger.error(
            "external_services: 略過語音環境變數匯入（協定無法辨識：%s）",
            protocol,
        )
        return
    try:
        enforce_base_url(url)
    except (UnsafeEndpointError, ExternalServiceUrlError) as exc:
        logger.error(
            "external_services: 略過語音的環境變數匯入（位址未通過出向檢查：%s）",
            exc.reason,
        )
        return
    if protocol == "openai":
        secret = os.environ.get("ASR_DECODE_API_KEY", "").strip()
        model = os.environ.get("ASR_OPENAI_MODEL", "").strip() or "whisper-1"
    else:
        secret = os.environ.get("ASR_DECODER_TOKEN", "").strip()
        model = "whisper-1"
    row.base_url = display_base_url(url)
    row.enabled = True
    row.protocol = protocol
    row.openai_model = model
    if secret:
        row.credential_envelope = encrypt_external_credential(secret)
    row.health_status = "unknown"
    _mark_seeded(row)
    logger.info(
        "external_services: 已從環境變數匯入語音辨識位址 host=%s protocol=%s 憑證%s",
        host or "(no host)",
        protocol,
        "已寫入" if secret else "未設定",
    )


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
    from app.config import settings
    from app.services import agent_credential_service

    if agent_credential_service.fleet_secret_retired(token):
        raise ReaderDenied(403, "無法確認服務類型的舊權杖")
    identity = agent_credential_service.verify_service_token(db, token=token)
    if identity is None:
        legacy = (settings.CSP_SERVICE_TOKEN or "").strip()
        if (
            legacy
            and len(legacy) == len(token)
            and hmac.compare_digest(token, legacy)
        ):
            raise ReaderDenied(403, "無法確認服務類型的舊權杖")
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


async def start_external_service_probe():
    """週期探測。測試把 ANILA_EXTERNAL_SERVICE_PROBE=0 時不啟動。"""
    if not probe_loop_enabled():
        return None

    async def _loop() -> None:
        from app.database import SessionLocal

        while True:
            db = SessionLocal()
            try:
                run_probe_cycle(db)
            except Exception:
                logger.exception("external_services: 背景探測失敗")
            finally:
                db.close()
            await asyncio.sleep(MIN_PROBE_INTERVAL_SECONDS)

    logger.info("external_services: 背景健康探測已啟動")
    return asyncio.create_task(_loop())
