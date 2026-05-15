from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.agent import Agent, UserAgentPermission
from app.models.alert import Alert
from app.models.api_key import ApiKey, ApiKeyModelPermission
from app.models.audit_log import AuditLog
from app.models.department import Department
from app.models.model_registry import ModelRegistry
from app.models.user import User, UserModelPermission
from app.schemas.user import (
    UserCreate,
    UserUpdate,
    UserResponse,
    AdminResetPassword,
    AllowedModelItem,
    UserAllowedModelsUpdate,
    UserAllowedAgentsUpdate,
)
from app.services.audit_service import log_audit_event
from app.services.auth_service import (
    get_current_user,
    is_admin_tier,
    is_owner,
    require_admin,
)
from app.utils.security import hash_password

router = APIRouter(prefix="/api/users", tags=["使用者管理"])

_ELEVATED_ROLES = {"admin", "owner"}


def _ensure_owner_for_elevated(target_role: str | None, current_user: User) -> None:
    """Block non-owner admins from creating / promoting / demoting /
    deleting / resetting-password admins or owners.

    Only ``owner`` may touch the elevated tier — keeps the
    "owner > admin > dev/user" hierarchy from being subverted by an
    admin promoting themselves or another account they control.
    """
    if target_role in _ELEVATED_ROLES and not is_owner(current_user):
        raise HTTPException(
            status_code=403,
            detail="需要 owner 權限以管理 admin/owner 帳號",
        )


def _cascade_user_key_permissions(db: Session, user: User, allowed_ids: set) -> int:
    """將 user 所有 API key 的 model 權限交集到 allowed_ids。回傳被刪除的 row 數。"""
    cascade_count = 0
    user_keys = db.query(ApiKey).filter(ApiKey.user_id == user.id).all()
    for key in user_keys:
        q = db.query(ApiKeyModelPermission).filter(
            ApiKeyModelPermission.api_key_id == key.id
        )
        if allowed_ids:
            q = q.filter(~ApiKeyModelPermission.model_id.in_(allowed_ids))
        cascade_count += q.delete(synchronize_session=False)
    return cascade_count


def _validate_department_id(db: Session, department_id: int | None) -> int | None:
    if department_id is None:
        return None
    department = db.query(Department).filter(Department.id == department_id).first()
    if not department or not department.is_active:
        raise HTTPException(status_code=400, detail="部門不存在或已停用")
    return department.id


