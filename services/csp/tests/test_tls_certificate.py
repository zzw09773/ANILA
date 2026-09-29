"""HTTPS 憑證到期：連 compose 裡的 nginx，讀伺服端憑證 notAfter。不讀私鑰。"""
from __future__ import annotations

import socket
import ssl
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from app.models.alert import Alert
from app.services.alert_detectors import evaluate_tls_certificate
from app.services.alert_notifier import set_notifier
from app.services.alert_service import upsert_alert
from app.services.auth_service import create_tokens
from app.services.tls_certificate import (
    NGINX_TLS_HOST,
    NGINX_TLS_PORT,
    CertificateUnreachable,
    certificate_alert_copy,
    public_certificate_view,
    read_served_not_after,
)
from tests.conftest import make_user

NOW = datetime(2026, 9, 29, 0, 0, tzinfo=timezone.utc)
HIGH_MESSAGE = "HTTPS 憑證將於 {days} 天後到期，請向資訊單位申請新憑證"
EXPIRED_MESSAGE = "HTTPS 憑證已到期，請向資訊單位申請新憑證"
FP = "tls:certificate"


class _Notifier:
    def send(self, _notification) -> None:
        return None


@pytest.fixture(autouse=True)
def _quiet_notifier():
    previous = set_notifier(_Notifier())
    yield
    set_notifier(previous)


def _by_fp(db) -> Alert | None:
    return db.query(Alert).filter(Alert.fingerprint == FP).first()


def _assert_no_secret(text: str) -> None:
    assert "BEGIN " not in text
    assert "PRIVATE" not in text
    assert "/etc/nginx" not in text
    assert "server.key" not in text


def test_thresholds_and_exact_messages():
    ok, *_rest = certificate_alert_copy(NOW + timedelta(days=30), NOW)
    assert ok is None
    ok, *_rest = certificate_alert_copy(NOW + timedelta(days=40), NOW)
    assert ok is None

    severity, title, message, days = certificate_alert_copy(NOW + timedelta(days=29), NOW)
    assert severity == "high"
    assert days == 29
    assert message == HIGH_MESSAGE.format(days=29)
    _assert_no_secret(message)
    _assert_no_secret(title)

    severity, _title, message, days = certificate_alert_copy(NOW + timedelta(days=7), NOW)
    assert severity == "high"
    assert days == 7
    assert message == HIGH_MESSAGE.format(days=7)

    severity, _title, message, days = certificate_alert_copy(NOW + timedelta(days=6), NOW)
    assert severity == "critical"
    assert days == 6
    assert message == HIGH_MESSAGE.format(days=6)

    severity, _title, message, days = certificate_alert_copy(NOW + timedelta(hours=12), NOW)
    assert severity == "critical"
    assert days == 1
    assert message == HIGH_MESSAGE.format(days=1)

    severity, title, message, days = certificate_alert_copy(NOW - timedelta(days=1), NOW)
    assert severity == "critical"
    assert days == 0
    assert message == EXPIRED_MESSAGE
    assert "已到期" in title


def test_evaluate_emits_then_resolves_when_renewed(db):
    assert evaluate_tls_certificate(db=db, now=NOW, not_after=NOW + timedelta(days=29)) == "high"
    db.commit()
    alert = _by_fp(db)
    assert alert is not None and alert.status == "open"
    assert alert.severity == "high"
    assert alert.message == HIGH_MESSAGE.format(days=29)
    _assert_no_secret(alert.message)

    assert evaluate_tls_certificate(db=db, now=NOW, not_after=NOW + timedelta(days=3)) == "critical"
    db.commit()
    alert = _by_fp(db)
    assert alert.status == "open"
    assert alert.severity == "critical"
    assert alert.message == HIGH_MESSAGE.format(days=3)

    assert evaluate_tls_certificate(db=db, now=NOW, not_after=NOW + timedelta(days=40)) == "ok"
    db.commit()
    assert _by_fp(db).status == "resolved"


def test_unreachable_nginx_does_not_emit_or_resolve(db):
    upsert_alert(
        db,
        fingerprint=FP,
        category="certificate",
        severity="high",
        title="HTTPS 憑證即將到期",
        message=HIGH_MESSAGE.format(days=10),
        source_type="certificate",
        source_id="nginx",
    )
    db.commit()

    def _down():
        raise CertificateUnreachable("refused")

    assert evaluate_tls_certificate(db=db, now=NOW, probe=_down) == "unreachable"
    db.commit()
    alert = _by_fp(db)
    assert alert.status == "open"
    assert alert.message == HIGH_MESSAGE.format(days=10)

    db.delete(alert)
    db.commit()
    assert evaluate_tls_certificate(db=db, now=NOW, probe=_down) == "unreachable"
    db.commit()
    assert _by_fp(db) is None


