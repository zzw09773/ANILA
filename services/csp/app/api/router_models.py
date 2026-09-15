"""Router model picker, campus default, grants, and access groups."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.middleware.caller import Caller, get_caller
from app.models.department import Department
from app.models.model_access_group import ModelAccessGroup, ModelAccessGroupMember
from app.models.model_registry import ModelRegistry
from app.models.router_model_grant import RouterModelGrant
from app.models.user import User
from app.schemas.router_model import (
    CampusDefaultIn,
    ModelAccessGroupIn,
    ModelAccessGroupMembersIn,
    ModelAccessGroupOut,
    GroupLinkedModelOut,
    GroupMemberOut,
    UserRouterModelOut,
    RouterGrantIn,
    RouterGrantOut,
    RouterGrantsReplaceIn,
    RouterModelListOut,
    RouterModelOut,
)
from app.services.audit_service import log_audit_event
from app.services.auth_service import require_admin
from app.services.health_checker import normalize_health_status
from app.services.router_model_policy import (
    PLATFORM_ROUTER_NAME,
    ROUTER_ELIGIBLE_TYPES,
    RouterModelPolicyError,
    campus_default_model,
    grant_sources_for_user,
    list_router_models_for_user,
)

router = APIRouter(tags=["Router 模型"])


def _raise_policy(exc: RouterModelPolicyError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _eligible_llm(model: ModelRegistry) -> bool:
    return (
        model.is_active
        and (model.model_type or "") in ROUTER_ELIGIBLE_TYPES
        and model.name != PLATFORM_ROUTER_NAME
    )


@router.get("/api/router-models", response_model=RouterModelListOut)
def list_router_models(
    caller: Caller = Depends(get_caller),
    db: Session = Depends(get_db),
):
    models = list_router_models_for_user(db, caller.user)
    if caller.api_key_id is not None:
        from app.models.api_key import ApiKeyModelPermission
        allowed = {
            row.model_id
            for row in db.query(ApiKeyModelPermission)
            .filter(ApiKeyModelPermission.api_key_id == caller.api_key_id)
            .all()
        }
        models = [m for m in models if m.id in allowed]
    default = campus_default_model(db)
    default_id = default.id if default is not None else None
    if default_id is not None and default_id not in {m.id for m in models}:
        default_id = None
    return RouterModelListOut(
        models=[
            RouterModelOut(
                id=m.id,
                name=m.name,
                display_name=m.display_name,
                health_status=normalize_health_status(
                    m.health_status, is_active=bool(m.is_active)
                ),
                grant_sources=grant_sources_for_user(db, caller.user, m),
                thinking_effort=getattr(m, "thinking_effort", None),
                thinking_levels_supported=getattr(
                    m, "thinking_levels_supported", None
                ),
                thinking_user_selectable=bool(
                    getattr(m, "thinking_user_selectable", True)
                ),
            )
            for m in models
        ],
        default_model_id=default_id,
    )



class RouterModelResolveIn(BaseModel):
    router_model: str | None = None
    conversation_id: int | None = None


@router.post("/api/router-models/resolve")
def resolve_router_models(
    body: RouterModelResolveIn,
    caller: Caller = Depends(get_caller),
    db: Session = Depends(get_db),
):
    from app.models.conversation import Conversation
    from app.services.router_model_policy import RouterModelPolicyError, resolve_router_model

    conv_model_id = None
    conv_version = 0
    if body.conversation_id is not None:
        conv = db.get(Conversation, body.conversation_id)
        if conv is None or conv.user_id != caller.user.id:
            raise HTTPException(status_code=404, detail="對話不存在")
        conv_model_id = getattr(conv, "router_model_id", None)
        conv_version = int(getattr(conv, "router_selection_version", 0) or 0)
    try:
        selection = resolve_router_model(
            db,
            caller.user,
            requested_name=body.router_model,
            conversation_model_id=conv_model_id,
            selection_version=conv_version,
            api_key_id=caller.api_key_id,
        )
    except RouterModelPolicyError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return {
        "id": selection.model_id,
        "name": selection.model_name,
        "display_name": selection.display_name,
        "selection_version": selection.selection_version,
        "grant_sources": list(selection.grant_sources),
    }

@router.put("/api/router-models/default")
def set_campus_default(
    body: CampusDefaultIn,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    model = db.get(ModelRegistry, body.model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="模型不存在")
    from app.services.router_model_policy import RouterModelPolicyError, require_campus_default_eligible
    try:
        require_campus_default_eligible(db, model)
    except RouterModelPolicyError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    (
        db.query(ModelRegistry)
        .filter(ModelRegistry.is_router_primary.is_(True), ModelRegistry.id != model.id)
        .update({"is_router_primary": False}, synchronize_session=False)
    )
    model.is_router_primary = True
    db.commit()
    log_audit_event(
        db,
        actor=admin,
        action="set_router_campus_default",
        resource_type="model",
        resource_id=model.id,
        detail=f"設為全院 Router 預設: {model.display_name}",
        commit=True,
    )
    return {"id": model.id, "name": model.name, "is_router_primary": True}



def _grant_out(db: Session, row: RouterModelGrant) -> RouterGrantOut:
    department_name = None
    group_name = None
    username = None
    if row.department_id:
        dept = db.get(Department, row.department_id)
        department_name = dept.name if dept else None
    if row.group_id:
        group = db.get(ModelAccessGroup, row.group_id)
        group_name = group.name if group else None
    if row.user_id:
        user = db.get(User, row.user_id)
        username = user.username if user else None
    return RouterGrantOut(
        id=row.id,
        model_id=row.model_id,
        scope_type=row.scope_type,
        department_id=row.department_id,
        group_id=row.group_id,
        user_id=row.user_id,
        include_descendants=bool(row.include_descendants),
        expires_at=row.expires_at,
        department_name=department_name,
        group_name=group_name,
        username=username,
    )


@router.get("/api/models/{model_id}/router-grants", response_model=list[RouterGrantOut])
def get_router_grants(
    model_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    model = db.get(ModelRegistry, model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="模型不存在")
    rows = (
        db.query(RouterModelGrant)
        .filter(RouterModelGrant.model_id == model_id)
        .order_by(RouterModelGrant.id.asc())
        .all()
    )
    return [
        _grant_out(db, r)
        for r in rows
    ]


def _validate_grant(db: Session, grant: RouterGrantIn) -> None:
    if grant.scope_type == "all":
        if grant.department_id or grant.group_id or grant.user_id:
            raise HTTPException(status_code=400, detail="全院授權不可帶部門／群組／使用者")
        return
    if grant.scope_type == "department":
        if grant.department_id is None or grant.group_id or grant.user_id:
            raise HTTPException(status_code=400, detail="部門授權必須且只能指定部門")
        if db.get(Department, grant.department_id) is None:
            raise HTTPException(status_code=404, detail="部門不存在")
        return
    if grant.scope_type == "group":
        if grant.group_id is None or grant.department_id or grant.user_id:
            raise HTTPException(status_code=400, detail="群組授權必須且只能指定群組")
        if db.get(ModelAccessGroup, grant.group_id) is None:
            raise HTTPException(status_code=404, detail="群組不存在")
        return
    if grant.user_id is None or grant.department_id or grant.group_id:
        raise HTTPException(status_code=400, detail="個別授權必須且只能指定使用者")
    if db.get(User, grant.user_id) is None:
        raise HTTPException(status_code=404, detail="使用者不存在")


@router.put("/api/models/{model_id}/router-grants", response_model=list[RouterGrantOut])
def replace_router_grants(
    model_id: int,
    body: RouterGrantsReplaceIn,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    model = db.get(ModelRegistry, model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="模型不存在")
    if model.name == PLATFORM_ROUTER_NAME:
        raise HTTPException(status_code=400, detail="平台入口不是可授權的基礎模型")
    for grant in body.grants:
        _validate_grant(db, grant)
    db.query(RouterModelGrant).filter(RouterModelGrant.model_id == model_id).delete()
    created: list[RouterModelGrant] = []
    for grant in body.grants:
        row = RouterModelGrant(
            model_id=model_id,
            scope_type=grant.scope_type,
            department_id=grant.department_id,
            group_id=grant.group_id,
            user_id=grant.user_id,
            include_descendants=grant.include_descendants,
            expires_at=grant.expires_at,
            created_by=admin.id,
        )
        db.add(row)
        created.append(row)
    db.commit()
    for row in created:
        db.refresh(row)
    log_audit_event(
        db,
        actor=admin,
        action="replace_router_grants",
        resource_type="model",
        resource_id=model_id,
        detail=f"替換 Router 授權 {len(created)} 條: {model.display_name}",
        commit=True,
    )
    return [
        _grant_out(db, r)
        for r in created
    ]


@router.get("/api/model-access-groups", response_model=list[ModelAccessGroupOut])
def list_access_groups(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    rows = db.query(ModelAccessGroup).order_by(ModelAccessGroup.id.asc()).all()
    return [
        ModelAccessGroupOut(
            id=g.id,
            name=g.name,
            is_active=bool(g.is_active),
            member_count=len(g.members or []),
        )
        for g in rows
    ]


@router.post("/api/model-access-groups", response_model=ModelAccessGroupOut, status_code=201)
def create_access_group(
    body: ModelAccessGroupIn,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    group = ModelAccessGroup(name=body.name.strip(), is_active=body.is_active, created_by=admin.id)
    db.add(group)
    db.commit()
    db.refresh(group)
    log_audit_event(
        db,
        actor=admin,
        action="create_model_access_group",
        resource_type="model_access_group",
        resource_id=group.id,
        detail=f"建立模型授權群組: {group.name}",
        commit=True,
    )
    return ModelAccessGroupOut(id=group.id, name=group.name, is_active=bool(group.is_active), member_count=0)


@router.put("/api/model-access-groups/{group_id}", response_model=ModelAccessGroupOut)
def update_access_group(
    group_id: int,
    body: ModelAccessGroupIn,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    group = db.get(ModelAccessGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="群組不存在")
    group.name = body.name.strip()
    group.is_active = body.is_active
    db.commit()
    db.refresh(group)
    log_audit_event(
        db,
        actor=admin,
        action="update_model_access_group",
        resource_type="model_access_group",
        resource_id=group.id,
        detail=f"更新模型授權群組: {group.name}",
        commit=True,
    )
    return ModelAccessGroupOut(
        id=group.id,
        name=group.name,
        is_active=bool(group.is_active),
        member_count=len(group.members or []),
    )


@router.delete("/api/model-access-groups/{group_id}", status_code=204)
def delete_access_group(
    group_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    group = db.get(ModelAccessGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="群組不存在")
    db.delete(group)
    db.commit()
    log_audit_event(
        db,
        actor=admin,
        action="delete_model_access_group",
        resource_type="model_access_group",
        resource_id=group_id,
        detail=f"刪除模型授權群組: {group.name}",
        commit=True,
    )
    return None


@router.put("/api/model-access-groups/{group_id}/members")
def replace_access_group_members(
    group_id: int,
    body: ModelAccessGroupMembersIn,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    group = db.get(ModelAccessGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="群組不存在")
    unique_ids = list(dict.fromkeys(body.user_ids))
    for uid in unique_ids:
        if db.get(User, uid) is None:
            raise HTTPException(status_code=404, detail=f"使用者 {uid} 不存在")
    db.query(ModelAccessGroupMember).filter(ModelAccessGroupMember.group_id == group_id).delete()
    for uid in unique_ids:
        db.add(ModelAccessGroupMember(group_id=group_id, user_id=uid))
    db.commit()
    log_audit_event(
        db,
        actor=admin,
        action="replace_model_access_group_members",
        resource_type="model_access_group",
        resource_id=group_id,
        detail=f"更新群組成員 {len(unique_ids)} 人: {group.name}",
        commit=True,
    )
    return {"group_id": group_id, "user_ids": unique_ids}


@router.get("/api/model-access-groups/{group_id}/members", response_model=list[GroupMemberOut])
def list_access_group_members(
    group_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    group = db.get(ModelAccessGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="群組不存在")
    rows = (
        db.query(User.id, User.username, Department.name)
        .join(ModelAccessGroupMember, ModelAccessGroupMember.user_id == User.id)
        .outerjoin(Department, User.department_id == Department.id)
        .filter(ModelAccessGroupMember.group_id == group_id)
        .order_by(User.username.asc())
        .all()
    )
    return [GroupMemberOut(id=row[0], username=row[1], department=row[2]) for row in rows]


@router.get("/api/model-access-groups/{group_id}/models", response_model=list[GroupLinkedModelOut])
def list_access_group_models(
    group_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    group = db.get(ModelAccessGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="群組不存在")
    rows = (
        db.query(ModelRegistry)
        .join(RouterModelGrant, RouterModelGrant.model_id == ModelRegistry.id)
        .filter(RouterModelGrant.group_id == group_id, RouterModelGrant.scope_type == "group")
        .order_by(ModelRegistry.display_name.asc())
        .all()
    )
    return [
        GroupLinkedModelOut(id=m.id, name=m.name, display_name=m.display_name)
        for m in rows
    ]


@router.get("/api/users/{user_id}/router-models", response_model=list[UserRouterModelOut])
def list_user_router_models(
    user_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="使用者不存在")
    models = list_router_models_for_user(db, user)
    return [
        UserRouterModelOut(
            id=m.id,
            name=m.name,
            display_name=m.display_name,
            grant_sources=grant_sources_for_user(db, user, m),
        )
        for m in models
    ]

