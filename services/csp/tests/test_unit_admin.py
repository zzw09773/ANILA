"""P1.3 — unit admin binding, scoped usage/users, conversation-block pin.

PLAN acceptance: 「甲單位的管理員查不到乙單位的資料」.
Quota allocation is OUT of this package (deferred to credit-ledger).
"""
from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from sqlalchemy.orm import Session as SaSession

from app.models.audit_log import AuditLog
from app.models.conversation import Conversation
from app.models.department import Department
from app.models.token_usage import TokenUsage
from app.models.unit_admin_assignment import UnitAdminAssignment
from app.models.user_memory import ConversationMemoryChunk, UserFact
from app.services import department_tree as dept_tree_mod
from app.services.usage_service import (
    _resolve_scope_ids,
    get_chart_data,
)
from tests.conftest import login, make_agent, make_model, make_user

TOKENS_B = 200
TOKENS_C = 400
TOKENS_D = 800


@pytest.fixture(autouse=True)
def _bypass_dev_secret_gate(monkeypatch):
    import app.services.startup_security as ss_module

    monkeypatch.setattr(ss_module, "assert_no_dev_defaults", lambda: None)


def _csrf_headers(client: TestClient) -> dict:
    from app.middleware.cookies import CSRF_COOKIE_NAME

    return {"X-CSRF-Token": client.cookies.get(CSRF_COOKIE_NAME)}


def _auth_headers(client: TestClient, db, username="ua_admin", role="admin") -> dict:
    make_user(db, username=username, role=role)
    return {"Authorization": f"Bearer {login(client, username=username)}"}


def _bearer(client: TestClient, username: str) -> dict:
    return {"Authorization": f"Bearer {login(client, username=username)}"}


def _seed_two_units(db: Session):
    """甲=院A/所B/組C vs 乙=D（reuse rollup fixture shape）."""
    dept_a = Department(name="ua院A", parent_id=None, is_active=True)
    db.add(dept_a)
    db.flush()
    dept_b = Department(name="ua所B", parent_id=dept_a.id, is_active=True)
    db.add(dept_b)
    db.flush()
    dept_c = Department(name="ua組C", parent_id=dept_b.id, is_active=True)
    dept_d = Department(name="ua無關D", parent_id=None, is_active=True)
    db.add_all([dept_c, dept_d])
    db.flush()

    user_b = make_user(db, username="ua_member_b", department_id=dept_b.id)
    user_c = make_user(db, username="ua_member_c", department_id=dept_c.id)
    user_d = make_user(db, username="ua_member_d", department_id=dept_d.id)
    pending_c = make_user(
        db,
        username="ua_pending_c",
        department_id=dept_c.id,
        is_approved=False,
    )
    model = make_model(db, name="ua-model")

    now = datetime.now(timezone.utc)
    for user, dept_id, tokens in [
        (user_b, dept_b.id, TOKENS_B),
        (user_c, dept_c.id, TOKENS_C),
        (user_d, dept_d.id, TOKENS_D),
    ]:
        db.add(
            TokenUsage(
                api_key_id=None,
                user_id=user.id,
                department_id=dept_id,
                model_id=model.id,
                prompt_tokens=tokens // 2,
                completion_tokens=tokens - tokens // 2,
                total_tokens=tokens,
                request_timestamp=now,
            )
        )
    db.commit()
    return dept_a, dept_b, dept_c, dept_d, user_b, user_c, user_d, pending_c


# ── a. Assignment CRUD ─────────────────────────────────────────────────────


