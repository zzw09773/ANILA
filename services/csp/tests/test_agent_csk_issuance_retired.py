"""P2.1 Task 2+4 — agent csk-/bsk- issuance + credentials/me are 410.

A2: each retired endpoint returns 410; surfaces that must keep working
still do (classification, admin credential list, service_clients
issue-static, router-primary via service_client token).

Mutation note (manual): restore any retired handler body → the matching
``test_*_returns_410`` goes red; restore the 410 stub → sha256 matches.
"""

from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.models.agent_credential import AgentCredential
from app.models.service_client import ServiceClient
from app.services.service_token_envelope import (
    compute_lookup_hash,
    encode_service_token_envelope,
    generate_service_token,
)
from tests.conftest import login, make_agent, make_user


def _admin_headers(client: TestClient, db, username: str = "csk-ret-admin"):
    make_user(db, username=username, role="admin")
    token = login(client, username)
    return {"Authorization": f"Bearer {token}"}


def _dev_headers(client: TestClient, db, username: str = "csk-ret-dev"):
    user = make_user(db, username=username, role="developer")
    token = login(client, username)
    return user, {"Authorization": f"Bearer {token}"}


# ── Retired agent issuance / poll faces (410) ──────────────────────────────


def test_issue_bootstrap_returns_410(client: TestClient, db):
    owner = make_user(db, username="csk-ret-owner-bsk", role="developer")
    agent = make_agent(db, owner, name="retire-bsk-agent")
    headers = _admin_headers(client, db, "csk-ret-admin-bsk")

    resp = client.post(
        f"/api/agents/{agent.id}/issue-bootstrap",
        json={"ttl_seconds": 900},
        headers=headers,
    )
    assert resp.status_code == 410
    assert "csk-" in resp.json()["detail"] or "bsk-" in resp.json()["detail"]


def test_bootstrap_exchange_returns_410(client: TestClient, db):
    owner = make_user(db, username="csk-ret-owner-xchg", role="developer")
    agent = make_agent(db, owner, name="retire-xchg-agent")

    # Public (token-gated) endpoint — no admin JWT required; still 410.
    resp = client.post(
        f"/api/agents/{agent.id}/bootstrap",
        json={
            "bootstrap_token": "bsk-deadbeef",
            "endpoint_url": agent.endpoint_url,
        },
    )
    assert resp.status_code == 410


def test_issue_static_returns_410(client: TestClient, db):
    owner, headers = _dev_headers(client, db, "csk-ret-owner-static")
    agent = make_agent(db, owner, name="retire-static-agent")

    resp = client.post(
        f"/api/agents/{agent.id}/credentials/issue-static",
        json={"label": "should-not-mint"},
        headers=headers,
    )
    assert resp.status_code == 410


def test_rotate_credential_returns_410(client: TestClient, db):
    owner = make_user(db, username="csk-ret-owner-rot", role="developer")
    agent = make_agent(db, owner, name="retire-rot-agent")
    # Seed an orphan row so the path param is realistic; rotate must
    # still refuse rather than mint a new plaintext.
    plaintext = generate_service_token()
    cred = AgentCredential(
        agent_id=agent.id,
        label="orphan",
        service_token_envelope=encode_service_token_envelope(plaintext),
        service_token_lookup_hash=compute_lookup_hash(plaintext),
        service_token_issued_at=datetime.now(timezone.utc),
        is_legacy=False,
        is_active=True,
    )
    db.add(cred)
    db.commit()
    db.refresh(cred)
    headers = _admin_headers(client, db, "csk-ret-admin-rot")

    resp = client.post(
        f"/api/agents/{agent.id}/credentials/{cred.id}/rotate",
        json={"grace_seconds": 3600},
        headers=headers,
    )
    assert resp.status_code == 410


def test_credentials_me_returns_410(client: TestClient, db):
    """Task 2: the only remaining agent-kind consumer is gone."""
    owner = make_user(db, username="csk-ret-owner-me", role="developer")
    agent = make_agent(db, owner, name="retire-me-agent")

    resp = client.get(f"/api/agents/{agent.id}/credentials/me")
    assert resp.status_code == 410
    detail = resp.json()["detail"]
    assert "P2.1" in detail or "廢止" in detail


# ── Must-still-work surfaces ───────────────────────────────────────────────


