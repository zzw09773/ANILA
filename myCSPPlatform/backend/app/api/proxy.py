"""OpenAI-compatible API proxy endpoints."""
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session
from app.database import get_db
from app.middleware.caller import Caller, get_caller
from app.models.agent import Agent, UserAgentPermission
from app.models.model_registry import ModelRegistry
from app.services.api_key_service import check_model_permission, check_agent_permission
from app.services.proxy_service import build_default_anila_meta, proxy_request, proxy_stream

router = APIRouter(tags=["API 代理"])


def _resolve_model(db: Session, caller: Caller, model_name: str) -> ModelRegistry:
    """Resolve model name to registry entry and check caller permissions."""
    model = db.query(ModelRegistry).filter(ModelRegistry.name == model_name).first()
    if not model:
        raise HTTPException(status_code=404, detail=f"模型 '{model_name}' 未註冊")
    if not model.is_active:
        raise HTTPException(status_code=400, detail=f"模型 '{model_name}' 已停用")
    if not check_model_permission(
        db, user=caller.user, api_key_id=caller.api_key_id, model_id=model.id
    ):
        raise HTTPException(
            status_code=403,
            detail=f"無權使用模型 '{model_name}'",
        )
    return model


def _resolve_agent(db: Session, caller: Caller, agent_name: str) -> Agent | None:
    """Return the Agent if agent_name matches an approved agent, else None."""
    agent = (
        db.query(Agent)
        .filter(Agent.name == agent_name, Agent.approval_status == "approved")
        .first()
    )
    if agent is None:
        return None
    if not check_agent_permission(
        db, user=caller.user, api_key_id=caller.api_key_id, agent_id=agent.id
    ):
        raise HTTPException(
            status_code=403,
            detail=f"無權呼叫 agent '{agent_name}'",
        )
    return agent


