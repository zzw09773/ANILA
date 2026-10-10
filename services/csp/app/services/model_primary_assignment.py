"""指定主路由／主簡報模型。回傳 ORM 列；HTTP 回應由 API 層組。"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.model_registry import ModelRegistry
from app.models.user import User
from app.services.audit_service import log_audit_event


def assign_router_primary(
    db: Session,
    model_id: int,
    admin: User,
    *,
    commit: bool = True,
) -> ModelRegistry:
    """Mark a model as ANILA Router's primary LLM (clearing any previous one)."""
    model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")
    from app.services.router_model_policy import (
        RouterModelPolicyError,
        require_campus_default_eligible,
    )
    try:
        require_campus_default_eligible(db, model, actor=admin)
    except RouterModelPolicyError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    # Clear previous primary only after eligibility is proven, same transaction.
    (
        db.query(ModelRegistry)
        .filter(ModelRegistry.is_router_primary.is_(True), ModelRegistry.id != model_id)
        .update({"is_router_primary": False}, synchronize_session=False)
    )
    model.is_router_primary = True
    if commit:
        db.commit()
        db.refresh(model)
    else:
        db.flush()
    log_audit_event(
        db,
        actor=admin,
        action="set_router_primary",
        resource_type="model",
        resource_id=model.id,
        detail=f"設為 ANILA 主路由模型: {model.display_name}",
        commit=commit,
    )
    return model


def assign_slides_primary(
    db: Session,
    model_id: int,
    admin: User,
    *,
    commit: bool = True,
) -> ModelRegistry:
    """Mark an LLM as the slide-deck (Studio) model, clearing any previous one."""
    model = db.query(ModelRegistry).filter(ModelRegistry.id == model_id).first()
    if not model:
        raise HTTPException(status_code=404, detail="模型不存在")
    if model.model_type != "llm":
        raise HTTPException(status_code=400, detail="僅 llm 類型可設為主簡報模型")
    if not model.is_active:
        raise HTTPException(status_code=400, detail="已停用的模型不能設為主簡報模型")
    (
        db.query(ModelRegistry)
        .filter(ModelRegistry.is_slides_primary.is_(True), ModelRegistry.id != model_id)
        .update({"is_slides_primary": False}, synchronize_session=False)
    )
    model.is_slides_primary = True
    if commit:
        db.commit()
        db.refresh(model)
    else:
        db.flush()
    log_audit_event(
        db,
        actor=admin,
        action="set_slides_primary",
        resource_type="model",
        resource_id=model.id,
        detail=f"設為主簡報模型: {model.display_name}",
        commit=commit,
    )
    return model
