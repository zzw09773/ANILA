"""Shared helpers for the ``app.api.agents`` package.

Split verbatim from the former single-module ``app/api/agents.py``
(behavior-preserving refactor). Only helpers used by 2+ submodules
live here.
"""
from fastapi import Depends, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.models.agent import Agent
from app.models.user import User
from app.schemas.contracts.agents import AgentManifest
from app.schemas.contracts.classification import ClassificationLevel
from app.services.auth_service import get_current_user, is_admin_tier


def validate_agent_manifest(payload: dict) -> dict:
    """Validate a submitted Agent manifest against the doc 05 §4 contract.

    Fail-closed: unknown fields / wrong types / invalid enum values (e.g. a
    non-四級 classification) raise ``422`` with a zh-TW detail. Returns the
    normalized manifest dict (JSON-mode) suitable for ``Agent.manifest_json``.
    """
    try:
        manifest = AgentManifest.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", ())) or "(root)"
        msg = first.get("msg", "格式不符")
        raise HTTPException(
            status_code=422,
            detail=f"Agent manifest 驗證失敗:欄位「{loc}」{msg}",
        ) from exc
    return manifest.model_dump(mode="json")


def _require_developer_or_admin(current_user: User = Depends(get_current_user)) -> User:
    # Sprint owner-tier: owner 是 admin 之上的最高權限,凡 admin 能做的事 owner 也能做。
    # 漏掉 owner 會讓系統最高權限者反而拿不到開發者頁面 (download template、register
    # agent 等),這是 RBAC 新增層級時典型的回歸坑。和 auth_service._ADMIN_TIER_ROLES
    # 保持一致。
    if current_user.role not in ("admin", "developer", "owner"):
        raise HTTPException(status_code=403, detail="需要開發者或管理員權限")
    return current_user


def _client_ip(request: Request | None) -> str | None:
    """Extract caller IP from the Request — tolerant of reverse-proxy
    setups (reads X-Forwarded-For first hop) and of None so endpoints
    that don't inject a Request stay safe."""
    if request is None:
        return None
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None


def _resolve_agent(db: Session, agent_id: int) -> Agent:
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    return agent


def _require_agent_editor(agent: Agent, current_user: User) -> None:
    """Same gate as ``PUT /api/agents/{id}``: admin-tier or owner."""
    if not is_admin_tier(current_user) and agent.owner_user_id != current_user.id:
        raise HTTPException(status_code=403, detail="無權限編輯此 Agent")


def parse_stored_classification_level(raw: str | None) -> ClassificationLevel:
    """Parse a stored level; illegal DB values refuse with 422 (not 500)."""
    try:
        return ClassificationLevel.from_storage(raw or "無機密")
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                "預設分類等級無效，請改設為合法四級之一："
                "無機密、營業秘密、密、機密"
            ),
        ) from exc


def requires_controlled_access(level: ClassificationLevel) -> bool:
    """Derive the legacy ``requires_encryption`` boolean from a level.

    Uses the conversation mirror threshold (``level >= 密`` / RESTRICTED;
    SYSTEM-MAP §8 / ``_mirror_legacy_boolean``) so boolean readers stay
    aligned with a single source of truth — the default classification level.
    """
    return level >= ClassificationLevel.RESTRICTED


def effective_agent_policy_level(agent: Agent) -> ClassificationLevel:
    """Effective policy level — same rule as ``proxy._agent_policy_level``.

    Stored ``default_classification_level``, floored at RESTRICTED(密) when
    the legacy ``requires_encryption`` flag is set. Writers that compare
    "current vs requested" must use this, not the raw stored column alone.
    """
    level = parse_stored_classification_level(
        getattr(agent, "default_classification_level", None)
    )
    if bool(getattr(agent, "requires_encryption", False)):
        level = ClassificationLevel.max_of(
            [level, ClassificationLevel.RESTRICTED]
        )
    return level


def refuse_classification_downgrade(
    agent: Agent, new_level: ClassificationLevel, current_user: User
) -> None:
    """Block non-admin writes that would lower the effective policy level.

    Developers may raise (or re-save at the same effective level); only
    administrator-tier callers may lower. Matches the one-way classification
    convention elsewhere — sanctioned lowering is supervisor-approved.
    """
    current = effective_agent_policy_level(agent)
    if new_level < current and not is_admin_tier(current_user):
        raise HTTPException(
            status_code=403,
            detail=(
                f"開發者不得降低 agent 的有效分類等級"
                f"（目前有效等級為「{current.to_storage()}」）；"
                f"降級需管理員處理"
            ),
        )


def apply_default_classification_level(
    agent: Agent, level: ClassificationLevel
) -> tuple[
    tuple[ClassificationLevel, ClassificationLevel] | None,
    bool,
    tuple[ClassificationLevel, ClassificationLevel],
]:
    """Write ``default_classification_level`` and derive ``requires_encryption``.

    Returns ``(effective_transition, changed, stored_transition)``:
    - ``effective_transition`` is ``(from_effective, to_effective)`` when the
      effective policy level changed (caller must audit); ``None`` when
      effective is unchanged — including a boolean-only repair that keeps
      the same floor, or a pure no-op.
    - ``changed`` is True when either the stored level or the derived
      boolean was written — callers must commit in that case even if audit
      is skipped.
    - ``stored_transition`` is always ``(from_stored, to_stored)`` for the
      requested write (useful as audit metadata alongside the effective
      transition).
    """
    previous_stored = parse_stored_classification_level(
        getattr(agent, "default_classification_level", None)
    )
    previous_effective = effective_agent_policy_level(agent)
    stored_transition = (previous_stored, level)
    new_bool = requires_controlled_access(level)
    level_changed = previous_stored != level
    bool_changed = bool(getattr(agent, "requires_encryption", False)) != new_bool
    if not level_changed and not bool_changed:
        return None, False, stored_transition
    agent.default_classification_level = level.to_storage()
    agent.requires_encryption = new_bool
    new_effective = effective_agent_policy_level(agent)
    if previous_effective != new_effective:
        return (previous_effective, new_effective), True, stored_transition
    return None, True, stored_transition
