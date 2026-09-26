"""Task 3 — platform s2s endpoints must state admitted principal kinds.

A1: agent-kind ``csk-`` is rejected on each of the four endpoints.
A2: router-kind ``service_client`` still succeeds on router-primary;
    agent-kind does not.
R2: deactivating the owning ``service_clients`` row must not silently
    widen a ``client_type`` restriction via the env fallback.
R4: denials leave a diagnosable audit/log record (no token, no model URL).

``client_type`` taxonomy (``app.api.service_clients._CLIENT_TYPES``):
``router`` | ``worker`` | ``admin_tool``.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi.testclient import TestClient

from app.api import _service_principal as sp
from app.models.audit_log import AuditLog
from app.models.model_registry import ModelRegistry
from app.models.service_client import ServiceClient
from app.models.token_revocation import TokenRevocation
from app.services import agent_credential_service
from app.services.service_token_envelope import (
    compute_lookup_hash,
    encode_service_token_envelope,
    generate_service_token,
)
from tests.conftest import make_agent, make_user


def _agent_csk(db) -> str:
    admin = make_user(db, username="kindgate-admin", role="admin")
    owner = make_user(db, username="kindgate-owner")
    agent = make_agent(db, owner=owner, name="kindgate-agent", approval_status="approved")
    _cred, csk = agent_credential_service.issue_static_credential(
        db, agent=agent, issuer=admin, label="kind-gate"
    )
    db.commit()
    return csk


def _service_client_token(db, *, name: str, client_type: str) -> str:
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


def _plant_router_primary(db) -> ModelRegistry:
    model = ModelRegistry(
        name="kindgate-router-llm",
        display_name="kindgate-router-llm",
        model_type="llm",
        endpoint_url="https://llm.example.internal/v1",
        is_active=True,
        is_router_primary=True,
    )
    db.add(model)
    db.commit()
    db.refresh(model)
    return model


def _plant_image_primary(db) -> ModelRegistry:
    model = ModelRegistry(
        name="kindgate-image",
        display_name="kindgate-image",
        model_type="image",
        endpoint_url="https://flux.example.internal/generate",
        is_active=True,
        is_image_primary=True,
    )
    db.add(model)
    db.commit()
    db.refresh(model)
    return model


def _plant_asr_primary(db) -> ModelRegistry:
    model = ModelRegistry(
        name="kindgate-asr",
        display_name="kindgate-asr",
        model_type="asr",
        endpoint_url="https://asr.example.internal/v1",
        is_active=True,
        is_asr_primary=True,
    )
    db.add(model)
    db.commit()
    db.refresh(model)
    return model


@pytest.mark.parametrize(
    "endpoint,setup",
    [
        ("GET /api/auth/revocations", "revocations"),
        ("GET /api/models/router-primary", "router"),
        ("GET /api/models/image-primary", "image"),
        ("GET /api/models/asr-primary", "asr"),
    ],
    ids=[
        "revocations",
        "router-primary",
        "image-primary",
        "asr-primary",
    ],
)
def test_a1_agent_kind_rejected_on_each_platform_endpoint(
    client: TestClient, db, endpoint: str, setup: str
):
    """長效 agent csk- 在驗證階段就是 401，到不了 kind gate。"""
    csk = _agent_csk(db)
    headers = {"X-CSP-Service-Token": csk}

    if setup == "revocations":
        since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        resp = client.get(
            "/api/auth/revocations",
            params={"since": since},
            headers=headers,
        )
    elif setup == "router":
        _plant_router_primary(db)
        resp = client.get("/api/models/router-primary", headers=headers)
    elif setup == "image":
        _plant_image_primary(db)
        resp = client.get("/api/models/image-primary", headers=headers)
    else:
        _plant_asr_primary(db)
        resp = client.get("/api/models/asr-primary", headers=headers)

    assert resp.status_code == 401, (
        f"{endpoint}: expected 401 for a retired agent credential, got "
        f"{resp.status_code}: {resp.text}"
    )
    assert resp.json()["detail"] == "服務權杖無效"


def test_a2_router_kind_succeeds_agent_kind_fails_on_router_primary(
    client: TestClient, db
):
    _plant_router_primary(db)

    router_tok = _service_client_token(
        db, name="kindgate-router-primary", client_type="router"
    )
    ok = client.get(
        "/api/models/router-primary",
        headers={"X-CSP-Service-Token": router_tok},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["name"] == "kindgate-router-llm"
    assert ok.json()["endpoint_url"] == "https://llm.example.internal/v1"

    agent_tok = _agent_csk(db)
    denied = client.get(
        "/api/models/router-primary",
        headers={"X-CSP-Service-Token": agent_tok},
    )
    assert denied.status_code == 401, denied.text
    assert denied.json()["detail"] == "服務權杖無效"


def test_router_primary_rejects_worker_client_type(client: TestClient, db):
    """client_type='worker' is a real taxonomy value (qa1b-verify) but not
    admitted on router-primary."""
    _plant_router_primary(db)
    worker_tok = _service_client_token(
        db, name="kindgate-worker", client_type="worker"
    )
    resp = client.get(
        "/api/models/router-primary",
        headers={"X-CSP-Service-Token": worker_tok},
    )
    assert resp.status_code == 403, resp.text
    assert "client_type" in resp.json()["detail"]


def test_revocations_still_accepts_legacy_env_token(
    client: TestClient, db, monkeypatch
):
    """Unattributed env fallback still admitted when no client_type gate."""
    from app.config import settings as canonical_settings
    from app.services import auth_service

    token = "csk-kindgate-legacy-revocations"
    monkeypatch.setattr(canonical_settings, "CSP_SERVICE_TOKEN", token, raising=False)
    monkeypatch.setattr(auth_service.settings, "CSP_SERVICE_TOKEN", token, raising=False)

    admin = make_user(db, username="kindgate-rev-admin", role="admin")
    db.add(
        TokenRevocation(
            user_id=admin.id,
            revoked_at_version=1,
            revoked_at=datetime.now(timezone.utc),
        )
    )
    db.commit()

    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    resp = client.get(
        "/api/auth/revocations",
        params={"since": since},
        headers={"X-CSP-Service-Token": token},
    )
    assert resp.status_code == 200, resp.text


def test_as_str_set_rejects_bare_str():
    """PROVE RED: delete the ``isinstance(values, str)`` branch in
    ``_as_str_set`` → this test fails (bare ``"router"`` becomes a
    frozenset of characters and the classic char-match bug returns).
    """
    with pytest.raises(TypeError, match="bare str"):
        sp._as_str_set("router", param="allowed_kinds")
    with pytest.raises(TypeError, match="bare str"):
        sp._as_str_set("router", param="allowed_client_types")
    # Happy path still normalises sequences.
    assert sp._as_str_set(("router",), param="allowed_kinds") == frozenset({"router"})


def test_r2_helper_refuses_unattributed_when_client_types_set(db):
    """Even with allow_legacy_env=True, a client_type restriction must
    refuse identity is None — otherwise deactivating the owning row
    silently widens admission.
    """
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        sp.require_admitted_service_principal(
            None,
            db=db,
            allowed_kinds=("service_client",),
            allowed_client_types=("router",),
            allow_legacy_env=True,
            endpoint="GET /api/models/router-primary",
        )
    assert ei.value.status_code == 403
    assert "client_type" in ei.value.detail


def test_f6_env_rotated_without_db_row_returns_403_on_router_primary(
    client: TestClient, db, monkeypatch
):
    """LANDMINE: rotate CSP_SERVICE_TOKEN in .env without rotating the
    ``router-primary`` service_clients row → callers present the new
    secret → DB miss → env fallback (identity None) → kind gate 403.

    Exact ``detail`` string is what operators see; keep in sync with
    ``docs/runbooks/service-token-cutover.md`` Hazard section.
    """
    from app.config import settings as canonical_settings
    from app.services import auth_service

    _plant_router_primary(db)
    old = "csk-kindgate-fleet-old-f6"
    new = "csk-kindgate-fleet-rotated-f6"
    db.add(
        ServiceClient(
            client_name="router-primary",
            client_type="router",
            service_token_envelope=encode_service_token_envelope(old),
            service_token_lookup_hash=compute_lookup_hash(old),
            is_legacy=True,
            is_active=True,
        )
    )
    db.commit()

    # Operator rotated .env only — CSP now holds the new secret.
    monkeypatch.setattr(canonical_settings, "CSP_SERVICE_TOKEN", new, raising=False)
    monkeypatch.setattr(auth_service.settings, "CSP_SERVICE_TOKEN", new, raising=False)

    resp = client.get(
        "/api/models/router-primary",
        headers={"X-CSP-Service-Token": new},
    )
    assert resp.status_code == 403, (
        f"expected 403 when env rotated without DB row, "
        f"got {resp.status_code}: {resp.text}"
    )
    expected_detail = (
        "GET /api/models/router-primary 要求 client_type=['router'];"
        "未歸屬的 legacy env token 無法證明 client_type"
    )
    assert resp.json() == {"detail": expected_detail}

    # Control: presenting the still-current DB secret still works.
    ok = client.get(
        "/api/models/router-primary",
        headers={"X-CSP-Service-Token": old},
    )
    assert ok.status_code == 200, ok.text


def test_r2_inactive_router_primary_row_fails_closed_on_router_primary(
    client: TestClient, db, monkeypatch
):
    """INVARIANT: client_type restriction must not vanish when the owning
    service_clients row is deactivated (ordinary rotate/cutover action).

    Production shape: fleet secret lives in service_clients
    client_name='router-primary'. Deactivate → DB miss → env fallback
    → identity None. Gate must 403, not silently allow.

    PROVE RED (helper layer): see
    ``test_r2_helper_refuses_unattributed_when_client_types_set`` — flip
    the ``client_types is not None`` fail-closed while keeping
    ``allow_legacy_env=True``. Call site here uses ``allow_legacy_env=False``
    as a second belt; mutating only the helper branch will not redden
    *this* integration test.
    """
    from app.config import settings as canonical_settings
    from app.services import auth_service

    _plant_router_primary(db)
    fleet = "csk-kindgate-fleet-shared-r2"
    row = ServiceClient(
        client_name="router-primary",
        client_type="router",
        service_token_envelope=encode_service_token_envelope(fleet),
        service_token_lookup_hash=compute_lookup_hash(fleet),
        is_legacy=True,
        is_active=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    # Ordinary operator action: deactivate the owning principal.
    row.is_active = False
    db.commit()

    monkeypatch.setattr(canonical_settings, "CSP_SERVICE_TOKEN", fleet, raising=False)
    monkeypatch.setattr(auth_service.settings, "CSP_SERVICE_TOKEN", fleet, raising=False)

    resp = client.get(
        "/api/models/router-primary",
        headers={"X-CSP-Service-Token": fleet},
    )
    assert resp.status_code == 403, (
        f"expected fail-closed 403 after deactivating owning row, "
        f"got {resp.status_code}: {resp.text}"
    )
    detail = resp.json()["detail"]
    assert "client_type" in detail or "legacy" in detail.lower()


def test_r4_denial_writes_diagnosable_audit_without_secrets(
    client: TestClient, db, caplog
):
    """A kind-gate 403 must leave an audit row + log line naming the
    endpoint and principal kind — never the token or a model URL."""
    _plant_router_primary(db)
    # 長效 agent csk- 在驗證階段就是 401，到不了 kind gate。
    # 這裡用仍會通過驗證、但 client_type 不被接受的 worker，確認拒絕稽核還在。
    csk = _service_client_token(
        db, name="kindgate-worker-audit", client_type="worker"
    )
    model_url = "https://llm.example.internal/v1"

    with caplog.at_level(logging.WARNING, logger="app.api._service_principal"):
        resp = client.get(
            "/api/models/router-primary",
            headers={"X-CSP-Service-Token": csk},
        )
    assert resp.status_code == 403, resp.text

    events = (
        db.query(AuditLog)
        .filter(AuditLog.action == sp.AUDIT_KIND_DENIED)
        .order_by(AuditLog.id.desc())
        .all()
    )
    assert events, "expected service_principal_kind_denied audit row"
    latest = events[0]
    blob = " ".join(
        filter(
            None,
            [latest.detail, latest.metadata_json, latest.resource_id],
        )
    )
    assert "GET /api/models/router-primary" in blob
    assert "kind" in blob
    assert csk not in blob
    assert model_url not in blob
    assert "service_token_envelope" not in blob

    log_blob = " ".join(r.message for r in caplog.records)
    assert "service_principal_kind_denied" in log_blob
    assert "GET /api/models/router-primary" in log_blob
    assert csk not in log_blob
    assert model_url not in log_blob


def test_me_runtime_config_endpoint_is_gone(client: TestClient, db):
    """Task 1: agent self-fetch poll target removed."""
    csk = _agent_csk(db)
    resp = client.get(
        "/api/agents/me/runtime-config",
        headers={"X-CSP-Service-Token": csk},
    )
    # FastAPI may 404 (no route) or 405/422 depending on path matching;
    # must not be the old 200 JSON payload.
    assert resp.status_code != 200
    body = resp.json() if resp.headers.get("content-type", "").startswith(
        "application/json"
    ) else {}
    assert "etag" not in body
    assert body.get("runtime_config") is None or "runtime_config" not in body