def test_missing_anila_host_does_not_connect(monkeypatch):
    monkeypatch.delenv("ANILA_HOST", raising=False)

    def _boom(*_args, **_kwargs):
        raise AssertionError("不該連線")

    monkeypatch.setattr("app.services.tls_certificate.socket.create_connection", _boom)
    with pytest.raises(CertificateUnreachable):
        read_served_not_after()


def test_csp_compose_passes_anila_host_like_nginx():
    """憑證 SNI 讀 ANILA_HOST。csp 沒有這一行時，偵測器在容器裡永遠沒有 SNI。"""
    import yaml

    repo = Path(__file__).resolve().parents[3]
    for label, relative in (
        ("platform", "infra/compose/platform.yml"),
        ("dev", "infra/compose/dev.yml"),
    ):
        doc = yaml.safe_load((repo / relative).read_text(encoding="utf-8"))
        csp_env = doc["services"]["csp"]["environment"]
        nginx_host = str(doc["services"]["nginx"]["environment"]["ANILA_HOST"])
        assert "ANILA_HOST" in csp_env, (
            f"{label} 的 csp 沒有 ANILA_HOST，憑證偵測的 SNI 到不了容器"
        )
        assert str(csp_env["ANILA_HOST"]) == nginx_host


def test_missing_sni_logs_a_warning_once(monkeypatch, caplog):
    """沒有 SNI 時不連線，而且只警告一次，不能每輪靜默略過。"""
    import logging

    import app.services.tls_certificate as tls

    monkeypatch.delenv("ANILA_HOST", raising=False)
    if hasattr(tls, "reset_missing_sni_warning_for_tests"):
        tls.reset_missing_sni_warning_for_tests()

    def _boom(*_args, **_kwargs):
        raise AssertionError("不該連線")

    monkeypatch.setattr(tls.socket, "create_connection", _boom)
    with caplog.at_level(logging.WARNING, logger="app.services.tls_certificate"):
        with pytest.raises(CertificateUnreachable):
            read_served_not_after()
        with pytest.raises(CertificateUnreachable):
            read_served_not_after()
    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "SNI" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert "ANILA_HOST" in warnings[0].getMessage()


def test_default_target_is_compose_nginx(monkeypatch):
    monkeypatch.setenv("ANILA_HOST", "anila.example")
    seen: dict = {}

    def _connect(addr, timeout=None):
        seen["addr"] = addr
        seen["timeout"] = timeout
        raise OSError("stop")

    monkeypatch.setattr("app.services.tls_certificate.socket.create_connection", _connect)
    with pytest.raises(CertificateUnreachable):
        read_served_not_after()
    assert seen["addr"] == (NGINX_TLS_HOST, NGINX_TLS_PORT)
    assert NGINX_TLS_HOST == "nginx"
    assert NGINX_TLS_PORT == 443
    source = Path(__file__).resolve().parents[1].joinpath("app/services/tls_certificate.py").read_text(
        encoding="utf-8"
    )
    assert "server.key" not in source
    assert "/etc/nginx/certs" not in source


def _write_cert(directory: Path, not_after: datetime) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "anila.example")])
    not_after = not_after.replace(microsecond=0)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_after - timedelta(days=30))
        .not_valid_after(not_after)
        .sign(key, hashes.SHA256())
    )
    key_path = directory / "server.key"
    crt_path = directory / "server.crt"
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    crt_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key_path, crt_path


def _serve(directory: Path, not_after: datetime) -> tuple[int, threading.Event, list[str | None]]:
    key_path, crt_path = _write_cert(directory, not_after)
    captured: list[str | None] = []
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(crt_path), keyfile=str(key_path))

    def _sni(sslobj, server_name, _initial):
        captured.append(server_name)
        return None

    ctx.sni_callback = _sni
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.listen(1)
    sock.settimeout(0.2)
    stop = threading.Event()

    def _loop() -> None:
        while not stop.is_set():
            try:
                conn, _addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                tls = ctx.wrap_socket(conn, server_side=True)
                tls.recv(8)
                tls.close()
            except Exception:
                try:
                    conn.close()
                except OSError:
                    pass

    threading.Thread(target=_loop, daemon=True).start()
    return port, stop, captured


def test_live_tls_reads_not_after_with_sni(tmp_path: Path):
    not_after = datetime(2026, 12, 1, tzinfo=timezone.utc)
    port, stop, captured = _serve(tmp_path, not_after)
    try:
        got = read_served_not_after(host="127.0.0.1", port=port, server_hostname="anila.example")
    finally:
        stop.set()
    assert abs((got - not_after).total_seconds()) < 2
    assert captured == ["anila.example"]


