"""單位管理員指派 API（P1.3）。

指派是 admin 行為（SYSTEM-MAP「外單位來文,由本組指派」）。額度分配
遞延至 credit-ledger epic，本模組不承載。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.department import Department
from app.models.unit_admin_assignment import UnitAdminAssignment
from app.models.user import User
from app.services.auth_service import require_admin
from app.services.unit_admin_service import assign, revoke

router = APIRouter(prefix="/api/unit-admins", tags=["單位管理員"])


class UnitAdminAssignRequest(BaseModel):
    user_id: int
    department_id: int


class UnitAdminAssignmentResponse(BaseModel):
    id: int
    user_id: int
    department_id: int
    granted_by: int | None
    granted_at: datetime
    revoked_at: datetime | None

    model_config = {"from_attributes": True}


@router.post("", response_model=UnitAdminAssignmentResponse, status_code=201)
def create_assignment(
    body: UnitAdminAssignRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.id == body.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="使用者不存在")
    department = (
        db.query(Department)
        .filter(Department.id == body.department_id)
        .first()
    )
    if not department or not department.is_active:
        raise HTTPException(status_code=400, detail="部門不存在或已停用")
    return assign(
        db, user=user, department=department, granted_by=admin
    )


@router.get("", response_model=list[UnitAdminAssignmentResponse])
def list_assignments(
    department_id: int | None = Query(None),
    include_revoked: int = Query(0, description="1 = 含歷史撤銷紀錄"),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    query = db.query(UnitAdminAssignment)
    if department_id is not None:
        query = query.filter(
            UnitAdminAssignment.department_id == department_id
        )
    if not include_revoked:
        query = query.filter(UnitAdminAssignment.revoked_at.is_(None))
    return query.order_by(UnitAdminAssignment.granted_at.desc()).all()


@router.delete("/{assignment_id}")
def revoke_assignment(
    assignment_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    assignment = (
        db.query(UnitAdminAssignment)
        .filter(UnitAdminAssignment.id == assignment_id)
        .first()
    )
    if not assignment:
        raise HTTPException(status_code=404, detail="指派不存在")
    revoke(db, assignment=assignment, actor=admin)
    return {
        "message": "已撤銷單位管理員指派",
        "revoked_at": assignment.revoked_at,
    }
