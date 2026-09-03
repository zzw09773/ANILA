# -*- coding: utf-8 -*-
"""P4.6b — endpoint address author grants.

Owner ruling: entering a model endpoint address is the owner's right plus
that of developers designated one by one. Undesignated administrators keep
view / activate / assign / delete / bulk-import, but cannot introduce an
address (closes the grouping-key confirmation oracle at registration).
"""
from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.model_registry import ModelRegistry
from app.schemas.model_registry import ModelCreate, ModelUpdate
from app.api import models as models_api
from app.services import endpoint_author_service as ea_svc
from tests.conftest import login, make_model, make_user

import dataclasses as _dataclasses

from app.services.proxy.service import ProxyTuning

#: 這一支只想跑一次上游、不想等重試的退避。重試策略是固定程式常數，
#: 測試用的 tuning 直接覆蓋為一次。
_PROXY_TUNING = _dataclasses.replace(
    ProxyTuning.from_registry_defaults(), max_retries=1, retry_base_delay=0.0
)



@pytest.fixture(autouse=True)
def _bypass_dev_secret_gate(monkeypatch):
    import app.services.startup_security as ss_module

    monkeypatch.setattr(ss_module, "assert_no_dev_defaults", lambda: None)


@pytest.fixture(autouse=True)
def _allow_endpoints(monkeypatch):
    # Public https hosts only — do NOT set ANILA_TRUSTED_HOSTS here.
    # TestClient lifespan backfills that env into the DB-backed trusted-host
    # cache, which would leak past monkeypatch teardown and break SSRF tests.
    monkeypatch.setenv("ANILA_ALLOW_HTTP_ENDPOINT", "1")
    monkeypatch.setenv("ANILA_ALLOW_PRIVATE_ENDPOINT", "1")


def _csrf_headers(client: TestClient) -> dict:
    from app.middleware.cookies import CSRF_COOKIE_NAME

    return {"X-CSRF-Token": client.cookies.get(CSRF_COOKIE_NAME)}


def _auth(client: TestClient, db: Session, username: str, role: str) -> tuple:
    user = make_user(db, username=username, role=role)
    headers = {
        "Authorization": f"Bearer {login(client, username=username)}",
        **_csrf_headers(client),
    }
    return user, headers


def _grant(client: TestClient, owner_h: dict, user_id: int):
    return client.post(
        "/api/endpoint-authors",
        headers=owner_h,
        json={"user_id": user_id},
    )


# ── designation CRUD + audit ──────────────────────────────────────────────────


def test_owner_can_grant_and_list_developers(client: TestClient, db: Session):
    _, oh = _auth(client, db, "ea_owner", role="owner")
    dev = make_user(db, username="ea_dev", role="developer")

    r = _grant(client, oh, dev.id)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["user_id"] == dev.id
    assert body["username"] == "ea_dev"
    assert body["revoked_at"] is None

    listed = client.get("/api/endpoint-authors", headers=oh)
    assert listed.status_code == 200
    assert any(g["user_id"] == dev.id for g in listed.json())


def test_admin_cannot_manage_grants(client: TestClient, db: Session):
    _, ah = _auth(client, db, "ea_admin_mgmt", role="admin")
    dev = make_user(db, username="ea_dev_mgmt", role="developer")
    r = client.post(
        "/api/endpoint-authors",
        headers=ah,
        json={"user_id": dev.id},
    )
    assert r.status_code == 403
    assert "owner" in r.json()["detail"]


def test_cannot_grant_plain_user(client: TestClient, db: Session):
    _, oh = _auth(client, db, "ea_owner_nd", role="owner")
    plain = make_user(db, username="ea_plain", role="user")
    r = _grant(client, oh, plain.id)
    assert r.status_code == 400
    assert "開發者或管理員" in r.json()["detail"]


def test_owner_can_grant_admin(client: TestClient, db: Session):
    """Invariant 2: designation may name an admin, not only a developer."""
    _, oh = _auth(client, db, "ea_owner_ga", role="owner")
    admin = make_user(db, username="ea_admin_ga", role="admin")
    r = _grant(client, oh, admin.id)
    assert r.status_code == 201, r.text
    assert r.json()["user_id"] == admin.id
    assert ea_svc.can_set_endpoint_address(db, admin) is True
    assert ea_svc.can_see_endpoint_address(db, admin) is True
    # users.role itself is untouched.
    db.refresh(admin)
    assert admin.role == "admin"