def test_assignment_crud(client: TestClient, db: Session):
    headers = _auth_headers(client, db, username="ua_asg_admin")
    _, dept_b, _, _, user_b, user_c, user_d, _ = _seed_two_units(db)

    # assign OK
    r = client.post(
        "/api/unit-admins",
        json={"user_id": user_b.id, "department_id": dept_b.id},
        headers={**headers, **_csrf_headers(client)},
    )
    assert r.status_code == 201, r.text
    asg_id = r.json()["id"]

    # duplicate 400
    r2 = client.post(
        "/api/unit-admins",
        json={"user_id": user_b.id, "department_id": dept_b.id},
        headers={**headers, **_csrf_headers(client)},
    )
    assert r2.status_code == 400
    assert "已是該單位的管理員" in r2.json()["detail"]

    # 4th admin on one node 400
    extras = [
        make_user(db, username=f"ua_extra_{i}", department_id=dept_b.id)
        for i in range(3)
    ]
    # already 1 (user_b); add 2 more → total 3; 4th fails
    for u in extras[:2]:
        assert (
            client.post(
                "/api/unit-admins",
                json={"user_id": u.id, "department_id": dept_b.id},
                headers={**headers, **_csrf_headers(client)},
            ).status_code
            == 201
        )
    r4 = client.post(
        "/api/unit-admins",
        json={"user_id": extras[2].id, "department_id": dept_b.id},
        headers={**headers, **_csrf_headers(client)},
    )
    assert r4.status_code == 400
    assert "每單位最多 3 名單位管理員" in r4.json()["detail"]

    # revoke then re-assign OK
    assert (
        client.delete(
            f"/api/unit-admins/{asg_id}",
            headers={**headers, **_csrf_headers(client)},
        ).status_code
        == 200
    )
    r_re = client.post(
        "/api/unit-admins",
        json={"user_id": user_b.id, "department_id": dept_b.id},
        headers={**headers, **_csrf_headers(client)},
    )
    assert r_re.status_code == 201, r_re.text

    # assignment endpoints 403 for non-admin
    plain = make_user(db, username="ua_plain_asg", department_id=dept_b.id)
    plain_h = _bearer(client, "ua_plain_asg")
    assert (
        client.post(
            "/api/unit-admins",
            json={"user_id": user_c.id, "department_id": dept_b.id},
            headers={**plain_h, **_csrf_headers(client)},
        ).status_code
        == 403
    )
    assert client.get("/api/unit-admins", headers=plain_h).status_code == 403

    # list active
    listed = client.get(
        f"/api/unit-admins?department_id={dept_b.id}", headers=headers
    )
    assert listed.status_code == 200
    assert all(row["revoked_at"] is None for row in listed.json())


# ── b. THE ACCEPTANCE ──────────────────────────────────────────────────────


def test_acceptance_cross_unit_isolation(client: TestClient, db: Session):
    admin_h = _auth_headers(client, db, username="ua_acc_admin")
    _, dept_b, dept_c, dept_d, user_b, user_c, user_d, pending_c = _seed_two_units(
        db
    )
    unit_admin = make_user(db, username="ua_of_b", department_id=dept_b.id)
    assert (
        client.post(
            "/api/unit-admins",
            json={"user_id": unit_admin.id, "department_id": dept_b.id},
            headers={**admin_h, **_csrf_headers(client)},
        ).status_code
        == 201
    )

    ua_h = _bearer(client, "ua_of_b")

    # usage: D out of scope → 403
    r_d = client.get(
        f"/api/usage/summary?department_id={dept_d.id}", headers=ua_h
    )
    assert r_d.status_code == 403
    assert "僅能查詢自己管理單位的資料" in r_d.json()["detail"]

    # usage: C in subtree → 200 correct sum
    r_c = client.get(
        f"/api/usage/summary?department_id={dept_c.id}", headers=ua_h
    )
    assert r_c.status_code == 200, r_c.text
    assert r_c.json()["total_tokens"] == TOKENS_C

    # omitted → B-subtree sum
    r_def = client.get("/api/usage/summary", headers=ua_h)
    assert r_def.status_code == 200
    assert r_def.json()["total_tokens"] == TOKENS_B + TOKENS_C

    # list_users: only B/C
    users = client.get("/api/users", headers=ua_h)
    assert users.status_code == 200
    names = {u["username"] for u in users.json()}
    assert "ua_member_b" in names
    assert "ua_member_c" in names
    assert "ua_pending_c" in names
    assert "ua_member_d" not in names
    assert "ua_acc_admin" not in names  # admin has no department

    # approve D → 404 (same as missing — must not distinguish existence)
    r_out = client.post(
        f"/api/users/{user_d.id}/approve",
        headers={**ua_h, **_csrf_headers(client)},
    )
    assert r_out.status_code == 404
    assert r_out.json()["detail"] == "使用者不存在"

    # approve in-scope pending → 200
    r_ok = client.post(
        f"/api/users/{pending_c.id}/approve",
        headers={**ua_h, **_csrf_headers(client)},
    )
    assert r_ok.status_code == 200, r_ok.text
    db.refresh(pending_c)
    assert pending_c.is_approved is True


