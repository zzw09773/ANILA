"""P1.1 — department three-level tree (院 → 所 → 組) API tests.

Pins PLAN.md P1.1 acceptance: build a three-level tree, GET /tree returns
the nested shape, GET /{院}/descendants returns {所, 組}; depth/cycle/
deactivation guards return 400 with zh-TW messages.
"""
from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi.testclient import TestClient

from app.models.audit_log import AuditLog
from app.models.department import Department
from app.services.department_tree import acquire_dept_tree_lock, build_tree
from tests.conftest import login, make_user


@pytest.fixture(autouse=True)
def _bypass_dev_secret_gate(monkeypatch):
    import app.services.startup_security as ss_module

    monkeypatch.setattr(ss_module, "assert_no_dev_defaults", lambda: None)


def _auth_headers(client: TestClient, db, username="dept_admin", role="admin") -> dict:
    make_user(db, username=username, role=role)
    return {"Authorization": f"Bearer {login(client, username=username)}"}


def _create(client: TestClient, headers: dict, name: str, parent_id: int | None = None):
    body: dict = {"name": name}
    if parent_id is not None:
        body["parent_id"] = parent_id
    resp = client.post("/api/departments", json=body, headers=headers)
    return resp


def _build_three_level(client: TestClient, headers: dict):
    institute = _create(client, headers, "測試院").json()
    division = _create(client, headers, "測試所", parent_id=institute["id"]).json()
    section = _create(client, headers, "測試組", parent_id=division["id"]).json()
    return institute, division, section


# ── happy path: 院→所→組 + tree + descendants ──────────────────────────────


def test_three_level_tree_and_descendants(client: TestClient, db):
    headers = _auth_headers(client, db)
    institute, division, section = _build_three_level(client, headers)

    tree = client.get("/api/departments/tree", headers=headers)
    assert tree.status_code == 200, tree.text
    roots = tree.json()
    assert len(roots) == 1
    root = roots[0]
    assert root["id"] == institute["id"]
    assert root["name"] == "測試院"
    assert root["parent_id"] is None
    assert len(root["children"]) == 1
    child = root["children"][0]
    assert child["id"] == division["id"]
    assert child["parent_id"] == institute["id"]
    assert len(child["children"]) == 1
    grandchild = child["children"][0]
    assert grandchild["id"] == section["id"]
    assert grandchild["parent_id"] == division["id"]
    assert grandchild["children"] == []

    desc = client.get(
        f"/api/departments/{institute['id']}/descendants",
        headers=headers,
    )
    assert desc.status_code == 200, desc.text
    ids = {row["id"] for row in desc.json()}
    assert ids == {division["id"], section["id"]}


def test_descendants_404_for_missing_department(client: TestClient, db):
    headers = _auth_headers(client, db)
    resp = client.get("/api/departments/999999/descendants", headers=headers)
    assert resp.status_code == 404


# ── depth cap ──────────────────────────────────────────────────────────────


def test_depth_4_create_rejected(client: TestClient, db):
    headers = _auth_headers(client, db)
    _, _, section = _build_three_level(client, headers)

    resp = _create(client, headers, "第四層", parent_id=section["id"])
    assert resp.status_code == 400
    assert "層" in resp.json()["detail"]


def test_reparent_subtree_exceeding_depth_rejected(client: TestClient, db):
    """搬移「所→組」子樹到另一個「組」下會使最深節點超過 3 層。"""
    headers = _auth_headers(client, db, username="dept_admin_rp")
    institute, division, section = _build_three_level(client, headers)

    other_div = _create(client, headers, "另一所", parent_id=institute["id"]).json()
    other_sec = _create(client, headers, "另一組", parent_id=other_div["id"]).json()

    # 把「測試所」(含子孫「測試組」) 掛到「另一組」下 → 深度會變 4
    resp = client.put(
        f"/api/departments/{division['id']}",
        json={"parent_id": other_sec["id"]},
        headers=headers,
    )
    assert resp.status_code == 400
    assert "層" in resp.json()["detail"]
    # 確認未改寫
    listed = {d["id"]: d for d in client.get("/api/departments", headers=headers).json()}
    assert listed[division["id"]]["parent_id"] == institute["id"]
    assert listed[section["id"]]["parent_id"] == division["id"]


