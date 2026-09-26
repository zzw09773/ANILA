# -*- coding: utf-8 -*-
"""模型角色：一個 API 涵蓋七個角色，新角色預設未設定。"""
from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest

from app.models.audit_log import AuditLog
from app.models.model_registry import ModelRegistry
from app.models.model_role import ModelRole
from tests.conftest import login, make_api_key, make_user


def _llm(db, name="gemma-a", *, is_active=True, model_type="llm") -> ModelRegistry:
    row = ModelRegistry(
        name=name,
        display_name=name,
        model_type=model_type,
        endpoint_url=f"http://172.16.120.35:28080/v1",
        is_active=is_active,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _admin(client, db, username="role-admin") -> dict[str, str]:
    make_user(db, username=username, role="admin")
    return {"Authorization": f"Bearer {login(client, username)}"}


@pytest.fixture
def service_token_header(monkeypatch) -> dict[str, str]:
    from app.config import settings as canonical_settings
    from app.services import auth_service

    token = "csk-test-model-roles-12345"
    monkeypatch.setattr(canonical_settings, "CSP_SERVICE_TOKEN", token, raising=False)
    monkeypatch.setattr(auth_service.settings, "CSP_SERVICE_TOKEN", token, raising=False)
    return {"X-CSP-Service-Token": token}


def test_list_leaves_new_roles_unset_and_keeps_existing_flags(client, db):
    primary = _llm(db, "router-llm")
    primary.is_router_primary = True
    embed = _llm(db, "embed-a", model_type="embedding")
    embed.is_platform_embedding = True
    db.commit()

    resp = client.get("/api/models/roles", headers=_admin(client, db))
    assert resp.status_code == 200, resp.text
    by_role = {row["role"]: row for row in resp.json()["roles"]}
    assert list(by_role) == [
        "router_primary",
        "platform_embedding",
        "slides",
        "image_generation",
        "vision",
        "summary",
        "knowledge_chat",
    ]
    assert by_role["router_primary"]["status"] == "ok"
    assert by_role["router_primary"]["model"]["name"] == "router-llm"
    assert by_role["platform_embedding"]["status"] == "ok"
    assert by_role["platform_embedding"]["model"]["name"] == "embed-a"
    for role in ("slides", "image_generation", "vision", "summary", "knowledge_chat"):
        assert by_role[role]["status"] == "unset"
        assert by_role[role]["model"] is None
        assert "尚未在治理中心設定" in by_role[role]["message"]
    assert db.query(ModelRole).count() == 0


def test_image_generation_role_accepts_only_image_models(client, db, service_token_header):
    """生圖角色用既有的 image 類型，並用終端使用者自己的憑證呼叫。"""
    headers = _admin(client, db, username="image-role-admin")
    chat = _llm(db, "chat-not-image", model_type="llm")
    painter = _llm(db, "painter", model_type="image")
    painter.health_status = "healthy"
    db.commit()

    listed = client.get("/api/models/roles", headers=headers)
    assert listed.status_code == 200, listed.text
    by_role = {row["role"]: row for row in listed.json()["roles"]}
    role = by_role["image_generation"]
    assert role["label"] == "生圖模型"
    assert role["description"] == (
        "簡報與資訊圖表配圖使用的生圖模型；未設定時簡報不配生成圖片"
    )
    assert role["accepted_types"] == ["image"]
    assert role["end_user_credential"] is True
    assert role["status"] == "unset"
    assert role["model"] is None

    wrong = client.put(
        "/api/models/roles/image_generation",
        json={"model_id": chat.id},
        headers=headers,
    )
    assert wrong.status_code == 400
    assert "生圖模型" in wrong.json()["detail"]

    assigned = client.put(
        "/api/models/roles/image_generation",
        json={"model_id": painter.id},
        headers=headers,
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["status"] == "ok"
    assert assigned.json()["model"]["name"] == "painter"
    assert assigned.json()["end_user_credential"] is True

    resolved = client.get(
        "/api/models/roles/image_generation",
        headers=service_token_header,
    )
    assert resolved.status_code == 200, resolved.text
    body = resolved.json()
    assert body["name"] == "painter"
    assert body["model_type"] == "image"
    assert body["health_status"] == "healthy"


def test_set_vision_requires_active_compatible_model_and_audits(client, db):
    headers = _admin(client, db)
    inactive = _llm(db, "off", is_active=False)
    embed = _llm(db, "emb", model_type="embedding")
    good = _llm(db, "see")

    denied = client.put(
        "/api/models/roles/vision",
        json={"model_id": inactive.id},
        headers=headers,
    )
    assert denied.status_code == 400
    assert "視覺模型" in denied.json()["detail"]

    wrong = client.put(
        "/api/models/roles/vision",
        json={"model_id": embed.id},
        headers=headers,
    )
    assert wrong.status_code == 400
    assert "視覺模型" in wrong.json()["detail"]

    ok = client.put(
        "/api/models/roles/vision",
        json={"model_id": good.id},
        headers=headers,
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["status"] == "ok"
    assert body["model"]["name"] == "see"
    audit = (
        db.query(AuditLog)
        .filter(AuditLog.action == "set_model_role", AuditLog.resource_id == "vision")
        .one()
    )
    assert "視覺模型" in (audit.detail or "")

    other = _llm(db, "see-2")
    replaced = client.put(
        "/api/models/roles/vision",
        json={"model_id": other.id},
        headers=headers,
    )
    assert replaced.status_code == 200
    assert replaced.json()["model"]["name"] == "see-2"
    assert db.query(ModelRole).filter(ModelRole.role == "vision").count() == 1


def test_clear_role_is_idempotent(client, db):
    headers = _admin(client, db)
    model = _llm(db, "sum")
    assert client.put(
        "/api/models/roles/summary",
        json={"model_id": model.id},
        headers=headers,
    ).status_code == 200
    first = client.delete("/api/models/roles/summary", headers=headers)
    assert first.status_code == 200
    assert first.json()["status"] == "unset"
    assert "摘要模型尚未在治理中心設定" == first.json()["message"]
    second = client.delete("/api/models/roles/summary", headers=headers)
    assert second.status_code == 200
    assert second.json()["status"] == "unset"
    cleared = (
        db.query(AuditLog)
        .filter(AuditLog.action == "clear_model_role", AuditLog.resource_id == "summary")
        .count()
    )
    assert cleared == 1


def test_resolve_names_the_role_when_unset_or_inactive(client, db, service_token_header):
    missing = client.get("/api/models/roles/slides", headers=service_token_header)
    assert missing.status_code == 404
    assert missing.json()["detail"] == "簡報模型尚未在治理中心設定"

    model = _llm(db, "deck")
    headers = _admin(client, db, username="slides-role-admin")
    assert client.put(
        "/api/models/roles/slides",
        json={"model_id": model.id},
        headers=headers,
    ).status_code == 200
    db.refresh(model)
    assert model.is_slides_primary is True

    model.is_active = False
    db.commit()
    disabled = client.get("/api/models/roles/slides", headers=service_token_header)
    assert disabled.status_code == 409
    assert disabled.json()["detail"] == "簡報模型已停用，請在治理中心改選啟用中的模型"

    listed = client.get("/api/models/roles", headers=headers).json()["roles"]
    slides = next(row for row in listed if row["role"] == "slides")
    assert slides["status"] == "inactive"


def test_service_token_and_api_key_can_resolve(client, db, service_token_header):
    model = _llm(db, "chat-llm")
    headers = _admin(client, db, username="chat-role-admin")
    assert client.put(
        "/api/models/roles/knowledge_chat",
        json={"model_id": model.id},
        headers=headers,
    ).status_code == 200

    via_service = client.get(
        "/api/models/roles/knowledge_chat", headers=service_token_header
    )
    assert via_service.status_code == 200, via_service.text
    assert via_service.json()["name"] == "chat-llm"
    assert "secret" not in via_service.text

    user = make_user(db, username="worker-key", role="system")
    make_api_key(db, user, raw_key="sk-worker-role-key")
    via_key = client.get(
        "/api/models/roles/knowledge_chat",
        headers={"Authorization": "Bearer sk-worker-role-key"},
    )
    assert via_key.status_code == 200, via_key.text
    assert via_key.json()["name"] == "chat-llm"


def test_plain_user_cannot_assign(client, db):
    model = _llm(db, "nope")
    make_user(db, username="plain-role", role="user")
    resp = client.put(
        "/api/models/roles/summary",
        json={"model_id": model.id},
        headers={"Authorization": f"Bearer {login(client, 'plain-role')}"},
    )
    assert resp.status_code == 403


def test_unknown_role_rejected(client, db):
    headers = _admin(client, db, username="unknown-role-admin")
    assert client.get("/api/models/roles/not-a-role", headers=headers).status_code == 404
    assert client.put(
        "/api/models/roles/not-a-role",
        json={"model_id": 1},
        headers=headers,
    ).status_code == 404


def test_cookie_assignment_requires_csrf(client, db):
    model = _llm(db, "csrf-llm")
    make_user(db, username="csrf-role-admin", role="admin")
    login_resp = client.post(
        "/api/auth/login",
        json={"username": "csrf-role-admin", "password": "password"},
    )
    assert login_resp.status_code == 200, login_resp.text
    blocked = client.put("/api/models/roles/summary", json={"model_id": model.id})
    assert blocked.status_code == 403
    token = client.cookies.get("anila_csrf")
    assert token
    allowed = client.put(
        "/api/models/roles/summary",
        json={"model_id": model.id},
        headers={"X-CSRF-Token": token},
    )
    assert allowed.status_code == 200, allowed.text


def test_router_primary_keeps_eligibility_check(client, db):
    model = _llm(db, "not-router-ready")
    resp = client.put(
        "/api/models/roles/router_primary",
        json={"model_id": model.id},
        headers=_admin(client, db, username="router-role-admin"),
    )
    assert resp.status_code == 400
    db.refresh(model)
    assert model.is_router_primary is False


def test_platform_embedding_role_probes(client, db, monkeypatch):
    from app.api import models as models_api

    async def fake_probe(_model, _tuning):
        return 1024

    monkeypatch.setattr(models_api, "_probe_embedding_native_dim", fake_probe)
    model = _llm(db, "emb-role", model_type="embedding")
    resp = client.put(
        "/api/models/roles/platform_embedding",
        json={"model_id": model.id},
        headers=_admin(client, db, username="embed-role-admin"),
    )
    assert resp.status_code == 200, resp.text
    db.refresh(model)
    assert model.is_platform_embedding is True
    assert model.embedding_native_dim == 1024


def test_unauthenticated_resolve_is_rejected(client, db):
    assert client.get("/api/models/roles/summary").status_code == 401
    assert client.get("/api/models/roles").status_code == 401


def _end_user(db, username="role-end-user"):
    return make_user(db, username=username, role="user")


def _deny_model(db, user, model_name: str):
    from fastapi import HTTPException

    from app.api.proxy import _resolve_model
    from app.middleware.caller import Caller

    with pytest.raises(HTTPException) as exc:
        _resolve_model(db, Caller(user=user, api_key_id=None), model_name)
    assert exc.value.status_code == 403


def test_end_user_roles_report_whether_the_model_is_granted_to_everyone(client, db):
    """Assignment stays successful. The body says if every user can call it."""
    headers = _admin(client, db, username="grant-status-admin")
    model = _llm(db, "kb-llm")
    assigned = client.put(
        "/api/models/roles/knowledge_chat",
        json={"model_id": model.id},
        headers=headers,
    )
    assert assigned.status_code == 200, assigned.text
    body = assigned.json()
    assert body["end_user_credential"] is True
    assert body["all_users_grant"] is False
    assert body["status"] == "ok"

    listed = client.get("/api/models/roles", headers=headers).json()["roles"]
    by_role = {row["role"]: row for row in listed}
    for role in ("knowledge_chat", "summary", "slides", "vision", "image_generation"):
        assert by_role[role]["end_user_credential"] is True
    for role in ("router_primary", "platform_embedding"):
        assert by_role[role]["end_user_credential"] is False
        assert by_role[role]["all_users_grant"] is None

    user = _end_user(db)
    _deny_model(db, user, "kb-llm")


def test_grant_all_users_opens_only_that_role_model(client, db):
    from datetime import datetime, timedelta, timezone

    from app.api.proxy import _resolve_model
    from app.middleware.caller import Caller
    from app.models.router_model_grant import RouterModelGrant
    from app.services.api_key_service import check_model_permission

    headers = _admin(client, db, username="grant-all-admin")
    model = _llm(db, "kb-open")
    other = _llm(db, "kb-other")
    someone = make_user(db, username="grant-keep-user", role="user")
    db.add(RouterModelGrant(model_id=model.id, scope_type="user", user_id=someone.id))
    db.add(RouterModelGrant(model_id=other.id, scope_type="user", user_id=someone.id))
    expired = RouterModelGrant(
        model_id=model.id,
        scope_type="all",
        expires_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    db.add(expired)
    db.commit()

    assert client.put(
        "/api/models/roles/knowledge_chat",
        json={"model_id": model.id},
        headers=headers,
    ).status_code == 200
    slides = _llm(db, "deck-open")
    assert client.put(
        "/api/models/roles/slides",
        json={"model_id": slides.id},
        headers=headers,
    ).status_code == 200

    user = _end_user(db, username="kb-caller")
    key = make_api_key(db, user, raw_key="sk-role-grant-key")
    _deny_model(db, user, "kb-open")

    refused = client.post(
        "/api/models/roles/platform_embedding/grant-all-users",
        headers=headers,
    )
    assert refused.status_code == 400, refused.text

    granted = client.post(
        "/api/models/roles/knowledge_chat/grant-all-users",
        headers=headers,
    )
    assert granted.status_code == 200, granted.text
    assert granted.json()["all_users_grant"] is True
    assert granted.json()["model"]["name"] == "kb-open"

    db.expire_all()
    db.refresh(model)
    assert model.router_enabled is False
    all_rows = (
        db.query(RouterModelGrant)
        .filter(
            RouterModelGrant.model_id == model.id,
            RouterModelGrant.scope_type == "all",
        )
        .all()
    )
    assert len(all_rows) == 1
    assert all_rows[0].expires_at is None
    assert (
        db.query(RouterModelGrant)
        .filter(
            RouterModelGrant.model_id == model.id,
            RouterModelGrant.scope_type == "user",
        )
        .count()
        == 1
    )
    assert (
        db.query(RouterModelGrant)
        .filter(RouterModelGrant.model_id == other.id)
        .count()
        == 1
    )
    audit = (
        db.query(AuditLog)
        .filter(AuditLog.action == "grant_model_role_all_users")
        .one()
    )
    assert audit.resource_id == "knowledge_chat"
    assert "kb-open" in (audit.detail or "")

    resolved = _resolve_model(db, Caller(user=user, api_key_id=None), "kb-open")
    assert resolved.id == model.id
    assert check_model_permission(
        db, user=user, api_key_id=key.id, model_id=model.id
    )
    _deny_model(db, user, "kb-other")
    assert not check_model_permission(
        db, user=user, api_key_id=key.id, model_id=other.id
    )
    _deny_model(db, user, "deck-open")

    again = client.post(
        "/api/models/roles/knowledge_chat/grant-all-users",
        headers=headers,
    )
    assert again.status_code == 200, again.text
    assert (
        db.query(AuditLog)
        .filter(AuditLog.action == "grant_model_role_all_users")
        .count()
        == 1
    )

    assert client.delete(
        "/api/models/roles/knowledge_chat", headers=headers
    ).status_code == 200
    db.expire_all()
    _deny_model(db, user, "kb-open")


def _fail_role_audit(monkeypatch):
    import app.api.model_roles as roles_api
    import app.services.audit_service as audit_mod
    from app.services.audit_service import AuditWriteError

    def boom(db_session, *args, **kwargs):
        try:
            db_session.rollback()
        except Exception:
            pass
        if kwargs.get("strict"):
            raise AuditWriteError("forced audit failure")
        return None

    monkeypatch.setattr(audit_mod, "log_audit_event", boom)
    if hasattr(roles_api, "log_audit_event"):
        monkeypatch.setattr(roles_api, "log_audit_event", boom)


def test_role_assign_audit_failure_keeps_the_previous_model(client, db, monkeypatch):
    headers = _admin(client, db, username="audit-role-admin")
    first = _llm(db, "role-first")
    second = _llm(db, "role-second")
    assert client.put(
        "/api/models/roles/vision",
        json={"model_id": first.id},
        headers=headers,
    ).status_code == 200
    _fail_role_audit(monkeypatch)

    failed = client.put(
        "/api/models/roles/vision",
        json={"model_id": second.id},
        headers=headers,
    )
    assert failed.status_code == 500, failed.text
    assert "稽核紀錄寫入失敗" in failed.json()["detail"]

    db.expire_all()
    link = db.query(ModelRole).filter(ModelRole.role == "vision").one()
    assert link.model_id == first.id
    assert (
        db.query(AuditLog)
        .filter(AuditLog.action == "set_model_role", AuditLog.resource_id == "vision")
        .count()
        == 1
    )


def test_role_clear_audit_failure_keeps_the_assignment(client, db, monkeypatch):
    headers = _admin(client, db, username="audit-clear-admin")
    model = _llm(db, "role-stay")
    assert client.put(
        "/api/models/roles/summary",
        json={"model_id": model.id},
        headers=headers,
    ).status_code == 200
    _fail_role_audit(monkeypatch)

    failed = client.delete("/api/models/roles/summary", headers=headers)
    assert failed.status_code == 500, failed.text

    db.expire_all()
    link = db.query(ModelRole).filter(ModelRole.role == "summary").one()
    assert link.model_id == model.id


def test_grant_all_users_audit_failure_does_not_persist(client, db, monkeypatch):
    from app.models.router_model_grant import RouterModelGrant

    headers = _admin(client, db, username="audit-grant-admin")
    model = _llm(db, "grant-rollback")
    assert client.put(
        "/api/models/roles/knowledge_chat",
        json={"model_id": model.id},
        headers=headers,
    ).status_code == 200
    _fail_role_audit(monkeypatch)

    failed = client.post(
        "/api/models/roles/knowledge_chat/grant-all-users",
        headers=headers,
    )
    assert failed.status_code == 500, failed.text

    db.expire_all()
    assert (
        db.query(RouterModelGrant)
        .filter(
            RouterModelGrant.model_id == model.id,
            RouterModelGrant.scope_type == "all",
        )
        .count()
        == 0
    )