# ── c. Elevated-target guard ───────────────────────────────────────────────


def test_unit_admin_cannot_touch_elevated(client: TestClient, db: Session):
    admin_h = _auth_headers(client, db, username="ua_elev_admin")
    _, dept_b, _, _, _, _, _, _ = _seed_two_units(db)
    unit_admin = make_user(db, username="ua_elev_ua", department_id=dept_b.id)
    scoped_admin = make_user(
        db, username="ua_scoped_admin", role="admin", department_id=dept_b.id
    )
    scoped_owner = make_user(
        db, username="ua_scoped_owner", role="owner", department_id=dept_b.id
    )
    assert (
        client.post(
            "/api/unit-admins",
            json={"user_id": unit_admin.id, "department_id": dept_b.id},
            headers={**admin_h, **_csrf_headers(client)},
        ).status_code
        == 201
    )
    ua_h = _bearer(client, "ua_elev_ua")
    for target in (scoped_admin, scoped_owner):
        assert (
            client.post(
                f"/api/users/{target.id}/approve",
                headers={**ua_h, **_csrf_headers(client)},
            ).status_code
            == 403
        )
        assert (
            client.delete(
                f"/api/users/{target.id}",
                headers={**ua_h, **_csrf_headers(client)},
            ).status_code
            == 403
        )


# ── d. Reactivate ──────────────────────────────────────────────────────────


def test_reactivate(client: TestClient, db: Session):
    admin_h = _auth_headers(client, db, username="ua_re_admin")
    _, dept_b, _, dept_d, user_b, _, user_d, _ = _seed_two_units(db)
    unit_admin = make_user(db, username="ua_re_ua", department_id=dept_b.id)
    assert (
        client.post(
            "/api/unit-admins",
            json={"user_id": unit_admin.id, "department_id": dept_b.id},
            headers={**admin_h, **_csrf_headers(client)},
        ).status_code
        == 201
    )
    # deactivate via admin first
    assert (
        client.delete(
            f"/api/users/{user_b.id}",
            headers={**admin_h, **_csrf_headers(client)},
        ).status_code
        == 200
    )
    user_d.is_active = False
    db.commit()

    ua_h = _bearer(client, "ua_re_ua")
    # in-scope 200
    r = client.post(
        f"/api/users/{user_b.id}/reactivate",
        headers={**ua_h, **_csrf_headers(client)},
    )
    assert r.status_code == 200, r.text
    # already-active 400
    r2 = client.post(
        f"/api/users/{user_b.id}/reactivate",
        headers={**ua_h, **_csrf_headers(client)},
    )
    assert r2.status_code == 400
    # out-of-scope 404 (same as missing)
    r_out = client.post(
        f"/api/users/{user_d.id}/reactivate",
        headers={**ua_h, **_csrf_headers(client)},
    )
    assert r_out.status_code == 404
    assert r_out.json()["detail"] == "使用者不存在"


# ── e. Cross-node union ────────────────────────────────────────────────────


def test_cross_node_union_scope(client: TestClient, db: Session):
    admin_h = _auth_headers(client, db, username="ua_union_admin")
    _, dept_b, dept_c, dept_d, user_b, user_c, user_d, _ = _seed_two_units(db)
    # second 所 under A for a cleaner "two 所" story — use B and D
    unit_admin = make_user(db, username="ua_union", department_id=dept_b.id)
    for dept in (dept_b, dept_d):
        assert (
            client.post(
                "/api/unit-admins",
                json={"user_id": unit_admin.id, "department_id": dept.id},
                headers={**admin_h, **_csrf_headers(client)},
            ).status_code
            == 201
        ), dept.name

    ua_h = _bearer(client, "ua_union")
    r = client.get("/api/usage/summary", headers=ua_h)
    assert r.status_code == 200
    assert r.json()["total_tokens"] == TOKENS_B + TOKENS_C + TOKENS_D

    names = {u["username"] for u in client.get("/api/users", headers=ua_h).json()}
    assert "ua_member_b" in names
    assert "ua_member_c" in names
    assert "ua_member_d" in names