def test_grant_and_revoke_write_audit(client: TestClient, db: Session):
    owner, oh = _auth(client, db, "ea_owner_aud", role="owner")
    dev = make_user(db, username="ea_dev_aud", role="developer")

    r = _grant(client, oh, dev.id)
    assert r.status_code == 201
    grant_id = r.json()["id"]

    grant_logs = (
        db.query(AuditLog)
        .filter(AuditLog.action == "endpoint_author_grant")
        .all()
    )
    assert any(
        "ea_owner_aud" in (a.detail or "") and "ea_dev_aud" in (a.detail or "")
        for a in grant_logs
    )

    rev = client.delete(f"/api/endpoint-authors/{grant_id}", headers=oh)
    assert rev.status_code == 200
    revoke_logs = (
        db.query(AuditLog)
        .filter(AuditLog.action == "endpoint_author_revoke")
        .all()
    )
    assert any(
        "ea_owner_aud" in (a.detail or "") and "ea_dev_aud" in (a.detail or "")
        for a in revoke_logs
    )


# ── address-setting gate ──────────────────────────────────────────────────────


def test_designated_developer_can_create_and_update_address(
    client: TestClient, db: Session
):
    _, oh = _auth(client, db, "ea_owner_crud", role="owner")
    dev, dh = _auth(client, db, "ea_dev_crud", role="developer")
    assert _grant(client, oh, dev.id).status_code == 201

    create = client.post(
        "/api/models",
        headers=dh,
        json={
            "name": "ea-dev-model",
            "display_name": "EA Dev Model",
            "model_type": "llm",
            "endpoint_url": "https://gateway.example.com/v1",
        },
    )
    assert create.status_code == 200, create.text
    model_id = create.json()["id"]

    update = client.put(
        f"/api/models/{model_id}",
        headers=dh,
        json={"endpoint_url": "https://other.example.com/v1"},
    )
    assert update.status_code == 200, update.text
    row = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).one()
    assert row.endpoint_url == "https://other.example.com/v1"


def test_undesignated_admin_cannot_create_or_change_address(
    client: TestClient, db: Session
):
    admin, ah = _auth(client, db, "ea_admin_block", role="admin")
    model = make_model(db, name="ea-existing")
    original = model.endpoint_url

    create = client.post(
        "/api/models",
        headers=ah,
        json={
            "name": "ea-admin-probe",
            "display_name": "Admin Probe",
            "model_type": "llm",
            "endpoint_url": "https://secret-gpu-box.example.com/v1",
        },
    )
    assert create.status_code == 403
    assert create.json()["detail"] == "需要端點位址設定權限"

    update = client.put(
        f"/api/models/{model.id}",
        headers=ah,
        json={"endpoint_url": "https://secret-gpu-box.example.com/v1"},
    )
    assert update.status_code == 403
    assert update.json()["detail"] == "需要端點位址設定權限"
    db.refresh(model)
    assert model.endpoint_url == original


def test_admin_can_update_non_address_fields(client: TestClient, db: Session):
    _, ah = _auth(client, db, "ea_admin_ok", role="admin")
    model = make_model(db, name="ea-admin-edit")
    original_url = model.endpoint_url

    r = client.put(
        f"/api/models/{model.id}",
        headers=ah,
        json={"display_name": "改名後", "description": "admin ok"},
    )
    assert r.status_code == 200, r.text
    db.refresh(model)
    assert model.display_name == "改名後"
    assert model.endpoint_url == original_url


def test_owner_always_can_set_address(client: TestClient, db: Session):
    _, oh = _auth(client, db, "ea_owner_addr", role="owner")
    r = client.post(
        "/api/models",
        headers=oh,
        json={
            "name": "ea-owner-model",
            "display_name": "Owner Model",
            "model_type": "llm",
            "endpoint_url": "https://gateway.example.com/v1",
        },
    )
    assert r.status_code == 200, r.text