@router.get("", response_model=list[UserResponse])
def list_users(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return db.query(User).order_by(User.created_at.desc()).all()


@router.post("", response_model=UserResponse)
def create_user(
    request: UserCreate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    _ensure_owner_for_elevated(request.role, admin)
    existing = db.query(User).filter(User.username == request.username).first()
    if existing:
        raise HTTPException(status_code=400, detail="使用者名稱已存在")

    user = User(
        username=request.username,
        email=request.email,
        hashed_password=hash_password(request.password),
        role=request.role,
        department_id=_validate_department_id(db, request.department_id),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    log_audit_event(
        db,
        actor=admin,
        action="create",
        resource_type="user",
        resource_id=user.id,
        detail=f"建立使用者「{user.username}」",
        commit=True,
    )
    return user


@router.get("/{user_id}", response_model=UserResponse)
def get_user(
    user_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="使用者不存在")
    return user


@router.put("/{user_id}", response_model=UserResponse)
def update_user(
    user_id: int,
    request: UserUpdate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="使用者不存在")

    update_data = request.model_dump(exclude_unset=True)
    # Owner-only gate: editing an admin/owner row OR promoting any row
    # to admin/owner both go through this check. Either side being
    # elevated is enough to require the higher tier.
    new_role = update_data.get("role")
    _ensure_owner_for_elevated(user.role, admin)
    _ensure_owner_for_elevated(new_role, admin)

    if "department_id" in update_data:
        update_data["department_id"] = _validate_department_id(
            db,
            update_data["department_id"],
        )
    # admin / owner 都是 admin tier;捕捉「從 admin-tier 掉下去」的轉換,
    # owner→admin 維持 admin-tier 不需要 cascade,admin→owner 是升級也不需要。
    # 只在離開 admin-tier 時 (admin→user / developer 或 owner→user / developer)
    # 才需要把 API key 權限收斂回 user allowlist 並讓舊 JWT 失效。
    was_admin_tier = is_admin_tier(user)
    for field, value in update_data.items():
        setattr(user, field, value)

    if was_admin_tier and not is_admin_tier(user):
        allowed_ids = {m.id for m in user.allowed_models}
        _cascade_user_key_permissions(db, user, allowed_ids)
        user.token_version = (user.token_version or 0) + 1

    db.commit()
    db.refresh(user)
    log_audit_event(
        db,
        actor=admin,
        action="update",
        resource_type="user",
        resource_id=user.id,
        detail=f"更新使用者「{user.username}」",
        commit=True,
    )
    return user


@router.post("/{user_id}/reset-password")
def admin_reset_password(
    user_id: int,
    request: AdminResetPassword,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="使用者不存在")
    _ensure_owner_for_elevated(user.role, admin)
    user.hashed_password = hash_password(request.new_password)
    user.token_version = (user.token_version or 0) + 1
    db.commit()
    log_audit_event(
        db,
        actor=admin,
        action="reset_password",
        resource_type="user",
        resource_id=user.id,
        detail=f"重設使用者「{user.username}」密碼",
        commit=True,
    )
    return {"message": f"已重設使用者「{user.username}」的密碼，現有權杖已失效"}


@router.get("/me/allowed-models", response_model=list[AllowedModelItem])
def get_my_allowed_models(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if is_admin_tier(current_user):
        models = db.query(ModelRegistry).filter(ModelRegistry.is_active == True).all()
    else:
        models = current_user.allowed_models
    return [{"id": m.id, "display_name": m.display_name, "model_type": m.model_type} for m in models]


@router.get("/{user_id}/allowed-models", response_model=list[AllowedModelItem])
def get_user_allowed_models(
    user_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="使用者不存在")
    models = user.allowed_models
    return [{"id": m.id, "display_name": m.display_name, "model_type": m.model_type} for m in models]


@router.put("/{user_id}/allowed-models")
def update_user_allowed_models(
    user_id: int,
    request: UserAllowedModelsUpdate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="使用者不存在")

    new_ids = set(request.model_ids)

    # Validate model IDs
    if new_ids:
        valid_count = db.query(ModelRegistry).filter(ModelRegistry.id.in_(new_ids)).count()
        if valid_count != len(new_ids):
            raise HTTPException(status_code=400, detail="包含不存在的模型 ID")

    # Overwrite user_model_permissions
    db.query(UserModelPermission).filter(UserModelPermission.user_id == user_id).delete()
    for mid in new_ids:
        db.add(UserModelPermission(user_id=user_id, model_id=mid))

    # Cascade: remove revoked models from all this user's API keys
    # (admin-tier accounts skip — their keys bypass model permission rows).
    cascade_count = 0
    if not is_admin_tier(user):
        cascade_count = _cascade_user_key_permissions(db, user, new_ids)

    db.commit()
    log_audit_event(
        db,
        actor=admin,
        action="update_allowed_models",
        resource_type="user",
        resource_id=user.id,
        detail=f"更新使用者「{user.username}」可用模型",
        commit=True,
    )
    return {
        "message": f"已更新使用者「{user.username}」的可用模型，並同步調整 {cascade_count} 筆 API key 權限"
    }


@router.get("/{user_id}/allowed-agents")
def get_user_allowed_agents(
    user_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="使用者不存在")
    agents = user.allowed_agents
    return [{"id": a.id, "name": a.name, "description_for_router": a.description_for_router,
             "approval_status": a.approval_status} for a in agents]


@router.put("/{user_id}/allowed-agents")
def update_user_allowed_agents(
    user_id: int,
    request: UserAllowedAgentsUpdate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="使用者不存在")

    new_ids = set(request.agent_ids)
    if new_ids:
        valid_count = db.query(Agent).filter(Agent.id.in_(new_ids)).count()
        if valid_count != len(new_ids):
            raise HTTPException(status_code=400, detail="包含不存在的 Agent ID")

    db.query(UserAgentPermission).filter(UserAgentPermission.user_id == user_id).delete()
    for aid in new_ids:
        db.add(UserAgentPermission(user_id=user_id, agent_id=aid))

    db.commit()
    log_audit_event(
        db, actor=admin, action="update_allowed_agents", resource_type="user",
        resource_id=user.id, detail=f"更新使用者「{user.username}」可用 agents", commit=True,
    )
    return {"message": f"已更新使用者「{user.username}」的可用 agents"}


@router.post("/{user_id}/approve")
def approve_user(
    user_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="使用者不存在")
    if user.is_approved:
        return {"message": f"使用者「{user.username}」已是核准狀態"}
    user.is_approved = True
    db.commit()
    log_audit_event(
        db,
        actor=admin,
        action="approve",
        resource_type="user",
        resource_id=user.id,
        detail=f"核准使用者「{user.username}」",
        commit=True,
    )
    return {"message": f"已核准使用者「{user.username}」"}


@router.delete("/{user_id}")
def deactivate_user(
    user_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="使用者不存在")
    _ensure_owner_for_elevated(user.role, admin)
    user.is_active = False
    user.token_version = (user.token_version or 0) + 1
    db.commit()
    log_audit_event(
        db,
        actor=admin,
        action="deactivate",
        resource_type="user",
        resource_id=user.id,
        detail=f"停用使用者「{user.username}」",
        commit=True,
    )
    return {"message": "使用者已停用"}


@router.delete("/{user_id}/permanent")
def hard_delete_user(
    user_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """**永久刪除**使用者 (跟 ``DELETE /{user_id}`` 的 soft-deactivate 對照)。

    跟 deactivate 的差別：

    - deactivate：``is_active=False``，row 還在，audit / 對話歷史皆保留
      原 actor 名，未來可由 admin reactivate。
    - **permanent**：row 從 DB 移除，CASCADE FK 的相關資料（對話、私人記憶、
      external identity、grant、permission）一起被刪；SET NULL FK
      （audit log actor、handoff、attachment uploader）保留資料但失去 actor。
      無法 undo，必須 admin 重新刷卡建立。

    Safeguards:
    - 不允許刪自己（會把系統變孤兒）
    - 動 admin/owner 必須 owner 自己（``_ensure_owner_for_elevated``）
    - 若 user 是某個 agent 的 owner，拒絕刪除（``agents.owner_user_id`` 是
      ``nullable=False`` 又沒設 ondelete，硬刪會 IntegrityError；改要 admin
      先轉移 agent 擁有權或刪該 agent）

    Manual cleanup（FK 未設 ondelete 的 4 個 table）：
    - ``api_keys`` + ``api_key_model_permissions``：一起刪（user 沒了 key 無意義）
    - ``audit_logs.actor_user_id``：SET NULL（保留歷史紀錄）
    - ``alerts.acknowledged_by_user_id``：SET NULL（保留歷史紀錄）

    其餘 FK 在 model schema 已設 CASCADE 或 SET NULL，由 DB 自動處理。
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="使用者不存在")

    if user.id == admin.id:
        raise HTTPException(
            status_code=400,
            detail="無法刪除自己；如需停用請改用其他帳號操作。",
        )

    _ensure_owner_for_elevated(user.role, admin)

    # Pre-flight：擁有 agent 的人不能直接硬刪
    owned_agents = (
        db.query(Agent.id, Agent.name)
        .filter(Agent.owner_user_id == user.id)
        .all()
    )
    if owned_agents:
        names = ", ".join(f"{a.name} (id={a.id})" for a in owned_agents)
        raise HTTPException(
            status_code=409,
            detail=(
                f"使用者擁有 {len(owned_agents)} 個 agent：{names}。"
                "請先轉移或刪除這些 agent 才能永久刪除使用者。"
            ),
        )

    # 在刪除前 snapshot 資料給 audit log（user row 一旦刪掉就拿不到 username 等）
    api_keys_count = (
        db.query(ApiKey).filter(ApiKey.user_id == user.id).count()
    )
    snapshot = {
        "username": user.username,
        "email": user.email,
        "role": user.role,
        "department_id": user.department_id,
        "api_keys": api_keys_count,
    }

    # Manual cleanup #1：api_keys + 其 model 權限
    user_key_ids = [
        k.id for k in db.query(ApiKey).filter(ApiKey.user_id == user.id).all()
    ]
    if user_key_ids:
        db.query(ApiKeyModelPermission).filter(
            ApiKeyModelPermission.api_key_id.in_(user_key_ids)
        ).delete(synchronize_session=False)
        db.query(ApiKey).filter(ApiKey.id.in_(user_key_ids)).delete(
            synchronize_session=False
        )

    # Manual cleanup #2：audit_logs.actor_user_id → NULL（保留歷史）
    db.query(AuditLog).filter(AuditLog.actor_user_id == user.id).update(
        {"actor_user_id": None}, synchronize_session=False
    )

    # Manual cleanup #3：alerts.acknowledged_by_user_id → NULL
    db.query(Alert).filter(Alert.acknowledged_by_user_id == user.id).update(
        {"acknowledged_by_user_id": None}, synchronize_session=False
    )

    # Audit log 寫在 user.delete 之前（同個 transaction 內 flush）— 確保即使
    # delete 失敗，刪除意圖也有紀錄；admin 才有資料可以 forensic。
    log_audit_event(
        db,
        actor=admin,
        action="hard_delete",
        resource_type="user",
        resource_id=user.id,
        detail=(
            f"永久刪除使用者「{snapshot['username']}」"
            f" (role={snapshot['role']}, email={snapshot['email']}, "
            f"dept_id={snapshot['department_id']}, "
            f"刪除 {snapshot['api_keys']} 把 API key)"
        ),
        commit=False,
    )

    db.delete(user)
    db.commit()

    return {
        "message": "使用者已永久刪除",
        "snapshot": snapshot,
    }