def test_admin_list_credentials_still_works(client: TestClient, db):
    owner = make_user(db, username="csk-ret-owner-list", role="developer")
    agent = make_agent(db, owner, name="retire-list-agent")
    headers = _admin_headers(client, db, "csk-ret-admin-list")

    resp = client.get(
        f"/api/agents/{agent.id}/credentials",
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json() == []


def test_classification_still_works(client: TestClient, db):
    owner, headers = _dev_headers(client, db, "csk-ret-owner-class")
    agent = make_agent(db, owner, name="retire-class-agent")

    resp = client.post(
        f"/api/agents/{agent.id}/classification",
        json={"default_classification_level": "機密"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["default_classification_level"] == "機密"


def test_service_clients_issue_static_still_works(client: TestClient, db):
    """A4: platform s2s issuance is a DIFFERENT endpoint — must stay."""
    headers = _admin_headers(client, db, "csk-ret-admin-svc")
    token = generate_service_token()
    row = ServiceClient(
        client_name="csk-ret-svc-client",
        client_type="worker",
        service_token_envelope=encode_service_token_envelope(token),
        service_token_lookup_hash=compute_lookup_hash(token),
        service_token_issued_at=datetime.now(timezone.utc),
        is_legacy=False,
        is_active=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    resp = client.post(
        f"/api/service-clients/{row.id}/issue-static",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["service_token"].startswith("csk-")
    assert body["client"]["id"] == row.id
    assert body["service_token"] != token


def test_router_primary_still_admits_router_service_client(
    client: TestClient, db
):
    """A3: router-kind service_client still reaches router-primary."""
    from app.models.model_registry import ModelRegistry

    model = ModelRegistry(
        name="csk-ret-router-llm",
        display_name="csk-ret-router-llm",
        model_type="llm",
        endpoint_url="https://llm.example.internal/v1",
        is_active=True,
        is_router_primary=True,
    )
    db.add(model)
    db.commit()
    db.refresh(model)

    token = generate_service_token()
    db.add(
        ServiceClient(
            client_name="csk-ret-router-primary",
            client_type="router",
            service_token_envelope=encode_service_token_envelope(token),
            service_token_lookup_hash=compute_lookup_hash(token),
            service_token_issued_at=datetime.now(timezone.utc),
            is_legacy=False,
            is_active=True,
        )
    )
    db.commit()

    resp = client.get(
        "/api/models/router-primary",
        headers={"X-CSP-Service-Token": token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == model.id
    assert resp.json()["name"] == "csk-ret-router-llm"


def test_register_approve_dispatch_jwt_no_csk(client: TestClient, db, monkeypatch):
    """Q3 / A1: new agent path needs no agent csk-; dispatch mints JWT."""
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_TRUSTED_HOSTS", "agent,mock-llm")

    from app.services.proxy.headers import build_agent_headers
    from app.services.proxy.dispatch_token import verify_dispatch_token
    from tests.conftest import make_model

    make_user(db, username="csk-ret-reg-dev", role="developer")
    make_user(db, username="csk-ret-reg-admin", role="admin")
    model = make_model(db, name="csk-ret-reg-model")
    dev_token = login(client, "csk-ret-reg-dev")
    admin_token = login(client, "csk-ret-reg-admin")

    reg = client.post(
        "/api/agents/register",
        json={
            "name": "csk-ret-new-agent",
            "endpoint_url": "http://agent:9100",
            "description_for_router": "Q3 path without agent credential",
            "base_model_id": model.id,
        },
        headers={"Authorization": f"Bearer {dev_token}"},
    )
    assert reg.status_code == 200, reg.text
    body = reg.json()
    agent_id = body["id"]
    assert "service_token" not in body
    assert "credential_id" not in body
    assert "bootstrap_token" not in body

    # Issuance face is closed even for the freshly registered agent.
    issue = client.post(
        f"/api/agents/{agent_id}/credentials/issue-static",
        json={},
        headers={"Authorization": f"Bearer {dev_token}"},
    )
    assert issue.status_code == 410

    approve = client.post(
        f"/api/agents/{agent_id}/approve",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert approve.status_code == 200, approve.text
    assert "已核准" in approve.json()["message"]

    get_resp = client.get(
        f"/api/agents/{agent_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert get_resp.status_code == 200
    assert get_resp.json()["approval_status"] == "approved"

    # Dispatch header path (what proxy sends to the agent) — JWT only.
    headers = build_agent_headers(
        user_id=1, department=None, agent_id=agent_id
    )
    assert "X-CSP-Service-Token" not in headers
    assert headers["Authorization"].startswith("Bearer ")
    claims = verify_dispatch_token(headers["Authorization"].split(" ", 1)[1])
    assert claims is not None
    assert int(claims["agent_id"]) == agent_id

    # Live agent HTTP reachability is environment-dependent (no mock
    # agent listening in unit tests). The contract that replaces csk-
    # dispatch is proven above.
