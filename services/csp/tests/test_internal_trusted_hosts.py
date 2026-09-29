"""asr-gateway 與 ingestion-worker 各用自己的憑證讀治理中心的信任主機。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.models.trusted_host import TrustedHost
from tests.conftest import make_api_key, make_user
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


def test_worker_key_reads_the_console_trusted_hosts(client: TestClient, db):
    """遠端 Docling 在私網時，worker 的放行名單來自這張表，不是 .env。"""
    worker = make_user(db, username="ingestion-worker", role="system")
    make_api_key(db, worker, "sk-worker-hosts")
    db.add(TrustedHost(host="172.16.120.35", note="Docling"))
    db.commit()

    resp = client.get(
        "/api/internal/trusted-hosts",
        headers={"Authorization": "Bearer sk-worker-hosts"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"hosts": ["172.16.120.35"]}


def test_other_users_keys_cannot_read_trusted_hosts(client: TestClient, db):
    stranger = make_user(db, username="not-the-worker-hosts", role="user")
    make_api_key(db, stranger, "sk-stranger-hosts")
    resp = client.get(
        "/api/internal/trusted-hosts",
        headers={"Authorization": "Bearer sk-stranger-hosts"},
    )
    assert resp.status_code == 403, resp.text