# ── f. Conversation-blocking regression ────────────────────────────────────


def test_conversation_blocking_and_admin_only_routes(
    client: TestClient, db: Session
):
    admin_h = _auth_headers(client, db, username="ua_safe_admin")
    _, dept_b, _, _, user_b, user_c, _, _ = _seed_two_units(db)
    unit_admin = make_user(db, username="ua_safe_ua", department_id=dept_b.id)
    assert (
        client.post(
            "/api/unit-admins",
            json={"user_id": unit_admin.id, "department_id": dept_b.id},
            headers={**admin_h, **_csrf_headers(client)},
        ).status_code
        == 201
    )

    # Other user's conversation
    other_conv = Conversation(
        user_id=user_c.id,
        title="secret-title-should-not-leak",
    )
    db.add(other_conv)
    own_conv = Conversation(user_id=unit_admin.id, title="own-ok")
    db.add(own_conv)
    db.commit()

    ua_h = _bearer(client, "ua_safe_ua")

    detail = client.get(
        f"/api/conversations/{other_conv.id}", headers=ua_h
    )
    assert detail.status_code in (403, 404)

    listed = client.get("/api/conversations", headers=ua_h)
    assert listed.status_code == 200
    titles = {c.get("title") for c in listed.json()}
    assert "secret-title-should-not-leak" not in titles
    assert "own-ok" in titles

    assert client.get("/api/audit-logs", headers=ua_h).status_code == 403
    assert client.get("/api/usage/by-client", headers=ua_h).status_code == 403
    assert (
        client.get("/api/usage/legacy-token-stats", headers=ua_h).status_code
        == 403
    )

    users_body = client.get("/api/users", headers=ua_h).json()
    assert users_body
    conv_keys = {
        "conversation",
        "conversations",
        "conversation_id",
        "conversation_count",
        "title",
        "message_count",
        "last_message_at",
    }
    for row in users_body:
        assert conv_keys.isdisjoint(row.keys()), row.keys()


# ── g. Scope-load count pin (P1.2 fold-in) ──────────────────────────────────


def test_summary_one_department_edges_load(
    client: TestClient, db: Session, monkeypatch
):
    admin_h = _auth_headers(client, db, username="ua_load_admin")
    _, dept_b, _, _, _, _, _, _ = _seed_two_units(db)
    unit_admin = make_user(db, username="ua_load_ua", department_id=dept_b.id)
    assert (
        client.post(
            "/api/unit-admins",
            json={"user_id": unit_admin.id, "department_id": dept_b.id},
            headers={**admin_h, **_csrf_headers(client)},
        ).status_code
        == 201
    )

    calls = {"n": 0}
    real_load = dept_tree_mod._load_edges

    def spy_load(db_sess):
        calls["n"] += 1
        return real_load(db_sess)

    monkeypatch.setattr(dept_tree_mod, "_load_edges", spy_load)

    ua_h = _bearer(client, "ua_load_ua")
    # Reset after assign (assign/get_scope during login path may have loaded)
    calls["n"] = 0
    r = client.get("/api/usage/summary", headers=ua_h)
    assert r.status_code == 200
    assert calls["n"] == 1, f"expected 1 edges load, got {calls['n']}"


# ── FIX 1: uniform 404 for missing vs out-of-scope ─────────────────────────


