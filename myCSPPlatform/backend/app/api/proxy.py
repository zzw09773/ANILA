"""OpenAI-compatible API proxy endpoints."""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.api_key import ApiKey
from app.models.model_registry import ModelRegistry
from app.middleware.api_key_auth import get_api_key
from app.services.api_key_service import check_model_permission
from app.services.proxy_service import proxy_request

router = APIRouter(tags=["API 代理"])


def _resolve_model(db: Session, api_key: ApiKey, model_name: str) -> ModelRegistry:
    """Resolve model name to registry entry and check permissions."""
    model = db.query(ModelRegistry).filter(ModelRegistry.name == model_name).first()
    if not model:
        raise HTTPException(status_code=404, detail=f"模型 '{model_name}' 未註冊")
    if not model.is_active:
        raise HTTPException(status_code=400, detail=f"模型 '{model_name}' 已停用")
    if not check_model_permission(db, api_key.id, model.id):
        raise HTTPException(
            status_code=403,
            detail=f"此 API Key 無權使用模型 '{model_name}'",
        )
    return model


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    api_key: ApiKey = Depends(get_api_key),
    db: Session = Depends(get_db),
):
    body = await request.json()
    model_name = body.get("model")
    if not model_name:
        raise HTTPException(status_code=400, detail="缺少 model 參數")

    model = _resolve_model(db, api_key, model_name)
    return await proxy_request(
        model=model,
        api_key_id=api_key.id,
        user_id=api_key.user_id,
        department_id=api_key.user.department_id if api_key.user else None,
        request_body=body,
        endpoint_path="/v1/chat/completions",
    )


@router.post("/v1/embeddings")
async def embeddings_v1(
    request: Request,
    api_key: ApiKey = Depends(get_api_key),
    db: Session = Depends(get_db),
):
    body = await request.json()
    model_name = body.get("model")
    if not model_name:
        raise HTTPException(status_code=400, detail="缺少 model 參數")

    model = _resolve_model(db, api_key, model_name)
    return await proxy_request(
        model=model,
        api_key_id=api_key.id,
        user_id=api_key.user_id,
        department_id=api_key.user.department_id if api_key.user else None,
        request_body=body,
        endpoint_path="/v1/embeddings",
    )


@router.post("/v2/embeddings")
async def embeddings_v2(
    request: Request,
    api_key: ApiKey = Depends(get_api_key),
    db: Session = Depends(get_db),
):
    body = await request.json()
    model_name = body.get("model")
    if not model_name:
        raise HTTPException(status_code=400, detail="缺少 model 參數")

    model = _resolve_model(db, api_key, model_name)
    return await proxy_request(
        model=model,
        api_key_id=api_key.id,
        user_id=api_key.user_id,
        department_id=api_key.user.department_id if api_key.user else None,
        request_body=body,
        endpoint_path="/v2/embeddings",
    )
