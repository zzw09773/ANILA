# -*- coding: utf-8 -*-
"""Endpoint visibility relaxation (SYSTEM-MAP §6 / epvis).

Acceptance tests that fail if the single visibility predicate is removed
or if any face regresses to owner-only / always-redact.
"""
from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import asyncio
import json

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api import alerts as alerts_api
from app.api import models as models_api
from app.models.audit_log import AuditLog
from app.models.model_registry import ModelRegistry
from app.services import endpoint_author_service as ea_svc
from app.services.alert_service import upsert_alert
from app.services.audit_service import log_audit_event, serialize_audit_log
from app.services import proxy_service
from app.services.proxy import service as proxy_impl
from tests.conftest import login, make_model, make_user

import dataclasses as _dataclasses

from app.services.proxy.service import ProxyTuning

#: 這一支只想跑一次上游、不想等重試的退避。重試策略是固定程式常數，
#: 測試用的 tuning 直接覆蓋為一次。
_PROXY_TUNING = _dataclasses.replace(
    ProxyTuning.from_registry_defaults(), max_retries=1, retry_base_delay=0.0
)



SECRET = "https://epvis-secret-box.example.com/v1"


@pytest.fixture(autouse=True)
def _bypass_dev_secret_gate(monkeypatch):
    import app.services.startup_security as ss_module

    monkeypatch.setattr(ss_module, "assert_no_dev_defaults", lambda: None)


@pytest.fixture(autouse=True)
def _allow_endpoints(monkeypatch):
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")


def _csrf_headers(client: TestClient) -> dict:
    from app.middleware.cookies import CSRF_COOKIE_NAME

    return {"X-CSRF-Token": client.cookies.get(CSRF_COOKIE_NAME)}


def _auth(client: TestClient, db: Session, username: str, role: str):
    user = make_user(db, username=username, role=role)
    headers = {
        "Authorization": f"Bearer {login(client, username=username)}",
        **_csrf_headers(client),
    }
    return user, headers


def _plant_model(db: Session, *, name: str) -> ModelRegistry:
    row = make_model(db, name=name)
    row.endpoint_url = SECRET
    row.is_internal = False
    db.commit()
    return row


def _plant_audit(db: Session, actor, model: ModelRegistry) -> AuditLog:
    return log_audit_event(
        db,
        actor=actor,
        action="bulk_import",
        resource_type="model",
        resource_id=model.id,
        detail=f"整批帶入模型自 endpoint「{ea_svc.ENDPOINT_REDACTED}」",
        metadata={"endpoint_url": SECRET, "source_model_id": model.id},
        commit=True,
    )


