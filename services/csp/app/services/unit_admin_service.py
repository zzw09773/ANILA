"""單位管理員指派服務（P1.3）。

綁定表 ``unit_admin_assignments``；``users.role`` 不動。額度分配遞延至
credit-ledger epic（見 model / migration docstring）。

每節點最多 3 名 active 管理員、以及重複指派檢查，一律在
``acquire_dept_tree_lock`` 下執行。
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException
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

    active_count = (
        db.query(UnitAdminAssignment)
        .filter(
            UnitAdminAssignment.department_id == department.id,
            UnitAdminAssignment.revoked_at.is_(None),
        )
        .count()
    )
    if active_count >= MAX_UNIT_ADMINS_PER_NODE:
        raise HTTPException(
            status_code=400, detail="每單位最多 3 名單位管理員"
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


def revoke(
    db: Session,
    *,
    assignment: UnitAdminAssignment,
    actor: User,
) -> UnitAdminAssignment:
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
