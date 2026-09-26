# -*- coding: utf-8 -*-
"""治理中心的外部服務：位址、加密憑證、健康、一次匯入。"""
from __future__ import annotations

import logging

import pytest

from app.models.audit_log import AuditLog
from app.models.external_service import DOCUMENT_PARSER, SPEECH, ExternalService
from app.services.external_service_crypto import open_external_credential
from app.services.external_services import import_legacy_env_once
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
    monkeypatch.setattr(canonical_settings, "CSP_SERVICE_TOKEN", token, raising=False)
    monkeypatch.setattr(auth_service.settings, "CSP_SERVICE_TOKEN", token, raising=False)
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
    assert internal.status_code == 403, internal.text
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
    assert probe_client.calls == []
    assert SECRET not in response.text


def test_legacy_docling_env_is_imported_once_and_the_secret_is_not_logged(
    db, monkeypatch, caplog
):
    monkeypatch.setenv("DOC_PARSER", "docling")
    monkeypatch.setenv("DOCLING_URL", PARSER_URL)
    monkeypatch.setenv("DOCLING_SERVICE_TOKEN", SECRET)
    monkeypatch.delenv("ANILA_ALLOW_HTTP_ENDPOINT", raising=False)
    with caplog.at_level(logging.INFO):
        import_legacy_env_once(db)
    assert SECRET not in caplog.text
    row = db.get(ExternalService, DOCUMENT_PARSER)
    db.refresh(row)
    assert row.enabled is True
    assert row.base_url == PARSER_URL
    assert row.env_seeded is True
    assert open_external_credential(row.credential_envelope) == SECRET

    row.enabled = False
    row.base_url = ""
    row.credential_envelope = None
    db.commit()
    import_legacy_env_once(db)
    db.refresh(row)
    assert row.base_url == ""
    assert row.enabled is False


def test_native_doc_parser_and_retired_local_asr_are_not_imported(db, monkeypatch, caplog):
    monkeypatch.setenv("DOC_PARSER", "native")
    monkeypatch.setenv("DOCLING_URL", PARSER_URL)
    monkeypatch.setenv("DOCLING_SERVICE_TOKEN", SECRET)
    monkeypatch.setenv("ASR_DECODE_URL", "http://asr-decoder:9000")
    monkeypatch.setenv("ASR_DECODER_TOKEN", SECRET)
    monkeypatch.delenv("ANILA_ALLOW_HTTP_ENDPOINT", raising=False)
    with caplog.at_level(logging.INFO):
        import_legacy_env_once(db)
    assert SECRET not in caplog.text
    parser = db.get(ExternalService, DOCUMENT_PARSER)
    speech = db.get(ExternalService, SPEECH)
    db.refresh(parser)
    db.refresh(speech)
    assert parser.enabled is False
    assert parser.base_url == ""
    assert speech.enabled is False
    assert speech.base_url == ""


def test_unsafe_legacy_url_is_not_imported(db, monkeypatch, caplog):
    monkeypatch.setenv("DOC_PARSER", "docling")
    monkeypatch.setenv("DOCLING_URL", "http://172.16.120.35:9100")
    monkeypatch.setenv("DOCLING_SERVICE_TOKEN", SECRET)
    monkeypatch.delenv("ANILA_ALLOW_HTTP_ENDPOINT", raising=False)
    monkeypatch.delenv("ANILA_ALLOW_PRIVATE_ENDPOINT", raising=False)
    with caplog.at_level(logging.ERROR):
        import_legacy_env_once(db)
    assert SECRET not in caplog.text
    row = db.get(ExternalService, DOCUMENT_PARSER)
    db.refresh(row)
    assert row.base_url == ""
    assert row.enabled is False