def test_revoke_takes_effect_immediately(client: TestClient, db: Session):
    _, oh = _auth(client, db, "ea_owner_rev", role="owner")
    dev, dh = _auth(client, db, "ea_dev_rev", role="developer")
    grant_r = _grant(client, oh, dev.id)
    assert grant_r.status_code == 201
    grant_id = grant_r.json()["id"]

    ok = client.post(
        "/api/models",
        headers=dh,
        json={
            "name": "ea-before-revoke",
            "display_name": "Before",
            "model_type": "llm",
            "endpoint_url": "https://gateway.example.com/v1",
        },
    )
    assert ok.status_code == 200

    rev = client.delete(f"/api/endpoint-authors/{grant_id}", headers=oh)
    assert rev.status_code == 200

    blocked = client.post(
        "/api/models",
        headers=dh,
        json={
            "name": "ea-after-revoke",
            "display_name": "After",
            "model_type": "llm",
            "endpoint_url": "https://gateway.example.com/v1",
        },
    )
    assert blocked.status_code == 403
    assert blocked.json()["detail"] == "需要端點位址設定權限"


def test_admin_keeps_activate_delete_and_bulk_import(
    client: TestClient, db: Session, monkeypatch
):
    admin, ah = _auth(client, db, "ea_admin_ops", role="admin")
    source = make_model(db, name="ea-import-src")
    # Public https host — avoids ANILA_TRUSTED_HOSTS backfill into the
    # process-local trusted-host cache (see module fixture note).
    source.endpoint_url = "https://gateway.example.com/v1"
    db.commit()

    async def _fake(endpoint_url, api_key):
        return [{"id": "ea-imported-one"}]

    monkeypatch.setattr(
        models_api, "_fetch_upstream_model_listing", _fake
    )

    imported = client.post(
        "/api/models/import",
        headers=ah,
        json={"source_model_id": source.id},
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["created"] >= 1

    created = (
        db.query(ModelRegistry)
        .filter(ModelRegistry.name == "ea-imported-one")
        .one()
    )
    assert created.is_active is False

    act = client.post(f"/api/models/{created.id}/activate", headers=ah)
    assert act.status_code == 200
    db.refresh(created)
    assert created.is_active is True

    deact = client.delete(f"/api/models/{created.id}", headers=ah)
    assert deact.status_code == 200
    db.refresh(created)
    assert created.is_active is False


def test_me_endpoint_reflects_designation(client: TestClient, db: Session):
    _, oh = _auth(client, db, "ea_owner_me", role="owner")
    dev, dh = _auth(client, db, "ea_dev_me", role="developer")
    _, ah = _auth(client, db, "ea_admin_me", role="admin")

    assert client.get("/api/endpoint-authors/me", headers=dh).json()[
        "can_set_endpoint_address"
    ] is False
    assert client.get("/api/endpoint-authors/me", headers=ah).json()[
        "can_set_endpoint_address"
    ] is False
    assert client.get("/api/endpoint-authors/me", headers=oh).json()[
        "can_set_endpoint_address"
    ] is True

    assert _grant(client, oh, dev.id).status_code == 201
    assert client.get("/api/endpoint-authors/me", headers=dh).json()[
        "can_set_endpoint_address"
    ] is True


def test_service_helpers_gate_create_and_update(db: Session):
    import asyncio

    owner = make_user(db, "ea_svc_owner", role="owner")
    admin = make_user(db, "ea_svc_admin", role="admin")
    dev = make_user(db, "ea_svc_dev", role="developer")
    ea_svc.assign(db, user=dev, granted_by=owner)

    ok = asyncio.run(
        models_api.create_model(
            ModelCreate(
                name="ea-svc-ok",
                display_name="OK",
                model_type="llm",
                endpoint_url="https://gateway.example.com/v1",
            ),
            dev,
            db,
        )
    )
    assert ok["name"] == "ea-svc-ok"

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            models_api.create_model(
                ModelCreate(
                    name="ea-svc-no",
                    display_name="No",
                    model_type="llm",
                    endpoint_url="https://gateway.example.com/v1",
                ),
                admin,
                db,
            )
        )
    assert exc.value.status_code == 403

    model = make_model(db, name="ea-svc-upd")
    with pytest.raises(HTTPException) as exc2:
        asyncio.run(
            models_api.update_model(
                model.id,
                ModelUpdate(endpoint_url="https://other.example.com/v1"),
                admin,
                db,
            )
        )
    assert exc2.value.status_code == 403

    updated = asyncio.run(
        models_api.update_model(
            model.id,
            ModelUpdate(endpoint_url="https://other.example.com/v1"),
            owner,
            db,
        )
    )
    assert "other.example.com" in updated["endpoint_url"]
    row = db.query(ModelRegistry).filter(ModelRegistry.id == model.id).one()
    assert row.endpoint_url == "https://other.example.com/v1"


