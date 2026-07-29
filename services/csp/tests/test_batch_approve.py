"""P1.4 — batch approval.

PLAN acceptance: 「一次核准 50 個帳號」.
"""
from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.department import Department
from app.models.user import User
from app.services.unit_admin_service import get_unit_admin_scope_ids
from tests.conftest import login, make_user

BATCH_CAP = 500
BATCH_INPUT_LIMIT = 1000


@pytest.fixture(autouse=True)
def _bypass_dev_secret_gate(monkeypatch):
    import app.services.startup_security as ss_module

    monkeypatch.setattr(ss_module, "assert_no_dev_defaults", lambda: None)


def _csrf_headers(client: TestClient) -> dict:
    from app.middleware.cookies import CSRF_COOKIE_NAME

    return {"X-CSRF-Token": client.cookies.get(CSRF_COOKIE_NAME)}


def _auth_headers(client: TestClient, db, username="ba_admin", role="admin") -> dict:
    make_user(db, username=username, role=role)
    return {"Authorization": f"Bearer {login(client, username=username)}"}


def _bearer(client: TestClient, username: str) -> dict:
    return {"Authorization": f"Bearer {login(client, username=username)}"}


def _seed_two_units(db: Session):
    """甲=院A/所B/組C vs 乙=D（reuse unit-admin fixture shape）."""
    dept_a = Department(name="ba院A", parent_id=None, is_active=True)
    db.add(dept_a)
    db.flush()
    dept_b = Department(name="ba所B", parent_id=dept_a.id, is_active=True)
    db.add(dept_b)
    db.flush()
    dept_c = Department(name="ba組C", parent_id=dept_b.id, is_active=True)
    dept_d = Department(name="ba無關D", parent_id=None, is_active=True)
    db.add_all([dept_c, dept_d])
    db.flush()
    db.commit()
    return dept_a, dept_b, dept_c, dept_d


def _post_batch(client: TestClient, headers: dict, payload: dict):
    return client.post(
        "/api/users/batch-approve",
        json=payload,
        headers={**headers, **_csrf_headers(client)},
    )


def _assign_unit_admin(client: TestClient, admin_h: dict, user_id: int, dept_id: int):
    assert (
        client.post(
            "/api/unit-admins",
            json={"user_id": user_id, "department_id": dept_id},
            headers={**admin_h, **_csrf_headers(client)},
        ).status_code
        == 201
    )


# ── a. THE ACCEPTANCE ──────────────────────────────────────────────────────


def test_acceptance_batch_approve_50(client: TestClient, db: Session):
    headers = _auth_headers(client, db, username="ba_acc_admin")
    dept_a, _, _, _ = _seed_two_units(db)
    pending = [
        make_user(
            db,
            username=f"ba_pending_{i:02d}",
            department_id=dept_a.id,
            is_approved=False,
        )
        for i in range(50)
    ]

    r = _post_batch(client, headers, {"department_id": dept_a.id})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["approved"]) == 50
    assert body["skipped_already_approved"] == []
    assert body["rejected"] == []
    assert body["dry_run"] is False
    # Department has exactly 50 members; none silently omitted.
    assert body["total_requested"] == 50
    assert set(body["approved"]) == {u.id for u in pending}

    for u in pending:
        db.refresh(u)
        assert u.is_approved is True


# ── b. Subtree selection ───────────────────────────────────────────────────