def test_public_view_whitelist(monkeypatch):
    monkeypatch.setattr(
        "app.services.tls_certificate.read_served_not_after",
        lambda **_kwargs: NOW + timedelta(days=12),
    )
    view = public_certificate_view(now=NOW)
    assert set(view) == {"status", "not_after", "days_remaining", "severity"}
    assert view["status"] == "ok"
    assert view["severity"] == "high"
    assert view["days_remaining"] == 12
    assert "BEGIN" not in str(view)

    def _down(**_kwargs):
        raise CertificateUnreachable("down")

    monkeypatch.setattr("app.services.tls_certificate.read_served_not_after", _down)
    quiet = public_certificate_view(now=NOW)
    assert quiet == {
        "status": "unreachable",
        "not_after": None,
        "days_remaining": None,
        "severity": None,
    }


def _bearer(user) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_tokens(user)['access_token']}"}


def _garbage_peer(monkeypatch):
    class _CM:
        def __init__(self, value):
            self._value = value

        def __enter__(self):
            return self._value

        def __exit__(self, *_args):
            return False

    class _Tls:
        def getpeercert(self, binary_form=False):
            assert binary_form is True
            return b"not-a-certificate"

    monkeypatch.setattr(
        "app.services.tls_certificate.socket.create_connection",
        lambda *_args, **_kwargs: _CM(object()),
    )

    def _wrap(_self, _sock, server_hostname=None):
        return _CM(_Tls())

    monkeypatch.setattr("app.services.tls_certificate.ssl.SSLContext.wrap_socket", _wrap)


def test_unparsable_certificate_is_unreachable_and_logged(monkeypatch, caplog):
    import logging

    monkeypatch.setenv("ANILA_HOST", "anila.example")
    _garbage_peer(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="app.services.tls_certificate"):
        with pytest.raises(CertificateUnreachable):
            read_served_not_after()
    warnings = [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING and "無法解析" in record.getMessage()
    ]
    assert len(warnings) == 1
    view = public_certificate_view(now=NOW)
    assert view["status"] == "unreachable"
    assert view["not_after"] is None


def test_certificate_with_an_unreadable_expiry_is_unreachable(monkeypatch):
    """憑證本身解析得開、但到期日欄位壞掉時，cryptography 丟的不是 ValueError。"""

    class _BrokenExpiry:
        @property
        def not_valid_after_utc(self):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad notAfter")

    monkeypatch.setenv("ANILA_HOST", "anila.example")
    _garbage_peer(monkeypatch)
    monkeypatch.setattr(
        "app.services.tls_certificate.x509.load_der_x509_certificate", lambda der: _BrokenExpiry()
    )
    with pytest.raises(CertificateUnreachable):
        read_served_not_after()


def test_unparsable_certificate_does_not_skip_the_rest_of_the_pass(monkeypatch, caplog):
    import asyncio
    import logging

    monkeypatch.setenv("ANILA_HOST", "anila.example")
    _garbage_peer(monkeypatch)
    seen: list[str] = []
    monkeypatch.setattr(
        "app.services.alert_detectors.evaluate_database", lambda: seen.append("db")
    )
    monkeypatch.setattr(
        "app.services.alert_detectors.evaluate_disk", lambda: seen.append("disk")
    )
    monkeypatch.setattr(
        "app.services.alert_detectors.evaluate_backup", lambda: seen.append("backup")
    )

    async def _ingress():
        seen.append("ingress")

    monkeypatch.setattr(
        "app.services.alert_detectors.evaluate_platform_ingress", _ingress
    )
    monkeypatch.setattr(
        "app.services.alert_mail.retry_due_alert_mail", lambda: seen.append("mail")
    )
    from app.services.alert_detectors import alert_detector_pass

    with caplog.at_level(logging.WARNING, logger="app.services.tls_certificate"):
        asyncio.run(alert_detector_pass())
    assert seen == ["db", "disk", "backup", "ingress", "mail"]
    assert any("無法解析" in record.getMessage() for record in caplog.records)


def test_tls_api_unparsable_cert_is_not_a_server_error(client, db, monkeypatch):
    monkeypatch.setenv("ANILA_HOST", "anila.example")
    _garbage_peer(monkeypatch)
    admin = make_user(db, username="cert-bad", role="admin")
    response = client.get("/api/admin/tls-certificate", headers=_bearer(admin))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "unreachable"


def test_tls_certificate_api_requires_admin(client, db, monkeypatch):
    monkeypatch.setattr(
        "app.services.tls_certificate.read_served_not_after",
        lambda **_kwargs: datetime.now(timezone.utc) + timedelta(days=40),
    )
    monkeypatch.setattr("app.api.admin.capacity.public_certificate_view", public_certificate_view)
    assert client.get("/api/admin/tls-certificate").status_code == 401
    user = make_user(db, username="cert-user", role="user")
    assert client.get("/api/admin/tls-certificate", headers=_bearer(user)).status_code == 403
    admin = make_user(db, username="cert-admin", role="admin")
    denied = client.get("/api/admin/tls-certificate?host=10.1.1.1", headers=_bearer(admin))
    assert denied.status_code == 400
    response = client.get("/api/admin/tls-certificate", headers=_bearer(admin))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["severity"] is None
    assert "path" not in body
