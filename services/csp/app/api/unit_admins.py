"""單位管理員指派 API（P1.3）。

指派是 admin 行為（「外單位來文,由本組指派」）。額度分配
遞延至 credit-ledger epic，本模組不承載。

"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models.department import Department
from app.models.unit_admin_assignment import UnitAdminAssignment
from app.models.user import User
from app.services.auth_service import require_admin
from app.services.unit_admin_service import assign, counts_by_department, revoke
from app.schemas.base import ApiResponseModel

router = APIRouter(prefix="/api/unit-admins", tags=["單位管理員"])


class UnitAdminAssignRequest(BaseModel):
    user_id: int
    department_id: int


class UnitAdminAssignmentResponse(ApiResponseModel):
    id: int
    user_id: int
    department_id: int
    granted_by: int | None
    granted_at: datetime
    # manual＝管理員指派（計入每單位上限）；hr＝人資依職稱帶入（不計）
    source: str = "manual"
    revoked_at: datetime | None
    display_name: str | None = None
    employee_no: str | None = None
    department_name: str | None = None
    # 只有人資列帶上次看到的職稱。指派列是 null，避免和人資列混在一起。
    hr_titles: list[str] | None = None

    model_config = {"from_attributes": True}


class UnitAdminDepartmentCount(BaseModel):
    department_id: int
    assigned_count: int
    hr_count: int


class UnitAdminCountsResponse(BaseModel):
    limit: int
    departments: list[UnitAdminDepartmentCount]


def _titles_last_seen(user: User | None, source: str) -> list[str] | None:
    if source != "hr" or user is None:
        return None
    raw = user.hr_titles or []
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if isinstance(item, str) and item.strip()]


def _serialize(row: UnitAdminAssignment) -> UnitAdminAssignmentResponse:
    user = row.user
    department = row.department
    source = row.source or "manual"
    return UnitAdminAssignmentResponse(
        id=row.id,
        user_id=row.user_id,
        department_id=row.department_id,
        granted_by=row.granted_by,
        granted_at=row.granted_at,
        source=source,
        revoked_at=row.revoked_at,
        display_name=user.display_name if user is not None else None,
        employee_no=user.username if user is not None else None,
        department_name=department.name if department is not None else None,
        hr_titles=_titles_last_seen(user, source),
    )


def _assignment_query(db: Session):
    # user、department 一次帶出，清單不要一列再打一回。
    return db.query(UnitAdminAssignment).options(
        joinedload(UnitAdminAssignment.user),
        joinedload(UnitAdminAssignment.department),
    )


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
    created = assign(
        db, user=user, department=department, granted_by=admin
    )
    row = (
        _assignment_query(db)
        .filter(UnitAdminAssignment.id == created.id)
        .one()
    )
    return _serialize(row)


@router.get("/counts", response_model=UnitAdminCountsResponse)
def list_counts(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return counts_by_department(db)


@router.get("", response_model=list[UnitAdminAssignmentResponse])
def list_assignments(
    department_id: int | None = Query(None),
    include_revoked: int = Query(0, description="1 = 含歷史撤銷紀錄"),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    query = _assignment_query(db)
    if department_id is not None:
        query = query.filter(
            UnitAdminAssignment.department_id == department_id
        )
    if not include_revoked:
        query = query.filter(UnitAdminAssignment.revoked_at.is_(None))
    rows = query.order_by(UnitAdminAssignment.granted_at.desc()).all()
    return [_serialize(row) for row in rows]


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
