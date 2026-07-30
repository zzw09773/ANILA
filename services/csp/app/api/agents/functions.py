"""Per-agent functions (prompts) CRUD + system-prompt suggest.

Split verbatim from the former single-module ``app/api/agents.py``
(behavior-preserving refactor).
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.agent import Agent
from app.models.agent_prompt import AgentFunction
from app.models.user import User
from app.services.audit_service import log_audit_event
from app.services.auth_service import get_current_user, is_admin_tier

from app.api.agents._common import _client_ip, _require_developer_or_admin

router = APIRouter()


# ============================================================================
# Per-agent functions (2026-06-11, extensible)
# ----------------------------------------------------------------------------
# Developers design functions per agent in the CSP console; the ANILA chat UI
# surfaces them for the active agent. A function = ``kind`` + ``config`` (JSON)
# so new kinds add a frontend renderer, not a schema/API change.
#
#   kind='preset_prompt'  config={"text": str, "autosend": bool}
#   kind='prompt_action'  config={"template": str}
#
# Write gated to the agent's owner (developer) or admin tier; read open to any
# authenticated user (the chat UI needs them; they aren't secret). Agent ref
# accepts integer id (CSP console) or name (ANILA data plane uses the name).
# config is plain declarative data — never executed server-side.
# ============================================================================

# Known kinds and the config keys each expects. Adding a kind here + a
# renderer in the ANILA UI is the whole extension surface.
_FUNCTION_KINDS = {"preset_prompt", "prompt_action"}


class AgentFunctionCreate(BaseModel):
    kind: str = Field(default="preset_prompt", max_length=40)
    label: str = Field(min_length=1, max_length=120)
    config: dict = Field(default_factory=dict)
    sort_order: int = 0


class AgentFunctionUpdate(BaseModel):
    kind: str | None = Field(default=None, max_length=40)
    label: str | None = Field(default=None, min_length=1, max_length=120)
    config: dict | None = None
    sort_order: int | None = None


class AgentFunctionResponse(BaseModel):
    id: int
    agent_id: int
    kind: str
    label: str
    config: dict
    sort_order: int


def _serialize_function(f: AgentFunction) -> dict:
    return {
        "id": f.id,
        "agent_id": f.agent_id,
        "kind": f.kind,
        "label": f.label,
        "config": f.config or {},
        "sort_order": f.sort_order,
    }


def _agent_or_404(agent_ref: str, db: Session) -> Agent:
    """Resolve an agent by integer id (CSP console) OR by name (the ANILA
    chat UI / data plane identifies agents by name, e.g. 'image-generator',
    not the DB integer id)."""
    q = db.query(Agent)
    if agent_ref.isdigit():
        agent = q.filter(Agent.id == int(agent_ref)).first()
    else:
        agent = q.filter(Agent.name == agent_ref).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    return agent


def _require_function_editor(agent: Agent, user: User) -> None:
    """Owner of the agent (developer) or admin tier may edit its functions.

    Non-editors collapse to the same 404 as a missing agent (models pattern).
    """
    if not is_admin_tier(user) and agent.owner_user_id != user.id:
        raise HTTPException(status_code=404, detail="Agent 不存在")


def _require_function_reader(agent: Agent, user: User, db: Session) -> None:
    """Read access to an agent's functions: admin, the owner, or a user
    permitted to USE the agent. Mirrors the dispatch-time permission
    (``check_agent_permission``) so the chat UI for a permitted user still
    works, while a caller with no relationship to the agent gets an
    indistinguishable 404 — closing both the over-exposure and the
    agent-name enumeration oracle (a name lookup no longer reveals existence).
    """
    if is_admin_tier(user) or agent.owner_user_id == user.id:
        return
    from app.services.api_key_service import check_agent_permission

    if not check_agent_permission(db, user=user, api_key_id=None, agent_id=agent.id):
        raise HTTPException(status_code=404, detail="Agent 不存在")


def _validate_kind(kind: str) -> None:
    if kind not in _FUNCTION_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"未知的 function kind '{kind}'（可用：{', '.join(sorted(_FUNCTION_KINDS))}）",
        )


@router.get("/{agent_ref}/functions", response_model=list[AgentFunctionResponse])
def list_agent_functions(
    agent_ref: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List an agent's functions (admin / owner / a user permitted to use the
    agent — the chat UI renders these for whoever can use the agent)."""
    agent = _agent_or_404(agent_ref, db)
    _require_function_reader(agent, current_user, db)
    rows = (
        db.query(AgentFunction)
        .filter(AgentFunction.agent_id == agent.id)
        .order_by(AgentFunction.sort_order, AgentFunction.id)
        .all()
    )
    return [_serialize_function(r) for r in rows]