def test_grouping_key_retired_from_list_response(db: Session):
    """Invariant 4: endpoint_group_key is gone; undesignated admin still redacted."""
    target = make_model(db, name="ea-group-a")
    target.endpoint_url = "http://mock-llm:8080/v1"
    sibling = make_model(db, name="ea-group-b")
    sibling.endpoint_url = "http://mock-llm:8080/v1"
    db.commit()

    admin = make_user(db, "ea_group_admin", role="admin")
    user = make_user(db, "ea_group_user", role="user")
    user.allowed_models.append(target)
    user.allowed_models.append(sibling)
    db.commit()

    admin_rows = models_api.list_models(
        model_type=None, current_user=admin, db=db
    )
    admin_by = {r["name"]: r for r in admin_rows}
    assert "endpoint_group_key" not in admin_by["ea-group-a"]
    assert "endpoint_group_key" not in admin_by["ea-group-b"]
    assert admin_by["ea-group-a"]["endpoint_url"] in (
        models_api.ENDPOINT_INTERNAL,
        models_api.ENDPOINT_REDACTED,
    )

    user_rows = models_api.list_models(
        model_type=None, current_user=user, db=db
    )
    assert all("endpoint_group_key" not in r for r in user_rows)


# ── review findings: narrowed write + author visibility loop ──────────────────


def test_designated_developer_address_only_update_refuses_other_fields(
    client: TestClient, db: Session
):
    """Finding 3: designation is address entry, not full model write."""
    _, oh = _auth(client, db, "ea_owner_narrow", role="owner")
    dev, dh = _auth(client, db, "ea_dev_narrow", role="developer")
    assert _grant(client, oh, dev.id).status_code == 201

    created = client.post(
        "/api/models",
        headers=dh,
        json={
            "name": "ea-narrow-model",
            "display_name": "Narrow",
            "model_type": "llm",
            "endpoint_url": "https://gateway.example.com/v1",
        },
    )
    assert created.status_code == 200, created.text
    model_id = created.json()["id"]

    refused = client.put(
        f"/api/models/{model_id}",
        headers=dh,
        json={
            "endpoint_url": "https://other.example.com/v1",
            "is_active": True,
            "classification_ceiling": "密",
        },
    )
    assert refused.status_code == 403
    assert "僅可變更端點位址" in refused.json()["detail"]

    row = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).one()
    assert row.endpoint_url == "https://gateway.example.com/v1"