@pytest.mark.parametrize(
    "path_suffix",
    ["approve", "deactivate", "reactivate"],
)
def test_account_mgmt_uniform_404_for_invisible(
    client: TestClient, db: Session, path_suffix: str
):
    """Plain user and unit admin: missing id == out-of-scope target."""
    admin_h = _auth_headers(client, db, username=f"ua_404_{path_suffix}_adm")
    _, dept_b, _, dept_d, _, _, user_d, _ = _seed_two_units(db)
    unit_admin = make_user(
        db, username=f"ua_404_{path_suffix}_ua", department_id=dept_b.id
    )
    plain = make_user(
        db, username=f"ua_404_{path_suffix}_plain", department_id=dept_b.id
    )
    assert (
        client.post(
            "/api/unit-admins",
            json={"user_id": unit_admin.id, "department_id": dept_b.id},
            headers={**admin_h, **_csrf_headers(client)},
        ).status_code
        == 201
    )
    if path_suffix == "reactivate":
        user_d.is_active = False
        db.commit()

    missing_id = 9_999_999
    endpoints = {
        "approve": (
            "post",
            f"/api/users/{{id}}/approve",
        ),
        "deactivate": ("delete", "/api/users/{id}"),
        "reactivate": ("post", "/api/users/{id}/reactivate"),
    }
    method, tmpl = endpoints[path_suffix]

    for username in (plain.username, unit_admin.username):
        h = {**_bearer(client, username), **_csrf_headers(client)}
        call = getattr(client, method)
        r_missing = call(tmpl.format(id=missing_id), headers=h)
        r_hidden = call(tmpl.format(id=user_d.id), headers=h)
        assert r_missing.status_code == r_hidden.status_code == 404
        assert r_missing.json()["detail"] == r_hidden.json()["detail"]
        assert r_missing.json()["detail"] == "使用者不存在"


# ── FIX 2: get_chart_data positional group_by compatibility ────────────────


def test_get_chart_data_positional_group_by(db: Session, monkeypatch):
    """Base positional form must bind group_by, not scope_ids."""
    import app.services.usage_service as us

    _seed_two_units(db)

    # get_chart_data bucket SQL is Postgres EXTRACT(); adapt for SQLite so
    # the positional-binding assertion can run against a real return value.
    real_lc = us.literal_column

    def _sqlite_bucket_lc(clause, *args, **kwargs):
        if isinstance(clause, str) and "EXTRACT(EPOCH FROM request_timestamp)" in clause:
            clause = clause.replace(
                "CAST(EXTRACT(EPOCH FROM request_timestamp) AS INTEGER)",
                "CAST(strftime('%s', request_timestamp) AS INTEGER)",
            )
        return real_lc(clause, *args, **kwargs)

    monkeypatch.setattr(us, "literal_column", _sqlite_bucket_lc)

    result = get_chart_data(db, "24h", None, None, None, None, "department")
    assert "timestamps" in result
    assert "series" in result
    # Department grouping produces named series (not a single 總計 bucket).
    names = {s["name"] for s in result["series"]}
    assert "總計" not in names
    assert names & {"ua院A", "ua所B", "ua組C", "ua無關D"}


# ── FIX 3: assign() lock then refresh order ────────────────────────────────


def test_assign_lock_before_refresh_order(
    client: TestClient, db: Session, monkeypatch
):
    from app.services import unit_admin_service as ua_svc

    order: list[str] = []
    real_refresh = SaSession.refresh
    real_lock = ua_svc.acquire_dept_tree_lock

    def _lock_spy(session):
        order.append("lock")
        return real_lock(session)

    def _refresh_spy(self, instance, *args, **kwargs):
        order.append("refresh")
        return real_refresh(self, instance, *args, **kwargs)

    monkeypatch.setattr(ua_svc, "acquire_dept_tree_lock", _lock_spy)
    monkeypatch.setattr(SaSession, "refresh", _refresh_spy)

    headers = _auth_headers(client, db, username="ua_lock_order_adm")
    _, dept_b, _, _, user_b, _, _, _ = _seed_two_units(db)
    order.clear()

    r = client.post(
        "/api/unit-admins",
        json={"user_id": user_b.id, "department_id": dept_b.id},
        headers={**headers, **_csrf_headers(client)},
    )
    assert r.status_code == 201, r.text
    assert "lock" in order
    assert "refresh" in order
    assert order.index("lock") < order.index("refresh")


# ── FIX 4: reject admin-tier assignment targets ────────────────────────────