@router.post("/{agent_ref}/functions", response_model=AgentFunctionResponse, status_code=201)
def create_agent_function(
    agent_ref: str,
    payload: AgentFunctionCreate,
    http_request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    agent = _agent_or_404(agent_ref, db)
    _require_function_editor(agent, current_user)
    _validate_kind(payload.kind)
    fn = AgentFunction(
        agent_id=agent.id,
        kind=payload.kind,
        label=payload.label,
        config=payload.config or {},
        sort_order=payload.sort_order,
        created_by_user_id=current_user.id,
    )
    db.add(fn)
    db.commit()
    db.refresh(fn)
    log_audit_event(
        db,
        actor=current_user,
        action="create_agent_function",
        resource_type="agent",
        resource_id=agent.id,
        detail=f"新增功能 [{payload.kind}] '{payload.label}'",
        ip_address=_client_ip(http_request),
        commit=True,
    )
    return _serialize_function(fn)


@router.put("/{agent_ref}/functions/{function_id}", response_model=AgentFunctionResponse)
def update_agent_function(
    agent_ref: str,
    function_id: int,
    payload: AgentFunctionUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    agent = _agent_or_404(agent_ref, db)
    _require_function_editor(agent, current_user)
    fn = (
        db.query(AgentFunction)
        .filter(AgentFunction.id == function_id, AgentFunction.agent_id == agent.id)
        .first()
    )
    if not fn:
        raise HTTPException(status_code=404, detail="功能不存在")
    if payload.kind is not None:
        _validate_kind(payload.kind)
        fn.kind = payload.kind
    if payload.label is not None:
        fn.label = payload.label
    if payload.config is not None:
        fn.config = payload.config
    if payload.sort_order is not None:
        fn.sort_order = payload.sort_order
    db.commit()
    db.refresh(fn)
    return _serialize_function(fn)


@router.delete("/{agent_ref}/functions/{function_id}", status_code=204)
def delete_agent_function(
    agent_ref: str,
    function_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    agent = _agent_or_404(agent_ref, db)
    _require_function_editor(agent, current_user)
    fn = (
        db.query(AgentFunction)
        .filter(AgentFunction.id == function_id, AgentFunction.agent_id == agent.id)
        .first()
    )
    if not fn:
        raise HTTPException(status_code=404, detail="功能不存在")
    db.delete(fn)
    db.commit()


# ── system prompt 產生器（開發者 guide 頁用）─────────────────────────────────
class SystemPromptSuggestRequest(BaseModel):
    collection_id: int = Field(..., ge=1)
    ideas: str = Field(..., min_length=1, max_length=4000)


class SystemPromptSuggestResponse(BaseModel):
    system_prompt: str


@router.post("/system-prompt/suggest", response_model=SystemPromptSuggestResponse)
async def suggest_system_prompt(
    payload: SystemPromptSuggestRequest,
    current_user: User = Depends(_require_developer_or_admin),
    db: Session = Depends(get_db),
):
    """dev：選 collection + 輸入構想 → 抽該 collection 文件 grounding → LLM 產生領域 system prompt。

    產出供 dev 貼進 anila-agent ``prompts/system.md``，或前端再存成該 agent 的 preset_prompt。
    """
    from app.services.prompt_gen_service import generate_system_prompt

    try:
        text = await generate_system_prompt(
            db, payload.collection_id, payload.ideas, current_user
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return SystemPromptSuggestResponse(system_prompt=text)