def test_designated_developer_lists_and_edits_registered_row(
    client: TestClient, db: Session
):
    """Authorship visibility: register → list → fetch → fix-address."""
    _, oh = _auth(client, db, "ea_owner_loop", role="owner")
    admin, ah = _auth(client, db, "ea_admin_loop", role="admin")
    dev, dh = _auth(client, db, "ea_dev_loop", role="developer")
    assert _grant(client, oh, dev.id).status_code == 201

    created = client.post(
        "/api/models",
        headers=dh,
        json={
            "name": "ea-loop-model",
            "display_name": "Loop Model",
            "model_type": "llm",
            "endpoint_url": "https://typo.example.com/v1",
        },
    )
    assert created.status_code == 200, created.text
    body = created.json()
    model_id = body["id"]
    # Designated author sees the real address (visibility = designation).
    assert body["endpoint_url"] == "https://typo.example.com/v1"
    assert "endpoint_group_key" not in body

    # Admin rewrites allowed models wholesale — must not strip authorship view.
    other = make_model(db, name="ea-loop-other")
    rewritten = client.put(
        f"/api/users/{dev.id}/allowed-models",
        headers=ah,
        json={"model_ids": [other.id]},
    )
    assert rewritten.status_code == 200, rewritten.text

    listed = client.get("/api/models", headers=dh)
    assert listed.status_code == 200
    listed_ids = {r["id"] for r in listed.json()}
    assert model_id in listed_ids
    authored = next(r for r in listed.json() if r["id"] == model_id)
    assert authored["endpoint_url"] == "https://typo.example.com/v1"
    assert "endpoint_group_key" not in authored
    fetched = client.get(f"/api/models/{model_id}", headers=dh)
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "ea-loop-model"

    fixed = client.put(
        f"/api/models/{model_id}",
        headers=dh,
        json={"endpoint_url": "https://fixed.example.com/v1"},
    )
    assert fixed.status_code == 200, fixed.text
    row = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).one()
    assert row.endpoint_url == "https://fixed.example.com/v1"
    assert row.created_by_user_id == dev.id


def test_designated_developer_cannot_address_foreign_row(
    client: TestClient, db: Session
):
    """Write basis == read authorship: foreign id refused like missing.

    Live gap: designation alone let a developer retarget any registry
    row (including router primary) while GET returned 404 — write
    success was an existence oracle. Both paths now ask
    ``_caller_is_designated_author_of``.
    """
    _, oh = _auth(client, db, "ea_owner_foreign", role="owner")
    other_dev, other_h = _auth(client, db, "ea_other_dev", role="developer")
    assert _grant(client, oh, other_dev.id).status_code == 201

    foreign = client.post(
        "/api/models",
        headers=other_h,
        json={
            "name": "ea-foreign-model",
            "display_name": "Foreign",
            "model_type": "llm",
            "endpoint_url": "https://foreign.example.com/v1",
        },
    )
    assert foreign.status_code == 200, foreign.text
    foreign_id = foreign.json()["id"]
    original = (
        db.query(ModelRegistry)
        .filter(ModelRegistry.id == foreign_id)
        .one()
        .endpoint_url
    )

    attacker, ah = _auth(client, db, "ea_attacker_dev", role="developer")
    assert _grant(client, oh, attacker.id).status_code == 201

    # Own row: address update still succeeds.
    own = client.post(
        "/api/models",
        headers=ah,
        json={
            "name": "ea-attacker-own",
            "display_name": "Own",
            "model_type": "llm",
            "endpoint_url": "https://own.example.com/v1",
        },
    )
    assert own.status_code == 200, own.text
    own_ok = client.put(
        f"/api/models/{own.json()['id']}",
        headers=ah,
        json={"endpoint_url": "https://own-fixed.example.com/v1"},
    )
    assert own_ok.status_code == 200, own_ok.text

    missing_id = foreign_id + 10_000_000
    get_missing = client.get(f"/api/models/{missing_id}", headers=ah)
    put_missing = client.put(
        f"/api/models/{missing_id}",
        headers=ah,
        json={"endpoint_url": "https://evil.example.com/v1"},
    )
    get_foreign = client.get(f"/api/models/{foreign_id}", headers=ah)
    put_foreign = client.put(
        f"/api/models/{foreign_id}",
        headers=ah,
        json={"endpoint_url": "https://evil.example.com/v1"},
    )

    # Refusal indistinguishable from a non-existent row (status + body).
    assert get_missing.status_code == 404
    assert put_missing.status_code == 404
    assert get_foreign.status_code == 404
    assert put_foreign.status_code == 404
    assert (
        get_missing.json()
        == put_missing.json()
        == get_foreign.json()
        == put_foreign.json()
    )
    assert put_foreign.json()["detail"] == "模型不存在"

    row = db.query(ModelRegistry).filter(ModelRegistry.id == foreign_id).one()
    db.refresh(row)
    assert row.endpoint_url == original


