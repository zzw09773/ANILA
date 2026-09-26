# -*- coding: utf-8 -*-
"""外部服務憑證的讀取範圍、金鑰、探測與網址邊界。"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from cryptography.exceptions import InvalidTag

from anila_core.security.credential_crypto import unpack_credential_envelope
from app.models.external_service import DOCUMENT_PARSER, SPEECH, ExternalService
from app.models.service_client import ServiceClient
from app.services.external_services import import_legacy_env_once
from app.services.service_token_envelope import (
    compute_lookup_hash,
    encode_service_token_envelope,
    generate_service_token,
)
from tests.conftest import login, make_api_key, make_user

SECRET = "plain-secret-do-not-leak"
PARSER_URL = "https://docling.example.test:9100"
SPEECH_URL = "https://asr.example.test:9000"
REPO = Path(__file__).resolve().parents[3]


def _admin(client, db):
    make_user(db, username="iso-admin", role="admin")
    return {"Authorization": f"Bearer {login(client, 'iso-admin')}"}


def _mint_service_client(db, *, name: str, client_type: str) -> str:
    plaintext = generate_service_token()
    db.add(
        ServiceClient(
            client_name=name,
            client_type=client_type,
            service_token_envelope=encode_service_token_envelope(plaintext),
            service_token_lookup_hash=compute_lookup_hash(plaintext),
        )
    )
    db.commit()
    return plaintext


def _save(client, headers, key, **extra):
    body = {"enabled": True, "base_url": extra.pop("base_url", PARSER_URL)}
    body.update(extra)
    response = client.put(
        f"/api/admin/external-services/{key}",
        headers=headers,
        json=body,
    )
    assert response.status_code == 200, response.text
    return response


class _Resp:
    def __init__(self, status_code: int):
        self.status_code = status_code
        self.text = ""


class _Client:
    calls: list[tuple] = []
    entered = 0

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        type(self).entered += 1
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, headers=None):
        type(self).calls.append(("get", url, headers or {}))
        return _Resp(200)

    def post(self, url, headers=None, files=None, data=None):
        type(self).calls.append(("post", url, headers or {}))
        return _Resp(200)


class _SlowClient(_Client):
    started = threading.Event()
    release = threading.Event()

    def get(self, url, headers=None):
        type(self).started.set()
        assert type(self).release.wait(2)
        return super().get(url, headers=headers)


@pytest.fixture
def probe_client(monkeypatch):
    _Client.calls = []
    _Client.entered = 0
    _SlowClient.started = threading.Event()
    _SlowClient.release = threading.Event()
    monkeypatch.setattr("app.services.external_services.httpx.Client", _Client)
    return _Client


def test_only_asr_reads_speech_and_only_the_worker_key_reads_docling(client, db):
    headers = _admin(client, db)
    _save(client, headers, "speech", base_url=SPEECH_URL, credential=SECRET)
    _save(client, headers, "document_parser", credential=SECRET)
    asr = _mint_service_client(db, name="asr-iso", client_type="asr")
    studio = _mint_service_client(db, name="studio-iso", client_type="studio")
    router = _mint_service_client(db, name="router-iso", client_type="router")
    worker = make_user(db, username="ingestion-worker", role="system")
    make_api_key(db, worker, "sk-worker-iso")
    stranger = make_user(db, username="not-the-worker", role="user")
    make_api_key(db, stranger, "sk-stranger-iso")

    speech = client.get(
        "/api/internal/external-services/speech",
        headers={"X-CSP-Service-Token": asr},
    )
    assert speech.status_code == 200, speech.text
    assert speech.json()["credential"] == SECRET

    parser = client.get(
        "/api/internal/external-services/document_parser",
        headers={"Authorization": "Bearer sk-worker-iso"},
    )
    assert parser.status_code == 200, parser.text
    assert parser.json()["credential"] == SECRET

    denied = [
        client.get(
            "/api/internal/external-services/document_parser",
            headers={"X-CSP-Service-Token": asr},
        ),
        client.get(
            "/api/internal/external-services/speech",
            headers={"X-CSP-Service-Token": studio},
        ),
        client.get(
            "/api/internal/external-services/speech",
            headers={"X-CSP-Service-Token": router},
        ),
        client.get(
            "/api/internal/external-services/speech",
            headers={"Authorization": "Bearer sk-worker-iso"},
        ),
        client.get(
            "/api/internal/external-services/document_parser",
            headers={"Authorization": "Bearer sk-stranger-iso"},
        ),
    ]
    assert [item.status_code for item in denied] == [403, 403, 403, 403, 403]
    assert all(SECRET not in item.text for item in denied)


def test_legacy_env_token_cannot_read_either_credential(client, db, monkeypatch):
    from app.config import settings as canonical_settings
    from app.services import auth_service

    legacy = "csk-legacy-external-iso"
    monkeypatch.setattr(canonical_settings, "CSP_SERVICE_TOKEN", legacy, raising=False)
    monkeypatch.setattr(auth_service.settings, "CSP_SERVICE_TOKEN", legacy, raising=False)
    headers = _admin(client, db)
    _save(client, headers, "speech", base_url=SPEECH_URL, credential=SECRET)
    _save(client, headers, "document_parser", credential=SECRET)
    for key in ("speech", "document_parser"):
        response = client.get(
            f"/api/internal/external-services/{key}",
            headers={"X-CSP-Service-Token": legacy},
        )
        assert response.status_code == 403, response.text
        assert SECRET not in response.text


def test_external_credential_is_not_opened_by_the_worker_master_key(client, db):
    headers = _admin(client, db)
    _save(client, headers, "speech", base_url=SPEECH_URL, credential=SECRET)
    row = db.get(ExternalService, SPEECH)
    db.refresh(row)
    with pytest.raises((ValueError, InvalidTag)):
        unpack_credential_envelope(row.credential_envelope)
    from app.services.external_service_crypto import open_external_credential

    assert open_external_credential(row.credential_envelope) == SECRET


def test_worker_compose_does_not_mount_the_csp_only_key_volume():
    for relative in ("infra/compose/platform.yml", "infra/compose/dev.yml"):
        doc = yaml.safe_load((REPO / relative).read_text(encoding="utf-8"))
        csp_mounts = doc["services"]["csp"]["volumes"]
        worker_mounts = doc["services"]["ingestion-worker"]["volumes"]
        assert any("csp-local-secrets" in item for item in csp_mounts), relative
        assert not any("csp-local-secrets" in item for item in worker_mounts), relative
    dockerfile = (REPO / "infra/docker/csp.Dockerfile").read_text(encoding="utf-8")
    assert "/var/anila/csp-local-secrets" in dockerfile
    worker_source = (
        REPO / "services/ingestion-worker/src/ingestion_worker/docling_source.py"
    ).read_text(encoding="utf-8")
    assert "external_services" not in worker_source
    assert "unpack_credential_envelope" not in worker_source


def test_status_get_and_admin_probe_do_not_call_upstream(client, db, probe_client):
    headers = _admin(client, db)
    _save(
        client,
        headers,
        "speech",
        base_url=SPEECH_URL,
        protocol="openai",
        credential=SECRET,
    )
    make_user(db, username="iso-listener", role="user")
    user_headers = {"Authorization": f"Bearer {login(client, 'iso-listener')}"}
    first = client.get("/api/external-services/speech/status", headers=user_headers)
    second = client.get("/api/external-services/speech/status", headers=user_headers)
    posted = client.post("/api/admin/external-services/speech/probe", headers=headers)
    assert first.status_code == 200
    assert second.status_code == 200
    assert posted.status_code == 200
    assert first.json()["enabled"] is True
    assert first.json()["healthy"] is False
    assert probe_client.calls == []
    assert SECRET not in first.text
    assert SECRET not in posted.text


def test_probe_cycle_is_single_flight_and_waits_out_the_interval(db, probe_client, monkeypatch):
    from app.services import external_services as svc

    row = ExternalService(
        service_key=SPEECH,
        enabled=True,
        base_url=SPEECH_URL,
        protocol="openai",
        credential_envelope=svc.encrypt_external_credential(SECRET),
        health_status="unknown",
        env_seeded=True,
        updated_at=datetime.now(timezone.utc),
    )
    db.merge(row)
    db.commit()

    held = svc.probe_flight_lock()
    assert held.acquire(blocking=False)
    try:
        assert svc.run_probe_cycle(db) is False
        assert probe_client.calls == []
    finally:
        held.release()

    assert svc.run_probe_cycle(db) is True
    assert len(probe_client.calls) == 1
    assert probe_client.calls[0][0] == "post"
    assert probe_client.calls[0][2].get("Authorization") == f"Bearer {SECRET}"

    probe_client.calls = []
    assert svc.run_probe_cycle(db) is True
    assert probe_client.calls == []

    stored = db.get(ExternalService, SPEECH)
    db.refresh(stored)
    stored.health_checked_at = datetime.now(timezone.utc) - timedelta(seconds=31)
    db.commit()
    assert svc.run_probe_cycle(db) is True
    assert len(probe_client.calls) == 1


def test_userinfo_is_rejected_and_masked_when_already_stored(
    client, db, caplog, monkeypatch
):
    headers = _admin(client, db)
    poisoned = "https://alice:s3cret@docling.example.test:9100/parse"
    rejected = client.put(
        "/api/admin/external-services/document_parser",
        headers=headers,
        json={"enabled": True, "base_url": poisoned, "credential": SECRET},
    )
    assert rejected.status_code == 400, rejected.text
    assert "s3cret" not in rejected.text
    assert "alice:s3cret" not in rejected.text
    row = db.get(ExternalService, DOCUMENT_PARSER)
    if row is not None:
        db.refresh(row)
        assert "s3cret" not in (row.base_url or "")

    from app.services.external_services import ensure_rows

    ensure_rows(db)
    stored = db.get(ExternalService, DOCUMENT_PARSER)
    stored.base_url = poisoned
    stored.enabled = True
    db.commit()
    listed = client.get("/api/admin/external-services", headers=headers)
    assert listed.status_code == 200
    assert "s3cret" not in listed.text
    assert "alice" not in listed.text
    assert "docling.example.test" in listed.text

    monkeypatch.setenv("DOC_PARSER", "docling")
    monkeypatch.setenv("DOCLING_URL", poisoned)
    monkeypatch.setenv("DOCLING_SERVICE_TOKEN", SECRET)
    monkeypatch.delenv("ANILA_ALLOW_HTTP_ENDPOINT", raising=False)
    stored.base_url = ""
    stored.enabled = False
    stored.env_seeded = False
    stored.credential_envelope = None
    db.commit()
    with caplog.at_level(logging.INFO):
        import_legacy_env_once(db)
    assert "s3cret" not in caplog.text
    db.refresh(stored)
    assert "s3cret" not in (stored.base_url or "")


def test_external_service_urls_reject_grpc_even_when_the_model_flag_is_on(
    client, db, monkeypatch
):
    monkeypatch.setenv("ANILA_ALLOW_GRPC_ENDPOINT", "1")
    headers = _admin(client, db)
    for url in (
        "grpcs://triton.example.test:9001",
        "grpc://triton.example.test:9001",
        "ftp://files.example.test/doc",
    ):
        response = client.put(
            "/api/admin/external-services/document_parser",
            headers=headers,
            json={"enabled": True, "base_url": url},
        )
        assert response.status_code == 400, response.text
        row = db.get(ExternalService, DOCUMENT_PARSER)
        if row is not None:
            db.refresh(row)
            assert (row.base_url or "") == ""
