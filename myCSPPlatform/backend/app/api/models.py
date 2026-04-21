import httpx
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.agent import Agent
from app.models.model_registry import ModelRegistry
from app.models.token_usage import TokenUsage
from app.models.user import User
from app.schemas.model_registry import ModelCreate, ModelUpdate, ModelResponse
from app.services.audit_service import log_audit_event
from app.services.auth_service import get_current_user, require_admin

router = APIRouter(prefix="/api/models", tags=["模型管理"])


def _build_response(model: ModelRegistry) -> dict:
    data = {
        "id": model.id,
        "name": model.name,
        "display_name": model.display_name,
        "model_type": model.model_type,
        "endpoint_url": model.endpoint_url,
        "api_version": model.api_version,
        "is_active": model.is_active,
        "health_status": model.health_status,
        "health_checked_at": model.health_checked_at,
        "description": model.description,
        "context_window": model.context_window,
        "base_model_id": model.base_model_id,
        "base_model_name": model.base_model.display_name if model.base_model else None,
        "created_at": model.created_at,
        "updated_at": model.updated_at,
    }
    return data


@router.get("", response_model=list[ModelResponse])
def list_models(
    model_type: str | None = Query(None, description="篩選模型類型: llm/vlm/embedding/agent"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = db.query(ModelRegistry).order_by(ModelRegistry.model_type, ModelRegistry.name)
    if model_type:
        query = query.filter(ModelRegistry.model_type == model_type)
    # Non-admin users only see models they are authorized for
    if current_user.role != "admin":
        allowed_ids = [m.id for m in current_user.allowed_models]
        if not allowed_ids:
            return []
        query = query.filter(ModelRegistry.id.in_(allowed_ids))
    return [_build_response(m) for m in query.all()]


@router.post("", response_model=ModelResponse)
def create_model(
    request: ModelCreate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    existing = db.query(ModelRegistry).filter(ModelRegistry.name == request.name).first()
    if existing:
        raise HTTPException(status_code=400, detail="模型名稱已存在")

    # Validate base_model_id if provided
    if request.base_model_id:
        base = db.query(ModelRegistry).filter(ModelRegistry.id == request.base_model_id).first()
        if not base:
            raise HTTPException(status_code=400, detail="底層模型不存在")

    model = ModelRegistry(**request.model_dump())
    db.add(model)
    db.commit()
    db.refresh(model)
    log_audit_event(
        db,
        actor=admin,
        action="create",
        resource_type="model",
        resource_id=model.id,
        detail=f"建立模型「{model.display_name}」",
        commit=True,
    )
    return _build_response(model)


@router.get("/{model_id}", response_model=ModelResponse)
def get_model(
    model_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")
    if current_user.role != "admin":
        allowed_ids = {m.id for m in current_user.allowed_models}
        if model.id not in allowed_ids:
            raise HTTPException(status_code=404, detail="模型不存在")
    return _build_response(model)


@router.put("/{model_id}", response_model=ModelResponse)
def update_model(
    model_id: int,
    request: ModelUpdate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")

    update_data = request.model_dump(exclude_unset=True)

    # Validate base_model_id if provided
    if "base_model_id" in update_data and update_data["base_model_id"]:
        base = db.query(ModelRegistry).filter(ModelRegistry.id == update_data["base_model_id"]).first()
        if not base:
            raise HTTPException(status_code=400, detail="底層模型不存在")
        if base.id == model_id:
            raise HTTPException(status_code=400, detail="不能將自己設為底層模型")

    for field, value in update_data.items():
        setattr(model, field, value)

    db.commit()
    db.refresh(model)
    log_audit_event(
        db,
        actor=admin,
        action="update",
        resource_type="model",
        resource_id=model.id,
        detail=f"更新模型「{model.display_name}」",
        commit=True,
    )
    return _build_response(model)


@router.delete("/{model_id}")
def deactivate_model(
    model_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")
    model.is_active = False
    db.commit()
    log_audit_event(
        db,
        actor=admin,
        action="deactivate",
        resource_type="model",
        resource_id=model.id,
        detail=f"停用模型「{model.display_name}」",
        commit=True,
    )
    return {"message": "模型已停用"}


@router.delete("/{model_id}/purge")
def purge_model(
    model_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Hard-delete a model. Blocked if token_usage or other models reference it."""
    model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")

    usage_count = db.query(TokenUsage).filter(TokenUsage.model_id == model_id).count()
    if usage_count > 0:
        raise HTTPException(
            status_code=409,
            detail=f"此模型尚有 {usage_count} 筆用量紀錄，無法硬刪除。請改以「停用」保留歷史",
        )

    dependent_count = (
        db.query(ModelRegistry)
        .filter(ModelRegistry.base_model_id == model_id)
        .count()
    )
    if dependent_count > 0:
        raise HTTPException(
            status_code=409,
            detail=f"尚有 {dependent_count} 個模型以此為底層模型，請先解除關聯",
        )

    display_name = model.display_name
    # agents.base_model_id will be set NULL via ondelete="SET NULL"
    # user_allowed_models / api_key_allowed_models cascade delete
    db.delete(model)
    db.commit()
    log_audit_event(
        db,
        actor=admin,
        action="delete",
        resource_type="model",
        resource_id=model_id,
        detail=f"刪除模型「{display_name}」",
        commit=True,
    )
    return {"message": f"已刪除模型「{display_name}」"}


@router.post("/{model_id}/health-check")
async def trigger_health_check(
    model_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            # Try common health endpoints
            for path in ["/health", "/v1/models", "/"]:
                try:
                    resp = await client.get(f"{model.endpoint_url.rstrip('/')}{path}")
                    if resp.status_code < 500:
                        model.health_status = "online"
                        model.health_checked_at = datetime.now(timezone.utc)
                        db.commit()
                        log_audit_event(
                            db,
                            actor=admin,
                            action="health_check",
                            resource_type="model",
                            resource_id=model.id,
                            detail=f"手動健康檢查成功: {model.display_name}",
                            commit=True,
                        )
                        return {"status": "online", "detail": f"端點 {path} 回應正常"}
                except httpx.ConnectError:
                    continue

            model.health_status = "offline"
            model.health_checked_at = datetime.now(timezone.utc)
            db.commit()
            log_audit_event(
                db,
                actor=admin,
                action="health_check",
                resource_type="model",
                resource_id=model.id,
                detail=f"手動健康檢查離線: {model.display_name}",
                commit=True,
            )
            return {"status": "offline", "detail": "無法連線到模型端點"}
    except Exception as e:
        model.health_status = "offline"
        model.health_checked_at = datetime.now(timezone.utc)
        db.commit()
        log_audit_event(
            db,
            actor=admin,
            action="health_check",
            resource_type="model",
            resource_id=model.id,
            status="failure",
            detail=f"手動健康檢查失敗: {model.display_name} ({e})",
            commit=True,
        )
        return {"status": "offline", "detail": str(e)}