def test_admin_updates_row_they_did_not_create(
    client: TestClient, db: Session
):
    """Administrator tier still acts on any row (non-address fields)."""
    _, oh = _auth(client, db, "ea_owner_admin_any", role="owner")
    author, auth_h = _auth(client, db, "ea_author_admin_any", role="developer")
    assert _grant(client, oh, author.id).status_code == 201

    created = client.post(
        "/api/models",
        headers=auth_h,
        json={
            "name": "ea-admin-any-model",
            "display_name": "Before",
            "model_type": "llm",
            "endpoint_url": "https://gateway.example.com/v1",
        },
    )
    assert created.status_code == 200, created.text
    model_id = created.json()["id"]

    admin, ah = _auth(client, db, "ea_admin_any", role="admin")
    r = client.put(
        f"/api/models/{model_id}",
        headers=ah,
        json={"display_name": "Admin Renamed"},
    )
    assert r.status_code == 200, r.text
    row = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).one()
    assert row.display_name == "Admin Renamed"
    assert row.created_by_user_id == author.id
    assert row.endpoint_url == "https://gateway.example.com/v1"


def test_designated_developer_create_does_not_grant_inference(
    client: TestClient, db: Session
):
    """Creating a registry row must not grant invoke permission."""
    from app.models.user import UserModelPermission
    from app.services.api_key_service import check_model_permission

    _, oh = _auth(client, db, "ea_owner_infer", role="owner")
    dev, dh = _auth(client, db, "ea_dev_infer", role="developer")
    assert _grant(client, oh, dev.id).status_code == 201

    created = client.post(
        "/api/models",
        headers=dh,
        json={
            "name": "ea-no-infer-model",
            "display_name": "No Infer",
            "model_type": "llm",
            "endpoint_url": "https://gateway.example.com/v1",
        },
    )
    assert created.status_code == 200, created.text
    model_id = created.json()["id"]

    perm = (
        db.query(UserModelPermission)
        .filter(
            UserModelPermission.user_id == dev.id,
            UserModelPermission.model_id == model_id,
        )
        .first()
    )
    assert perm is None
    db.refresh(dev)
    assert not check_model_permission(
        db, user=dev, api_key_id=None, model_id=model_id
    )


def test_explicit_null_endpoint_url_refused(client: TestClient, db: Session):
    """Finding 6a: null address → clean 400, not a server error."""
    _, oh = _auth(client, db, "ea_owner_null", role="owner")
    model = make_model(db, name="ea-null-addr")
    r = client.put(
        f"/api/models/{model.id}",
        headers=oh,
        json={"endpoint_url": None},
    )
    assert r.status_code == 400
    assert "不可為空" in r.json()["detail"]


def test_concurrent_grant_unique_index_refuses_cleanly(
    client: TestClient, db: Session, monkeypatch
):
    """Finding 6b: unique-index race → intended 400 refusal."""
    _, oh = _auth(client, db, "ea_owner_race", role="owner")
    dev = make_user(db, username="ea_dev_race", role="developer")
    assert _grant(client, oh, dev.id).status_code == 201

    # Simulate TOCTOU: the pre-check misses the active row.
    monkeypatch.setattr(ea_svc, "get_active_grant", lambda *_a, **_k: None)
    raced = _grant(client, oh, dev.id)
    assert raced.status_code == 400
    assert raced.json()["detail"] == "已具有端點位址設定權限"


def test_grant_and_revoke_abort_when_audit_fails(db: Session, monkeypatch):
    """Finding 5: privilege + audit share one transaction."""
    from fastapi import HTTPException

    owner = make_user(db, "ea_owner_audfail", role="owner")
    dev = make_user(db, "ea_dev_audfail", role="developer")

    monkeypatch.setattr(
        ea_svc,
        "log_audit_event",
        lambda *a, **k: None,
    )
    with pytest.raises(HTTPException) as exc:
        ea_svc.assign(db, user=dev, granted_by=owner)
    assert exc.value.status_code == 500
    assert ea_svc.get_active_grant(db, dev) is None

    # Restore a live grant with a working audit, then fail revoke audit.
    monkeypatch.setattr(
        ea_svc,
        "log_audit_event",
        __import__("app.services.audit_service", fromlist=["log_audit_event"]).log_audit_event,
    )
    grant = ea_svc.assign(db, user=dev, granted_by=owner)
    assert grant.revoked_at is None

    monkeypatch.setattr(ea_svc, "log_audit_event", lambda *a, **k: None)
    with pytest.raises(HTTPException) as exc2:
        ea_svc.revoke(db, grant=grant, actor=owner)
    assert exc2.value.status_code == 500
    db.refresh(grant)
    assert grant.revoked_at is None


