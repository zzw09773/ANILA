"""``DELETE /api/users/{user_id}/permanent`` (admin/owner hard-delete) tests.

Pins the contract:
- admin 可以硬刪 user / developer；要 owner 才能硬刪 admin/owner
- 不可刪自己（防自殺把系統孤兒）
- 擁有 agents 的 user 被拒（``agents.owner_user_id`` 強 FK），admin 必須先處理 agent
- ``api_keys`` cascade 跟著刪；``audit_logs.actor_user_id`` 改成 NULL 保留歷史
- 跟既有 ``DELETE /api/users/{user_id}`` (deactivate) 並存，行為不同
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.models.agent import Agent
from app.models.api_key import ApiKey
from app.models.audit_log import AuditLog
from app.models.user import User

from tests.conftest import login, make_user


def _make_admin(db) -> User:
    """Helper：建 admin role + approved 使用者。"""
    return make_user(db, username="root-admin", role="admin")


def _make_owner(db) -> User:
    return make_user(db, username="root-owner", role="owner")


def _csrf_headers(client: TestClient) -> dict:
    """既有 CSRF middleware 對 mutating cookie request 要 echo X-CSRF-Token。"""
    from app.middleware.cookies import CSRF_COOKIE_NAME
    return {"X-CSRF-Token": client.cookies.get(CSRF_COOKIE_NAME)}


@pytest.mark.integration
def test_admin_can_hard_delete_regular_user(client: TestClient, db):
    _make_admin(db)
    target = make_user(db, username="target")
    target_id = target.id

    login(client, "root-admin")
    resp = client.delete(
        f"/api/users/{target_id}/permanent",
        headers=_csrf_headers(client),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["snapshot"]["username"] == "target"

    # User row 真的不見了
    assert db.query(User).filter(User.id == target_id).first() is None


@pytest.mark.integration
def test_admin_cannot_hard_delete_owner(client: TestClient, db):
    """admin tier 不夠 — 動 admin/owner 要 owner 親自下手。"""
    _make_admin(db)
    other_owner = _make_owner(db)

    login(client, "root-admin")
    resp = client.delete(
        f"/api/users/{other_owner.id}/permanent",
        headers=_csrf_headers(client),
    )
    assert resp.status_code == 403
    # 仍然存在
    assert db.query(User).filter(User.id == other_owner.id).first() is not None


@pytest.mark.integration
def test_owner_can_hard_delete_admin(client: TestClient, db):
    _make_owner(db)
    target_admin = make_user(db, username="target-admin", role="admin")
    target_id = target_admin.id

    login(client, "root-owner")
    resp = client.delete(
        f"/api/users/{target_id}/permanent",
        headers=_csrf_headers(client),
    )
    assert resp.status_code == 200, resp.text
    assert db.query(User).filter(User.id == target_id).first() is None


@pytest.mark.integration
def test_cannot_hard_delete_self(client: TestClient, db):
    me = _make_owner(db)
    login(client, "root-owner")
    resp = client.delete(
        f"/api/users/{me.id}/permanent",
        headers=_csrf_headers(client),
    )
    assert resp.status_code == 400
    assert "自己" in resp.json()["detail"]


@pytest.mark.integration
def test_hard_delete_returns_404_for_missing_user(client: TestClient, db):
    _make_owner(db)
    login(client, "root-owner")
    resp = client.delete(
        "/api/users/9999/permanent",
        headers=_csrf_headers(client),
    )
    assert resp.status_code == 404


@pytest.mark.integration
def test_hard_delete_refused_when_user_owns_agents(client: TestClient, db):
    _make_owner(db)
    target = make_user(db, username="agent-owner")
    # 給 target 弄一個 agent — 模擬「developer 開發者擁有 agent」
    agent = Agent(
        name="my-agent",
        owner_user_id=target.id,
        endpoint_url="http://agent:24786",
        description_for_router="Test agent",
        is_active=True,
        is_approved=True,
    )
    db.add(agent)
    db.commit()

    login(client, "root-owner")
    resp = client.delete(
        f"/api/users/{target.id}/permanent",
        headers=_csrf_headers(client),
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "agent" in detail.lower() or "agents" in detail.lower()
    # 仍存在
    assert db.query(User).filter(User.id == target.id).first() is not None


@pytest.mark.integration
def test_hard_delete_cascades_api_keys(client: TestClient, db):
    _make_owner(db)
    target = make_user(db, username="key-haver")
    # 給 target 弄 2 把 api key
    for n in range(2):
        k = ApiKey(
            user_id=target.id,
            key_hash=f"deadbeef-{n}",
            name=f"k-{n}",
            is_active=True,
        )
        db.add(k)
    db.commit()
    assert db.query(ApiKey).filter(ApiKey.user_id == target.id).count() == 2

    login(client, "root-owner")
    resp = client.delete(
        f"/api/users/{target.id}/permanent",
        headers=_csrf_headers(client),
    )
    assert resp.status_code == 200, resp.text
    # API keys 跟著刪
    assert db.query(ApiKey).filter(ApiKey.user_id == target.id).count() == 0


@pytest.mark.integration
def test_hard_delete_preserves_audit_history_via_set_null(client: TestClient, db):
    """audit_logs.actor_user_id 應該被 SET NULL 而非整列刪 — 歷史保留。"""
    _make_owner(db)
    target = make_user(db, username="historical")
    target_id = target.id
    # 寫一條 audit log 把 actor 設為 target
    log = AuditLog(
        actor_user_id=target.id,
        action="some-action",
        resource_type="test",
        resource_id=42,
        detail="historical event",
    )
    db.add(log)
    db.commit()
    audit_id = log.id

    login(client, "root-owner")
    resp = client.delete(
        f"/api/users/{target_id}/permanent",
        headers=_csrf_headers(client),
    )
    assert resp.status_code == 200, resp.text

    # Audit row 還在，但 actor_user_id = NULL
    refreshed = db.query(AuditLog).filter(AuditLog.id == audit_id).first()
    assert refreshed is not None
    assert refreshed.actor_user_id is None


@pytest.mark.integration
def test_hard_delete_writes_audit_record(client: TestClient, db):
    """硬刪本身會在 audit log 留紀錄，actor=admin、resource_id=被刪 user."""
    admin = _make_owner(db)
    target = make_user(db, username="bye-bye")
    target_id = target.id

    login(client, "root-owner")
    client.delete(
        f"/api/users/{target_id}/permanent",
        headers=_csrf_headers(client),
    )

    audit = (
        db.query(AuditLog)
        .filter(
            AuditLog.action == "hard_delete",
            AuditLog.resource_type == "user",
            AuditLog.resource_id == target_id,
        )
        .first()
    )
    assert audit is not None
    assert audit.actor_user_id == admin.id
    assert "bye-bye" in (audit.detail or "")


@pytest.mark.integration
def test_regular_user_cannot_hard_delete(client: TestClient, db):
    """非 admin/owner 一律 403。"""
    make_user(db, username="alice")  # 一般 user
    target = make_user(db, username="bob")

    login(client, "alice")
    resp = client.delete(
        f"/api/users/{target.id}/permanent",
        headers=_csrf_headers(client),
    )
    assert resp.status_code in (401, 403)
    assert db.query(User).filter(User.id == target.id).first() is not None


@pytest.mark.integration
def test_deactivate_endpoint_still_works(client: TestClient, db):
    """既有 ``DELETE /api/users/{id}`` (deactivate) 行為不變。"""
    _make_owner(db)
    target = make_user(db, username="soft-target")

    login(client, "root-owner")
    resp = client.delete(
        f"/api/users/{target.id}",
        headers=_csrf_headers(client),
    )
    assert resp.status_code == 200
    db.refresh(target)
    assert target.is_active is False
    # row 仍存在
    assert db.query(User).filter(User.id == target.id).first() is not None