def test_subtree_include_descendants(client: TestClient, db: Session):
    headers = _auth_headers(client, db, username="ba_sub_admin")
    dept_a, dept_b, dept_c, _ = _seed_two_units(db)
    u_a = make_user(db, username="ba_sub_a", department_id=dept_a.id, is_approved=False)
    u_b = make_user(db, username="ba_sub_b", department_id=dept_b.id, is_approved=False)
    u_c = make_user(db, username="ba_sub_c", department_id=dept_c.id, is_approved=False)

    r_all = _post_batch(
        client,
        headers,
        {"department_id": dept_a.id, "include_descendants": True},
    )
    assert r_all.status_code == 200, r_all.text
    assert set(r_all.json()["approved"]) == {u_a.id, u_b.id, u_c.id}
    assert r_all.json()["total_requested"] == 3  # subtree member count

    # Reset for include_descendants=false (refresh — client session wrote True)
    for u in (u_a, u_b, u_c):
        db.refresh(u)
        u.is_approved = False
    db.commit()

    r_own = _post_batch(
        client,
        headers,
        {"department_id": dept_a.id, "include_descendants": False},
    )
    assert r_own.status_code == 200, r_own.text
    assert set(r_own.json()["approved"]) == {u_a.id}
    assert r_own.json()["total_requested"] == 1  # own-node member count
    db.refresh(u_b)
    db.refresh(u_c)
    assert u_b.is_approved is False
    assert u_c.is_approved is False


# ── c. Idempotency ─────────────────────────────────────────────────────────


def test_idempotent_second_run_skips(client: TestClient, db: Session):
    headers = _auth_headers(client, db, username="ba_idem_admin")
    dept_a, _, _, _ = _seed_two_units(db)
    users = [
        make_user(
            db,
            username=f"ba_idem_{i}",
            department_id=dept_a.id,
            is_approved=False,
        )
        for i in range(3)
    ]
    ids = [u.id for u in users]

    r1 = _post_batch(client, headers, {"user_ids": ids})
    assert r1.status_code == 200, r1.text
    assert set(r1.json()["approved"]) == set(ids)
    assert r1.json()["total_requested"] == len(ids)  # all 3 ids visible

    approve_audits_after_first = (
        db.query(AuditLog)
        .filter(AuditLog.action == "approve", AuditLog.resource_type == "user")
        .count()
    )
    assert approve_audits_after_first == 3

    r2 = _post_batch(client, headers, {"user_ids": ids})
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["approved"] == []
    assert set(body["skipped_already_approved"]) == set(ids)
    assert body["rejected"] == []
    assert body["total_requested"] == len(ids)

    approve_audits_after_second = (
        db.query(AuditLog)
        .filter(AuditLog.action == "approve", AuditLog.resource_type == "user")
        .count()
    )
    assert approve_audits_after_second == approve_audits_after_first

    summaries = (
        db.query(AuditLog).filter(AuditLog.action == "batch_approve").count()
    )
    assert summaries == 2


# ── d. Unit-admin scoping ──────────────────────────────────────────────────


def test_unit_admin_scoping(client: TestClient, db: Session):
    admin_h = _auth_headers(client, db, username="ba_scope_admin")
    dept_a, dept_b, dept_c, dept_d = _seed_two_units(db)
    unit_admin = make_user(db, username="ba_ua", department_id=dept_b.id)
    _assign_unit_admin(client, admin_h, unit_admin.id, dept_b.id)

    in_scope = make_user(
        db, username="ba_ua_in", department_id=dept_c.id, is_approved=False
    )
    out_scope = make_user(
        db, username="ba_ua_out", department_id=dept_d.id, is_approved=False
    )
    ua_h = _bearer(client, "ba_ua")

    # Own department subtree OK
    r_ok = _post_batch(client, ua_h, {"department_id": dept_b.id})
    assert r_ok.status_code == 200, r_ok.text
    assert in_scope.id in r_ok.json()["approved"]
    db.refresh(in_scope)
    assert in_scope.is_approved is True

    # Explicit out-of-scope id: absent entirely (not rejected)
    r_ids = _post_batch(
        client, ua_h, {"user_ids": [out_scope.id, in_scope.id]}
    )
    assert r_ids.status_code == 200, r_ids.text
    body = r_ids.json()
    assert out_scope.id not in body["approved"]
    assert out_scope.id not in body["skipped_already_approved"]
    assert all(item["user_id"] != out_scope.id for item in body["rejected"])
    # in_scope already approved → skipped; out_scope silently omitted (1 of 2)
    assert in_scope.id in body["skipped_already_approved"]
    assert body["total_requested"] == 1

    # department_id outside scope → 403
    r_403 = _post_batch(client, ua_h, {"department_id": dept_d.id})
    assert r_403.status_code == 403
    assert "僅能查詢自己管理單位的資料" in r_403.json()["detail"]

    # Also 院A (parent of assignment) is outside unit-admin scope
    r_parent = _post_batch(client, ua_h, {"department_id": dept_a.id})
    assert r_parent.status_code == 403


