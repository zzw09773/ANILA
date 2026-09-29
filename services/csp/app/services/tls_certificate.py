"""讀 compose 裡 nginx 送出來的 HTTPS 憑證到期日。

連 ``nginx:443``，SNI 用既有的 ``ANILA_HOST``。不掛載、也不讀私鑰檔。
連不上時呼叫端不開憑證告警（入口無回應由別的偵測器負責）。
"""
from __future__ import annotations

import logging
import os
import socket
import ssl
import threading
from datetime import datetime, timedelta, timezone

from cryptography import x509

logger = logging.getLogger(__name__)
_missing_sni_warned = False
_missing_sni_lock = threading.Lock()

NGINX_TLS_HOST = "nginx"
NGINX_TLS_PORT = 443
FP_TLS_CERTIFICATE = "tls:certificate"

_HIGH_MESSAGE = "HTTPS 憑證將於 {days} 天後到期，請向資訊單位申請新憑證"
_EXPIRED_MESSAGE = "HTTPS 憑證已到期，請向資訊單位申請新憑證"
_WARN_WITHIN = timedelta(days=30)
_CRITICAL_WITHIN = timedelta(days=7)


class CertificateUnreachable(Exception):
    """nginx 沒應，或沒有 SNI。不是憑證到期。"""


def reset_missing_sni_warning_for_tests() -> None:
    global _missing_sni_warned
    with _missing_sni_lock:
        _missing_sni_warned = False


def _warn_missing_sni_once() -> None:
    global _missing_sni_warned
    with _missing_sni_lock:
        if _missing_sni_warned:
            return
        _missing_sni_warned = True
    logger.warning("HTTPS 憑證偵測沒有 SNI：ANILA_HOST 未設定，在設定之前不會讀憑證")


def read_served_not_after(
    *,
    host: str | None = None,
    port: int | None = None,
    server_hostname: str | None = None,
    timeout: float = 5.0,
) -> datetime:
    """TLS 握手後讀對端憑證的 notAfter（aware UTC）。"""
    target_host = NGINX_TLS_HOST if host is None else host
    target_port = NGINX_TLS_PORT if port is None else port
    if server_hostname is None:
        sni = os.environ.get("ANILA_HOST", "").strip()
    else:
        sni = server_hostname.strip()
    if not sni:
        _warn_missing_sni_once()
        raise CertificateUnreachable("no_sni")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # 院內 CA 不在公開信任庫。這次連線不送任何憑證，只讀對端 notAfter。
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((target_host, target_port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=sni) as tls:
                der = tls.getpeercert(binary_form=True)
    except OSError as exc:
        raise CertificateUnreachable(type(exc).__name__) from exc
    if not der:
        raise CertificateUnreachable("no_peer_cert")
    # 到期日欄位壞掉時 cryptography 會丟 ValueError 以外的例外（例如 UnicodeDecodeError），
    # 一起接住，免得整輪警報偵測被中斷。
    try:
        cert = x509.load_der_x509_certificate(der)
        not_after = getattr(cert, "not_valid_after_utc", None)
        if not_after is None:
            not_after = cert.not_valid_after.replace(tzinfo=timezone.utc)
    except Exception as exc:
        logger.warning("HTTPS 憑證無法解析，這次視為讀不到")
        raise CertificateUnreachable("invalid_cert") from exc
    if not_after.tzinfo is None:
        not_after = not_after.replace(tzinfo=timezone.utc)
    return not_after.astimezone(timezone.utc)


def certificate_alert_copy(
    not_after: datetime, now: datetime
) -> tuple[str | None, str, str, int]:
    """回傳 (severity 或 None, 標題, 訊息, 剩餘整天數)。

    剛好 30 天不告警。剛好 7 天是 high，未滿 7 天或已過期是 critical。
    已過期的剩餘天數是 0。還沒滿一天、但還沒過期，訊息寫 1 天。
    """
    if not_after.tzinfo is None:
        not_after = not_after.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    remaining = not_after - now
    expired = remaining.total_seconds() <= 0
    if expired or remaining < _CRITICAL_WITHIN:
        severity = "critical"
    elif remaining < _WARN_WITHIN:
        severity = "high"
    else:
        severity = None
    if expired:
        return severity, "HTTPS 憑證已到期", _EXPIRED_MESSAGE, 0
    days = remaining.days if remaining.days >= 1 else 1
    if severity is None:
        return None, "", "", days
    return severity, "HTTPS 憑證即將到期", _HIGH_MESSAGE.format(days=days), days


def public_certificate_view(now: datetime | None = None) -> dict:
    """儀表板欄位。沒有憑證本文、沒有私鑰、沒有路徑。"""
    now = now or datetime.now(timezone.utc)
    try:
        not_after = read_served_not_after()
    except CertificateUnreachable:
        return {
            "status": "unreachable",
            "not_after": None,
            "days_remaining": None,
            "severity": None,
        }
    severity, _title, _message, days = certificate_alert_copy(not_after, now)
    return {
        "status": "ok",
        "not_after": not_after.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "days_remaining": days,
        "severity": severity,
    }
