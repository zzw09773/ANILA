"""OpenAI-compatible API proxy endpoints."""
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.agent import Agent, UserAgentPermission
from app.models.api_key import ApiKey
from app.models.model_registry import ModelRegistry
from app.middleware.api_key_auth import get_api_key
from app.services.api_key_service import check_model_permission, check_agent_permission
from app.services.proxy_service import build_default_anila_meta, proxy_request, proxy_stream

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


def _resolve_agent(db: Session, api_key: ApiKey, agent_name: str) -> Agent | None:
    """Return the Agent if agent_name matches an approved agent, else None."""
    agent = (
        db.query(Agent)
        .filter(Agent.name == agent_name, Agent.approval_status == "approved")
        .first()
    )
    if agent is None:
        return None
    if not check_agent_permission(db, api_key.id, agent.id):
        raise HTTPException(
            status_code=403,
            detail=f"此 API Key 無權呼叫 agent '{agent_name}'",
        )
    return agent


@router.get("/v1/agents")
def list_available_agents(
    api_key: ApiKey = Depends(get_api_key),
    db: Session = Depends(get_db),
):
    """Data-plane endpoint: return approved agents available to this API Key's owner.

    Used by RemoteAgentRegistry in the Router to discover agents.
    Response mirrors OpenAI /v1/models shape.
    """
    user = api_key.user
    if not user:
        return JSONResponse({"object": "list", "data": []})

    if user.role == "admin":
        agents = db.query(Agent).filter(Agent.approval_status == "approved").all()
    else:
        agents = (
            db.query(Agent)
            .join(UserAgentPermission, UserAgentPermission.agent_id == Agent.id)
            .filter(
                UserAgentPermission.user_id == user.id,
                Agent.approval_status == "approved",
            )
            .all()
        )

    data = [
        {
            "id": a.name,
            "object": "agent",
            "name": a.name,
            "description_for_router": a.description_for_router,
            "endpoint_url": a.endpoint_url,
            "capabilities": a.capabilities or {},
            "input_schema": a.input_schema,
        }
        for a in agents
    ]
    return JSONResponse({"object": "list", "data": data})


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

    stream: bool = body.get("stream", False)
    user = api_key.user
    department_id = user.department_id if user else None
    user_email = user.email if user else None

    # Audit fields from optional client headers
    conversation_id: str | None = request.headers.get("X-ANILA-Conversation-Id")
    trace_id: str | None = request.headers.get("X-ANILA-Trace-Id")

    # Try agent first, fallback to model_registry
    agent = _resolve_agent(db, api_key, model_name)
    if agent:
        if stream:
            return StreamingResponse(
                proxy_stream(
                    target_url=f"{agent.endpoint_url.rstrip('/')}/v1/chat/completions",
                    api_key_id=api_key.id,
                    user_id=api_key.user_id,
                    department_id=department_id,
                    usage_model_id=agent.id,
                    request_body=body,
                    user_email=user_email,
                    inject_identity=True,
                    model_name=agent.name,
                    conversation_id=conversation_id,
                    trace_id=trace_id,
                ),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        # Non-streaming agent call — use proxy_request with a synthetic ModelRegistry-like obj
        # by forwarding to the agent endpoint directly
        import httpx
        from fastapi import HTTPException as _HTTPException
        target = f"{agent.endpoint_url.rstrip('/')}/v1/chat/completions"
        from app.services.proxy_service import _build_downstream_headers
        headers = _build_downstream_headers(api_key.user_id, user_email)
        started_at = time.time()
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(target, json=body, headers=headers)
                resp.raise_for_status()
                payload = resp.json()
                payload.setdefault(
                    "anila_meta",
                    build_default_anila_meta(
                        agent.name,
                        detail=f"CSP proxy -> {target}",
                        latency_ms=int((time.time() - started_at) * 1000),
                    ),
                )
                return payload
        except httpx.HTTPStatusError as e:
            raise _HTTPException(status_code=e.response.status_code, detail=str(e))
        except Exception as e:
            raise _HTTPException(status_code=502, detail=f"Agent 呼叫失敗: {e}")

    model = _resolve_model(db, api_key, model_name)
    if stream:
        target_url = (
            f"{model.endpoint_url.rstrip('/')}/v2/chat/completions"
            if model.api_version == "v2"
            else f"{model.endpoint_url.rstrip('/')}/v1/chat/completions"
        )
        return StreamingResponse(
            proxy_stream(
                target_url=target_url,
                api_key_id=api_key.id,
                user_id=api_key.user_id,
                department_id=department_id,
                usage_model_id=model.id,
                request_body=body,
                user_email=user_email,
                inject_identity=False,
                model_name=model.name,
                conversation_id=conversation_id,
                trace_id=trace_id,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return await proxy_request(
        model=model,
        api_key_id=api_key.id,
        user_id=api_key.user_id,
        department_id=department_id,
        request_body=body,
        endpoint_path="/v1/chat/completions",
        conversation_id=conversation_id,
        trace_id=trace_id,
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
