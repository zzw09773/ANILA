"""High-risk mutations must fail-closed when audit write fails.

Owner ruling 2026-09-19: approve / deactivate / reactivate / API key
issue+revoke / service grant must not persist if the audit row cannot be
written. Low-risk callers keep fail-soft log_audit_event.
"""
from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.api_key import ApiKey
from app.models.registered_service import RegisteredService
from app.models.service_access_grant import ServiceAccessGrant
from app.models.user import User
from app.services.audit_service import AuditWriteError
from tests.conftest import login, make_model, make_user


@pytest.fixture(autouse=True)
def _bypass_dev_secret_gate(monkeypatch):
    import app.services.startup_security as ss_module

    monkeypatch.setattr(ss_module, "assert_no_dev_defaults", lambda: None)


def _bearer(client: TestClient, username: str) -> dict:
    return {"Authorization": f"Bearer {login(client, username=username)}"}


def _admin(client: TestClient, db: Session, username: str = "afc_admin") -> dict:
    make_user(db, username=username, role="admin")
    return _bearer(client, username)


def _fail_audit(monkeypatch):
    import app.services.audit_service as audit_mod

    def boom(db_session, *args, **kwargs):
        try:
            db_session.rollback()
        except Exception:
            pass
        if kwargs.get("strict"):
            raise AuditWriteError("forced audit failure")
        return None

    monkeypatch.setattr(audit_mod, "log_audit_event", boom)


def test_approve_audit_failure_rolls_back(client: TestClient, db: Session, monkeypatch):
    headers = _admin(client, db, username="afc_ap_admin")
    target = make_user(db, username="afc_pending", is_approved=False)
    _fail_audit(monkeypatch)

    r = client.post(f"/api/users/{target.id}/approve", headers=headers)
    assert r.status_code == 500, r.text
    assert "稽核紀錄寫入失敗" in r.json()["detail"]

    db.expire_all()
    refreshed = db.query(User).filter(User.id == target.id).one()
    assert refreshed.is_approved is False


def test_deactivate_audit_failure_leaves_user_active(
    client: TestClient, db: Session, monkeypatch
):
    headers = _admin(client, db, username="afc_de_admin")
    target = make_user(db, username="afc_victim")
    assert target.is_active is True
    _fail_audit(monkeypatch)

    r = client.delete(f"/api/users/{target.id}", headers=headers)
    assert r.status_code == 500, r.text

    db.expire_all()
    refreshed = db.query(User).filter(User.id == target.id).one()
    assert refreshed.is_active is True


def test_reactivate_audit_failure_leaves_user_inactive(
    client: TestClient, db: Session, monkeypatch
):
    headers = _admin(client, db, username="afc_re_admin")
    target = make_user(db, username="afc_inactive")
    target.is_active = False
    db.commit()
    _fail_audit(monkeypatch)

    r = client.post(f"/api/users/{target.id}/reactivate", headers=headers)
    assert r.status_code == 500, r.text

    db.expire_all()
    refreshed = db.query(User).filter(User.id == target.id).one()
    assert refreshed.is_active is False


def test_api_key_create_audit_failure_does_not_persist_key(
    client: TestClient, db: Session, monkeypatch
):
    headers = _admin(client, db, username="afc_key_admin")
    model = make_model(db, name="afc-llm")
    before = db.query(ApiKey).count()
    _fail_audit(monkeypatch)

    r = client.post(
        "/api/keys",
        json={"name": "should-not-land", "model_ids": [model.id]},
        headers=headers,
    )
    assert r.status_code == 500, r.text

    db.expire_all()
    assert db.query(ApiKey).count() == before


def test_api_key_revoke_audit_failure_leaves_key_active(
    client: TestClient, db: Session, monkeypatch
):
    headers = _admin(client, db, username="afc_rev_admin")
    model = make_model(db, name="afc-llm-rev")
    created = client.post(
        "/api/keys",
        json={"name": "live-key", "model_ids": [model.id]},
        headers=headers,
    )
    assert created.status_code == 200, created.text
    key_id = created.json()["id"]
    _fail_audit(monkeypatch)

    r = client.delete(f"/api/keys/{key_id}", headers=headers)
    assert r.status_code == 500, r.text

    db.expire_all()
    key = db.query(ApiKey).filter(ApiKey.id == key_id).one()
    assert key.is_active is True


def test_service_grant_audit_failure_does_not_persist_grant(
    client: TestClient, db: Session, monkeypatch
):
    headers = _admin(client, db, username="afc_gr_admin")
    target = make_user(db, username="afc_grantee")
    svc = RegisteredService(
        name="afc-svc",
        slug="afc-svc",
        entry_url="https://afc.local/app",
    )
    db.add(svc)
    db.commit()
    db.refresh(svc)
    before = db.query(ServiceAccessGrant).count()
    _fail_audit(monkeypatch)

    r = client.post(
        "/api/service-access-grants",
        json={"user_id": target.id, "platform_link_id": svc.id},
        headers=headers,
    )
    assert r.status_code == 500, r.text

    db.expire_all()
    assert db.query(ServiceAccessGrant).count() == before
