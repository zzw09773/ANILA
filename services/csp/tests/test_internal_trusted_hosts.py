"""asr-gateway 用自己的服務權杖讀治理中心的信任主機。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.models.trusted_host import TrustedHost
from tests.test_external_service_isolation import _mint_service_client


def test_asr_reads_the_console_trusted_hosts(client: TestClient, db):
    token = _mint_service_client(db, name="asr-hosts", client_type="asr")
    db.add(TrustedHost(host="Asr-Decoder", note="語音"))
    db.commit()

    resp = client.get(
        "/api/internal/trusted-hosts",
        headers={"X-CSP-Service-Token": token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"hosts": ["asr-decoder"]}


def test_other_service_clients_cannot_read_trusted_hosts(client: TestClient, db):
    token = _mint_service_client(db, name="router-hosts", client_type="router")
    resp = client.get(
        "/api/internal/trusted-hosts",
        headers={"X-CSP-Service-Token": token},
    )
    assert resp.status_code == 403, resp.text


def test_missing_service_token_is_unauthorized(client: TestClient, db):
    resp = client.get("/api/internal/trusted-hosts")
    assert resp.status_code == 401, resp.text