# ── review findings: proxy + alert disclosure ─────────────────────────────────


def test_proxy_response_gates_endpoint_by_visibility(monkeypatch, db: Session):
    """Proxy trace / failure include real URL only for designated viewers."""
    import asyncio
    import json

    import httpx
    from fastapi import HTTPException

    from app.services import proxy_service
    from app.services.proxy import service as proxy_impl
    from app.services.endpoint_author_service import (
        ENDPOINT_REDACTED,
        visible_endpoint_url,
    )

    secret = "https://secret-gpu-box.example.com/v1"
    model_name = "ea-secret-model"
    undesignated = make_user(db, "ea_proxy_admin", role="admin")
    owner = make_user(db, "ea_proxy_owner", role="owner")
    hidden = visible_endpoint_url(
        secret, is_internal=False, db=db, caller=undesignated
    )
    shown = visible_endpoint_url(
        secret, is_internal=False, db=db, caller=owner
    )
    assert hidden == ENDPOINT_REDACTED
    assert shown == secret

    class _Resp:
        status_code = 200
        headers = {"content-type": "application/json"}
        text = (
            '{"choices":[{"message":{"role":"assistant","content":"ok"}}],'
            '"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}'
        )

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}}
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            return _Resp()

    class _Model:
        id = 99
        name = model_name
        model_type = "llm"
        endpoint_url = secret
        api_version = "v1"
        api_key_secret_ref = None

    async def _noop_enqueue(**_k):
        return None

    monkeypatch.setattr(
        proxy_service.httpx, "AsyncClient", lambda *a, **k: _Client()
    )
    monkeypatch.setattr(proxy_impl, "enqueue_usage", _noop_enqueue)
    monkeypatch.setattr(proxy_impl, "_guard_outbound", lambda *a, **k: None)

    async def _run(endpoint_display):
        return await proxy_service.proxy_request(
            model=_Model(),
            api_key_id=1,
            user_id=2,
            department_id=None,
            request_body={
                "model": model_name,
                "messages": [{"role": "user", "content": "hi"}],
            },
            endpoint_path="/v1/chat/completions",
            endpoint_display=endpoint_display,
            tuning=_PROXY_TUNING,
        )

    payload = asyncio.run(_run(hidden))
    blob = json.dumps(payload, ensure_ascii=False)
    assert secret not in blob
    assert "secret-gpu-box" not in blob
    detail = payload["anila_meta"]["trace"][0]["detail"]
    assert model_name in detail
    assert ENDPOINT_REDACTED in detail

    payload_owner = asyncio.run(_run(shown))
    assert secret in payload_owner["anila_meta"]["trace"][0]["detail"]

    class _FailClient(_Client):
        async def post(self, url, json=None, headers=None):
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(
        proxy_service.httpx, "AsyncClient", lambda *a, **k: _FailClient()
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(_run(hidden))
    err = str(exc.value.detail)
    assert secret not in err
    assert "secret-gpu-box" not in err
    assert model_name in err
    assert ENDPOINT_REDACTED in err

    with pytest.raises(HTTPException) as exc2:
        asyncio.run(_run(shown))
    assert secret in str(exc2.value.detail)

def test_alert_listing_gates_endpoint_by_visibility(db: Session):
    """Invariant 5 + face: alerts metadata follow can_see_endpoint_address."""
    import json

    from app.api import alerts as alerts_api
    from app.models.alert import Alert
    from app.services.alert_service import upsert_alert

    secret = "https://secret-gpu-box.example.com/v1"
    admin = make_user(db, "ea_alert_admin", role="admin")
    owner = make_user(db, "ea_alert_owner", role="owner")
    designated = make_user(db, "ea_alert_desig", role="admin")
    ea_svc.assign(db, user=designated, granted_by=owner)

    upsert_alert(
        db,
        fingerprint="health:model:fixture",
        category="health",
        severity="high",
        title="模型 Secret LLM 離線",
        message="無法連線至模型「Secret LLM」（secret-llm）",
        source_type="model",
        source_id=1,
        metadata={
            "model_name": "secret-llm",
            "display_name": "Secret LLM",
            "endpoint_url": secret,
        },
    )
    db.commit()

    admin_rows = alerts_api.list_alerts(
        status=None, severity=None, category=None, admin=admin, db=db
    )
    assert len(admin_rows) == 1
    assert admin_rows[0]["metadata"] is None
    assert secret not in admin_rows[0]["message"]
    assert "Secret LLM" in admin_rows[0]["message"]
    assert secret not in json.dumps(admin_rows[0], default=str)

    owner_rows = alerts_api.list_alerts(
        status=None, severity=None, category=None, admin=owner, db=db
    )
    assert owner_rows[0]["metadata"] is not None
    assert owner_rows[0]["metadata"]["endpoint_url"] == secret

    desig_rows = alerts_api.list_alerts(
        status=None, severity=None, category=None, admin=designated, db=db
    )
    assert desig_rows[0]["metadata"]["endpoint_url"] == secret

    stored = db.query(Alert).one()
    assert secret in (stored.metadata_json or "")

def test_agent_proxy_response_names_agent_not_address(
    client: TestClient, db: Session, monkeypatch
):
    """Finding follow-up 2: agent anila_meta names the agent, not the URL."""
    import httpx
    import json

    from tests.conftest import make_agent

    secret = "https://secret-agent-box.example.com/v1"
    agent_name = "ea-secret-agent"

    admin = make_user(db, "ea_agent_admin", role="admin")
    agent = make_agent(
        db, admin, name=agent_name, approval_status="approved"
    )
    agent.endpoint_url = secret
    db.commit()

    class _Resp:
        status_code = 200
        headers = {"content-type": "application/json"}
        text = (
            '{"choices":[{"message":{"role":"assistant","content":"ok"}}],'
            '"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}'
        )

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}}
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            return _Resp()

    import app.services.proxy_service as proxy_facade

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _Client())
    # Agent path imports _guard_outbound from the facade at call time.
    monkeypatch.setattr(
        proxy_facade, "_guard_outbound", lambda *a, **k: None
    )

    token = login(client, username="ea_agent_admin")
    resp = client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {token}",
            **_csrf_headers(client),
        },
        json={
            "model": agent_name,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    blob = json.dumps(payload, ensure_ascii=False)
    assert secret not in blob
    assert "secret-agent-box" not in blob
    detail = payload["anila_meta"]["trace"][0]["detail"]
    assert agent_name in detail
    assert "http" not in detail
    assert secret not in detail


def test_proxy_generic_exception_uses_fixed_message(monkeypatch):
    """Finding follow-up 3: exception text never reaches the caller."""
    import asyncio

    import httpx
    from fastapi import HTTPException

    from app.services import proxy_service
    from app.services.proxy import service as proxy_impl

    leak = "https://leaked-host.example.com/v1/secret"

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):
            raise RuntimeError(f"upstream blew up at {leak}")

    class _Model:
        id = 99
        name = "ea-generic-err"
        model_type = "llm"
        endpoint_url = "https://real-gateway.example.com/v1"
        api_version = "v1"
        api_key_secret_ref = None

    monkeypatch.setattr(
        proxy_service.httpx, "AsyncClient", lambda *a, **k: _Client()
    )
    monkeypatch.setattr(proxy_impl, "_guard_outbound", lambda *a, **k: None)

    async def _run():
        return await proxy_service.proxy_request(
            model=_Model(),
            api_key_id=1,
            user_id=2,
            department_id=None,
            request_body={
                "model": "ea-generic-err",
                "messages": [{"role": "user", "content": "hi"}],
            },
            endpoint_path="/v1/chat/completions",
            tuning=_PROXY_TUNING,
        )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(_run())
    err = str(exc.value.detail)
    assert leak not in err
    assert "leaked-host" not in err
    assert "未預期的代理錯誤" in err
