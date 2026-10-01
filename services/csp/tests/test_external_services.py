# -*- coding: utf-8 -*-
"""治理中心的外部服務：位址、加密憑證、健康、一次匯入。"""
from __future__ import annotations

import pytest

from app.models.audit_log import AuditLog
from app.models.external_service import DOCUMENT_PARSER, SPEECH, ExternalService
from app.services.external_service_crypto import open_external_credential
from tests.conftest import login, make_user

SECRET = "plain-secret-do-not-leak"
PARSER_URL = "https://docling.example.test:9100"
SPEECH_URL = "https://asr.example.test:9000"


def _admin(client, db, username="ext-admin"):
    make_user(db, username=username, role="admin")
    return {"Authorization": f"Bearer {login(client, username)}"}


@pytest.fixture
def service_token_header(monkeypatch):
    from app.config import settings as canonical_settings
    from app.services import auth_service

    token = "csk-test-external-services"
    return {"X-CSP-Service-Token": token}


class _Resp:
    def __init__(self, status_code: int):
        self.status_code = status_code
        self.text = ""


class _Client:
    calls: list[tuple] = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, headers=None):
        _Client.calls.append(("get", url))
        return _Resp(200)

    def post(self, url, headers=None, files=None, data=None):
        _Client.calls.append(("post", url))
        return _Resp(200)


@pytest.fixture
def probe_client(monkeypatch):
    _Client.calls = []
    monkeypatch.setattr("app.services.external_services.httpx.Client", _Client)
    return _Client