def test_assign_rejects_admin_tier_targets(client: TestClient, db: Session):
    headers = _auth_headers(client, db, username="ua_no_elev_adm")
    _, dept_b, _, _, _, _, _, _ = _seed_two_units(db)
    target_admin = make_user(
        db, username="ua_tgt_admin", role="admin", department_id=dept_b.id
    )
    target_owner = make_user(
        db, username="ua_tgt_owner", role="owner", department_id=dept_b.id
    )
    # Pre-fill one real assignment so count pin is meaningful
    holder = make_user(db, username="ua_holder", department_id=dept_b.id)
    assert (
        client.post(
            "/api/unit-admins",
            json={"user_id": holder.id, "department_id": dept_b.id},
            headers={**headers, **_csrf_headers(client)},
        ).status_code
        == 201
    )
    before = (
        db.query(UnitAdminAssignment)
        .filter(
            UnitAdminAssignment.department_id == dept_b.id,
            UnitAdminAssignment.revoked_at.is_(None),
        )
        .count()
    )
    for target in (target_admin, target_owner):
        r = client.post(
            "/api/unit-admins",
            json={"user_id": target.id, "department_id": dept_b.id},
            headers={**headers, **_csrf_headers(client)},
        )
        assert r.status_code == 400, r.text
        assert "admin/owner" in r.json()["detail"]
    after = (
        db.query(UnitAdminAssignment)
        .filter(
            UnitAdminAssignment.department_id == dept_b.id,
            UnitAdminAssignment.revoked_at.is_(None),
        )
        .count()
    )
    assert after == before == 1


# ── FIX 5: _resolve_scope_ids intersection ─────────────────────────────────


def test_resolve_scope_ids_intersects(db: Session):
    dept_a, dept_b, dept_c, dept_d = _seed_two_units(db)[:4]
    # scope = B-subtree; department_id = A 展開為 {A,B,C} → 交集 = B-subtree
    broad = _resolve_scope_ids(
        db, department_id=dept_a.id, scope_ids=[dept_b.id, dept_c.id]
    )
    assert broad == sorted([dept_b.id, dept_c.id])

    # Non-overlapping → empty (not fallback)
    empty = _resolve_scope_ids(
        db, department_id=dept_d.id, scope_ids=[dept_b.id, dept_c.id]
    )
    assert empty == []

    # department_id inside scope → just that subtree
    narrow = _resolve_scope_ids(
        db, department_id=dept_c.id, scope_ids=[dept_b.id, dept_c.id]
    )
    assert narrow == [dept_c.id]


# ── FIX 6a–i: behavioural pins ─────────────────────────────────────────────


def _assign_ua(client, db, admin_h, username, dept):
    ua = make_user(db, username=username, department_id=dept.id)
    assert (
        client.post(
            "/api/unit-admins",
            json={"user_id": ua.id, "department_id": dept.id},
            headers={**admin_h, **_csrf_headers(client)},
        ).status_code
        == 201
    )
    return ua, _bearer(client, username)


def test_out_of_scope_department_403_on_usage_endpoints(
    client: TestClient, db: Session
):
    admin_h = _auth_headers(client, db, username="ua_oos_adm")
    _, dept_b, _, dept_d, _, _, _, _ = _seed_two_units(db)
    _, ua_h = _assign_ua(client, db, admin_h, "ua_oos", dept_b)
    detail = "僅能查詢自己管理單位的資料"
    paths = [
        f"/api/usage/top-models?department_id={dept_d.id}",
        f"/api/usage/top-users?department_id={dept_d.id}",
        f"/api/usage/top-departments?department_id={dept_d.id}",
        f"/api/usage/top-agents?department_id={dept_d.id}",
        f"/api/usage/export?department_id={dept_d.id}",
    ]
    for path in paths:
        r = client.get(path, headers=ua_h)
        assert r.status_code == 403, path
        assert detail in r.json()["detail"], path


def test_ancestor_of_bound_node_is_out_of_scope(client: TestClient, db: Session):
    admin_h = _auth_headers(client, db, username="ua_anc_adm")
    dept_a, dept_b, _, _, _, _, _, _ = _seed_two_units(db)
    _, ua_h = _assign_ua(client, db, admin_h, "ua_anc", dept_b)
    r = client.get(
        f"/api/usage/summary?department_id={dept_a.id}", headers=ua_h
    )
    assert r.status_code == 403
    assert "僅能查詢自己管理單位的資料" in r.json()["detail"]


