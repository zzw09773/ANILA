"""治理中心的模型角色：一份清單、一次指定、執行期解析。

管理員用同一組端點讀寫六個角色。Studio、ingestion-worker、Router
用服務憑證或既有的 API key 在執行期解析，不讀環境變數裡的模型名稱。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api._service_principal import require_admitted_service_principal
from app.api.models import (
    set_platform_embedding,
    set_router_primary,
    set_slides_primary,
    unset_platform_embedding,
    unset_router_primary,
    unset_slides_primary,
)
from app.database import get_db
from app.middleware.caller import get_caller
from app.models.model_registry import ModelRegistry
from app.models.router_model_grant import RouterModelGrant
from app.models.user import User
from app.services.audit_service import log_audit_event_or_raise
from app.services.auth_service import (
    get_current_user,
    require_admin,
    security,
    verify_service_token,
)
from app.services.endpoint_author_service import visible_endpoint_url
from app.services.model_roles import (
    ResolvedRole,
    assign_table_role,
    clear_table_role,
    get_spec,
    has_active_all_users_grant,
    inactive_assign_error,
    list_roles,
    resolve_role,
    type_error,
)

router = APIRouter(prefix="/api/models/roles", tags=["模型角色"])


class RoleAssignRequest(BaseModel):
    model_id: int = Field(..., ge=1)


def _model_brief(model: ModelRegistry | None) -> dict | None:
    if model is None:
        return None
    return {
        "id": model.id,
        "name": model.name,
        "display_name": model.display_name,
        "model_type": model.model_type,
        "is_active": bool(model.is_active),
    }


def _role_body(resolved: ResolvedRole, db: Session) -> dict:
    spec = resolved.spec
    if spec.end_user_credential:
        granted: bool | None = (
            has_active_all_users_grant(db, resolved.model.id)
            if resolved.model is not None
            else False
        )
    else:
        granted = None
    return {
        "role": spec.role,
        "label": spec.label,
        "description": spec.description,
        "accepted_types": sorted(spec.accepted_types),
        "status": resolved.status,
        "message": resolved.message,
        "model": _model_brief(resolved.model),
        "end_user_credential": spec.end_user_credential,
        "all_users_grant": granted,
    }


def _public_model(resolved: ResolvedRole, *, db: Session, caller: User | None, is_svc: bool) -> dict:
    model = resolved.model
    assert model is not None
    return {
        "id": model.id,
        "name": model.name,
        "display_name": model.display_name,
        "model_type": model.model_type,
        "endpoint_url": visible_endpoint_url(
            model.endpoint_url,
            is_internal=bool(getattr(model, "is_internal", False)),
            db=db,
            caller=caller,
            is_service_token=is_svc,
        ),
        "api_version": model.api_version,
        "health_status": model.health_status,
    }


def _admit_reader(
    request: Request,
    db: Session,
    *,
    x_csp_service_token: str | None,
    credentials: HTTPAuthorizationCredentials | None,
) -> tuple[bool, User | None]:
    """服務憑證、登入使用者、或既有的 sk- API key 都可以讀。"""
    if x_csp_service_token:
        identity = verify_service_token(request, db, x_csp_service_token)
        require_admitted_service_principal(
            identity,
            db=db,
            allowed_kinds=("service_client",),
            allowed_client_types=None,
            allow_legacy_env=True,
            endpoint="GET /api/models/roles/{role}",
        )
        return True, None
    token = credentials.credentials if credentials is not None else None
    if token and token.startswith("sk-"):
        return False, get_caller(request, db).user
    return False, get_current_user(request, credentials, db)


@router.get("")
def get_roles(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """治理中心模型角色面板。六個角色都在，沒設的也列出來。"""
    del admin
    return {"roles": [_role_body(item, db) for item in list_roles(db)]}


@router.get("/{role}")
def get_role(
    role: str,
    request: Request,
    db: Session = Depends(get_db),
    x_csp_service_token: str | None = Header(default=None, alias="X-CSP-Service-Token"),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """執行期解析。未設 404、已停用 409，detail 就是給使用者看的那句話。"""
    spec = get_spec(role)
    if spec is None:
        raise HTTPException(status_code=404, detail="未知的模型角色")
    is_svc, caller = _admit_reader(
        request,
        db,
        x_csp_service_token=x_csp_service_token,
        credentials=credentials,
    )
    resolved = resolve_role(db, role)
    if resolved.status == "unset":
        raise HTTPException(status_code=404, detail=resolved.message)
    if resolved.status != "ok":
        raise HTTPException(status_code=409, detail=resolved.message)
    return _public_model(resolved, db=db, caller=caller, is_svc=is_svc)


def _load_assign_target(db: Session, spec, model_id: int) -> ModelRegistry:
    model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).first()
    if model is None:
        raise HTTPException(status_code=404, detail="模型不存在")
    if not model.is_active:
        raise HTTPException(status_code=400, detail=inactive_assign_error(spec))
    if (model.model_type or "") not in spec.accepted_types:
        raise HTTPException(status_code=400, detail=type_error(spec))
    return model


@router.put("/{role}")
async def put_role(
    role: str,
    body: RoleAssignRequest,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """指定角色的模型。旗標角色沿用原端點的檢查（含嵌入探測與主路由資格）。"""
    spec = get_spec(role)
    if spec is None:
        raise HTTPException(status_code=404, detail="未知的模型角色")
    extra: dict = {}
    if spec.storage == "flag":
        if role == "router_primary":
            set_router_primary(body.model_id, admin, db)
        elif role == "slides":
            set_slides_primary(body.model_id, admin, db)
        elif role == "platform_embedding":
            embedded = await set_platform_embedding(body.model_id, admin, db)
            extra["truncation_warning"] = embedded.get("truncation_warning")
            extra["index_mismatch_warning"] = embedded.get("index_mismatch_warning")
            extra["measured_native_dim"] = embedded.get("measured_native_dim")
        else:
            raise HTTPException(status_code=404, detail="未知的模型角色")
    else:
        model = _load_assign_target(db, spec, body.model_id)
        assign_table_role(db, spec, model)
        log_audit_event_or_raise(
            db,
            actor=admin,
            action="set_model_role",
            resource_type="model_role",
            resource_id=role,
            detail=f"將{spec.label}設為 {model.display_name}",
            metadata={"role": role, "model_id": model.id, "model_name": model.name},
            commit=False,
        )
        db.commit()
    resolved = resolve_role(db, role)
    return {**_role_body(resolved, db), **extra}


@router.delete("/{role}")
def delete_role(
    role: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """取消角色。冪等。"""
    spec = get_spec(role)
    if spec is None:
        raise HTTPException(status_code=404, detail="未知的模型角色")
    if spec.storage == "flag":
        current = resolve_role(db, role)
        if current.model is not None:
            if role == "router_primary":
                unset_router_primary(current.model.id, admin, db)
            elif role == "slides":
                unset_slides_primary(current.model.id, admin, db)
            elif role == "platform_embedding":
                unset_platform_embedding(current.model.id, admin, db)
    else:
        previous = clear_table_role(db, role)
        if previous is not None:
            log_audit_event_or_raise(
                db,
                actor=admin,
                action="clear_model_role",
                resource_type="model_role",
                resource_id=role,
                detail=f"取消{spec.label}: {previous.display_name}",
                metadata={"role": role, "model_id": previous.id},
                commit=False,
            )
        db.commit()
    return _role_body(resolve_role(db, role), db)


@router.post("/{role}/grant-all-users")
def grant_role_to_all_users(
    role: str,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """把這個角色目前的模型授權給所有使用者。不改其他模型，也不開放 Router。"""
    spec = get_spec(role)
    if spec is None:
        raise HTTPException(status_code=404, detail="未知的模型角色")
    if not spec.end_user_credential:
        raise HTTPException(
            status_code=400,
            detail="這個角色不是由使用者自己的憑證呼叫，不能從這裡授權",
        )
    resolved = resolve_role(db, role)
    if resolved.model is None:
        raise HTTPException(status_code=409, detail=resolved.message or spec.unset_message)
    model = resolved.model
    grant = (
        db.query(RouterModelGrant)
        .filter(
            RouterModelGrant.model_id == model.id,
            RouterModelGrant.scope_type == "all",
        )
        .first()
    )
    changed = False
    if grant is None:
        db.add(
            RouterModelGrant(
                model_id=model.id,
                scope_type="all",
                created_by=admin.id,
            )
        )
        changed = True
    elif not has_active_all_users_grant(db, model.id):
        grant.expires_at = None
        changed = True
    if changed:
        log_audit_event_or_raise(
            db,
            actor=admin,
            action="grant_model_role_all_users",
            resource_type="model_role",
            resource_id=role,
            detail=f"將{spec.label}「{model.name}」授權給所有使用者",
            metadata={"role": role, "model_id": model.id, "model_name": model.name},
            commit=False,
        )
        db.commit()
    return _role_body(resolve_role(db, role), db)
