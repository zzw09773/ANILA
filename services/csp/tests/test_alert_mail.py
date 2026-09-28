"""警報要送到負責人：主控台寄信設定、測試信、同一指紋只寄一次。"""
from __future__ import annotations

import asyncio
import socket
import threading
from pathlib import Path

import pytest

from app.models.alert_mail import AlertMailSettings
from app.models.audit_log import AuditLog
from app.services.alert_detectors import (
    FP_PLATFORM_INGRESS,
    PLATFORM_INGRESS_STREAK,
    evaluate_platform_ingress,
    reset_streaks_for_tests,
)
from app.services.alert_mail import (
    deliver_open_alert,
    open_password,
    send_test_mail,
)
from app.services.alert_notifier import (
    AlertNotification,
    get_notifier,
    set_notifier,
)
from app.services.alert_service import resolve_alert_by_fingerprint, upsert_alert
from tests.conftest import login, make_user

SECRET = "smtp-password-do-not-leak"
HOST = "alerts.example.com"


class FakeSmtp:
    """夠用的 SMTP：不加密。可在 DATA 之後回 550。"""

    def __init__(self, *, fail_reply: str | None = None):
        self.fail_reply = fail_reply
        self.messages: list[str] = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        self._sock.listen(8)
        self._sock.settimeout(0.2)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except OSError:
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(5)
            file = conn.makefile("rwb")

            def send(line: str) -> None:
                file.write(line.encode("utf-8") + b"\r\n")
                file.flush()

            send("220 fake.smtp ESMTP")
            collecting = False
            lines: list[str] = []
            while True:
                raw = file.readline()
                if not raw:
                    break
                text = raw.decode("utf-8", "replace").rstrip("\r\n")
                if collecting:
                    if text == ".":
                        collecting = False
                        body = "\n".join(lines)
                        lines = []
                        if self.fail_reply:
                            send(f"550 {self.fail_reply}")
                        else:
                            self.messages.append(body)
                            send("250 queued")
                    else:
                        lines.append(text[1:] if text.startswith("..") else text)
                    continue
                command = text.upper()
                if command.startswith("EHLO") or command.startswith("HELO"):
                    send("250-fake.smtp")
                    send("250-AUTH PLAIN LOGIN")
                    send("250 OK")
                elif command.startswith("AUTH"):
                    send("235 OK")
                elif command.startswith("MAIL FROM"):
                    send("250 OK")
                elif command.startswith("RCPT TO"):
                    send("250 OK")
                elif command == "DATA":
                    collecting = True
                    send("354 end with <CRLF>.<CRLF>")
                elif command == "RSET":
                    send("250 OK")
                elif command == "QUIT":
                    send("221 bye")
                    break
                elif command.startswith("NOOP"):
                    send("250 OK")
                else:
                    send("502 command not implemented")
        except OSError:
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass


def _admin(client, db, username="mail-admin", role="admin"):
    make_user(db, username=username, role=role)
    return {"Authorization": f"Bearer {login(client, username)}"}


