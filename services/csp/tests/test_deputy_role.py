"""代理管理員：允許清單、三個人上限、其餘端點 403。"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.database import Base
from app.models.alert import Alert
from app.models.banner import Banner
from app.models.ingestion import IngestionCollection
from app.models.user import User
from app.services.alert_mail import merge_alert_recipients
from tests.conftest import login, make_user
from tests.test_alert_mail import FakeSmtp, _allow_loopback, _body


def _headers(client, username):
    return {"Authorization": f"Bearer {login(client, username)}"}


def _deputy(client, db, username="deputy-1"):
    make_user(db, username=username, role="deputy")
    return _headers(client, username)


def test_only_owner_assigns_deputy_and_the_cap_is_three(client, db):
    make_user(db, username="life-owner", role="owner")
    make_user(db, username="life-admin", role="admin")
    owner_h = _headers(client, "life-owner")
    admin_h = _headers(client, "life-admin")
    denied = client.post(
        "/api/users",
        headers=admin_h,
        json={"username": "not-deputy", "password": "password", "role": "deputy"},
    )
    assert denied.status_code == 403, denied.text

    for index in range(3):
        created = client.post(
            "/api/users",
            headers=owner_h,
            json={
                "username": f"dep-{index}",
                "password": "password",
                "role": "deputy",
                "email": f"dep-{index}@example.invalid",
            },
        )
        assert created.status_code == 200, created.text
    fourth = client.post(
        "/api/users",
        headers=owner_h,
        json={"username": "dep-extra", "password": "password", "role": "deputy"},
    )
    assert fourth.status_code == 409, fourth.text

    resting = db.query(User).filter(User.username == "dep-0").one()
    resting.is_active = False
    db.commit()
    # 停用仍佔名額。角色還是代理時，不能再指派下一位。
    opened = client.post(
        "/api/users",
        headers=owner_h,
        json={"username": "dep-rested", "password": "password", "role": "deputy"},
    )
    assert opened.status_code == 409, opened.text
    demoted = client.put(
        f"/api/users/{resting.id}",
        headers=owner_h,
        json={"role": "user"},
    )
    assert demoted.status_code == 200, demoted.text
    opened = client.post(
        "/api/users",
        headers=owner_h,
        json={"username": "dep-rested", "password": "password", "role": "deputy"},
    )
    assert opened.status_code == 200, opened.text


def test_deputy_can_use_the_allowlist(client, db, monkeypatch):
    headers = _deputy(client, db, "allow-deputy")
    pending = make_user(db, username="needs-approval", is_approved=False)
    disabled = make_user(db, username="needs-enable")
    disabled.is_active = False
    db.commit()
    rejected = make_user(db, username="needs-reject", is_approved=False)

    listed = client.get("/api/users", headers=headers)
    assert listed.status_code == 200, listed.text
    assert any(row["username"] == "needs-approval" for row in listed.json())
    one = client.get(f"/api/users/{pending.id}", headers=headers)
    assert one.status_code == 200, one.text
    approved = client.post(f"/api/users/{pending.id}/approve", headers=headers)
    assert approved.status_code == 200, approved.text
    batch = client.post(
        "/api/users/batch-approve",
        headers=headers,
        json={"user_ids": [rejected.id], "dry_run": True},
    )
    assert batch.status_code == 200, batch.text
    no = client.post(f"/api/users/{rejected.id}/reject", headers=headers)
    assert no.status_code == 200, no.text
    db.refresh(rejected)
    assert rejected.is_active is False
    assert rejected.disabled_reason is None
    yes = client.post(f"/api/users/{disabled.id}/reactivate", headers=headers)
    assert yes.status_code == 200, yes.text

    alert = Alert(
        fingerprint="deputy-fp",
        category="test",
        severity="low",
        title="測試警報",
        message="請確認",
        status="open",
    )
    db.add(alert)
    db.commit()
    alerts = client.get("/api/alerts", headers=headers)
    assert alerts.status_code == 200, alerts.text
    summary = client.get("/api/alerts/summary", headers=headers)
    assert summary.status_code == 200, summary.text
    ack = client.post(f"/api/alerts/{alert.id}/ack", headers=headers, json={})
    assert ack.status_code == 200, ack.text
    resolve = client.post(f"/api/alerts/{alert.id}/resolve", headers=headers, json={})
    assert resolve.status_code == 403, resolve.text

    async def _quiet(name, db=None):
        class _Result:
            status = "healthy"
            reason = "ok"
            latency_ms = 1

        return _Result()

    monkeypatch.setattr(
        "app.api.admin.health_overview.probe_base_service",
        _quiet,
    )
    health = client.get("/api/admin/health/overview", headers=headers)
    assert health.status_code == 200, health.text
    backup = client.get("/api/admin/backup-status", headers=headers)
    assert backup.status_code == 200, backup.text

    posted = client.post(
        "/api/banners",
        headers=headers,
        json={"content": "今晚維護", "level": "info"},
    )
    assert posted.status_code == 201, posted.text
    banner_id = posted.json()["id"]
    edited = client.put(
        f"/api/banners/{banner_id}",
        headers=headers,
        json={"content": "今晚維護，改期"},
    )
    assert edited.status_code == 200, edited.text
    banners = client.get("/api/banners", headers=headers)
    assert banners.status_code == 200, banners.text

    feedback = client.get("/api/admin/feedback", headers=headers)
    assert feedback.status_code == 200, feedback.text
    read = client.post(
        "/api/admin/feedback/read",
        headers=headers,
        json={"read_at": datetime.now(timezone.utc).isoformat()},
    )
    assert read.status_code == 200, read.text
    exported = client.get("/api/admin/feedback?format=csv", headers=headers)
    assert exported.status_code == 403, exported.text

    usage = client.get("/api/usage/summary?range=24h", headers=headers)
    assert usage.status_code == 200, usage.text
    seen: dict = {}

    def _chart(db, range_key, **kwargs):
        seen.update(kwargs)
        return {"timestamps": [], "series": []}

    monkeypatch.setattr("app.api.usage.get_chart_data", _chart)
    chart = client.get("/api/usage/chart?range=24h", headers=headers)
    assert chart.status_code == 200, chart.text
    assert seen.get("user_id") is None


def test_deputy_usage_rejects_per_user_detail(client, db, monkeypatch):
    headers = _deputy(client, db, "usage-deputy")
    victim = make_user(db, username="usage-victim")
    called = []

    def _chart(db, range_key, **kwargs):
        called.append(kwargs)
        return {"timestamps": [], "series": []}

    monkeypatch.setattr("app.api.usage.get_chart_data", _chart)
    by_user = client.get(
        "/api/usage/chart?range=24h&group_by=user",
        headers=headers,
    )
    assert by_user.status_code == 403, by_user.text
    filtered = client.get(
        f"/api/usage/chart?range=24h&user_id={victim.id}",
        headers=headers,
    )
    assert filtered.status_code == 403, filtered.text
    exported = client.get(
        f"/api/usage/export?range=24h&user_id={victim.id}",
        headers=headers,
    )
    assert exported.status_code == 403, exported.text
    top = client.get("/api/usage/top-users?range=24h", headers=headers)
    assert top.status_code == 403, top.text
    assert called == []

    aggregate = client.get(
        "/api/usage/chart?range=24h&group_by=model",
        headers=headers,
    )
    assert aggregate.status_code == 200, aggregate.text
    assert called == [{"model_id": None, "user_id": None, "model_type": None, "department_id": None, "group_by": "model"}]
    summary = client.get("/api/usage/summary?range=24h", headers=headers)
    assert summary.status_code == 200, summary.text


def test_deputy_is_forbidden_on_every_closed_endpoint_class(client, db):
    headers = _deputy(client, db, "closed-deputy")
    victim = make_user(db, username="closed-victim")
    db.add(Banner(level="info", content="既有公告", is_active=True, created_by_user_id=victim.id))
    db.commit()

    cases = [
        ("post", "/api/models", {
            "name": "deputy-no",
            "display_name": "deputy-no",
            "model_type": "llm",
            "endpoint_url": "http://127.0.0.1:9/v1",
        }),
        ("get", "/api/trusted-hosts", None),
        ("put", "/api/admin/external-services/document_parser", {
            "enabled": False,
            "base_url": "",
        }),
        ("put", "/api/platform-settings/auth.inactivity_disable_days", {"value": 90}),
        ("post", "/api/keys", {"name": "nope", "model_ids": [1]}),
        ("get", "/api/service-clients", None),
        ("put", f"/api/users/{victim.id}", {"role": "admin"}),
        ("delete", f"/api/users/{victim.id}", None),
        ("delete", "/api/banners/1", None),
        ("get", "/api/usage/top-users", None),
    ]
    for method, path, body in cases:
        response = client.request(method, path, headers=headers, json=body)
        assert response.status_code == 403, f"{method} {path} -> {response.status_code} {response.text}"


def test_deputy_deletes_their_own_collection_and_not_someone_elses(client, db):
    headers = _deputy(client, db, "lib-deputy")
    deputy = db.query(User).filter(User.username == "lib-deputy").one()
    other = make_user(db, username="lib-other")
    own = IngestionCollection(
        name="代理自己的庫",
        chunking_config={"strategy": "fixed", "params": {}},
        embedding_model="test-embed",
        embedding_dim=8,
        created_by=deputy.id,
        origin="csp",
        status="active",
    )
    foreign = IngestionCollection(
        name="別人的庫",
        chunking_config={"strategy": "fixed", "params": {}},
        embedding_model="test-embed",
        embedding_dim=8,
        created_by=other.id,
        origin="csp",
        status="active",
    )
    db.add(own)
    db.add(foreign)
    db.commit()
    own_id = own.id
    foreign_id = foreign.id
    blocked = client.delete(
        f"/api/ingestion/collections/{foreign_id}",
        headers=headers,
    )
    assert blocked.status_code == 403, blocked.text
    removed = client.delete(
        f"/api/ingestion/collections/{own_id}",
        headers=headers,
    )
    assert removed.status_code == 204, removed.text
    db.expire_all()
    assert db.get(IngestionCollection, own_id) is None
    assert db.get(IngestionCollection, foreign_id) is not None


def test_role_only_put_keeps_a_disabled_account_disabled(client, db):
    """只送角色時，不能把停用帳號的其他欄位清掉或悄悄啟用。

    明確的 null 可以清信箱與單位。is_active 與 local_password_disabled 不能設成 null。
    """
    from app.models.department import Department

    unit = Department(name="保留單位", is_active=True)
    db.add(unit)
    db.commit()
    make_user(db, username="field-owner", role="owner")
    target = make_user(db, username="field-target", department_id=unit.id)
    target.email = "keep@example.invalid"
    target.is_active = False
    target.local_password_disabled = True
    db.commit()
    headers = _headers(client, "field-owner")

    role_only = client.put(
        f"/api/users/{target.id}",
        headers=headers,
        json={"role": "deputy"},
    )
    assert role_only.status_code == 200, role_only.text
    db.expire_all()
    stored = db.get(User, target.id)
    assert stored.role == "deputy"
    assert stored.is_active is False
    assert stored.email == "keep@example.invalid"
    assert stored.department_id == unit.id
    assert stored.local_password_disabled is True

    cleared = client.put(
        f"/api/users/{target.id}",
        headers=headers,
        json={
            "role": "user",
            "email": None,
            "department_id": None,
            "is_active": None,
            "local_password_disabled": None,
        },
    )
    assert cleared.status_code == 200, cleared.text
    db.expire_all()
    stored = db.get(User, target.id)
    assert stored.role == "user"
    assert stored.is_active is False
    assert stored.local_password_disabled is True
    # 信箱與單位允許明確的 null。旗標不能寫成 null，否則停用帳號會被改壞。
    assert stored.email is None
    assert stored.department_id is None


def test_owner_cannot_be_assigned_deputy(client, db):
    make_user(db, username="cap-owner", role="owner")
    target = make_user(db, username="other-owner", role="owner")
    resp = client.put(
        f"/api/users/{target.id}",
        headers=_headers(client, "cap-owner"),
        json={"role": "deputy"},
    )
    assert resp.status_code == 403, resp.text
    db.refresh(target)
    assert target.role == "owner"


def test_deputy_unit_admin_approves_outside_their_unit(client, db):
    from app.models.department import Department
    from app.models.unit_admin_assignment import UnitAdminAssignment

    home = Department(name="代理的單位", is_active=True)
    other = Department(name="別的單位", is_active=True)
    db.add(home)
    db.add(other)
    db.commit()
    deputy = make_user(db, username="wide-deputy", role="deputy", department_id=home.id)
    db.add(UnitAdminAssignment(user_id=deputy.id, department_id=home.id, granted_by=deputy.id))
    pending = make_user(
        db, username="outside-pending", department_id=other.id, is_approved=False,
    )
    resp = client.post(
        f"/api/users/{pending.id}/approve",
        headers=_headers(client, "wide-deputy"),
    )
    assert resp.status_code == 200, resp.text
    db.refresh(pending)
    assert pending.is_approved is True


def test_alert_mail_drops_overflow_and_names_the_count(db, caplog):
    import logging

    make_user(db, username="cap-mail-owner", role="owner")
    owner = db.query(User).filter(User.username == "cap-mail-owner").one()
    owner.email = "cap-owner@example.invalid"
    for index in range(3):
        deputy = make_user(db, username=f"cap-deputy-{index}", role="deputy")
        deputy.email = f"cap-deputy-{index}@example.invalid"
    db.commit()
    configured = [f"group-{index}@example.invalid" for index in range(24)]
    with caplog.at_level(logging.WARNING, logger="app.services.alert_mail"):
        merged = merge_alert_recipients(db, configured)
    assert merged[:24] == configured
    assert "cap-owner@example.invalid" not in merged
    assert any("4" in record.message for record in caplog.records)


def test_oidc_deputy_default_role_provisions_a_normal_user(db, caplog):
    import logging

    from app.models.auth_provider import AuthProvider
    from app.services.external_auth_service import _provision_external_user

    provider = AuthProvider(
        name="deputy-default",
        provider_type="oidc",
        is_active=True,
        auto_create_users=True,
        default_role="deputy",
    )
    db.add(provider)
    db.commit()
    with caplog.at_level(logging.ERROR, logger="app.services.external_auth_service"):
        user = _provision_external_user(
            db,
            provider=provider,
            subject="ext-deputy",
            username="ext-person",
            email="ext-person@example.invalid",
            email_verified=True,
        )
    assert user.role == "user"
    assert "代理" in caplog.text


def test_alert_mail_includes_owner_and_active_deputies(client, db, monkeypatch):
    _allow_loopback(monkeypatch)
    make_user(db, username="mail-owner", role="owner")
    owner = db.query(User).filter(User.username == "mail-owner").one()
    owner.email = "owner@example.invalid"
    deputy = make_user(db, username="mail-deputy", role="deputy")
    deputy.email = "deputy@example.invalid"
    quiet = make_user(db, username="mail-deputy-off", role="deputy")
    quiet.email = "off@example.invalid"
    quiet.is_active = False
    db.commit()
    merged = merge_alert_recipients(db, ["ops@example.com", "owner@example.invalid"])
    assert merged[0] == "ops@example.com"
    assert merged[1:3] == ["owner@example.invalid", "deputy@example.invalid"]
    assert "ops@example.com" in merged
    assert "off@example.invalid" not in merged
    assert merged.count("owner@example.invalid") == 1

    make_user(db, username="mail-sender", role="admin")
    server = FakeSmtp()
    try:
        saved = client.put(
            "/api/alerts/mail",
            headers=_headers(client, "mail-sender"),
            json=_body(
                enabled=False,
                smtp_host="127.0.0.1",
                smtp_port=server.port,
                security="none",
                username="",
                password=None,
            ),
        )
        assert saved.status_code == 200, saved.text
        sent = client.post("/api/alerts/mail/test", headers=_headers(client, "mail-sender"))
        assert sent.status_code == 200, sent.text
        raw = server.messages[-1]
        assert "owner@example.invalid" in raw
        assert "deputy@example.invalid" in raw
        assert "ops@example.com" in raw
        assert "off@example.invalid" not in raw
        assert sent.json()["recipients"][0] == "ops@example.com"
        assert sent.json()["recipients"][:3] == merged[:3]
    finally:
        server.close()


def test_two_concurrent_promotions_stop_at_three_deputies(tmp_path):
    from app.services.auth_service import DEPUTY_ROLE, ensure_deputy_capacity
    from app.utils.security import hash_password

    engine = create_engine(
        f"sqlite:///{tmp_path / 'deputies.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
        poolclass=NullPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    seed = Session()
    for index in range(2):
        seed.add(User(
            username=f"held-{index}",
            hashed_password=hash_password("password"),
            role=DEPUTY_ROLE,
            is_active=True,
            is_approved=True,
        ))
    for index in range(2):
        seed.add(User(
            username=f"promote-{index}",
            hashed_password=hash_password("password"),
            role="user",
            is_active=True,
            is_approved=True,
        ))
    seed.commit()
    ids = [
        row.id
        for row in seed.query(User).filter(User.username.like("promote-%")).order_by(User.id)
    ]
    seed.close()

    barrier = threading.Barrier(2)
    results: list[int | str] = []

    def _promote(user_id: int) -> None:
        db = Session()
        try:
            user = db.query(User).filter(User.id == user_id).one()
            barrier.wait(timeout=5)
            ensure_deputy_capacity(db)
            user.role = DEPUTY_ROLE
            db.commit()
            results.append(200)
        except HTTPException as exc:
            results.append(exc.status_code)
            db.rollback()
        except Exception as exc:  # noqa: BLE001 — surfaced as a result
            results.append(f"{type(exc).__name__}: {exc}")
            db.rollback()
        finally:
            db.close()

    threads = [threading.Thread(target=_promote, args=(user_id,)) for user_id in ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert sorted(results) == [200, 409]
    check = Session()
    try:
        active = (
            check.query(User)
            .filter(
                User.role == DEPUTY_ROLE,
                User.is_active.is_(True),
                User.is_approved.is_(True),
            )
            .count()
        )
        assert active == 3
    finally:
        check.close()
        engine.dispose()


def test_postgres_deputy_cap_locks_before_counting(db, monkeypatch):
    from app.services.auth_service import ensure_deputy_capacity

    executed: list[str] = []
    order: list[str] = []

    class _Dialect:
        name = "postgresql"

    class _Bind:
        dialect = _Dialect()

    def _capture(stmt, *args, **kwargs):
        sql = str(stmt)
        executed.append(sql)
        order.append("lock" if "pg_advisory_xact_lock" in sql else sql)
        class _Result:
            def fetchone(self):
                return (None,)
        return _Result()

    def _count(*args, **kwargs):
        order.append("count")
        return 3

    monkeypatch.setattr(db, "get_bind", lambda: _Bind())
    monkeypatch.setattr(db, "execute", _capture)
    monkeypatch.setattr("app.services.auth_service.active_deputy_count", _count)
    with pytest.raises(HTTPException) as caught:
        ensure_deputy_capacity(db)
    assert caught.value.status_code == 409
    assert any("pg_advisory_xact_lock" in sql for sql in executed)
    assert order[0] == "lock"
    assert "count" in order


def test_unapproved_or_inactive_role_change_still_takes_a_deputy_slot(client, db):
    """尚未核准或已停用的帳號，改成代理的當下就佔名額，不能等核准才失敗。

    名額＝角色是代理的人數，不論是否已核准、是否啟用。
    部門等輸入先驗證；名額滿了又送不存在的部門，先回部門錯誤。
    """
    make_user(db, username="slot-owner", role="owner")
    owner_h = _headers(client, "slot-owner")
    for index in range(2):
        created = client.post(
            "/api/users",
            headers=owner_h,
            json={
                "username": f"slot-{index}",
                "password": "password",
                "role": "deputy",
            },
        )
        assert created.status_code == 200, created.text

    pending = make_user(db, username="slot-pending", is_approved=False)
    assigned = client.put(
        f"/api/users/{pending.id}",
        headers=owner_h,
        json={"role": "deputy"},
    )
    assert assigned.status_code == 200, assigned.text
    db.refresh(pending)
    assert pending.role == "deputy"
    assert pending.is_approved is False

    third = client.post(
        "/api/users",
        headers=owner_h,
        json={"username": "slot-third", "password": "password", "role": "deputy"},
    )
    assert third.status_code == 409, third.text
    approved = client.post(f"/api/users/{pending.id}/approve", headers=owner_h)
    assert approved.status_code == 200, approved.text
    db.refresh(pending)
    assert pending.is_approved is True

    # 上面已有 slot-0、slot-1、slot-pending 三位代理。再指派必須 409。
    idle = make_user(db, username="slot-idle", is_approved=False)
    idle.is_active = False
    db.commit()
    refused = client.put(
        f"/api/users/{idle.id}",
        headers=owner_h,
        json={"role": "deputy"},
    )
    assert refused.status_code == 409, refused.text
    db.refresh(idle)
    assert idle.role == "user"

    active_pending = make_user(db, username="slot-active-pending", is_approved=False)
    bad_department = client.put(
        f"/api/users/{active_pending.id}",
        headers=owner_h,
        json={"role": "deputy", "department_id": 999_999},
    )
    assert bad_department.status_code == 400, bad_department.text
    assert "部門" in bad_department.json()["detail"]
    db.refresh(active_pending)
    assert active_pending.role == "user"

    bad_create = client.post(
        "/api/users",
        headers=owner_h,
        json={
            "username": "slot-bad-dept",
            "password": "password",
            "role": "deputy",
            "department_id": 999_999,
        },
    )
    assert bad_create.status_code == 400, bad_create.text
    assert "部門" in bad_create.json()["detail"]


def test_sqlite_deputy_lock_warns_callers_about_pending_changes():
    import inspect

    from app.services.auth_service import lock_deputy_capacity

    doc = inspect.getdoc(lock_deputy_capacity) or ""
    assert "尚未提交" in doc


_EMPTY_SUMMARY = {
    "total_requests": 0,
    "total_prompt_tokens": 0,
    "total_completion_tokens": 0,
    "total_tokens": 0,
    "active_models": 0,
    "active_api_keys": 0,
}


def test_deputy_who_is_also_unit_admin_still_only_gets_platform_aggregates(
    client, db, monkeypatch
):
    from app.models.department import Department
    from app.models.unit_admin_assignment import UnitAdminAssignment

    unit = Department(name="代理的單位", is_active=True)
    db.add(unit)
    db.commit()
    deputy = make_user(db, username="deputy-also-unit", role="deputy", department_id=unit.id)
    db.add(UnitAdminAssignment(
        user_id=deputy.id,
        department_id=unit.id,
        granted_by=deputy.id,
    ))
    db.commit()
    headers = _headers(client, "deputy-also-unit")
    captured = {}

    def _summary(db, **kwargs):
        captured["summary"] = kwargs
        return _EMPTY_SUMMARY

    def _models(db, **kwargs):
        captured["models"] = kwargs
        return []

    def _export(db, range_key, **kwargs):
        captured["export"] = kwargs
        return "col\n"

    monkeypatch.setattr("app.api.usage.get_usage_summary", _summary)
    monkeypatch.setattr("app.api.usage.get_top_models", _models)
    monkeypatch.setattr("app.api.usage.export_usage_csv", _export)

    top = client.get("/api/usage/top-users?range=24h", headers=headers)
    assert top.status_code == 403, top.text

    summary = client.get("/api/usage/summary?range=24h", headers=headers)
    assert summary.status_code == 200, summary.text
    assert captured["summary"]["user_id"] is None
    assert captured["summary"]["scope_ids"] is None

    models = client.get("/api/usage/top-models?range=24h", headers=headers)
    assert models.status_code == 200, models.text
    assert captured["models"]["user_id"] is None
    assert captured["models"]["scope_ids"] is None

    def _departments(db, **kwargs):
        captured["departments"] = kwargs
        return []

    def _agents(db, **kwargs):
        captured["agents"] = kwargs
        return []

    monkeypatch.setattr("app.api.usage.get_top_departments", _departments)
    monkeypatch.setattr("app.api.usage.get_top_agents", _agents)

    departments = client.get(
        f"/api/usage/top-departments?range=24h&department_id={unit.id}",
        headers=headers,
    )
    assert departments.status_code == 200, departments.text
    assert captured["departments"]["department_id"] is None
    assert captured["departments"]["scope_ids"] is None

    agents = client.get(
        f"/api/usage/top-agents?days=7&department_id={unit.id}",
        headers=headers,
    )
    assert agents.status_code == 200, agents.text
    assert captured["agents"]["department_id"] is None
    assert captured["agents"]["scope_ids"] is None

    exported = client.get("/api/usage/export?range=24h", headers=headers)
    assert exported.status_code == 403, exported.text
    assert "export" not in captured

    admin = make_user(db, username="usage-export-admin", role="admin")
    admin_export = client.get(
        "/api/usage/export?range=24h",
        headers=_headers(client, admin.username),
    )
    assert admin_export.status_code == 200, admin_export.text
    assert captured["export"]["user_id"] is None

    named = client.get(
        f"/api/usage/export?range=24h&user_id={deputy.id}",
        headers=headers,
    )
    assert named.status_code == 403, named.text