@router.get("/v1/agents")
def list_available_agents(
    caller: Caller = Depends(get_caller),
    db: Session = Depends(get_db),
):
    """Data-plane endpoint: return approved agents available to the caller.

    Used by RemoteAgentRegistry in the Router to discover agents.
    Response mirrors OpenAI /v1/models shape.
    """
    user = caller.user

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

    # Encryption is an agent-level policy only. Base models (LLMs) do NOT carry
    # a requires_encryption flag — classification is decided per-agent so the
    # same LLM can serve both classified and non-classified agents.
    data = [
        {
            "id": a.name,
            "object": "agent",
            "name": a.name,
            "description_for_router": a.description_for_router,
            "endpoint_url": a.endpoint_url,
            "capabilities": a.capabilities or {},
            "input_schema": a.input_schema,
            "requires_encryption": bool(getattr(a, "requires_encryption", False)),
        }
        for a in agents
    ]
    return JSONResponse({"object": "list", "data": data})


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request,
    caller: Caller = Depends(get_caller),
    db: Session = Depends(get_db),
):
    body = await request.json()
    model_name = body.get("model")
    if not model_name:
        raise HTTPException(status_code=400, detail="缺少 model 參數")

    stream: bool = body.get("stream", False)
    user = caller.user
    department_id = user.department_id
    user_email = user.email

    # Audit fields from optional client headers
    conversation_id: str | None = request.headers.get("X-ANILA-Conversation-Id")
    trace_id: str | None = request.headers.get("X-ANILA-Trace-Id")

    # Try agent first, fallback to model_registry
    agent = _resolve_agent(db, caller, model_name)
    if agent:
        agent_requires_encryption = bool(getattr(agent, "requires_encryption", False))
        if stream:
            return StreamingResponse(
                proxy_stream(
                    target_url=f"{agent.endpoint_url.rstrip('/')}/v1/chat/completions",
                    api_key_id=caller.api_key_id,
                    user_id=user.id,
                    department_id=department_id,
                    usage_model_id=agent.id,
                    request_body=body,
                    user_email=user_email,
                    inject_identity=True,
                    model_name=agent.name,
                    conversation_id=conversation_id,
                    trace_id=trace_id,
                    requires_encryption=agent_requires_encryption,
                ),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        # Non-streaming agent call — use proxy_request with a synthetic ModelRegistry-like obj
        # by forwarding to the agent endpoint directly
        import httpx
        from fastapi import HTTPException as _HTTPException
        target = f"{agent.endpoint_url.rstrip('/')}/v1/chat/completions"
        from app.services.proxy_service import _build_downstream_headers, _aggregate_sse_to_chat_completion
        headers = _build_downstream_headers(user.id, user_email)
        started_at = time.time()
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(target, json=body, headers=headers)
                resp.raise_for_status()
                # SSE-only agents (e.g. asrd) ignore ``stream: false`` and
                # respond with event-stream regardless. Aggregate in that
                # case so the caller still gets JSON.
                ct = resp.headers.get("content-type", "")
                preview = resp.text[:8].lstrip()
                if "text/event-stream" in ct or preview.startswith("data:"):
                    payload = _aggregate_sse_to_chat_completion(resp.text, agent.name)
                else:
                    payload = resp.json()
                existing_meta = payload.get("anila_meta")
                if not existing_meta:
                    payload["anila_meta"] = build_default_anila_meta(
                        agent.name,
                        detail=f"CSP proxy -> {target}",
                        latency_ms=int((time.time() - started_at) * 1000),
                        classified=agent_requires_encryption,
                    )
                elif agent_requires_encryption and isinstance(existing_meta, dict):
                    existing_meta["classified"] = True
                return payload
        except httpx.HTTPStatusError as e:
            raise _HTTPException(status_code=e.response.status_code, detail=str(e))
        except Exception as e:
            raise _HTTPException(status_code=502, detail=f"Agent 呼叫失敗: {e}")

    model = _resolve_model(db, caller, model_name)
    # Direct LLM calls (not through an agent) do NOT trigger CSP-side classified
    # latch. Encryption is agent-level policy; the same LLM can back both
    # classified and non-classified agents. Downstream-reported classified=True
    # still latches via proxy_service's normal meta merge.
    if stream:
        target_url = (
            f"{model.endpoint_url.rstrip('/')}/v2/chat/completions"
            if model.api_version == "v2"
            else f"{model.endpoint_url.rstrip('/')}/v1/chat/completions"
        )
        return StreamingResponse(
            proxy_stream(
                target_url=target_url,
                api_key_id=caller.api_key_id,
                user_id=user.id,
                department_id=department_id,
                usage_model_id=model.id,
                request_body=body,
                user_email=user_email,
                inject_identity=False,
                model_name=model.name,
                conversation_id=conversation_id,
                trace_id=trace_id,
                requires_encryption=False,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return await proxy_request(
        model=model,
        api_key_id=caller.api_key_id,
        user_id=user.id,
        department_id=department_id,
        request_body=body,
        endpoint_path="/v1/chat/completions",
        conversation_id=conversation_id,
        trace_id=trace_id,
        requires_encryption=False,
    )


@router.post("/v1/embeddings")
async def embeddings_v1(
    request: Request,
    caller: Caller = Depends(get_caller),
    db: Session = Depends(get_db),
):
    body = await request.json()
    model_name = body.get("model")
    if not model_name:
        raise HTTPException(status_code=400, detail="缺少 model 參數")

    model = _resolve_model(db, caller, model_name)
    return await proxy_request(
        model=model,
        api_key_id=caller.api_key_id,
        user_id=caller.user.id,
        department_id=caller.user.department_id,
        request_body=body,
        endpoint_path="/v1/embeddings",
    )


@router.post("/v2/embeddings")
async def embeddings_v2(
    request: Request,
    caller: Caller = Depends(get_caller),
    db: Session = Depends(get_db),
):
    body = await request.json()
    model_name = body.get("model")
    if not model_name:
        raise HTTPException(status_code=400, detail="缺少 model 參數")

    model = _resolve_model(db, caller, model_name)
    return await proxy_request(
        model=model,
        api_key_id=caller.api_key_id,
        user_id=caller.user.id,
        department_id=caller.user.department_id,
        request_body=body,
        endpoint_path="/v2/embeddings",
    )
