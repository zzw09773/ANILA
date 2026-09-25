"""Resolve which Router base LLMs a caller may use.

Campus grants are one row per model, never 6000 copies. Department,
group, and user grants union together. Disabled models lose access
regardless of grants. Expiry is evaluated at request time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.department import Department
from app.models.model_access_group import ModelAccessGroup, ModelAccessGroupMember
from app.models.model_registry import ModelRegistry
from app.models.router_model_grant import RouterModelGrant
from app.models.user import User

PLATFORM_ROUTER_NAME = "anila-router"
ROUTER_ELIGIBLE_TYPES = frozenset({"llm", "vlm"})


@dataclass(frozen=True)
class RouterModelSelection:
    model_id: int
    model_name: str
    display_name: str
    selection_version: int
    grant_sources: tuple[str, ...]


class RouterModelPolicyError(Exception):
    def __init__(self, status_code: int, detail: str | dict):
        super().__init__(detail if isinstance(detail, str) else detail.get("code", "router_model"))
        self.status_code = status_code
        self.detail = detail


# 對話模型不能用的原因。停用優先於授權，避免「還有全院授權但已下線」被說成沒權限。
MODEL_UNAVAILABLE = "model_unavailable"
UNAVAILABLE_INACTIVE = "inactive"
UNAVAILABLE_MISSING = "missing"
UNAVAILABLE_NOT_ROUTER_ENABLED = "not_router_enabled"
UNAVAILABLE_NOT_GRANTED = "not_granted"


def unavailable_model_detail(
    reason: str,
    *,
    display_name: str | None,
    name: str | None = None,
) -> dict:
    """機器可讀的 model_unavailable。display_name 給使用者看的名稱。"""
    label = (display_name or name or "已刪除的模型").strip() or "已刪除的模型"
    detail = {
        "code": MODEL_UNAVAILABLE,
        "reason": reason,
        "display_name": label,
    }
    if name:
        detail["name"] = name
    return detail


def raise_model_unavailable(
    reason: str,
    *,
    display_name: str | None,
    name: str | None = None,
    status_code: int = 409,
) -> None:
    raise RouterModelPolicyError(
        status_code,
        unavailable_model_detail(reason, display_name=display_name, name=name),
    )


def bound_model_block_reason(
    db: Session, user: User, model: ModelRegistry | None, *, now: datetime | None = None
) -> str | None:
    """這位使用者不能再用此列當對話模型時的原因。能用則回 None。"""
    if model is None:
        return UNAVAILABLE_MISSING
    if not model.is_active:
        return UNAVAILABLE_INACTIVE
    if model.name == PLATFORM_ROUTER_NAME:
        return UNAVAILABLE_NOT_ROUTER_ENABLED
    if (model.model_type or "") not in ROUTER_ELIGIBLE_TYPES:
        return UNAVAILABLE_NOT_ROUTER_ENABLED
    if not bool(getattr(model, "router_enabled", False)):
        return UNAVAILABLE_NOT_ROUTER_ENABLED
    if not user_can_use_router_model(db, user, model, now=now):
        return UNAVAILABLE_NOT_GRANTED
    return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _grant_active(grant: RouterModelGrant, now: datetime) -> bool:
    if grant.expires_at is None:
        return True
    exp = grant.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp > now


def _department_lineage_ids(db: Session, department_id: int | None) -> set[int]:
    if department_id is None:
        return set()
    ids: set[int] = set()
    current = department_id
    seen: set[int] = set()
    while current is not None and current not in seen:
        seen.add(current)
        ids.add(current)
        row = db.get(Department, current)
        if row is None:
            break
        current = row.parent_id
    return ids


def _is_router_eligible(model: ModelRegistry) -> bool:
    if not model.is_active:
        return False
    if not bool(getattr(model, "router_enabled", False)):
        return False
    if model.name == PLATFORM_ROUTER_NAME:
        return False
    if (model.model_type or "") not in ROUTER_ELIGIBLE_TYPES:
        return False
    return True


def grant_sources_for_user(
    db: Session, user: User, model: ModelRegistry, *, now: datetime | None = None
) -> list[str]:
    now = now or _now()
    if not user.is_active:
        return []
    if not _is_router_eligible(model):
        return []
    grants = (
        db.query(RouterModelGrant)
        .filter(RouterModelGrant.model_id == model.id)
        .all()
    )
    lineage = _department_lineage_ids(db, user.department_id)
    member_group_ids = {
        row.group_id
        for row in db.query(ModelAccessGroupMember)
        .filter(ModelAccessGroupMember.user_id == user.id)
        .all()
    }
    active_groups = {
        g.id
        for g in db.query(ModelAccessGroup)
        .filter(ModelAccessGroup.id.in_(member_group_ids or {-1}), ModelAccessGroup.is_active.is_(True))
        .all()
    } if member_group_ids else set()
    sources: list[str] = []
    for grant in grants:
        if not _grant_active(grant, now):
            continue
        if grant.scope_type == "all":
            sources.append("all")
        elif grant.scope_type == "user" and grant.user_id == user.id:
            sources.append("user")
        elif grant.scope_type == "group" and grant.group_id in active_groups:
            sources.append("group")
        elif grant.scope_type == "department" and grant.department_id is not None:
            if grant.include_descendants:
                if user.department_id is not None and grant.department_id in lineage:
                    sources.append("department")
            elif grant.department_id == user.department_id:
                sources.append("department")
    return sources


def user_can_use_router_model(db: Session, user: User, model: ModelRegistry, *, now: datetime | None = None) -> bool:
    return bool(grant_sources_for_user(db, user, model, now=now))


def list_router_models_for_user(db: Session, user: User, *, now: datetime | None = None) -> list[ModelRegistry]:
    now = now or _now()
    models = (
        db.query(ModelRegistry)
        .filter(ModelRegistry.router_enabled.is_(True), ModelRegistry.is_active.is_(True))
        .order_by(ModelRegistry.id.asc())
        .all()
    )
    return [m for m in models if user_can_use_router_model(db, user, m, now=now)]



def require_campus_default_eligible(db: Session, model: ModelRegistry, *, now: datetime | None = None) -> None:
    """Raise RouterModelPolicyError if model cannot be the unique campus default."""
    now = now or _now()
    if model is None:
        raise RouterModelPolicyError(404, "模型不存在")
    if model.name == PLATFORM_ROUTER_NAME:
        raise RouterModelPolicyError(400, "平台入口不能設為全院預設基礎模型")
    if not model.is_active:
        raise RouterModelPolicyError(400, "已停用的模型不能設為主路由模型")
    if (model.model_type or "") not in ROUTER_ELIGIBLE_TYPES:
        raise RouterModelPolicyError(400, "此模型不能設為全院預設")
    if not bool(getattr(model, "router_enabled", False)):
        raise RouterModelPolicyError(400, "請先開放此模型給 Router")
    grant = (
        db.query(RouterModelGrant)
        .filter(RouterModelGrant.model_id == model.id, RouterModelGrant.scope_type == "all")
        .first()
    )
    if grant is None or not _grant_active(grant, now):
        raise RouterModelPolicyError(400, "全院預設必須具有有效的全院授權")

def campus_default_model(db: Session) -> ModelRegistry | None:
    return (
        db.query(ModelRegistry)
        .filter(
            ModelRegistry.is_router_primary.is_(True),
            ModelRegistry.router_enabled.is_(True),
            ModelRegistry.is_active.is_(True),
            ModelRegistry.name != PLATFORM_ROUTER_NAME,
            ModelRegistry.model_type.in_(tuple(ROUTER_ELIGIBLE_TYPES)),
        )
        .first()
    )


def resolve_router_model(
    db: Session,
    caller: User,
    *,
    conversation_id: int | None = None,
    requested_name: str | None = None,
    conversation_model_id: int | None = None,
    selection_version: int = 0,
    api_key_id: int | None = None,
    now: datetime | None = None,
) -> RouterModelSelection:
    """Return an immutable selection or raise RouterModelPolicyError."""
    from app.models.api_key import ApiKeyModelPermission

    now = now or _now()
    if conversation_model_id is not None and requested_name:
        if requested_name == PLATFORM_ROUTER_NAME:
            raise RouterModelPolicyError(409, "anila-router 是平台入口，不是可選基礎模型")
        named = db.query(ModelRegistry).filter(ModelRegistry.name == requested_name).first()
        if named is None or named.id != conversation_model_id:
            raise RouterModelPolicyError(409, "對話已指定模型，與請求衝突")

    target: ModelRegistry | None = None
    if conversation_model_id is not None:
        target = db.get(ModelRegistry, conversation_model_id)
    elif requested_name:
        if requested_name == PLATFORM_ROUTER_NAME:
            raise RouterModelPolicyError(409, "anila-router 是平台入口，不是可選基礎模型")
        target = db.query(ModelRegistry).filter(ModelRegistry.name == requested_name).first()
        if target is None:
            raise_model_unavailable(
                UNAVAILABLE_MISSING,
                display_name=requested_name,
                name=requested_name,
                status_code=404,
            )
    else:
        target = campus_default_model(db)
        if target is None:
            raise RouterModelPolicyError(409, "尚未設定可用的全院預設模型，請先選擇")

    if target is None:
        # 對話列還指著 id，但登錄列已經不在。不要改去抓全院預設。
        if conversation_model_id is not None:
            raise_model_unavailable(
                UNAVAILABLE_MISSING,
                display_name=requested_name or "已刪除的模型",
                name=requested_name,
            )
        raise RouterModelPolicyError(409, "請重新選擇對話模型")
    block = bound_model_block_reason(db, caller, target, now=now)
    if block == UNAVAILABLE_NOT_GRANTED:
        raise_model_unavailable(
            block,
            display_name=target.display_name or target.name,
            name=target.name,
            status_code=403,
        )
    if block:
        raise_model_unavailable(
            block,
            display_name=target.display_name or target.name,
            name=target.name,
        )

    if api_key_id is not None:
        perm = (
            db.query(ApiKeyModelPermission)
            .filter(
                ApiKeyModelPermission.api_key_id == api_key_id,
                ApiKeyModelPermission.model_id == target.id,
            )
            .first()
        )
        if perm is None:
            raise RouterModelPolicyError(403, "此 API key 未授權該模型")

    sources = tuple(grant_sources_for_user(db, caller, target, now=now))
    return RouterModelSelection(
        model_id=target.id,
        model_name=target.name,
        display_name=target.display_name,
        selection_version=selection_version,
        grant_sources=sources,
    )