def _public_dns(host, port, *args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


def _allow_loopback(monkeypatch):
    monkeypatch.setattr(
        "app.services.alert_mail.validate_smtp_host",
        lambda host: None,
    )


def _body(**overrides):
    payload = {
        "enabled": True,
        "smtp_host": HOST,
        "smtp_port": 587,
        "security": "starttls",
        "username": "alerts",
        "password": SECRET,
        "from_address": "anila@example.com",
        "recipients": "ops@example.com",
    }
    payload.update(overrides)
    return payload


def _note(fingerprint="fp-1", title="資料庫連不上"):
    return AlertNotification(
        fingerprint=fingerprint,
        category="database",
        severity="critical",
        title=title,
        message="連續兩次無法探測。",
    )


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr(
        "anila_core.security.url_guard.socket.getaddrinfo",
        _public_dns,
    )


def test_smtp_is_not_configured_from_the_environment():
    from app.config import Settings

    assert not any(name.startswith("ANILA_ALERT_SMTP") for name in Settings.model_fields)
    root = Path(__file__).resolve().parents[3]
    compose = (root / "infra/compose/platform.yml").read_text(encoding="utf-8")
    example = (root / ".env.example").read_text(encoding="utf-8")
    notifier = (
        root / "services/csp/app/services/alert_notifier.py"
    ).read_text(encoding="utf-8")
    assert "ANILA_ALERT_SMTP" not in compose
    assert "ANILA_ALERT_SMTP" not in example
    assert "ANILA_ALERT_SMTP" not in notifier


def test_mail_settings_are_sealed_and_never_returned(client, db, public_dns):
    headers = _admin(client, db)
    saved = client.put("/api/alerts/mail", headers=headers, json=_body())
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["enabled"] is True
    assert body["smtp_host"] == HOST
    assert body["has_password"] is True
    assert body["username"] == "alerts"
    assert "password" not in body
    assert SECRET not in saved.text
    assert "password_envelope" not in body

    listed = client.get("/api/alerts/mail", headers=headers)
    assert listed.status_code == 200, listed.text
    assert SECRET not in listed.text
    assert listed.json()["has_password"] is True

    db.expire_all()
    row = db.query(AlertMailSettings).one()
    assert row.password_envelope
    assert SECRET not in row.password_envelope
    assert open_password(row.password_envelope) == SECRET

    audit = (
        db.query(AuditLog)
        .filter(AuditLog.action == "alert_mail_update")
        .one()
    )
    assert SECRET not in (audit.detail or "")
    assert SECRET not in (audit.metadata_json or "")

    again = client.put(
        "/api/alerts/mail",
        headers=headers,
        json=_body(password=None, recipients="ops@example.com, desk@example.com"),
    )
    assert again.status_code == 200, again.text
    assert "password" not in again.json()
    db.expire_all()
    assert open_password(db.query(AlertMailSettings).one().password_envelope) == SECRET


def test_user_cannot_read_or_write_mail_settings(client, db, public_dns):
    make_user(db, username="mail-user", role="user")
    headers = {"Authorization": f"Bearer {login(client, 'mail-user')}"}
    assert client.get("/api/alerts/mail", headers=headers).status_code == 403
    denied = client.put("/api/alerts/mail", headers=headers, json=_body())
    assert denied.status_code == 403


def test_smtp_host_uses_the_outbound_guard(client, db, monkeypatch, public_dns):
    headers = _admin(client, db, username="mail-guard")
    monkeypatch.delenv("ANILA_ALLOW_PRIVATE_ENDPOINT", raising=False)
    blocked = client.put(
        "/api/alerts/mail",
        headers=headers,
        json=_body(smtp_host="127.0.0.1"),
    )
    assert blocked.status_code == 400, blocked.text
    assert SECRET not in blocked.text

    private = client.put(
        "/api/alerts/mail",
        headers=headers,
        json=_body(smtp_host="10.1.2.3"),
    )
    assert private.status_code == 400, private.text

    named = client.put(
        "/api/alerts/mail",
        headers=headers,
        json=_body(smtp_host="localhost"),
    )
    assert named.status_code == 400, named.text


def test_test_mail_reaches_a_fake_smtp_and_reports_errors(client, db, monkeypatch):
    _allow_loopback(monkeypatch)
    headers = _admin(client, db, username="mail-probe")
    server = FakeSmtp()
    try:
        saved = client.put(
            "/api/alerts/mail",
            headers=headers,
            json=_body(
                enabled=False,
                smtp_host="127.0.0.1",
                smtp_port=server.port,
                security="none",
                username="",
                password=None,
            ),
        )
        assert saved.status_code == 200, saved.text

        ok = client.post("/api/alerts/mail/test", headers=headers)
        assert ok.status_code == 200, ok.text
        assert ok.json()["ok"] is True
        assert SECRET not in ok.text
        assert server.messages
        assert "ANILA" in server.messages[-1]
        assert "測試" in server.messages[-1]
    finally:
        server.close()

    failing = FakeSmtp(fail_reply=f"relay denied {SECRET}")
    try:
        client.put(
            "/api/alerts/mail",
            headers=headers,
            json=_body(
                enabled=True,
                smtp_host="127.0.0.1",
                smtp_port=failing.port,
                security="none",
                username="alerts",
                password=SECRET,
            ),
        )
        bad = client.post("/api/alerts/mail/test", headers=headers)
        assert bad.status_code == 200, bad.text
        payload = bad.json()
        assert payload["ok"] is False
        assert "relay denied" in payload["error"]
        assert SECRET not in payload["error"]
        assert SECRET not in bad.text
        listed = client.get("/api/alerts/mail", headers=headers)
        assert "relay denied" in (listed.json()["last_error"] or "")
        assert SECRET not in listed.text
    finally:
        failing.close()


def test_notifier_sends_once_per_fingerprint_until_resolved(db, monkeypatch):
    _allow_loopback(monkeypatch)
    reset_streaks_for_tests()
    server = FakeSmtp()
    try:
        from app.services.alert_mail import save_mail_settings

        save_mail_settings(
            db,
            enabled=True,
            smtp_host="127.0.0.1",
            smtp_port=server.port,
            security="none",
            username="",
            password=None,
            password_set=False,
            clear_password=False,
            from_address="anila@example.com",
            recipients="ops@example.com",
            actor=None,
        )
        db.commit()
        upsert_alert(
            db,
            fingerprint="fp-1",
            category="database",
            severity="critical",
            title="資料庫連不上",
            message="連續兩次無法探測。",
        )
        db.commit()

        deliver_open_alert(db, _note())
        db.commit()
        deliver_open_alert(db, _note())
        db.commit()
        assert len(server.messages) == 1
        assert "資料庫連不上" in server.messages[0]
        assert "嚴重" in server.messages[0]

        resolve_alert_by_fingerprint(db, "fp-1")
        db.commit()
        deliver_open_alert(db, _note())
        db.commit()
        assert len(server.messages) == 2
    finally:
        server.close()
        reset_streaks_for_tests()


def test_disabled_mail_does_not_send(db, monkeypatch):
    _allow_loopback(monkeypatch)
    server = FakeSmtp()
    try:
        from app.services.alert_mail import save_mail_settings

        save_mail_settings(
            db,
            enabled=False,
            smtp_host="127.0.0.1",
            smtp_port=server.port,
            security="none",
            username="",
            password=None,
            password_set=False,
            clear_password=False,
            from_address="anila@example.com",
            recipients="ops@example.com",
            actor=None,
        )
        db.commit()
        deliver_open_alert(db, _note("fp-off"))
        db.commit()
        assert server.messages == []
        send_test_mail(db)
        db.commit()
        assert len(server.messages) == 1
    finally:
        server.close()


def test_open_transition_cooldown_sends_one_mail_until_reopen(db, monkeypatch):
    """偵測器既有的連續失敗門檻還在：同一段未解決期間只寄一封。"""
    _allow_loopback(monkeypatch)
    reset_streaks_for_tests()
    server = FakeSmtp()

    class _Bridge:
        def send(self, notification):
            deliver_open_alert(db, notification)

    previous = set_notifier(_Bridge())
    state = {"ok": False}

    async def _probe():
        from app.services.alert_detectors import PlatformProbeOutcome

        if state["ok"]:
            return PlatformProbeOutcome(True, "ok")
        return PlatformProbeOutcome(False, "unreachable")

    monkeypatch.setattr(
        "app.services.alert_detectors.probe_platform_ingress",
        _probe,
    )
    try:
        from app.services.alert_mail import save_mail_settings

        save_mail_settings(
            db,
            enabled=True,
            smtp_host="127.0.0.1",
            smtp_port=server.port,
            security="none",
            username="",
            password=None,
            password_set=False,
            clear_password=False,
            from_address="anila@example.com",
            recipients="ops@example.com",
            actor=None,
        )
        db.commit()
        for _ in range(PLATFORM_INGRESS_STREAK + 2):
            asyncio.run(evaluate_platform_ingress(db=db))
        db.commit()
        assert len(server.messages) == 1
        assert FP_PLATFORM_INGRESS in server.messages[0] or "入口" in server.messages[0]

        state["ok"] = True
        asyncio.run(evaluate_platform_ingress(db=db))
        db.commit()
        state["ok"] = False
        for _ in range(PLATFORM_INGRESS_STREAK):
            asyncio.run(evaluate_platform_ingress(db=db))
        db.commit()
        assert len(server.messages) == 2
    finally:
        set_notifier(previous)
        server.close()
        reset_streaks_for_tests()


def test_starttls_and_ssl_select_the_matching_client(db, monkeypatch):
    _allow_loopback(monkeypatch)
    calls = {"smtp": 0, "ssl": 0, "starttls": 0}

    class _Client:
        def __init__(self, host, port, timeout=None):
            self.actions = []

        def ehlo(self):
            self.actions.append("ehlo")

        def starttls(self):
            calls["starttls"] += 1

        def login(self, user, password):
            self.actions.append(("login", user))

        def sendmail(self, sender, recipients, message):
            self.actions.append("send")

        def quit(self):
            self.actions.append("quit")

    class _Smtp(_Client):
        def __init__(self, host, port, timeout=None):
            calls["smtp"] += 1
            super().__init__(host, port, timeout)

    class _Ssl(_Client):
        def __init__(self, host, port, timeout=None):
            calls["ssl"] += 1
            super().__init__(host, port, timeout)

    monkeypatch.setattr("smtplib.SMTP", _Smtp)
    monkeypatch.setattr("smtplib.SMTP_SSL", _Ssl)

    from app.services.alert_mail import save_mail_settings

    save_mail_settings(
        db,
        enabled=False,
        smtp_host="127.0.0.1",
        smtp_port=2525,
        security="starttls",
        username="alerts",
        password=SECRET,
        password_set=True,
        clear_password=False,
        from_address="anila@example.com",
        recipients="ops@example.com",
        actor=None,
    )
    db.commit()
    assert send_test_mail(db) is None
    assert calls["smtp"] >= 1
    assert calls["starttls"] >= 1
    assert calls["ssl"] == 0

    save_mail_settings(
        db,
        enabled=False,
        smtp_host="127.0.0.1",
        smtp_port=465,
        security="ssl",
        username="",
        password=None,
        password_set=False,
        clear_password=True,
        from_address="anila@example.com",
        recipients="ops@example.com",
        actor=None,
    )
    db.commit()
    assert send_test_mail(db) is None
    assert calls["ssl"] == 1


def test_summary_names_the_highest_open_severity_and_clears_when_resolved(db):
    from app.services.alert_service import acknowledge_alert, resolve_alert, summarize_alerts, upsert_alert

    low = upsert_alert(
        db,
        fingerprint="banner-low",
        category="disk",
        severity="low",
        title="低",
        message="低",
    )
    high = upsert_alert(
        db,
        fingerprint="banner-high",
        category="disk",
        severity="high",
        title="高",
        message="高",
    )
    critical = upsert_alert(
        db,
        fingerprint="banner-critical",
        category="disk",
        severity="critical",
        title="嚴重",
        message="嚴重",
    )
    acknowledge_alert(db, critical)
    db.commit()
    summary = summarize_alerts(db)
    assert summary["open_count"] == 2
    assert summary["highest_open_severity"] == "high"

    resolve_alert(db, high)
    db.commit()
    summary = summarize_alerts(db)
    assert summary["open_count"] == 1
    assert summary["highest_open_severity"] == "low"

    resolve_alert(db, low)
    db.commit()
    summary = summarize_alerts(db)
    assert summary["open_count"] == 0
    assert summary["highest_open_severity"] is None


def test_owner_can_save_mail_settings(client, db, public_dns):
    headers = _admin(client, db, username="mail-owner", role="owner")
    saved = client.put("/api/alerts/mail", headers=headers, json=_body(enabled=False, password=None))
    assert saved.status_code == 200, saved.text


def test_default_notifier_is_the_console_mailer():
    notifier = get_notifier()
    assert notifier.__class__.__name__ == "SmtpAlertNotifier"