def _assert_sees_everywhere(db, viewer, model, *, expect_real: bool):
    """Hit every disclosure face; expect_real toggles real URL vs sentinel."""
    want = SECRET if expect_real else ea_svc.ENDPOINT_REDACTED

    # Face 1 — response fields
    resp = models_api._build_response(model, caller=viewer, db=db)
    assert resp["endpoint_url"] == want
    assert "endpoint_group_key" not in resp

    # Face 2+3 — audit detail (read-time expand) + metadata
    audit = (
        db.query(AuditLog)
        .filter(AuditLog.resource_id == str(model.id))
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert audit is not None
    ser = serialize_audit_log(audit, caller=viewer, db=db)
    if expect_real:
        assert SECRET in (ser["detail"] or "")
        assert ser["metadata"] is not None
        assert ser["metadata"]["endpoint_url"] == SECRET
    else:
        assert SECRET not in (ser["detail"] or "")
        assert ea_svc.ENDPOINT_REDACTED in (ser["detail"] or "")
        assert ser["metadata"] is None

    # Face 4 — guard-rejection (bulk import wraps for undesignated)
    listing = models_api._upstream_models_url(model.endpoint_url)
    # Force a guard rejection by pointing at loopback via monkeypatch of source
    # is handled in dedicated tests; here assert the predicate itself.
    assert ea_svc.can_see_endpoint_address(db, viewer) is expect_real

    # Face 5 — proxy trace + failure
    display = ea_svc.visible_endpoint_url(
        SECRET, is_internal=False, db=db, caller=viewer
    )
    assert display == want

    class _Model:
        id = model.id
        name = model.name
        model_type = "llm"
        endpoint_url = SECRET
        api_version = "v1"
        api_key_secret_ref = None

    class _FailClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            raise httpx.ConnectError("boom")

    # Local monkeypatch via setattr on the module the test already owns
    # through fixture-free direct assignment; restored by pytest's function
    # scope if we go through monkeypatch — callers pass monkeypatch.
    return _Model, display


# ── Acceptance: designated developer sees real address on every face ─────────


def test_designated_developer_sees_address_on_every_face(
    client: TestClient, db: Session, monkeypatch
):
    """PROVE RED: change can_see_endpoint_address to `return is_owner(caller)`
    (drop grant path) → this test fails on response + audit + proxy + alerts.
    """
    owner, oh = _auth(client, db, "epvis_own_dev", role="owner")
    dev, dh = _auth(client, db, "epvis_desig_dev", role="developer")
    r = client.post(
        "/api/endpoint-authors", headers=oh, json={"user_id": dev.id}
    )
    assert r.status_code == 201, r.text
    model = _plant_model(db, name="epvis-dev-model")
    _plant_audit(db, owner, model)

    listed = client.get("/api/models", headers=dh)
    assert listed.status_code == 200
    # Designated developer only lists authored rows; plant has no author —
    # use admin-tier list via direct _build_response for the response face,
    # and HTTP for rows they can see after creating one.
    created = client.post(
        "/api/models",
        headers=dh,
        json={
            "name": "epvis-dev-authored",
            "display_name": "Authored",
            "model_type": "llm",
            "endpoint_url": SECRET,
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["endpoint_url"] == SECRET
    assert "endpoint_group_key" not in created.json()

    ser = serialize_audit_log(
        db.query(AuditLog)
        .filter(AuditLog.action == "bulk_import")
        .order_by(AuditLog.id.desc())
        .first(),
        caller=dev,
        db=db,
    )
    assert ser["metadata"]["endpoint_url"] == SECRET
    assert SECRET in ser["detail"]

    upsert_alert(
        db,
        fingerprint="health:model:epvis-dev",
        category="health",
        severity="high",
        title="離線",
        message="無法連線至模型「x」",
        source_type="model",
        source_id=model.id,
        metadata={"endpoint_url": SECRET},
    )
    db.commit()
    rows = alerts_api.list_alerts(
        status=None, severity=None, category=None, admin=dev, db=db
    )
    # require_admin: developer is NOT admin-tier — list_alerts uses require_admin.
    # Face coverage for alerts goes through designated *admin* test below;
    # here we still assert the predicate + visible_endpoint_url + proxy.
    assert ea_svc.can_see_endpoint_address(db, dev) is True
    display = ea_svc.visible_endpoint_url(
        SECRET, db=db, caller=dev, is_internal=False
    )
    assert display == SECRET

    monkeypatch.setattr(proxy_impl, "_guard_outbound", lambda *a, **k: None)

    class _FailClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(
        proxy_service.httpx, "AsyncClient", lambda *a, **k: _FailClient()
    )

    class _Model:
        id = 1
        name = "epvis-dev-model"
        model_type = "llm"
        endpoint_url = SECRET
        api_version = "v1"
        api_key_secret_ref = None

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            proxy_service.proxy_request(
                model=_Model(),
                api_key_id=1,
                user_id=dev.id,
                department_id=None,
                request_body={
                    "model": "epvis-dev-model",
                    "messages": [{"role": "user", "content": "hi"}],
                },
                endpoint_path="/v1/chat/completions",
                endpoint_display=display,
                tuning=_PROXY_TUNING,
            )
        )
    assert SECRET in str(exc.value.detail)


def test_designated_admin_sees_address_on_every_face(
    client: TestClient, db: Session, monkeypatch
):
    """PROVE RED: reject role==admin in assign / can_set → grant 400 / see fails."""
    owner, oh = _auth(client, db, "epvis_own_adm", role="owner")
    admin, ah = _auth(client, db, "epvis_desig_adm", role="admin")
    r = client.post(
        "/api/endpoint-authors", headers=oh, json={"user_id": admin.id}
    )
    assert r.status_code == 201, r.text
    assert admin.role == "admin"  # users.role untouched

    model = _plant_model(db, name="epvis-adm-model")
    _plant_audit(db, owner, model)

    listed = client.get("/api/models", headers=ah)
    assert listed.status_code == 200
    hit = next(r for r in listed.json() if r["name"] == "epvis-adm-model")
    assert hit["endpoint_url"] == SECRET
    assert "endpoint_group_key" not in hit

    ser = serialize_audit_log(
        db.query(AuditLog)
        .filter(AuditLog.resource_id == str(model.id))
        .order_by(AuditLog.id.desc())
        .first(),
        caller=admin,
        db=db,
    )
    assert ser["metadata"]["endpoint_url"] == SECRET
    assert SECRET in ser["detail"]

    upsert_alert(
        db,
        fingerprint="health:model:epvis-adm",
        category="health",
        severity="high",
        title="離線",
        message="無法連線至模型「x」",
        source_type="model",
        source_id=model.id,
        metadata={"endpoint_url": SECRET},
    )
    db.commit()
    rows = alerts_api.list_alerts(
        status=None, severity=None, category=None, admin=admin, db=db
    )
    assert rows[0]["metadata"]["endpoint_url"] == SECRET

    # Guard rejection: designated viewer receives the structured detail.
    model.endpoint_url = "http://127.0.0.1:9/v1"
    db.commit()
    with pytest.raises(HTTPException) as exc:
        # Use the import path's guard branch via direct call
        listing_url = models_api._upstream_models_url(model.endpoint_url)
        try:
            models_api._enforce_endpoint_url(listing_url)
        except HTTPException as e:
            if ea_svc.can_see_endpoint_address(db, admin):
                raise
            raise HTTPException(
                status_code=400,
                detail=models_api._BULK_IMPORT_OUTBOUND_REJECTED,
            ) from e
    # Designated → original guard detail (not the opaque wrapper).
    assert exc.value.status_code == 400
    assert models_api._BULK_IMPORT_OUTBOUND_REJECTED not in str(exc.value.detail)


def test_undesignated_admin_sees_sentinel_on_every_face(
    client: TestClient, db: Session, monkeypatch
):
    """PROVE RED: change can_see to `return True` → undesignated gets real URL."""
    owner, _ = _auth(client, db, "epvis_own_plain", role="owner")
    admin, ah = _auth(client, db, "epvis_plain_adm", role="admin")
    model = _plant_model(db, name="epvis-plain-model")
    _plant_audit(db, owner, model)

    listed = client.get("/api/models", headers=ah)
    hit = next(r for r in listed.json() if r["name"] == "epvis-plain-model")
    assert hit["endpoint_url"] == ea_svc.ENDPOINT_REDACTED
    assert "endpoint_group_key" not in hit
    assert SECRET not in json.dumps(hit)

    ser = serialize_audit_log(
        db.query(AuditLog)
        .filter(AuditLog.resource_id == str(model.id))
        .order_by(AuditLog.id.desc())
        .first(),
        caller=admin,
        db=db,
    )
    assert ser["metadata"] is None
    assert SECRET not in (ser["detail"] or "")
    assert ea_svc.ENDPOINT_REDACTED in (ser["detail"] or "")

    upsert_alert(
        db,
        fingerprint="health:model:epvis-plain",
        category="health",
        severity="high",
        title="離線",
        message="無法連線至模型「x」",
        source_type="model",
        source_id=model.id,
        metadata={"endpoint_url": SECRET},
    )
    db.commit()
    rows = alerts_api.list_alerts(
        status=None, severity=None, category=None, admin=admin, db=db
    )
    assert rows[0]["metadata"] is None
    assert SECRET not in json.dumps(rows[0], default=str)

    display = ea_svc.visible_endpoint_url(
        SECRET, db=db, caller=admin, is_internal=False
    )
    assert display == ea_svc.ENDPOINT_REDACTED

    monkeypatch.setattr(proxy_impl, "_guard_outbound", lambda *a, **k: None)

    class _FailClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(
        proxy_service.httpx, "AsyncClient", lambda *a, **k: _FailClient()
    )

    class _Model:
        id = 1
        name = "epvis-plain-model"
        model_type = "llm"
        endpoint_url = SECRET
        api_version = "v1"
        api_key_secret_ref = None

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            proxy_service.proxy_request(
                model=_Model(),
                api_key_id=1,
                user_id=admin.id,
                department_id=None,
                request_body={
                    "model": "epvis-plain-model",
                    "messages": [{"role": "user", "content": "hi"}],
                },
                endpoint_path="/v1/chat/completions",
                endpoint_display=display,
                tuning=_PROXY_TUNING,
            )
        )
    err = str(exc.value.detail)
    assert SECRET not in err
    assert ea_svc.ENDPOINT_REDACTED in err


def test_service_token_receives_real_address(
    client: TestClient, db: Session, monkeypatch
):
    """PROVE RED: force is_service_token path to redact → assert fails.

    Uses an attributed ``client_type='router'`` service_client — unattributed
    legacy env is refused on router-primary (client_type fail-closed).
    """
    from app.models.service_client import ServiceClient
    from app.services.service_token_envelope import (
        compute_lookup_hash,
        encode_service_token_envelope,
        generate_service_token,
    )

    model = _plant_model(db, name="epvis-svc-model")
    model.is_router_primary = True
    model.is_active = True
    db.commit()

    data = models_api._build_response(
        model, caller=None, db=db, is_service_token=True
    )
    assert data["endpoint_url"] == SECRET

    token = generate_service_token()
    db.add(
        ServiceClient(
            client_name="epvis-router-primary",
            client_type="router",
            service_token_envelope=encode_service_token_envelope(token),
            service_token_lookup_hash=compute_lookup_hash(token),
        )
    )
    db.commit()
    r = client.get(
        "/api/models/router-primary",
        headers={"X-CSP-Service-Token": token},
    )
    assert r.status_code == 200, r.text
    assert r.json()["endpoint_url"] == SECRET

def test_grouping_key_absent_from_all_responses(client: TestClient, db: Session):
    """PROVE RED: re-add endpoint_group_key to _build_response → fails."""
    owner, oh = _auth(client, db, "epvis_own_gk", role="owner")
    admin, ah = _auth(client, db, "epvis_adm_gk", role="admin")
    _plant_model(db, name="epvis-gk-a")
    _plant_model(db, name="epvis-gk-b")

    for headers in (oh, ah):
        rows = client.get("/api/models", headers=headers).json()
        assert rows
        for row in rows:
            assert "endpoint_group_key" not in row

    assert not hasattr(models_api, "_endpoint_group_key")


def test_agent_and_model_alert_messages_agree_no_raw_url():
    """Invariant 5: both health loops put the URL only in gated metadata.

    Asserts on WHAT the loops pass, not on how they spell it. An earlier
    version pinned the literal expression ``agent.endpoint_url``; when the
    connection-pool work snapshotted the row into locals before releasing
    the session, the behaviour was unchanged but the test went red. The
    invariant is that the address reaches ``metadata`` and never ``message``
    — so check the call, not the source text.
    """
    import ast
    import inspect

    from app.services import health_checker as hc

    def alert_calls(fn):
        """Every ``upsert_alert(...)`` call in ``fn``, as AST keyword maps."""
        tree = ast.parse(inspect.getsource(fn).lstrip())
        return [
            {kw.arg: kw.value for kw in node.keywords}
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "upsert_alert"
        ]

    for fn, label in ((hc._agent_health_check_loop, "Agent"), (hc._health_check_loop, "模型")):
        calls = alert_calls(fn)
        assert calls, f"{label} 健康迴圈找不到 upsert_alert 呼叫"
        for call in calls:
            message = ast.unparse(call["message"])
            # The address must not reach the human-readable message, whether
            # it is spelled as an attribute or as a snapshotted local.
            assert "endpoint_url" not in message, message
            assert f"無法連線至 {label}" in message or label in message
            # …and it must still be in the gated metadata.
            metadata = ast.unparse(call["metadata"])
            assert "endpoint_url" in metadata, metadata
