"""平台模型角色的單一讀寫點。

六個角色：

* ``router_primary``／``platform_embedding``／``slides`` 沿用
  ``model_registry`` 上既有的旗標（資料留在原欄，不搬）。
* ``vision``／``summary``／``knowledge_chat`` 存在 ``model_roles``。

一個角色最多一個模型。模型必須啟用，且類型符合該角色。沒設、或指到
已停用／已刪除的模型，呼叫端拿到的訊息都點名角色，不猜模型名稱。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.model_registry import ModelRegistry
from app.models.model_role import ModelRole
from app.models.router_model_grant import RouterModelGrant

# 角色 → 使用者看得到的說法。訊息格式固定，呼叫端直接顯示。
_UNSET = "{label}尚未在治理中心設定"
_INACTIVE = "{label}已停用，請在治理中心改選啟用中的模型"
_BAD_TYPE = "{label}只能指定{types}類型的模型"
_INACTIVE_ASSIGN = "已停用的模型不能擔任{label}"


@dataclass(frozen=True)
class RoleSpec:
    role: str
    label: str
    description: str
    accepted_types: frozenset[str]
    storage: str  # "flag" | "table"
    column: str | None = None
    # 呼叫這個角色時用的是終端使用者自己的憑證，不是平台服務帳號。
    end_user_credential: bool = False

    @property
    def unset_message(self) -> str:
        return _UNSET.format(label=self.label)

    @property
    def inactive_message(self) -> str:
        return _INACTIVE.format(label=self.label)


ROLE_SPECS: dict[str, RoleSpec] = {
    "router_primary": RoleSpec(
        role="router_primary",
        label="主路由模型",
        description="ANILA Router 的預設基礎模型。使用者仍可在對話中另選已授權的模型。",
        accepted_types=frozenset({"llm"}),
        storage="flag",
        column="is_router_primary",
    ),
    "platform_embedding": RoleSpec(
        role="platform_embedding",
        label="平台嵌入模型",
        description="記憶與知識庫共用的嵌入模型。更換後，以其他模型建立索引的知識庫會檢索不到。",
        accepted_types=frozenset({"embedding"}),
        storage="flag",
        column="is_platform_embedding",
    ),
    "slides": RoleSpec(
        role="slides",
        label="簡報模型",
        description="Studio 撰寫簡報、資訊圖表、心智圖與資料表時使用的語言模型。",
        accepted_types=frozenset({"llm"}),
        storage="flag",
        column="is_slides_primary",
        end_user_credential=True,
    ),
    "vision": RoleSpec(
        role="vision",
        label="視覺模型",
        description="Studio 簡報視覺檢查，以及知識庫入庫時的圖片說明與掃描 PDF 的文字辨識。",
        accepted_types=frozenset({"llm", "vlm"}),
        storage="table",
        end_user_credential=True,
    ),
    "summary": RoleSpec(
        role="summary",
        label="摘要模型",
        description="記憶抽取、對話壓縮、思考過程摘要與對話標題使用的語言模型。",
        accepted_types=frozenset({"llm"}),
        storage="table",
        end_user_credential=True,
    ),
    "knowledge_chat": RoleSpec(
        role="knowledge_chat",
        label="知識庫對話模型",
        description="ANILA LM 知識庫對話的預設模型。使用者若已自行選擇模型，則沿用該選擇。",
        accepted_types=frozenset({"llm"}),
        storage="table",
        end_user_credential=True,
    ),
}

ROLE_ORDER: tuple[str, ...] = tuple(ROLE_SPECS)


@dataclass(frozen=True)
class ResolvedRole:
    spec: RoleSpec
    model: ModelRegistry | None
    status: str  # ok | unset | inactive
    message: str | None


def get_spec(role: str) -> RoleSpec | None:
    return ROLE_SPECS.get(role)


def _flag_row(db: Session, spec: RoleSpec) -> ModelRegistry | None:
    column = getattr(ModelRegistry, spec.column)
    return db.query(ModelRegistry).filter(column.is_(True)).first()


def _table_row(db: Session, role: str) -> ModelRole | None:
    return db.get(ModelRole, role)


def resolve_role(db: Session, role: str) -> ResolvedRole:
    """讀一個角色現在指向哪一列。不猜名稱、不改資料。"""
    spec = ROLE_SPECS[role]
    model: ModelRegistry | None = None
    if spec.storage == "flag":
        model = _flag_row(db, spec)
    else:
        link = _table_row(db, role)
        if link is not None and link.model_id is not None:
            model = db.get(ModelRegistry, link.model_id)
    if model is None:
        return ResolvedRole(spec, None, "unset", spec.unset_message)
    if not model.is_active:
        return ResolvedRole(spec, model, "inactive", spec.inactive_message)
    if (model.model_type or "") not in spec.accepted_types:
        return ResolvedRole(spec, model, "inactive", spec.inactive_message)
    return ResolvedRole(spec, model, "ok", None)


def list_roles(db: Session) -> list[ResolvedRole]:
    return [resolve_role(db, role) for role in ROLE_ORDER]


def type_error(spec: RoleSpec) -> str:
    types = " 或 ".join(sorted(spec.accepted_types))
    return _BAD_TYPE.format(label=spec.label, types=types)


def inactive_assign_error(spec: RoleSpec) -> str:
    return _INACTIVE_ASSIGN.format(label=spec.label)


def assign_table_role(db: Session, spec: RoleSpec, model: ModelRegistry) -> None:
    """寫入表內角色，不提交。呼叫端要把這次變更和稽核放在同一筆交易。"""
    link = _table_row(db, spec.role)
    now = datetime.now(timezone.utc)
    if link is None:
        link = ModelRole(role=spec.role, model_id=model.id, updated_at=now)
        db.add(link)
    else:
        link.model_id = model.id
        link.updated_at = now


def clear_table_role(db: Session, role: str) -> ModelRegistry | None:
    """清掉表內角色，不提交。回傳被清掉的模型（沒有則 None）。冪等。"""
    link = _table_row(db, role)
    if link is None or link.model_id is None:
        return None
    model = db.get(ModelRegistry, link.model_id)
    link.model_id = None
    link.updated_at = datetime.now(timezone.utc)
    return model


def _grant_is_active(grant: RouterModelGrant, now: datetime) -> bool:
    if grant.expires_at is None:
        return True
    exp = grant.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp > now


def has_active_all_users_grant(
    db: Session, model_id: int, *, now: datetime | None = None
) -> bool:
    """一條有效的全院授權。過期不算。不看其他模型。"""
    now = now or datetime.now(timezone.utc)
    grant = (
        db.query(RouterModelGrant)
        .filter(
            RouterModelGrant.model_id == model_id,
            RouterModelGrant.scope_type == "all",
        )
        .first()
    )
    return grant is not None and _grant_is_active(grant, now)


def _matches_end_user_role(model: ModelRegistry, spec: RoleSpec) -> bool:
    return bool(model.is_active) and (model.model_type or "") in spec.accepted_types


def is_current_end_user_role_model(db: Session, model: ModelRegistry) -> bool:
    """這顆是不是某個「用使用者自己的憑證呼叫」的角色目前指定的模型。"""
    slides = ROLE_SPECS["slides"]
    if (
        slides.end_user_credential
        and model.is_slides_primary
        and _matches_end_user_role(model, slides)
    ):
        return True
    roles = [
        row.role
        for row in db.query(ModelRole.role).filter(ModelRole.model_id == model.id).all()
    ]
    for role in roles:
        spec = ROLE_SPECS.get(role)
        if spec is None or not spec.end_user_credential or spec.storage != "table":
            continue
        if _matches_end_user_role(model, spec):
            return True
    return False


def caller_may_use_end_user_role_model(db: Session, model: ModelRegistry) -> bool:
    """全院授權只放行目前的角色模型。其他模型的權限不變。"""
    if not is_current_end_user_role_model(db, model):
        return False
    return has_active_all_users_grant(db, model.id)
