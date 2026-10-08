"""單位管理員指派服務（P1.3）。

綁定表 ``unit_admin_assignments``；``users.role`` 不動。用量額度由管理員
設定，單位管理員只能查看自己範圍內的額度。

每節點最多 3 名指派的 active 管理員（人資帶入的主管不計）、以及重複指派檢查，一律在
``acquire_dept_tree_lock`` 下執行。
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import and_, case, func
from sqlalchemy.orm import Session

from app.models.department import Department
from app.models.unit_admin_assignment import UnitAdminAssignment
from app.models.user import User
from app.services.audit_service import log_audit_event
from app.services.auth_service import is_admin_tier
from app.services.department_tree import (
    acquire_dept_tree_lock,
    get_descendant_ids_many,
)

MAX_UNIT_ADMINS_PER_NODE = 3

# 人資登入自己寫 revoked_at，不呼叫 revoke()。
# 畫面若撤了人資列，下次登入又會授回，所以這裡直接拒絕。
HR_REVOKE_REFUSED = (
    "這位是人資帶入的主管，不能在這裡撤銷。"
    "人資不再列這個職稱，或是把人資資料庫的"
    "「主管自動成為單位管理員」關掉之後，"
    "這個人下次登入就會移除。"
)


def get_active_assignments(
    db: Session, user: User
) -> list[UnitAdminAssignment]:
    return (
        db.query(UnitAdminAssignment)
        .filter(
            UnitAdminAssignment.user_id == user.id,
            UnitAdminAssignment.revoked_at.is_(None),
        )
        .all()
    )


def is_unit_admin(db: Session, user: User) -> bool:
    """Any active assignment. Admin-tier callers also get False — tiers
    stay disjoint in behaviour (admins never need the unit-admin path).
    """
    if is_admin_tier(user):
        return False
    return (
        db.query(UnitAdminAssignment.id)
        .filter(
            UnitAdminAssignment.user_id == user.id,
            UnitAdminAssignment.revoked_at.is_(None),
        )
        .first()
        is not None
    )


def get_unit_admin_scope_ids(db: Session, user: User) -> set[int] | None:
    """Union of (bound node + descendants) over active assignments.

    ``None`` if the user has no active assignments. ONE department-edges
    load via ``get_descendant_ids_many``.
    """
    assignments = get_active_assignments(db, user)
    if not assignments:
        return None
    root_ids = {a.department_id for a in assignments}
    return get_descendant_ids_many(db, root_ids, include_self=True)


def assign(
    db: Session,
    *,
    user: User,
    department: Department,
    granted_by: User,
) -> UnitAdminAssignment:
    if is_admin_tier(user):
        raise HTTPException(
            status_code=400, detail="不可將 admin/owner 指派為單位管理員"
        )

    acquire_dept_tree_lock(db)
    # Re-read after lock so a concurrent deactivation is visible
    # (same pattern as departments.py update/delete paths).
    db.refresh(department)

    if not department.is_active:
        raise HTTPException(status_code=400, detail="部門不存在或已停用")

    existing = (
        db.query(UnitAdminAssignment)
        .filter(
            UnitAdminAssignment.user_id == user.id,
            UnitAdminAssignment.department_id == department.id,
            UnitAdminAssignment.revoked_at.is_(None),
        )
        .first()
    )
    if existing:
        raise HTTPException(status_code=400, detail="已是該單位的管理員")

    # 上限只算指派的。人資依職稱帶來的主管不占名額。
    active_count = (
        db.query(UnitAdminAssignment)
        .filter(
            UnitAdminAssignment.department_id == department.id,
            UnitAdminAssignment.revoked_at.is_(None),
            UnitAdminAssignment.source == "manual",
        )
        .count()
    )
    if active_count >= MAX_UNIT_ADMINS_PER_NODE:
        raise HTTPException(
            status_code=400,
            detail=f"每單位最多 {MAX_UNIT_ADMINS_PER_NODE} 名單位管理員",
        )

    assignment = UnitAdminAssignment(
        user_id=user.id,
        department_id=department.id,
        granted_by=granted_by.id,
    )
    db.add(assignment)
    db.commit()
    db.refresh(assignment)
    log_audit_event(
        db,
        actor=granted_by,
        action="unit_admin_assign",
        resource_type="user",
        resource_id=user.id,
        detail=(
            f"指派單位管理員「{user.username}」→ 部門「{department.name}」"
        ),
        commit=True,
    )
    return assignment


def counts_by_department(db: Session) -> dict:
    """每個部門自己的人數，一次算完。

    指派與人資分開。上層的管理員不算進下層——下層那一列是 0，
    就算父節點已經有人。已撤銷的列不計。
    """
    assigned_case = case(
        (UnitAdminAssignment.source == "manual", 1),
        else_=0,
    )
    hr_case = case(
        (UnitAdminAssignment.source == "hr", 1),
        else_=0,
    )
    rows = (
        db.query(
            Department.id,
            func.coalesce(func.sum(assigned_case), 0),
            func.coalesce(func.sum(hr_case), 0),
        )
        .outerjoin(
            UnitAdminAssignment,
            and_(
                UnitAdminAssignment.department_id == Department.id,
                UnitAdminAssignment.revoked_at.is_(None),
            ),
        )
        .group_by(Department.id)
        .all()
    )
    return {
        "limit": MAX_UNIT_ADMINS_PER_NODE,
        "departments": [
            {
                "department_id": dept_id,
                "assigned_count": int(assigned or 0),
                "hr_count": int(hr or 0),
            }
            for dept_id, assigned, hr in rows
        ],
    }


def revoke(
    db: Session,
    *,
    assignment: UnitAdminAssignment,
    actor: User,
) -> UnitAdminAssignment:
    if (assignment.source or "manual") == "hr":
        raise HTTPException(status_code=400, detail=HR_REVOKE_REFUSED)
    if assignment.revoked_at is not None:
        return assignment
    assignment.revoked_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(assignment)
    log_audit_event(
        db,
        actor=actor,
        action="unit_admin_revoke",
        resource_type="user",
        resource_id=assignment.user_id,
        detail=(
            f"撤銷單位管理員指派 id={assignment.id}"
            f"（user_id={assignment.user_id},"
            f" department_id={assignment.department_id}）"
        ),
        commit=True,
    )
    return assignment
