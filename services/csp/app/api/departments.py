from sqlalchemy import case, func
from sqlalchemy.orm import Session
from fastapi import APIRouter, Depends, HTTPException
from app.database import get_db
from app.models.department import Department
from app.models.user import User
from app.schemas.department import (
    DepartmentCreate,
    DepartmentUpdate,
    DepartmentResponse,
)
from app.services.audit_service import log_audit_event
from app.services.auth_service import require_admin

router = APIRouter(prefix="/api/departments", tags=["部門管理"])


def _ensure_unique_name(db: Session, name: str, exclude_id: int | None = None) -> None:
    query = db.query(Department).filter(Department.name == name)
    if exclude_id is not None:
        query = query.filter(Department.id != exclude_id)
    if query.first():
        raise HTTPException(status_code=400, detail="部門名稱已存在")


def _serialize_departments(db: Session) -> list[dict]:
    rows = (
        db.query(
            Department,
            func.count(User.id).label("user_count"),
            func.coalesce(
                func.sum(case((User.is_active == True, 1), else_=0)),
                0,
            ).label("active_user_count"),
        )
        .outerjoin(User, User.department_id == Department.id)
        .group_by(Department.id)
        .order_by(Department.is_active.desc(), Department.name)
        .all()
    )
    return [
        {
            "id": dept.id,
            "name": dept.name,
            "description": dept.description,
            "is_active": dept.is_active,
            "user_count": int(user_count or 0),
            "active_user_count": int(active_user_count or 0),
            "created_at": dept.created_at,
            "updated_at": dept.updated_at,
        }
        for dept, user_count, active_user_count in rows
    ]


def _get_serialized_department(db: Session, department_id: int) -> dict:
    return next(row for row in _serialize_departments(db) if row["id"] == department_id)


def _deactivate_department(db: Session, dept: Department) -> None:
    dept.is_active = False
    db.query(User).filter(User.department_id == dept.id).update(
        {User.department_id: None},
        synchronize_session=False,
    )


@router.get("", response_model=list[DepartmentResponse])
def list_departments(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return _serialize_departments(db)


@router.post("", response_model=DepartmentResponse)
def create_department(
    request: DepartmentCreate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ensure_unique_name(db, request.name)
    dept = Department(**request.model_dump())
    db.add(dept)
    db.commit()
    log_audit_event(
        db,
        actor=admin,
        action="create",
        resource_type="department",
        resource_id=dept.id,
        detail=f"建立部門「{dept.name}」",
        commit=True,
    )
    return _get_serialized_department(db, dept.id)


@router.put("/{department_id}", response_model=DepartmentResponse)
def update_department(
    department_id: int,
    request: DepartmentUpdate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    dept = db.query(Department).filter(Department.id == department_id).first()
    if not dept:
        raise HTTPException(status_code=404, detail="部門不存在")

    update_data = request.model_dump(exclude_unset=True)
    if "name" in update_data and update_data["name"]:
        _ensure_unique_name(db, update_data["name"], exclude_id=dept.id)

    for field, value in update_data.items():
        setattr(dept, field, value)

    if update_data.get("is_active") is False:
        _deactivate_department(db, dept)

    db.commit()
    log_audit_event(
        db,
        actor=admin,
        action="update",
        resource_type="department",
        resource_id=dept.id,
        detail=f"更新部門「{dept.name}」",
        commit=True,
    )
    return _get_serialized_department(db, dept.id)


@router.delete("/{department_id}")
def deactivate_department(
    department_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    dept = db.query(Department).filter(Department.id == department_id).first()
    if not dept:
        raise HTTPException(status_code=404, detail="部門不存在")
    if not dept.is_active:
        return {"message": "部門已停用"}

    _deactivate_department(db, dept)
    db.commit()
    log_audit_event(
        db,
        actor=admin,
        action="deactivate",
        resource_type="department",
        resource_id=dept.id,
        detail=f"停用部門「{dept.name}」",
        commit=True,
    )
    return {"message": "部門已停用，原部門使用者已解除綁定"}