# ── cycle / parent validation ──────────────────────────────────────────────


def test_cycle_and_self_parent_rejected(client: TestClient, db):
    headers = _auth_headers(client, db, username="dept_admin_cycle")
    institute, division, section = _build_three_level(client, headers)

    cycle = client.put(
        f"/api/departments/{institute['id']}",
        json={"parent_id": section["id"]},
        headers=headers,
    )
    assert cycle.status_code == 400
    assert "子孫" in cycle.json()["detail"]

    self_parent = client.put(
        f"/api/departments/{institute['id']}",
        json={"parent_id": institute["id"]},
        headers=headers,
    )
    assert self_parent.status_code == 400
    assert "自己" in self_parent.json()["detail"]


def test_nonexistent_or_inactive_parent_rejected(client: TestClient, db):
    headers = _auth_headers(client, db, username="dept_admin_parent")
    institute = _create(client, headers, "根院").json()

    missing = _create(client, headers, "孤兒所", parent_id=999999)
    assert missing.status_code == 400
    assert "不存在或已停用" in missing.json()["detail"]

    # 建一個葉並停用，再以它為父
    leaf = _create(client, headers, "待停用組", parent_id=institute["id"]).json()
    deact = client.put(
        f"/api/departments/{leaf['id']}",
        json={"is_active": False},
        headers=headers,
    )
    assert deact.status_code == 200, deact.text

    under_inactive = _create(client, headers, "掛停用下", parent_id=leaf["id"])
    assert under_inactive.status_code == 400
    assert "不存在或已停用" in under_inactive.json()["detail"]


# ── deactivation / reactivation ────────────────────────────────────────────


def test_deactivate_parent_with_active_child_rejected(client: TestClient, db):
    headers = _auth_headers(client, db, username="dept_admin_deact")
    institute, division, section = _build_three_level(client, headers)

    # 所仍有 active 組 → 不可停用
    blocked = client.put(
        f"/api/departments/{division['id']}",
        json={"is_active": False},
        headers=headers,
    )
    assert blocked.status_code == 400
    assert "子部門" in blocked.json()["detail"]

    # 先停用組，再停用所 → 成功
    assert client.put(
        f"/api/departments/{section['id']}",
        json={"is_active": False},
        headers=headers,
    ).status_code == 200
    assert client.put(
        f"/api/departments/{division['id']}",
        json={"is_active": False},
        headers=headers,
    ).status_code == 200

    # 所已停用 → 不可啟用組
    reactivate = client.put(
        f"/api/departments/{section['id']}",
        json={"is_active": True},
        headers=headers,
    )
    assert reactivate.status_code == 400
    assert "父部門" in reactivate.json()["detail"]

    # DELETE 路徑同樣擋有 active 子節點的停用
    other_div = _create(client, headers, "刪除測試所", parent_id=institute["id"]).json()
    _create(client, headers, "刪除測試組", parent_id=other_div["id"])
    delete_blocked = client.delete(
        f"/api/departments/{other_div['id']}",
        headers=headers,
    )
    assert delete_blocked.status_code == 400
    assert "子部門" in delete_blocked.json()["detail"]


# ── flat list shape regression ─────────────────────────────────────────────


def test_flat_list_includes_parent_id(client: TestClient, db):
    headers = _auth_headers(client, db, username="dept_admin_flat")
    created = _create(client, headers, "扁平部門").json()
    assert created["parent_id"] is None

    listed = client.get("/api/departments", headers=headers)
    assert listed.status_code == 200
    row = next(r for r in listed.json() if r["id"] == created["id"])
    assert "parent_id" in row
    assert row["parent_id"] is None
    assert row["name"] == "扁平部門"
    assert "user_count" in row
    assert "active_user_count" in row


