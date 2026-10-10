from typing import Annotated

from sqlalchemy.orm import Session
from fastapi import APIRouter, Depends, HTTPException
from app.database import get_db
from app.models.department import Department
from app.models.user import User
from app.schemas.department import (
    DepartmentCreate,
    DepartmentUpdate,
    DepartmentResponse,
    DepartmentTreeNode,
)
from app.services.auth_service import require_admin
from app.services.department_tree import (
    build_tree,
    get_descendant_ids,
)
from app.services.department_writes import (
    _serialize_departments,
    create_department as write_department,
    deactivate_department as write_deactivate_department,
    update_department as write_update_department,
)

router = APIRouter(prefix="/api/departments", tags=["部門管理"])


def _commit_by_default() -> bool:
    """HTTP 呼叫一律提交。同一行程直接呼叫時可傳 commit=False。"""
    return True


@router.get("", response_model=list[DepartmentResponse])
def list_departments(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    # 2026-09-19 裁決：單位管理員＝人事與用量，不含組織樹 CRUD。
    # 本列表（含 tree / descendants / create / update / deactivate）維持
    # admin-only。UsersView 對單位管理員不畫部門選單、也不打這個端點。
    return _serialize_departments(db)


@router.get("/tree", response_model=list[DepartmentTreeNode])
def get_department_tree(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return build_tree(db)


@router.get("/{department_id}/descendants", response_model=list[DepartmentResponse])
def list_department_descendants(
    department_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    dept = db.query(Department).filter(Department.id == department_id).first()
    if not dept:
        raise HTTPException(status_code=404, detail="部門不存在")

    descendant_ids = get_descendant_ids(db, department_id, include_self=False)
    if not descendant_ids:
        return []
    return [row for row in _serialize_departments(db) if row["id"] in descendant_ids]


@router.post("", response_model=DepartmentResponse)
def create_department(
    request: DepartmentCreate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
    commit: Annotated[bool, Depends(_commit_by_default)] = True,
):
    return write_department(request, admin, db, commit=commit)


@router.put("/{department_id}", response_model=DepartmentResponse)
def update_department(
    department_id: int,
    request: DepartmentUpdate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
    commit: Annotated[bool, Depends(_commit_by_default)] = True,
):
    return write_update_department(department_id, request, admin, db, commit=commit)


@router.delete("/{department_id}")
def deactivate_department(
    department_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return write_deactivate_department(department_id, admin, db)
