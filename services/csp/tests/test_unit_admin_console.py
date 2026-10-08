"""治理中心單位管理員頁要用的清單欄位、人數與拒絕撤銷人資列。"""
from __future__ import annotations

import os

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.department import Department
from app.models.unit_admin_assignment import UnitAdminAssignment
from app.services.unit_admin_service import MAX_UNIT_ADMINS_PER_NODE
from tests.conftest import login, make_user


@pytest.fixture(autouse=True)
def _bypass_dev_secret_gate(monkeypatch):
    import app.services.startup_security as ss_module

    monkeypatch.setattr(ss_module, "assert_no_dev_defaults", lambda: None)


def _csrf_headers(client: TestClient) -> dict:
    from app.middleware.cookies import CSRF_COOKIE_NAME

    return {"X-CSRF-Token": client.cookies.get(CSRF_COOKIE_NAME)}


def _admin_headers(client: TestClient, db: Session) -> dict:
    make_user(db, username="console_admin", role="admin")
    return {"Authorization": f"Bearer {login(client, username='console_admin')}"}


def _dept(db: Session, name: str, parent_id: int | None = None, *, active: bool = True) -> Department:
    dept = Department(name=name, parent_id=parent_id, is_active=active)
    db.add(dept)
    db.commit()
    db.refresh(dept)
    return dept


def test_list_fields_and_counts_exclude_hr_inherited_and_revoked(
    client: TestClient, db: Session
):
    headers = _admin_headers(client, db)
    parent = _dept(db, "console院")
    child = _dept(db, "console所", parent.id)
    empty = _dept(db, "console空組", child.id)
    inactive = _dept(db, "console停用", parent.id, active=False)

    assigned = make_user(db, username="2001", department_id=child.id)
    assigned.display_name = "王小明"
    assigned.hr_titles = ["不該出現在指派列"]
    boss = make_user(db, username="2002", department_id=child.id)
    boss.display_name = "林主管"
    boss.hr_titles = ["組長", "副組長"]
    extra = make_user(db, username="2003", department_id=child.id)
    extra.display_name = "多的人"
    revoked_user = make_user(db, username="2004", department_id=parent.id)
    db.commit()

    created = client.post(
        "/api/unit-admins",
        json={"user_id": assigned.id, "department_id": child.id},
        headers={**headers, **_csrf_headers(client)},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["display_name"] == "王小明"
    assert body["employee_no"] == "2001"
    assert body["department_name"] == "console所"
    assert body["source"] == "manual"
    assert body["hr_titles"] is None

    parent_asg = client.post(
        "/api/unit-admins",
        json={"user_id": extra.id, "department_id": parent.id},
        headers={**headers, **_csrf_headers(client)},
    )
    assert parent_asg.status_code == 201, parent_asg.text

    db.add(
        UnitAdminAssignment(
            user_id=boss.id,
            department_id=child.id,
            source="hr",
        )
    )
    db.add(
        UnitAdminAssignment(
            user_id=boss.id,
            department_id=empty.id,
            source="hr",
        )
    )
    gone = UnitAdminAssignment(
        user_id=revoked_user.id,
        department_id=child.id,
        source="manual",
    )
    db.add(gone)
    db.commit()
    db.refresh(gone)
    revoked = client.delete(
        f"/api/unit-admins/{gone.id}",
        headers={**headers, **_csrf_headers(client)},
    )
    assert revoked.status_code == 200, revoked.text

    listed = client.get("/api/unit-admins", headers=headers)
    assert listed.status_code == 200, listed.text
    rows = {row["id"]: row for row in listed.json()}
    manual = rows[created.json()["id"]]
    assert manual["employee_no"] == "2001"
    assert manual["hr_titles"] is None
    hr_rows = [row for row in rows.values() if row["source"] == "hr" and row["user_id"] == boss.id]
    assert len(hr_rows) == 2
    assert all(row["display_name"] == "林主管" for row in hr_rows)
    assert all(row["employee_no"] == "2002" for row in hr_rows)
    assert all(row["hr_titles"] == ["組長", "副組長"] for row in hr_rows)
    assert all(row["revoked_at"] is None for row in rows.values())

    counts = client.get("/api/unit-admins/counts", headers=headers)
    assert counts.status_code == 200, counts.text
    payload = counts.json()
    assert payload["limit"] == MAX_UNIT_ADMINS_PER_NODE
    by_dept = {row["department_id"]: row for row in payload["departments"]}
    assert by_dept[parent.id] == {
        "department_id": parent.id,
        "assigned_count": 1,
        "hr_count": 0,
    }
    # 子單位的人資不計入指派；父單位的指派也不算進子單位。
    assert by_dept[child.id]["assigned_count"] == 1
    assert by_dept[child.id]["hr_count"] == 1
    assert by_dept[empty.id]["assigned_count"] == 0
    assert by_dept[empty.id]["hr_count"] == 1
    assert by_dept[inactive.id]["assigned_count"] == 0
    assert by_dept[inactive.id]["hr_count"] == 0


def test_revoke_hr_row_is_refused(client: TestClient, db: Session):
    headers = _admin_headers(client, db)
    dept = _dept(db, "console人資所")
    boss = make_user(db, username="3001", department_id=dept.id)
    row = UnitAdminAssignment(user_id=boss.id, department_id=dept.id, source="hr")
    db.add(row)
    db.commit()
    db.refresh(row)

    refused = client.delete(
        f"/api/unit-admins/{row.id}",
        headers={**headers, **_csrf_headers(client)},
    )
    assert refused.status_code == 400, refused.text
    assert "不能在這裡撤銷" in refused.json()["detail"]
    assert "主管自動成為單位管理員" in refused.json()["detail"]
    db.refresh(row)
    assert row.revoked_at is None


def test_unit_admin_cannot_read_counts(client: TestClient, db: Session):
    _admin_headers(client, db)
    dept = _dept(db, "console範圍")
    person = make_user(db, username="console_unit", department_id=dept.id)
    db.add(
        UnitAdminAssignment(
            user_id=person.id,
            department_id=dept.id,
            source="manual",
        )
    )
    db.commit()
    headers = {"Authorization": f"Bearer {login(client, username='console_unit')}"}
    assert client.get("/api/unit-admins/counts", headers=headers).status_code == 403
    assert client.get("/api/unit-admins", headers=headers).status_code == 403