# ── Finding 2: cyclic data repair path ─────────────────────────────────────


def test_cycle_in_db_repairable_via_put_null(client: TestClient, db):
    """Manual cycle must not RecursionError; PUT parent_id=null repairs."""
    headers = _auth_headers(client, db, username="dept_admin_cycfix")
    a = Department(name="循環A", parent_id=None, is_active=True)
    b = Department(name="循環B", parent_id=None, is_active=True)
    db.add_all([a, b])
    db.commit()
    db.refresh(a)
    db.refresh(b)
    # Bypass API: A→B→A
    a.parent_id = b.id
    b.parent_id = a.id
    db.commit()

    # build_tree must not raise
    tree = build_tree(db)
    assert isinstance(tree, list)

    repair = client.put(
        f"/api/departments/{a.id}",
        json={"parent_id": None},
        headers=headers,
    )
    assert repair.status_code == 200, repair.text
    assert repair.json()["parent_id"] is None

    db.refresh(a)
    db.refresh(b)
    assert a.parent_id is None


def test_three_node_cycle_repairable_via_put_null(client: TestClient, db):
    """A→B→C→A cycle: revisit height must be 0 so PUT null is not depth-blocked."""
    headers = _auth_headers(client, db, username="dept_admin_cyc3")
    a = Department(name="三環A", parent_id=None, is_active=True)
    b = Department(name="三環B", parent_id=None, is_active=True)
    c = Department(name="三環C", parent_id=None, is_active=True)
    db.add_all([a, b, c])
    db.commit()
    db.refresh(a)
    db.refresh(b)
    db.refresh(c)
    # Bypass API: A→B→C→A
    a.parent_id = b.id
    b.parent_id = c.id
    c.parent_id = a.id
    db.commit()

    repair = client.put(
        f"/api/departments/{a.id}",
        json={"parent_id": None},
        headers=headers,
    )
    assert repair.status_code == 200, repair.text
    assert repair.json()["parent_id"] is None

    db.refresh(a)
    db.refresh(b)
    db.refresh(c)
    assert a.parent_id is None
    # Tree must be acyclic and buildable after repair
    tree = build_tree(db)
    assert isinstance(tree, list)
    by_id = {d.id: d for d in db.query(Department).all()}
    assert by_id[a.id].parent_id is None
    # Walk parents from each node; must terminate without revisit
    for start in (a.id, b.id, c.id):
        seen: set[int] = set()
        cur = start
        while by_id[cur].parent_id is not None:
            assert cur not in seen
            seen.add(cur)
            cur = by_id[cur].parent_id
            assert cur in by_id


# ── Finding 3: advisory lock wiring ────────────────────────────────────────


def test_advisory_lock_called_on_hierarchy_mutations(client: TestClient, db, monkeypatch):
    calls: list[str] = []

    def _spy(session):
        calls.append("lock")

    monkeypatch.setattr(
        "app.api.departments.acquire_dept_tree_lock",
        _spy,
    )

    headers = _auth_headers(client, db, username="dept_admin_lock")
    root = _create(client, headers, "鎖根").json()
    # create without parent → no lock
    assert "lock" not in calls or calls == []

    child = _create(client, headers, "鎖子", parent_id=root["id"])
    assert child.status_code == 200
    assert calls.count("lock") >= 1
    create_locks = calls.count("lock")

    reparent = client.put(
        f"/api/departments/{child.json()['id']}",
        json={"parent_id": None},
        headers=headers,
    )
    assert reparent.status_code == 200
    assert calls.count("lock") == create_locks + 1

    deact_put = client.put(
        f"/api/departments/{child.json()['id']}",
        json={"is_active": False},
        headers=headers,
    )
    assert deact_put.status_code == 200
    assert calls.count("lock") == create_locks + 2

    # Reactivate then DELETE-deactivate another leaf
    client.put(
        f"/api/departments/{child.json()['id']}",
        json={"is_active": True},
        headers=headers,
    )
    after_reactivate = calls.count("lock")
    delete_resp = client.delete(
        f"/api/departments/{child.json()['id']}",
        headers=headers,
    )
    assert delete_resp.status_code == 200
    assert calls.count("lock") == after_reactivate + 1