# ── e. Elevated targets ────────────────────────────────────────────────────


def test_elevated_targets(client: TestClient, db: Session):
    admin_h = _auth_headers(client, db, username="ba_elev_admin")
    _, dept_b, _, _ = _seed_two_units(db)
    unit_admin = make_user(db, username="ba_elev_ua", department_id=dept_b.id)
    scoped_admin = make_user(
        db,
        username="ba_elev_target",
        role="admin",
        department_id=dept_b.id,
        is_approved=False,
    )
    _assign_unit_admin(client, admin_h, unit_admin.id, dept_b.id)
    ua_h = _bearer(client, "ba_elev_ua")

    r_ua = _post_batch(client, ua_h, {"user_ids": [scoped_admin.id]})
    assert r_ua.status_code == 200, r_ua.text
    body = r_ua.json()
    assert body["approved"] == []
    assert len(body["rejected"]) == 1
    assert body["rejected"][0]["user_id"] == scoped_admin.id
    assert body["rejected"][0]["reason"] == "需要管理員權限"
    assert body["total_requested"] == 1  # 1 id sent, all visible
    db.refresh(scoped_admin)
    assert scoped_admin.is_approved is False

    r_admin = _post_batch(client, admin_h, {"user_ids": [scoped_admin.id]})
    assert r_admin.status_code == 200, r_admin.text
    assert scoped_admin.id in r_admin.json()["approved"]
    db.refresh(scoped_admin)
    assert scoped_admin.is_approved is True


# ── f. Selector validation ─────────────────────────────────────────────────


def test_selector_validation(client: TestClient, db: Session):
    headers = _auth_headers(client, db, username="ba_sel_admin")
    _seed_two_units(db)

    r_neither = _post_batch(client, headers, {})
    assert r_neither.status_code == 400

    r_both = _post_batch(
        client, headers, {"user_ids": [1], "department_id": 1}
    )
    assert r_both.status_code == 400

    r_empty = _post_batch(client, headers, {"user_ids": []})
    assert r_empty.status_code == 400
    assert "user_ids" in r_empty.json()["detail"]


# ── g. Cap ─────────────────────────────────────────────────────────────────


def test_cap_over_500_department(client: TestClient, db: Session):
    headers = _auth_headers(client, db, username="ba_cap_admin")
    dept_a, _, _, _ = _seed_two_units(db)
    for i in range(BATCH_CAP + 1):
        make_user(
            db,
            username=f"ba_cap_{i:03d}",
            department_id=dept_a.id,
            is_approved=False,
        )

    r = _post_batch(client, headers, {"department_id": dept_a.id})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert str(BATCH_CAP) in detail


def test_cap_counts_pending_not_members(client: TestClient, db: Session):
    """大節點仍批得動:上限算待核准數,不算成員總數。

    3000 人的院級節點成員必然超過 500;若上限算在成員總數上,該節點只要
    大到超標就永遠無法批次核准(即使只剩一個待核准)。
    """
    headers = _auth_headers(client, db, username="ba_bigdept_admin")
    dept_a, _, _, _ = _seed_two_units(db)
    hashed = db.query(User).filter(User.username == "ba_bigdept_admin").first().hashed_password
    db.bulk_save_objects(
        [
            User(
                username=f"ba_big_{i:04d}",
                hashed_password=hashed,
                role="user",
                is_active=True,
                is_approved=True,
                department_id=dept_a.id,
            )
            for i in range(BATCH_CAP + 5)
        ]
    )
    db.commit()
    pending = make_user(
        db, username="ba_big_pending", department_id=dept_a.id, is_approved=False
    )

    r = _post_batch(client, headers, {"department_id": dept_a.id})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["approved"] == [pending.id]
    assert len(body["skipped_already_approved"]) == BATCH_CAP + 5
    db.refresh(pending)
    assert pending.is_approved is True