def test_revoke_immediately_drops_scoped_access(client: TestClient, db: Session):
    admin_h = _auth_headers(client, db, username="ua_revdrop_adm")
    _, dept_b, _, _, _, _, _, _ = _seed_two_units(db)
    ua, ua_h = _assign_ua(client, db, admin_h, "ua_revdrop", dept_b)
    asg = (
        db.query(UnitAdminAssignment)
        .filter(
            UnitAdminAssignment.user_id == ua.id,
            UnitAdminAssignment.revoked_at.is_(None),
        )
        .one()
    )
    assert client.get("/api/usage/top-users", headers=ua_h).status_code == 200
    assert (
        client.delete(
            f"/api/unit-admins/{asg.id}",
            headers={**admin_h, **_csrf_headers(client)},
        ).status_code
        == 200
    )
    # Same bearer session: no caching — loses unit-admin gate immediately
    r = client.get("/api/usage/top-users", headers=ua_h)
    assert r.status_code == 403
    assert r.json()["detail"] == "需要管理員權限"
    assert client.get("/api/users", headers=ua_h).status_code == 403


def test_assign_missing_or_inactive_department_400(
    client: TestClient, db: Session
):
    headers = _auth_headers(client, db, username="ua_bad_dept_adm")
    _, dept_b, _, _, user_b, _, _, _ = _seed_two_units(db)
    inactive = Department(name="ua停用", parent_id=None, is_active=False)
    db.add(inactive)
    db.commit()
    db.refresh(inactive)

    r_miss = client.post(
        "/api/unit-admins",
        json={"user_id": user_b.id, "department_id": 9_999_999},
        headers={**headers, **_csrf_headers(client)},
    )
    assert r_miss.status_code == 400
    assert "部門不存在或已停用" in r_miss.json()["detail"]

    r_inact = client.post(
        "/api/unit-admins",
        json={"user_id": user_b.id, "department_id": inactive.id},
        headers={**headers, **_csrf_headers(client)},
    )
    assert r_inact.status_code == 400
    assert "部門不存在或已停用" in r_inact.json()["detail"]


def test_revoke_twice_is_idempotent(client: TestClient, db: Session):
    headers = _auth_headers(client, db, username="ua_idemp_adm")
    _, dept_b, _, _, user_b, _, _, _ = _seed_two_units(db)
    r = client.post(
        "/api/unit-admins",
        json={"user_id": user_b.id, "department_id": dept_b.id},
        headers={**headers, **_csrf_headers(client)},
    )
    assert r.status_code == 201
    asg_id = r.json()["id"]

    r1 = client.delete(
        f"/api/unit-admins/{asg_id}",
        headers={**headers, **_csrf_headers(client)},
    )
    assert r1.status_code == 200
    audits_after_first = (
        db.query(AuditLog)
        .filter(AuditLog.action == "unit_admin_revoke")
        .count()
    )

    r2 = client.delete(
        f"/api/unit-admins/{asg_id}",
        headers={**headers, **_csrf_headers(client)},
    )
    assert r2.status_code == 200
    audits_after_second = (
        db.query(AuditLog)
        .filter(AuditLog.action == "unit_admin_revoke")
        .count()
    )
    assert audits_after_second == audits_after_first


def test_unit_admin_cannot_reactivate_elevated(client: TestClient, db: Session):
    admin_h = _auth_headers(client, db, username="ua_re_elev_adm")
    _, dept_b, _, _, _, _, _, _ = _seed_two_units(db)
    _, ua_h = _assign_ua(client, db, admin_h, "ua_re_elev", dept_b)
    for role, uname in (("admin", "ua_re_elev_adm_t"), ("owner", "ua_re_elev_own_t")):
        target = make_user(db, username=uname, role=role, department_id=dept_b.id)
        target.is_active = False
        db.commit()
        r = client.post(
            f"/api/users/{target.id}/reactivate",
            headers={**ua_h, **_csrf_headers(client)},
        )
        assert r.status_code == 403, role