def test_update_lock_before_refresh_order(client: TestClient, db, monkeypatch):
    """Update path must lock then refresh before reading parent_id / is_active."""
    from sqlalchemy.orm import Session as SaSession

    order: list[str] = []
    real_refresh = SaSession.refresh

    def _lock_spy(session):
        order.append("lock")

    def _refresh_spy(self, instance, *args, **kwargs):
        order.append("refresh")
        return real_refresh(self, instance, *args, **kwargs)

    monkeypatch.setattr("app.api.departments.acquire_dept_tree_lock", _lock_spy)
    monkeypatch.setattr(SaSession, "refresh", _refresh_spy)

    headers = _auth_headers(client, db, username="dept_admin_lock_order")
    root = _create(client, headers, "順序根").json()
    child = _create(client, headers, "順序子", parent_id=root["id"]).json()
    order.clear()

    resp = client.put(
        f"/api/departments/{child['id']}",
        json={"parent_id": None},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert "lock" in order
    assert "refresh" in order
    assert order.index("lock") < order.index("refresh")


def test_acquire_dept_tree_lock_emits_sql_on_postgresql(db, monkeypatch):
    """Helper emits pg_advisory_xact_lock when dialect is postgresql."""
    executed: list[str] = []

    class _Dialect:
        name = "postgresql"

    class _Bind:
        dialect = _Dialect()

    real_execute = db.execute

    def _capture(stmt, *args, **kwargs):
        executed.append(str(stmt))
        # Don't hit real SQLite with PG lock SQL — return a stub.
        class _Result:
            def fetchone(self):
                return (None,)

        return _Result()

    monkeypatch.setattr(db, "get_bind", lambda: _Bind())
    monkeypatch.setattr(db, "execute", _capture)
    acquire_dept_tree_lock(db)
    assert any("pg_advisory_xact_lock" in s for s in executed)
    # Restore not needed — test ends; but avoid breaking other uses:
    monkeypatch.undo()


# ── Finding 4a: leaf deactivation unbinds users ────────────────────────────


def test_leaf_deactivation_unbinds_users(client: TestClient, db):
    headers = _auth_headers(client, db, username="dept_admin_unbind")
    institute, division, section = _build_three_level(client, headers)

    user_put = make_user(db, username="bound_put", role="user")
    user_put.department_id = section["id"]
    db.commit()

    assert client.put(
        f"/api/departments/{section['id']}",
        json={"is_active": False},
        headers=headers,
    ).status_code == 200
    db.refresh(user_put)
    assert user_put.department_id is None

    # DELETE path on another leaf
    other_sec = _create(client, headers, "刪除解綁組", parent_id=division["id"]).json()
    user_del = make_user(db, username="bound_del", role="user")
    user_del.department_id = other_sec["id"]
    db.commit()

    assert client.delete(
        f"/api/departments/{other_sec['id']}",
        headers=headers,
    ).status_code == 200
    db.refresh(user_del)
    assert user_del.department_id is None


# ── Finding 4b: positive re-parent cases ───────────────────────────────────


def test_positive_reparent_cases(client: TestClient, db):
    headers = _auth_headers(client, db, username="dept_admin_pos_rp")
    institute = _create(client, headers, "正院").json()
    division = _create(client, headers, "正所", parent_id=institute["id"]).json()

    # Legal move to exactly depth 3
    orphan = _create(client, headers, "待掛組").json()
    to_depth3 = client.put(
        f"/api/departments/{orphan['id']}",
        json={"parent_id": division["id"]},
        headers=headers,
    )
    assert to_depth3.status_code == 200, to_depth3.text
    assert to_depth3.json()["parent_id"] == division["id"]

    # Height-2 subtree under a depth-1 node
    other_root = _create(client, headers, "另一院").json()
    sub_div = _create(client, headers, "搬移所").json()
    sub_sec = _create(client, headers, "搬移組", parent_id=sub_div["id"]).json()
    move_h2 = client.put(
        f"/api/departments/{sub_div['id']}",
        json={"parent_id": other_root["id"]},
        headers=headers,
    )
    assert move_h2.status_code == 200, move_h2.text
    assert move_h2.json()["parent_id"] == other_root["id"]
    listed = {d["id"]: d for d in client.get("/api/departments", headers=headers).json()}
    assert listed[sub_sec["id"]]["parent_id"] == sub_div["id"]

    # Promote subtree to root (parent_id=null) + Finding 5 audit
    promote = client.put(
        f"/api/departments/{division['id']}",
        json={"parent_id": None},
        headers=headers,
    )
    assert promote.status_code == 200, promote.text
    assert promote.json()["parent_id"] is None

    audit = (
        db.query(AuditLog)
        .filter(
            AuditLog.resource_type == "department",
            AuditLog.action == "update",
            AuditLog.resource_id == str(division["id"]),
        )
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert audit is not None
    assert "母節點" in (audit.detail or "")
    assert str(institute["id"]) in (audit.detail or "")
    assert "無" in (audit.detail or "")


# ── Finding 4c: auth on /tree and /descendants ─────────────────────────────


def test_tree_and_descendants_auth(client: TestClient, db):
    headers = _auth_headers(client, db, username="dept_admin_auth")
    institute, _, _ = _build_three_level(client, headers)
    institute_id = institute["id"]

    # Drop session cookies left by login() so bare requests are truly unauthenticated.
    client.cookies.clear()
    assert client.get("/api/departments/tree").status_code == 401
    assert client.get(
        f"/api/departments/{institute_id}/descendants"
    ).status_code == 401

    user_headers = _auth_headers(client, db, username="dept_plain_user", role="user")
    assert client.get("/api/departments/tree", headers=user_headers).status_code == 403
    assert client.get(
        f"/api/departments/{institute_id}/descendants",
        headers=user_headers,
    ).status_code == 403


# ── Finding 4d: inactive nodes included in tree / descendants ──────────────


def test_tree_and_descendants_include_inactive(client: TestClient, db):
    headers = _auth_headers(client, db, username="dept_admin_inactive")
    institute, division, section = _build_three_level(client, headers)

    assert client.put(
        f"/api/departments/{section['id']}",
        json={"is_active": False},
        headers=headers,
    ).status_code == 200

    tree = client.get("/api/departments/tree", headers=headers)
    assert tree.status_code == 200
    # Walk to the inactive leaf
    root = tree.json()[0]
    inactive_leaf = root["children"][0]["children"][0]
    assert inactive_leaf["id"] == section["id"]
    assert inactive_leaf["is_active"] is False

    desc = client.get(
        f"/api/departments/{institute['id']}/descendants",
        headers=headers,
    )
    assert desc.status_code == 200
    by_id = {row["id"]: row for row in desc.json()}
    assert section["id"] in by_id
    assert by_id[section["id"]]["is_active"] is False


# ── Finding: generic-path parent_id index parity ───────────────────────────


def test_generic_startup_creates_departments_parent_id_index():
    """Non-Postgres backfill must create ix_departments_parent_id (SQLite)."""
    from sqlalchemy import create_engine, text

    from app.services.startup_migrations import _ensure_schema_backfills

    eng = create_engine("sqlite:///:memory:")
    with eng.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE departments (
                    id INTEGER PRIMARY KEY,
                    name VARCHAR(100) NOT NULL UNIQUE,
                    description VARCHAR(255),
                    is_active BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP
                )
                """
            )
        )

    _ensure_schema_backfills(eng)

    with eng.connect() as conn:
        rows = conn.execute(
            text("PRAGMA index_list('departments')")
        ).fetchall()
    names = {row[1] for row in rows}  # (seq, name, unique, origin, partial)
    assert "ix_departments_parent_id" in names