def test_cap_501_ids_resolve_to_zero(client: TestClient, db: Session):
    """501 ids that resolve to zero visible targets → empty buckets (not 400)."""
    headers = _auth_headers(client, db, username="ba_cap_zero_admin")
    _seed_two_units(db)
    # High ids with no rows; under raw-input limit, over old pre-resolution 500.
    fake_ids = list(range(9_000_001, 9_000_001 + BATCH_CAP + 1))
    r = _post_batch(client, headers, {"user_ids": fake_ids})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["approved"] == []
    assert body["skipped_already_approved"] == []
    assert body["rejected"] == []
    # 501 sent, all silently omitted (nonexistent)
    assert body["total_requested"] == 0


def test_cap_1001_raw_ids_refused(client: TestClient, db: Session):
    headers = _auth_headers(client, db, username="ba_cap_input_admin")
    _seed_two_units(db)
    r = _post_batch(
        client,
        headers,
        {"user_ids": list(range(1, BATCH_INPUT_LIMIT + 2))},
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert str(BATCH_INPUT_LIMIT) in detail


# ── h. dry_run ─────────────────────────────────────────────────────────────


def test_dry_run_no_writes(client: TestClient, db: Session):
    headers = _auth_headers(client, db, username="ba_dry_admin")
    dept_a, _, _, _ = _seed_two_units(db)
    pending = [
        make_user(
            db,
            username=f"ba_dry_{i}",
            department_id=dept_a.id,
            is_approved=False,
        )
        for i in range(3)
    ]

    r = _post_batch(
        client,
        headers,
        {"department_id": dept_a.id, "dry_run": True},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    assert len(body["approved"]) == 3
    assert body["total_requested"] == 3  # department member count

    for u in pending:
        db.refresh(u)
        assert u.is_approved is False

    assert (
        db.query(AuditLog).filter(AuditLog.action == "batch_approve").count()
        == 0
    )
    assert (
        db.query(AuditLog).filter(AuditLog.action == "approve").count() == 0
    )


# ── i. Query efficiency ────────────────────────────────────────────────────


def test_query_efficiency_bounded_selects(client: TestClient, db: Session, db_engine):
    headers = _auth_headers(client, db, username="ba_eff_admin")
    dept_a, _, _, _ = _seed_two_units(db)
    for i in range(50):
        make_user(
            db,
            username=f"ba_eff_{i:02d}",
            department_id=dept_a.id,
            is_approved=False,
        )

    selects: list[str] = []

    def _count(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    event.listen(db_engine, "before_cursor_execute", _count)
    try:
        r = _post_batch(client, headers, {"department_id": dept_a.id})
    finally:
        event.remove(db_engine, "before_cursor_execute", _count)

    assert r.status_code == 200, r.text
    assert len(r.json()["approved"]) == 50
    # Pin the shape: must not scale with batch size (50 round-trips forbidden).
    assert len(selects) < 10, f"too many SELECTs: {len(selects)}"


# ── j. FIX 2 — selector symmetry on mixed approved/pending ─────────────────


def test_selectors_agree_on_mixed_department(client: TestClient, db: Session):
    headers = _auth_headers(client, db, username="ba_mix_admin")
    dept_a, _, _, _ = _seed_two_units(db)
    already = [
        make_user(
            db,
            username=f"ba_mix_done_{i}",
            department_id=dept_a.id,
            is_approved=True,
        )
        for i in range(3)
    ]
    pending = [
        make_user(
            db,
            username=f"ba_mix_pend_{i}",
            department_id=dept_a.id,
            is_approved=False,
        )
        for i in range(2)
    ]
    all_ids = [u.id for u in already + pending]

    r_by_ids = _post_batch(
        client, headers, {"user_ids": all_ids, "dry_run": True}
    )
    r_by_dept = _post_batch(
        client, headers, {"department_id": dept_a.id, "dry_run": True}
    )
    assert r_by_ids.status_code == 200, r_by_ids.text
    assert r_by_dept.status_code == 200, r_by_dept.text
    a = r_by_ids.json()
    b = r_by_dept.json()
    assert set(a["approved"]) == set(b["approved"]) == {u.id for u in pending}
    assert (
        set(a["skipped_already_approved"])
        == set(b["skipped_already_approved"])
        == {u.id for u in already}
    )
    assert a["rejected"] == b["rejected"] == []
    assert a["total_requested"] == b["total_requested"] == 5


# ── k. FIX 5a — invisible ≡ nonexistent (full body equality) ───────────────


def test_invisible_equals_nonexistent_response(client: TestClient, db: Session):
    admin_h = _auth_headers(client, db, username="ba_inv_admin")
    _, dept_b, _, dept_d = _seed_two_units(db)
    unit_admin = make_user(db, username="ba_inv_ua", department_id=dept_b.id)
    _assign_unit_admin(client, admin_h, unit_admin.id, dept_b.id)
    ua_h = _bearer(client, "ba_inv_ua")

    out_scope = make_user(
        db, username="ba_inv_out", department_id=dept_d.id, is_approved=False
    )
    null_dept = make_user(
        db, username="ba_inv_null", department_id=None, is_approved=False
    )
    nonexistent_id = 9_999_991
    assert db.query(User).filter(User.id == nonexistent_id).first() is None

    r_out = _post_batch(client, ua_h, {"user_ids": [out_scope.id]})
    r_missing = _post_batch(client, ua_h, {"user_ids": [nonexistent_id]})
    assert r_out.status_code == 200
    assert r_missing.status_code == 200
    assert r_out.json() == r_missing.json()

    r_null = _post_batch(client, ua_h, {"user_ids": [null_dept.id]})
    r_missing2 = _post_batch(client, ua_h, {"user_ids": [nonexistent_id + 1]})
    assert r_null.status_code == 200
    assert r_missing2.status_code == 200
    assert r_null.json() == r_missing2.json()


# ── l. FIX 5c — plain user refused on both selectors ───────────────────────


def test_plain_user_refused_both_selectors(client: TestClient, db: Session):
    _auth_headers(client, db, username="ba_plain_admin")  # seed admin if needed
    dept_a, _, _, _ = _seed_two_units(db)
    plain = make_user(db, username="ba_plain", role="user", department_id=dept_a.id)
    target = make_user(
        db, username="ba_plain_tgt", department_id=dept_a.id, is_approved=False
    )
    plain_h = _bearer(client, "ba_plain")

    r_ids = _post_batch(client, plain_h, {"user_ids": [target.id]})
    assert r_ids.status_code == 403

    r_dept = _post_batch(client, plain_h, {"department_id": dept_a.id})
    assert r_dept.status_code == 403


# ── m. FIX 5d — NULL-department never approved / never in buckets ──────────


def test_null_department_never_in_buckets(client: TestClient, db: Session):
    headers = _auth_headers(client, db, username="ba_null_admin")
    dept_a, dept_b, _, _ = _seed_two_units(db)
    null_user = make_user(
        db, username="ba_null_u", department_id=None, is_approved=False
    )
    in_dept = make_user(
        db, username="ba_null_in", department_id=dept_a.id, is_approved=False
    )

    r_dept = _post_batch(client, headers, {"department_id": dept_a.id})
    assert r_dept.status_code == 200, r_dept.text
    body = r_dept.json()
    assert null_user.id not in body["approved"]
    assert null_user.id not in body["skipped_already_approved"]
    assert all(item["user_id"] != null_user.id for item in body["rejected"])
    assert in_dept.id in body["approved"]
    db.refresh(null_user)
    assert null_user.is_approved is False

    ua = make_user(db, username="ba_null_ua", department_id=dept_b.id)
    _assign_unit_admin(client, headers, ua.id, dept_b.id)
    ua_h = _bearer(client, "ba_null_ua")
    r_ua = _post_batch(client, ua_h, {"user_ids": [null_user.id]})
    assert r_ua.status_code == 200
    body_ua = r_ua.json()
    assert null_user.id not in body_ua["approved"]
    assert null_user.id not in body_ua["skipped_already_approved"]
    assert all(item["user_id"] != null_user.id for item in body_ua["rejected"])
    assert body_ua["total_requested"] == 0  # 1 id sent, 1 silently omitted
    db.refresh(null_user)
    assert null_user.is_approved is False


# ── n. FIX 1 / 5e — scope snapshot must not expand ─────────────────────────


def test_scope_snapshot_intersection(client: TestClient, db: Session, monkeypatch):
    admin_h = _auth_headers(client, db, username="ba_snap_admin")
    _, dept_b, dept_c, dept_d = _seed_two_units(db)
    unit_admin = make_user(db, username="ba_snap_ua", department_id=dept_b.id)
    _assign_unit_admin(client, admin_h, unit_admin.id, dept_b.id)

    in_scope = make_user(
        db, username="ba_snap_in", department_id=dept_c.id, is_approved=False
    )
    extra = make_user(
        db, username="ba_snap_extra", department_id=dept_d.id, is_approved=False
    )

    frozen_scope = get_unit_admin_scope_ids(db, unit_admin)
    assert frozen_scope is not None
    assert dept_d.id not in frozen_scope
    assert dept_c.id in frozen_scope

    # Freeze the first-read authorised set, then re-parent D under B so a
    # second get_descendant_ids(B) would include D.
    monkeypatch.setattr(
        "app.api.users.get_unit_admin_scope_ids",
        lambda db_, user: frozen_scope,
    )
    dept_d.parent_id = dept_b.id
    db.commit()

    ua_h = _bearer(client, "ba_snap_ua")
    r = _post_batch(client, ua_h, {"department_id": dept_b.id})
    assert r.status_code == 200, r.text
    body = r.json()
    assert in_scope.id in body["approved"]
    assert extra.id not in body["approved"]
    assert extra.id not in body["skipped_already_approved"]
    assert all(item["user_id"] != extra.id for item in body["rejected"])
    db.refresh(extra)
    assert extra.is_approved is False


# ── o. FIX 3 / 5f — audit fail-soft must not overstate success ─────────────


def test_audit_fail_soft_rolls_back_batch(client: TestClient, db: Session, monkeypatch):
    headers = _auth_headers(client, db, username="ba_audit_admin")
    dept_a, _, _, _ = _seed_two_units(db)
    targets = [
        make_user(
            db,
            username=f"ba_audit_{i}",
            department_id=dept_a.id,
            is_approved=False,
        )
        for i in range(5)
    ]
    ids = [u.id for u in targets]

    import app.api.users as users_mod

    real_log = users_mod.log_audit_event
    call_n = {"n": 0}

    def flaky_log(db_session, *args, **kwargs):
        call_n["n"] += 1
        # Third of five per-target approve audits → emulate fail-soft path.
        if call_n["n"] == 3:
            try:
                db_session.rollback()
            except Exception:
                pass
            return None
        return real_log(db_session, *args, **kwargs)

    monkeypatch.setattr(users_mod, "log_audit_event", flaky_log)

    before_audits = db.query(AuditLog).count()
    r = _post_batch(client, headers, {"user_ids": ids})
    assert r.status_code != 200
    assert r.status_code == 500

    for u in targets:
        db.refresh(u)
        assert u.is_approved is False

    after_audits = db.query(AuditLog).count()
    assert after_audits == before_audits
