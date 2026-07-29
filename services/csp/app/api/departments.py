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
    DepartmentTreeNode,
)
from app.services.audit_service import log_audit_event
from app.services.auth_service import require_admin
from app.services.department_tree import (
    MAX_DEPTH,
    acquire_dept_tree_lock,
    build_tree,
    depth_under_parent,
    get_descendant_ids,
    get_subtree_height,
)

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
            "parent_id": dept.parent_id,
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


def _resolve_active_parent(db: Session, parent_id: int) -> Department:
    parent = db.query(Department).filter(Department.id == parent_id).first()
    if not parent or not parent.is_active:
        raise HTTPException(status_code=400, detail="父部門不存在或已停用")
    return parent


def _validate_parent_assignment(
    db: Session,
    *,
    parent_id: int | None,
    node_id: int | None = None,
) -> None:
    """建立／改掛父節點時檢查：存在性、循環、深度上限（含子樹高度）。"""
    if parent_id is not None:
        _resolve_active_parent(db, parent_id)
        if node_id is not None:
            if parent_id == node_id:
                raise HTTPException(status_code=400, detail="不可將部門設為自己的父部門")
            if parent_id in get_descendant_ids(db, node_id):
                raise HTTPException(status_code=400, detail="不可將子孫部門設為父部門")

    new_depth = depth_under_parent(db, parent_id)
    subtree_height = get_subtree_height(db, node_id) if node_id is not None else 1
    if new_depth + subtree_height - 1 > MAX_DEPTH:
        raise HTTPException(
            status_code=400,
            detail=f"部門層級不可超過 {MAX_DEPTH} 層（院→所→組）",
        )


def _ensure_no_active_children(db: Session, dept: Department) -> None:
    has_active_child = (
        db.query(Department.id)
        .filter(Department.parent_id == dept.id, Department.is_active.is_(True))
        .first()
    )
    if has_active_child:
        raise HTTPException(
            status_code=400,
            detail="請先停用所有子部門後再停用此部門",
        )


def _ensure_parent_active_for_reactivation(db: Session, dept: Department) -> None:
    if dept.parent_id is None:
        return
    parent = db.query(Department).filter(Department.id == dept.parent_id).first()
    if parent is not None and not parent.is_active:
        raise HTTPException(
            status_code=400,
            detail="父部門已停用，無法啟用此部門",
        )


def _parent_label(parent_id: int | None) -> str:
    return "無" if parent_id is None else str(parent_id)


@router.get("", response_model=list[DepartmentResponse])
def list_departments(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
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
):
    if request.parent_id is not None:
        acquire_dept_tree_lock(db)
    _ensure_unique_name(db, request.name)
    _validate_parent_assignment(db, parent_id=request.parent_id)
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
    if "parent_id" in update_data or "is_active" in update_data:
        acquire_dept_tree_lock(db)
        db.refresh(dept)

    if "name" in update_data and update_data["name"]:
        _ensure_unique_name(db, update_data["name"], exclude_id=dept.id)

    old_parent_id = dept.parent_id
    if "parent_id" in update_data:
        _validate_parent_assignment(
            db,
            parent_id=update_data["parent_id"],
            node_id=dept.id,
        )

    if update_data.get("is_active") is False:
        _ensure_no_active_children(db, dept)

    if update_data.get("is_active") is True and not dept.is_active:
        # 改掛已由 _validate_parent_assignment 驗過；僅在沿用現有 parent 時檢查
        if "parent_id" not in update_data:
            _ensure_parent_active_for_reactivation(db, dept)

    for field, value in update_data.items():
        setattr(dept, field, value)

    if update_data.get("is_active") is False:
        _deactivate_department(db, dept)

    db.commit()
    audit_detail = f"更新部門「{dept.name}」"
    if "parent_id" in update_data:
        audit_detail += (
            f"；母節點 {_parent_label(old_parent_id)}"
            f" -> {_parent_label(update_data['parent_id'])}"
        )
    log_audit_event(
        db,
        actor=admin,
        action="update",
        resource_type="department",
        resource_id=dept.id,
        detail=audit_detail,
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

    acquire_dept_tree_lock(db)
    db.refresh(dept)
    if not dept.is_active:
        return {"message": "部門已停用"}

    _ensure_no_active_children(db, dept)
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