def test_admin_sets_parser_without_returning_the_credential(client, db):
    headers = _admin(client, db)
    response = client.put(
        "/api/admin/external-services/document_parser",
        headers=headers,
        json={"enabled": True, "base_url": PARSER_URL, "credential": SECRET},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["enabled"] is True
    assert body["base_url"] == PARSER_URL
    assert body["has_credential"] is True
    assert body["fallback"] is None
    assert "不會改用內建原生解析器" in body["fallback_note"]
    assert SECRET not in response.text
    row = db.get(ExternalService, DOCUMENT_PARSER)
    assert open_external_credential(row.credential_envelope) == SECRET
    audit = (
        db.query(AuditLog)
        .filter(AuditLog.action == "external_service_update")
        .one()
    )
    assert SECRET not in (audit.detail or "")


def test_private_endpoint_names_only_the_missing_gate(client, db, monkeypatch):
    """開關沒開只講開關；開關開了、主機不在清單才回 host_not_trusted。"""
    from app.services.trusted_host_service import _invalidate_cache

    _invalidate_cache()
    monkeypatch.delenv("ANILA_ALLOW_PRIVATE_ENDPOINT", raising=False)
    monkeypatch.delenv("ANILA_TRUSTED_HOSTS", raising=False)
    headers = _admin(client, db, username="ext-private-gate")
    url = "https://172.16.120.35:9100"
    off = client.put(
        "/api/admin/external-services/document_parser",
        headers=headers,
        json={"enabled": True, "base_url": url},
    )
    assert off.status_code == 400, off.text
    assert off.json()["detail"] == (
        "這台平台未允許私有 IP 端點（ANILA_ALLOW_PRIVATE_ENDPOINT）"
        "。開啟後還需要把這台主機加入信任主機"
    )

    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
    on = client.put(
        "/api/admin/external-services/document_parser",
        headers=headers,
        json={"enabled": True, "base_url": url},
    )
    assert on.status_code == 400, on.text
    assert on.json()["detail"] == {
        "code": "host_not_trusted",
        "host": "172.16.120.35",
        "message": "主機 172.16.120.35 還不在信任主機清單",
    }


def test_omitted_credential_is_kept_and_blank_clears_it(client, db):
    headers = _admin(client, db)
    first = client.put(
        "/api/admin/external-services/document_parser",
        headers=headers,
        json={"enabled": True, "base_url": PARSER_URL, "credential": SECRET},
    )
    assert first.status_code == 200, first.text
    kept = client.put(
        "/api/admin/external-services/document_parser",
        headers=headers,
        json={"enabled": True, "base_url": PARSER_URL},
    )
    assert kept.status_code == 200, kept.text
    assert kept.json()["has_credential"] is True
    cleared = client.put(
        "/api/admin/external-services/document_parser",
        headers=headers,
        json={"enabled": True, "base_url": PARSER_URL, "credential": ""},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["has_credential"] is False
    row = db.get(ExternalService, DOCUMENT_PARSER)
    db.refresh(row)
    assert row.credential_envelope is None


def test_disabled_parser_tells_the_console_native_is_in_use(client, db):
    headers = _admin(client, db)
    response = client.get("/api/admin/external-services", headers=headers)
    assert response.status_code == 200, response.text
    parser = next(
        item for item in response.json()["services"]
        if item["service_key"] == "document_parser"
    )
    assert parser["fallback"] == "native"
    assert parser["configured"] is False
    assert "內建原生解析器" in parser["fallback_note"]


def test_non_admin_cannot_edit_and_anonymous_cannot_read_speech_status(client, db):
    make_user(db, username="ext-dev", role="developer")
    headers = {"Authorization": f"Bearer {login(client, 'ext-dev')}"}
    assert client.get("/api/admin/external-services", headers=headers).status_code == 403
    client.cookies.clear()
    assert client.get("/api/external-services/speech/status").status_code == 401


def test_logged_in_user_sees_speech_status_without_a_probe_when_disabled(
    client, db, probe_client
):
    make_user(db, username="ext-user", role="user")
    headers = {"Authorization": f"Bearer {login(client, 'ext-user')}"}
    response = client.get("/api/external-services/speech/status", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == {"enabled": False, "healthy": False}
    assert probe_client.calls == []
    assert SECRET not in response.text


def test_enabled_speech_status_probes_and_reports_health(client, db, probe_client):
    headers = _admin(client, db)
    saved = client.put(
        "/api/admin/external-services/speech",
        headers=headers,
        json={
            "enabled": True,
            "base_url": SPEECH_URL,
            "protocol": "native",
            "credential": SECRET,
        },
    )
    assert saved.status_code == 200, saved.text
    make_user(db, username="ext-listener", role="user")
    user_headers = {"Authorization": f"Bearer {login(client, 'ext-listener')}"}
    response = client.get("/api/external-services/speech/status", headers=user_headers)
    assert response.status_code == 200, response.text
    assert response.json() == {"enabled": True, "healthy": False}
    assert probe_client.calls == []
    assert SECRET not in response.text


def test_legacy_service_token_and_user_jwt_do_not_receive_the_credential(
    client, db, service_token_header
):
    headers = _admin(client, db)
    saved = client.put(
        "/api/admin/external-services/speech",
        headers=headers,
        json={"enabled": True, "base_url": SPEECH_URL, "credential": SECRET},
    )
    assert saved.status_code == 200, saved.text
    listed = client.get("/api/admin/external-services", headers=headers)
    assert SECRET not in listed.text
    internal = client.get(
        "/api/internal/external-services/speech",
        headers=service_token_header,
    )
    assert internal.status_code == 401, internal.text
    assert SECRET not in internal.text
    denied = client.get(
        "/api/internal/external-services/speech",
        headers=headers,
    )
    assert denied.status_code == 401


def test_metadata_address_is_rejected(client, db):
    headers = _admin(client, db)
    response = client.put(
        "/api/admin/external-services/document_parser",
        headers=headers,
        json={"enabled": True, "base_url": "https://169.254.169.254/latest"},
    )
    assert response.status_code == 400, response.text
    row = db.get(ExternalService, DOCUMENT_PARSER)
    assert row is None or not (row.base_url or "").strip()


def test_probe_failure_is_returned_and_stored_within_the_timeout(client, db, monkeypatch):
    from app.services import external_services as svc

    seen = {}

    class _Down:
        def __init__(self, *args, **kwargs):
            seen["timeout"] = kwargs.get("timeout")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None):
            class _Resp:
                status_code = 503
                text = SECRET

            return _Resp()

    monkeypatch.setattr("app.services.external_services.httpx.Client", _Down)
    headers = _admin(client, db, username="probe-down-admin")
    saved = client.put(
        "/api/admin/external-services/document_parser",
        headers=headers,
        json={"enabled": True, "base_url": PARSER_URL, "credential": SECRET},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["health_status"] == "unknown"
    response = client.post(
        "/api/admin/external-services/document_parser/probe",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["health_status"] == "unhealthy"
    assert "503" in (body["health_detail"] or "")
    assert SECRET not in response.text
    timeout = seen["timeout"]
    assert timeout is svc.PROBE_TIMEOUT
    assert timeout.connect == 3.0
    assert timeout.read == 8.0
    row = db.get(ExternalService, DOCUMENT_PARSER)
    db.refresh(row)
    assert row.health_status == "unhealthy"
    assert "503" in (row.health_detail or "")


def test_disabled_probe_does_not_call_upstream(client, db, probe_client):
    headers = _admin(client, db, username="probe-off-admin")
    client.put(
        "/api/admin/external-services/document_parser",
        headers=headers,
        json={"enabled": False, "base_url": PARSER_URL, "credential": SECRET},
    )
    response = client.post(
        "/api/admin/external-services/document_parser/probe",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["health_status"] == "disabled"
    assert probe_client.calls == []
    assert SECRET not in response.text


def test_speech_save_does_not_promise_a_fixed_gateway_delay(client, db):
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "app" / "services" / "external_services.py"
    ).read_text(encoding="utf-8")
    assert "SPEECH_GATEWAY_REFRESH_SECONDS" not in source
    assert "gateway_apply_within_seconds" not in source
    headers = _admin(client, db, username="speech-ttl-admin")
    saved = client.put(
        "/api/admin/external-services/speech",
        headers=headers,
        json={"enabled": True, "base_url": SPEECH_URL, "protocol": "native"},
    )
    assert saved.status_code == 200, saved.text
    assert "gateway_apply_within_seconds" not in saved.json()


def test_probe_reports_a_redirect_without_following_it(client, db, monkeypatch):
    seen = {"follow_redirects": None, "urls": []}

    class _Redirect:
        def __init__(self, *args, **kwargs):
            seen["follow_redirects"] = kwargs.get("follow_redirects")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None):
            seen["urls"].append(url)

            class _Resp:
                status_code = 302
                text = "https://elsewhere.example.test/health"

            return _Resp()

    monkeypatch.setattr("app.services.external_services.httpx.Client", _Redirect)
    headers = _admin(client, db, username="probe-redirect-admin")
    saved = client.put(
        "/api/admin/external-services/document_parser",
        headers=headers,
        json={"enabled": True, "base_url": PARSER_URL},
    )
    assert saved.status_code == 200, saved.text
    response = client.post(
        "/api/admin/external-services/document_parser/probe",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["health_status"] == "unhealthy"
    assert body["health_detail"] == "健康檢查被重新導向（HTTP 3xx）"
    assert seen["follow_redirects"] is False
    assert seen["urls"] == [f"{PARSER_URL}/health"]


def test_list_external_services_commits_only_when_a_row_is_inserted(client, db, monkeypatch):
    from sqlalchemy.orm import Session

    commits = {"n": 0}
    real_commit = Session.commit

    def _counting(self):
        commits["n"] += 1
        return real_commit(self)

    monkeypatch.setattr(Session, "commit", _counting)
    headers = _admin(client, db, username="list-commit-admin")
    commits["n"] = 0
    first = client.get("/api/admin/external-services", headers=headers)
    assert first.status_code == 200, first.text
    assert len(first.json()["services"]) == 2
    assert commits["n"] == 1
    second = client.get("/api/admin/external-services", headers=headers)
    assert second.status_code == 200, second.text
    assert len(second.json()["services"]) == 2
    assert commits["n"] == 1


def test_probe_wall_clock_deadline_is_not_the_inactivity_timeout(db, monkeypatch):
    """慢速回應在 read timeout 內仍有資料時，整段仍要在牆鐘截止回「探測逾時」。"""
    import threading
    import time

    from app.models.external_service import ExternalService
    from app.services import external_services as svc

    monkeypatch.setattr(svc, "PROBE_DEADLINE_SECONDS", 0.2)
    release = threading.Event()
    entered = threading.Event()

    class _Hang:
        def __init__(self, *args, **kwargs):
            assert kwargs.get("timeout") is svc.PROBE_TIMEOUT
            assert kwargs["timeout"].read == 8.0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None):
            entered.set()
            release.wait(5)

            class _Resp:
                status_code = 200
                text = ""

            return _Resp()

        def post(self, url, headers=None, files=None, data=None):
            return self.get(url, headers)

    monkeypatch.setattr(svc.httpx, "Client", _Hang)
    row = ExternalService(
        service_key="document_parser",
        enabled=True,
        base_url=PARSER_URL,
        protocol="native",
        health_status="unknown",
        env_seeded=True,
        updated_at=svc._utcnow(),
    )
    db.merge(row)
    db.commit()
    started = time.monotonic()
    try:
        probed = svc.probe_service_now(db, "document_parser")
        elapsed = time.monotonic() - started
        assert entered.wait(1)
        assert probed.health_status == "unhealthy"
        assert probed.health_detail == "探測逾時"
        assert elapsed < 1.0
    finally:
        release.set()


def test_probe_stores_status_without_the_secret(client, db, probe_client):
    headers = _admin(client, db)
    client.put(
        "/api/admin/external-services/document_parser",
        headers=headers,
        json={"enabled": True, "base_url": PARSER_URL, "credential": SECRET},
    )
    response = client.post(
        "/api/admin/external-services/document_parser/probe",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["health_status"] == "healthy"
    assert not body.get("health_detail")
    assert probe_client.calls
    assert probe_client.calls[0][0] == "get"
    assert SECRET not in response.text
    row = db.get(ExternalService, DOCUMENT_PARSER)
    db.refresh(row)
    assert row.health_status == "healthy"


def _enabled_parser(db, svc):
    db.merge(
        ExternalService(
            service_key=DOCUMENT_PARSER,
            enabled=True,
            base_url=PARSER_URL,
            protocol="native",
            health_status="unknown",
            env_seeded=True,
            updated_at=svc._utcnow(),
        )
    )
    db.commit()


def test_slow_dns_stays_inside_the_probe_deadline_and_drops_a_late_success(db, monkeypatch):
    """URL guard 的 DNS 與 HTTP 共用同一段 deadline。鎖不能等 DNS 慢慢回來。"""
    import socket
    import threading
    import time

    from app.services import external_services as svc

    monkeypatch.setattr(svc, "PROBE_DEADLINE_SECONDS", 0.2)
    release = threading.Event()
    entered = threading.Event()

    def _hang(host, *args, **kwargs):
        entered.set()
        release.wait(5)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr("anila_core.security.url_guard.socket.getaddrinfo", _hang)

    class _Ok:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None):
            class _Resp:
                status_code = 200
                text = ""

            return _Resp()

        def post(self, url, headers=None, files=None, data=None):
            return self.get(url, headers)

    monkeypatch.setattr(svc.httpx, "Client", _Ok)
    _enabled_parser(db, svc)
    holder: dict = {}

    def _run() -> None:
        holder["row"] = svc.probe_service_now(db, DOCUMENT_PARSER)

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    try:
        assert entered.wait(1)
        freed = False
        limit = time.monotonic() + 1.0
        while time.monotonic() < limit:
            if svc.probe_flight_lock().acquire(blocking=False):
                svc.probe_flight_lock().release()
                freed = True
                break
            time.sleep(0.02)
        assert freed
        worker.join(1.0)
        assert not worker.is_alive()
        assert holder["row"].health_status == "unhealthy"
        assert holder["row"].health_detail == "探測逾時"
    finally:
        release.set()
        worker.join(2)

    time.sleep(0.3)
    db.expire_all()
    row = db.get(ExternalService, DOCUMENT_PARSER)
    assert row.health_status == "unhealthy"
    assert row.health_detail == "探測逾時"


def test_probe_ignores_a_success_that_finishes_after_the_deadline(db, monkeypatch):
    """join 逾時與 is_alive() 之間若 worker 已結束，晚於 deadline 的 200 仍要丟掉。"""
    import threading
    import time

    from app.services import external_services as svc

    monkeypatch.setattr(svc, "PROBE_DEADLINE_SECONDS", 0.25)
    real_join = threading.Thread.join

    def _join_sees_the_finished_worker(self, timeout=None):
        if str(getattr(self, "name", "")).startswith("external-service-probe"):
            return real_join(self, None)
        return real_join(self, timeout)

    monkeypatch.setattr(threading.Thread, "join", _join_sees_the_finished_worker)

    class _SlowSuccess:
        def __init__(self, *args, **kwargs):
            assert kwargs.get("timeout") is svc.PROBE_TIMEOUT

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None):
            time.sleep(0.7)

            class _Resp:
                status_code = 200
                text = ""

            return _Resp()

        def post(self, url, headers=None, files=None, data=None):
            return self.get(url, headers)

    monkeypatch.setattr(svc.httpx, "Client", _SlowSuccess)
    _enabled_parser(db, svc)
    started = time.monotonic()
    probed = svc.probe_service_now(db, DOCUMENT_PARSER)
    elapsed = time.monotonic() - started
    assert probed.health_status == "unhealthy"
    assert probed.health_detail == "探測逾時"
    assert elapsed < 0.5
    time.sleep(0.6)
    db.expire_all()
    row = db.get(ExternalService, DOCUMENT_PARSER)
    assert row.health_status == "unhealthy"
    assert row.health_detail == "探測逾時"


def test_probe_health_write_does_not_land_on_a_replaced_base_url(db, monkeypatch):
    """寫回健康時若 base_url / updated_at 已變，這次探測結果不能落到新設定上。"""
    from sqlalchemy import event

    from app.services import external_services as svc

    class _Ok:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None):
            class _Resp:
                status_code = 200
                text = ""

            return _Resp()

        def post(self, url, headers=None, files=None, data=None):
            return self.get(url, headers)

    monkeypatch.setattr(svc.httpx, "Client", _Ok)
    _enabled_parser(db, svc)
    new_url = "https://replaced.example.test:9100"
    state = {"done": False}

    def _swap(conn, cursor, statement, parameters, context, executemany):
        del cursor, parameters, context, executemany
        if state["done"]:
            return
        sql = " ".join(str(statement).split()).lower()
        # updated_at 裡也有 "update" 這個子字，不能拿來判斷是不是 UPDATE。
        if not sql.startswith("update") or "health_status" not in sql:
            return
        state["done"] = True
        nested = conn.connection.cursor()
        try:
            nested.execute(
                "UPDATE external_services SET base_url = ? WHERE service_key = ?",
                (new_url, DOCUMENT_PARSER),
            )
        finally:
            nested.close()

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", _swap)
    try:
        probed = svc.probe_service_now(db, DOCUMENT_PARSER)
    finally:
        event.remove(engine, "before_cursor_execute", _swap)

    db.expire_all()
    row = db.get(ExternalService, DOCUMENT_PARSER)
    assert state["done"]
    assert row.base_url == new_url
    assert probed.base_url == new_url
    assert row.health_status == "unknown"
    assert probed.health_status == "unknown"