def test_get_top_agents_scoping_content(client: TestClient, db: Session):
    admin_h = _auth_headers(client, db, username="ua_agents_adm")
    _, dept_b, dept_c, dept_d, user_b, user_c, user_d, _ = _seed_two_units(db)
    _, ua_h = _assign_ua(client, db, admin_h, "ua_agents", dept_b)

    agent_b = make_agent(db, user_b, name="ua-agent-b", approval_status="approved")
    agent_c = make_agent(db, user_c, name="ua-agent-c", approval_status="approved")
    agent_d = make_agent(db, user_d, name="ua-agent-d", approval_status="approved")
    model = db.query(TokenUsage).first().model_id
    now = datetime.now(timezone.utc)
    for agent, user, dept_id, tokens in [
        (agent_b, user_b, dept_b.id, 111),
        (agent_c, user_c, dept_c.id, 222),
        (agent_d, user_d, dept_d.id, 333),
    ]:
        db.add(
            TokenUsage(
                api_key_id=None,
                user_id=user.id,
                department_id=dept_id,
                model_id=model,
                prompt_tokens=tokens // 2,
                completion_tokens=tokens - tokens // 2,
                total_tokens=tokens,
                request_timestamp=now,
                caller_agent_id=agent.id,
            )
        )
    db.commit()

    # Unit admin default scope = B-subtree → agents B+C only
    r_ua = client.get("/api/usage/top-agents", headers=ua_h)
    assert r_ua.status_code == 200, r_ua.text
    by_id = {row["agent_id"]: row for row in r_ua.json()}
    assert set(by_id) == {agent_b.id, agent_c.id}
    assert by_id[agent_b.id]["total_tokens"] == 111
    assert by_id[agent_c.id]["total_tokens"] == 222

    # Admin with explicit department filter = C only
    r_adm = client.get(
        f"/api/usage/top-agents?department_id={dept_c.id}", headers=admin_h
    )
    assert r_adm.status_code == 200
    adm_rows = r_adm.json()
    assert len(adm_rows) == 1
    assert adm_rows[0]["agent_id"] == agent_c.id
    assert adm_rows[0]["total_tokens"] == 222


def test_by_base_model_and_agent_usage_403_for_unit_admin(
    client: TestClient, db: Session
):
    admin_h = _auth_headers(client, db, username="ua_bbm_adm")
    _, dept_b, _, _, user_b, _, _, _ = _seed_two_units(db)
    _, ua_h = _assign_ua(client, db, admin_h, "ua_bbm", dept_b)
    agent = make_agent(db, user_b, name="ua-bbm-agent", approval_status="approved")
    assert client.get("/api/usage/by-base-model", headers=ua_h).status_code == 403
    assert (
        client.get(f"/api/usage/agents/{agent.id}", headers=ua_h).status_code
        == 403
    )


def test_memory_facts_and_chunks_own_rows_only(client: TestClient, db: Session):
    admin_h = _auth_headers(client, db, username="ua_mem_adm")
    _, dept_b, _, _, user_b, user_c, _, _ = _seed_two_units(db)
    ua, ua_h = _assign_ua(client, db, admin_h, "ua_mem", dept_b)

    own_conv = Conversation(user_id=ua.id, title="ua-mem-own")
    other_conv = Conversation(user_id=user_c.id, title="ua-mem-other")
    db.add_all([own_conv, other_conv])
    db.flush()

    db.add(
        UserFact(
            id=1,
            user_id=ua.id,
            key="own_key",
            value="own_val",
            confidence=1.0,
        )
    )
    db.add(
        UserFact(
            id=2,
            user_id=user_c.id,
            key="other_key",
            value="other_val",
            confidence=1.0,
        )
    )
    db.add(
        ConversationMemoryChunk(
            id=1,
            user_id=ua.id,
            conversation_id=own_conv.id,
            role="user",
            content="own chunk content",
            embedding="[]",
        )
    )
    db.add(
        ConversationMemoryChunk(
            id=2,
            user_id=user_c.id,
            conversation_id=other_conv.id,
            role="user",
            content="other chunk must not leak",
            embedding="[]",
        )
    )
    db.commit()

    facts = client.get("/api/memory/facts", headers=ua_h)
    assert facts.status_code == 200
    body = facts.json()
    assert body["total"] == 1
    assert body["facts"][0]["key"] == "own_key"
    assert body["facts"][0]["value"] == "own_val"

    chunks = client.get("/api/memory/chunks", headers=ua_h)
    assert chunks.status_code == 200
    cbody = chunks.json()
    assert cbody["total"] == 1
    assert "own chunk" in cbody["items"][0]["content"]
    assert all(
        "other chunk" not in item["content"] for item in cbody["items"]
    )
