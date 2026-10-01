"""把這套環境的非密設定搬到另一套。

匯出不含任何金鑰：模型閘道金鑰與外部服務憑證都寫成
``credential: null``，並用 ``credential_required`` 標出要另外填的項目。
匯入走既有的寫入函數（信任主機、出向檢查、角色資格、部門檢查、稽核），
不另開一條繞過驗證的寫入。

``credential`` 為 null 代表「檔案沒有這把金鑰」，不是「把現有金鑰清掉」。
同一份檔再匯入一次不會改資料。端點網址裡的帳密也算：匯出會拿掉
帳密，再匯入時若位址只差這段、檔案又沒帶金鑰，就當成沒變。

平台嵌入角色不在這裡指定。那個指定要連到模型量測維度，匯入不能代替。
檔案沒列出的角色、授權、部門、公告也不會被刪掉。
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlparse

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func
from sqlalchemy.orm import Session

from anila_core.ingestion.docling_source import invalidate_docling_source_cache
from anila_core.security.url_guard import (
    UnsafeEndpointError,
    push_request_trusted_hosts,
    reset_request_trusted_hosts,
)
from app.api.banners import BannerCreate, _validate_level
from app.api.departments import (
    create_department,
    update_department,
)
from app.api.models import (
    _enforce_endpoint_url,
    _enforce_protocol_endpoint,
    set_router_primary,
    set_slides_primary,
)
from app.api.router_models import _validate_grant
from app.models.banner import Banner
from app.models.department import Department
from app.models.external_service import SERVICE_KEYS, ExternalService
from app.models.model_registry import ModelRegistry
from app.models.router_model_grant import RouterModelGrant
from app.models.trusted_host import TrustedHost
from app.models.user import User
from app.schemas.department import DepartmentCreate, DepartmentUpdate
from app.schemas.model_registry import ModelCreate, _validate_gateway_key
from app.schemas.router_model import RouterGrantIn
from app.schemas.settings_transfer import (
    SCHEMA_VERSION,
    ModelTransfer,
    SettingsDocument,
)
from app.schemas.trusted_host import TrustedHostCreate
from app.services.agent_availability import mark_agents_base_model_offline
from app.services.audit_service import log_audit_event_or_raise
from app.services.auto_seed import PLATFORM_ROUTER_NAME
from app.services.department_tree import max_depth
from app.services.endpoint_author_service import (
    ENDPOINT_INTERNAL,
    ENDPOINT_REDACTED,
    require_endpoint_address_author,
    visible_endpoint_url,
)
from app.services import external_services as external_svc
from app.services.model_roles import (
    ROLE_SPECS,
    assign_table_role,
    get_spec,
    has_active_all_users_grant,
    inactive_assign_error,
    list_roles,
    type_error,
)
from app.services.service_token_envelope import encode_service_token_envelope
from app.services import trusted_host_service

_NEEDS_KEY = "需另外填入金鑰"
_NEEDS_ENDPOINT = "需另外填入端點"
_WRITE_KEY = "寫入金鑰"
_KEEP_ENDPOINT_SECRET = "端點帳密保留原值"
_DROPPED_ENDPOINT_SECRET = "原端點帶帳密，套用後需另外填入金鑰"
_PLATFORM_EMBEDDING_SKIP = (
    "平台嵌入模型要在端點可連線時於「模型角色」指定，匯入不會代替連線量測"
)
_REDACTED_ENDPOINTS = {ENDPOINT_REDACTED, ENDPOINT_INTERNAL}

_EXTERNAL_LABELS = {
    "document_parser": "文件解析",
    "speech": "語音辨識",
}


@dataclass
class PlanItem:
    entity: str
    key: str
    label: str
    action: str
    message: str = ""

    def as_dict(self) -> dict:
        return {
            "entity": self.entity,
            "key": self.key,
            "label": self.label,
            "action": self.action,
            "message": self.message,
        }


class SettingsTransferError(Exception):
    def __init__(self, messages: list[str]):
        self.messages = messages
        super().__init__("；".join(messages))


def _fail(messages: list[str]) -> None:
    if messages:
        raise SettingsTransferError(messages)


def _detail_text(exc: HTTPException) -> str:
    detail = exc.detail
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("detail") or detail)
    return str(detail)


@contextmanager
def _also_trust(hosts: set[str]):
    """試算時檔案裡的信任主機還沒寫進資料庫，出向檢查仍要把它們算進去。

    加在這個請求的 context 上，不進行程共用的 provider 清單。
    """
    token = push_request_trusted_hosts(hosts)
    try:
        yield
    finally:
        reset_request_trusted_hosts(token)


def _url_has_userinfo(url: str) -> bool:
    parsed = urlparse(url or "")
    return parsed.username is not None or parsed.password is not None


def _endpoint_userinfo_case(stored: str, incoming: str, supplied: str | None) -> str:
    """'keep'：檔案位址等於拿掉帳密的原值，而且沒帶金鑰。'drop'：host 或路徑不同。

    匯出用 display_base_url 拿掉帳密。再匯入時不能把那份比較當成位址改了，
    否則套用會把原網址上的帳密清掉。換成別的 host 或路徑才真的更新，
    原帳密不跟著過去。
    """
    raw = (stored or "").strip()
    wanted = (incoming or "").strip()
    if not _url_has_userinfo(raw):
        return ""
    stripped = external_svc.display_base_url(raw)
    if stripped == wanted and supplied is None:
        return "keep"
    if stripped != wanted:
        return "drop"
    return ""


def _endpoint_omitted(item: ModelTransfer) -> bool:
    return bool(item.endpoint_hidden)


def _model_by_name(db: Session, name: str) -> ModelRegistry | None:
    return (
        db.query(ModelRegistry)
        .filter(func.lower(ModelRegistry.name) == name.strip().lower())
        .first()
    )


def _department_path(db: Session, dept: Department) -> list[str]:
    names: list[str] = []
    seen: set[int] = set()
    current: Department | None = dept
    while current is not None and current.id not in seen:
        seen.add(current.id)
        names.append(current.name)
        if current.parent_id is None:
            break
        current = db.get(Department, current.parent_id)
    names.reverse()
    return names


def _find_department(db: Session, path: list[str]) -> Department | None:
    parent_id: int | None = None
    node: Department | None = None
    for name in path:
        query = db.query(Department).filter(Department.name == name)
        if parent_id is None:
            query = query.filter(Department.parent_id.is_(None))
        else:
            query = query.filter(Department.parent_id == parent_id)
        node = query.first()
        if node is None:
            return None
        parent_id = node.id
    return node


def _path_label(path: list[str]) -> str:
    return "／".join(path)


def _supplied_credential(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _nearly(left, right) -> bool:
    if left is None and right is None:
        return True
    if left is None or right is None:
        return False
    return abs(float(left) - float(right)) < 1e-6


def _model_payload(item: ModelTransfer, *, owner_department_id: int | None) -> dict:
    return {
        "name": item.name,
        "display_name": item.display_name,
        "model_type": item.model_type,
        "endpoint_url": item.endpoint_url,
        "api_version": item.api_version,
        "protocol": item.protocol,
        "router_enabled": item.router_enabled,
        "max_concurrent": item.max_concurrent,
        # ModelCreate 沒有 is_active；套用時直接寫到列上。
        "is_internal": item.is_internal,
        "description": item.description,
        "context_window": item.context_window,
        "classification_ceiling": item.classification_ceiling,
        "supports_streaming": item.supports_streaming,
        "supports_json_schema": item.supports_json_schema,
        "supports_tools": item.supports_tools,
        "thinking_effort": item.thinking_effort,
        "thinking_user_selectable": item.thinking_user_selectable,
        "temperature": item.temperature,
        "top_p": item.top_p,
        "presence_penalty": item.presence_penalty,
        "max_tokens": item.max_tokens,
        "base_model_id": None,
        "owner_department_id": owner_department_id,
        "api_key": _supplied_credential(item.credential),
    }


def _model_dirty(row: ModelRegistry, item: ModelTransfer, owner_id: int | None, base_id: int | None) -> bool:
    stored_endpoint = (row.endpoint_url or "").strip()
    if _endpoint_userinfo_case(
        stored_endpoint, item.endpoint_url or "", _supplied_credential(item.credential),
    ) == "keep":
        stored_endpoint = external_svc.display_base_url(stored_endpoint)
    pairs = (
        (row.display_name, item.display_name),
        (row.model_type, item.model_type),
        (stored_endpoint, item.endpoint_url),
        (row.api_version or "v1", item.api_version),
        (row.protocol or "openai_compatible", item.protocol),
        (bool(row.router_enabled), bool(item.router_enabled)),
        (row.max_concurrent, item.max_concurrent),
        (bool(row.is_active), bool(item.is_active)),
        (bool(row.is_internal), bool(item.is_internal)),
        (row.description, item.description),
        (row.context_window, item.context_window),
        (row.classification_ceiling, item.classification_ceiling),
        (bool(row.supports_streaming), bool(item.supports_streaming)),
        (bool(row.supports_json_schema), bool(item.supports_json_schema)),
        (bool(row.supports_tools), bool(item.supports_tools)),
        (row.thinking_effort, item.thinking_effort),
        (bool(row.thinking_user_selectable), bool(item.thinking_user_selectable)),
        (row.owner_department_id, owner_id),
        (row.base_model_id, base_id),
    )
    for current, wanted in pairs:
        if current != wanted:
            return True
    if not _nearly(row.temperature, item.temperature):
        return True
    if not _nearly(row.top_p, item.top_p):
        return True
    if not _nearly(row.presence_penalty, item.presence_penalty):
        return True
    if row.max_tokens != item.max_tokens:
        return True
    return False


def _credential_state(required: bool, stored: bool, supplied: str | None) -> str:
    """回傳 ''、'needs'。已有金鑰或這次有帶金鑰就不標。"""
    if required and not stored and not supplied:
        return "needs"
    return ""


def _action_for(
    exists: bool,
    dirty: bool,
    needs_key: bool,
    *,
    writing_key: bool = False,
) -> tuple[str, str]:
    """檔案帶了非空金鑰時，即使其他欄位相同也是更新。"""
    if writing_key and exists:
        message = _WRITE_KEY
    elif needs_key:
        message = _NEEDS_KEY
    else:
        message = ""
    if not exists:
        return ("needs-credential" if needs_key else "create"), message
    if dirty or writing_key:
        return "update", message
    if needs_key:
        return "needs-credential", message
    return "skip", ""


def build_export(db: Session, actor: User) -> dict:
    external_svc.ensure_rows(db)
    db.commit()

    hosts = [
        {"host": row.host, "note": row.note}
        for row in trusted_host_service.list_hosts(db)
    ]
    models = []
    for row in (
        db.query(ModelRegistry)
        .filter(ModelRegistry.name != PLATFORM_ROUTER_NAME)
        .order_by(ModelRegistry.name)
        .all()
    ):
        base_name = None
        if row.base_model_id is not None:
            base = db.get(ModelRegistry, row.base_model_id)
            if base is not None and base.name != PLATFORM_ROUTER_NAME:
                base_name = base.name
        dept_path = None
        if row.owner_department_id is not None:
            dept = db.get(Department, row.owner_department_id)
            if dept is not None:
                dept_path = _department_path(db, dept)
        raw_endpoint = row.endpoint_url or ""
        has_userinfo = _url_has_userinfo(raw_endpoint)
        visible = visible_endpoint_url(
            raw_endpoint,
            is_internal=bool(row.is_internal),
            db=db,
            caller=actor,
        )
        if visible in _REDACTED_ENDPOINTS:
            endpoint_url = None
            endpoint_hidden = True
        else:
            endpoint_url = external_svc.display_base_url(visible)
            endpoint_hidden = False
        models.append({
            "name": row.name,
            "display_name": row.display_name,
            "model_type": row.model_type,
            "endpoint_url": endpoint_url,
            "endpoint_hidden": endpoint_hidden,
            "api_version": row.api_version or "v1",
            "protocol": row.protocol or "openai_compatible",
            "router_enabled": bool(row.router_enabled),
            "max_concurrent": row.max_concurrent,
            "is_active": bool(row.is_active),
            "is_internal": bool(row.is_internal),
            "description": row.description,
            "context_window": row.context_window,
            "classification_ceiling": row.classification_ceiling,
            "supports_streaming": bool(row.supports_streaming),
            "supports_json_schema": bool(row.supports_json_schema),
            "supports_tools": bool(row.supports_tools),
            "thinking_effort": row.thinking_effort,
            "thinking_user_selectable": bool(row.thinking_user_selectable),
            "temperature": row.temperature,
            "top_p": row.top_p,
            "presence_penalty": row.presence_penalty,
            "max_tokens": row.max_tokens,
            "base_model_name": base_name,
            "owner_department_path": dept_path,
            "credential": None,
            "credential_required": bool(row.api_key_secret_ref) or has_userinfo,
        })

    roles = []
    for resolved in list_roles(db):
        if resolved.model is None or resolved.model.name == PLATFORM_ROUTER_NAME:
            continue
        roles.append({
            "role": resolved.spec.role,
            "model_name": resolved.model.name,
        })

    grants = []
    grant_rows = (
        db.query(RouterModelGrant, ModelRegistry)
        .join(ModelRegistry, ModelRegistry.id == RouterModelGrant.model_id)
        .filter(RouterModelGrant.scope_type == "all")
        .order_by(ModelRegistry.name)
        .all()
    )
    for grant, model in grant_rows:
        if model.name == PLATFORM_ROUTER_NAME:
            continue
        if not has_active_all_users_grant(db, model.id):
            continue
        grants.append({"model_name": model.name, "scope": "all"})

    services = []
    for key in SERVICE_KEYS:
        row = db.get(ExternalService, key)
        services.append({
            "kind": key,
            "enabled": bool(row.enabled) if row is not None else False,
            "base_url": external_svc.display_base_url((row.base_url if row else "") or ""),
            "protocol": (row.protocol if row else None) or "native",
            "openai_model": (row.openai_model if row else None) or "whisper-1",
            "credential": None,
            "credential_required": bool(row and row.credential_envelope),
        })

    departments = []
    for dept in db.query(Department).order_by(Department.id).all():
        departments.append({
            "path": _department_path(db, dept),
            "description": dept.description,
            "is_active": bool(dept.is_active),
        })
    departments.sort(key=lambda item: (len(item["path"]), item["path"]))

    banners = [
        {
            "level": row.level,
            "content": row.content,
            "show_on_login": bool(row.show_on_login),
            "sort_order": row.sort_order,
        }
        for row in (
            db.query(Banner)
            .filter(Banner.is_active.is_(True))
            .order_by(Banner.sort_order, Banner.id)
            .all()
        )
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "trusted_hosts": hosts,
        "models": models,
        "model_roles": roles,
        "router_grants": grants,
        "external_services": services,
        "departments": departments,
        "banners": banners,
    }


def _blocked_by_hidden_endpoint(db: Session, document: SettingsDocument, name: str) -> bool:
    """端點被省略、而且這套環境還沒有這筆模型。不能靠這份檔建出來。"""
    key = name.strip().lower()
    for item in document.models:
        if item.name.lower() == key and _endpoint_omitted(item):
            return _model_by_name(db, name) is None
    return False


def _planned_model(db: Session, document: SettingsDocument, name: str) -> ModelTransfer | ModelRegistry | None:
    for item in document.models:
        if item.name.lower() == name.lower():
            return item
    return _model_by_name(db, name)


def _planned_active_llm(model: ModelTransfer | ModelRegistry) -> tuple[bool, str, bool]:
    if isinstance(model, ModelTransfer):
        return bool(model.is_active), model.model_type, bool(model.router_enabled)
    return bool(model.is_active), model.model_type or "", bool(model.router_enabled)


def _grant_names(document: SettingsDocument) -> set[str]:
    return {item.model_name.lower() for item in document.router_grants}


def _validate_models(db: Session, document: SettingsDocument, errors: list[str]) -> None:
    seen: set[str] = set()
    names_in_file = {item.name.lower() for item in document.models}
    trusted: set[str] = set()
    for host in document.trusted_hosts:
        try:
            parsed_host = TrustedHostCreate(host=host.host, note=host.note).host
        except ValidationError as exc:
            errors.append(f"信任主機「{host.host}」：{_validation_message(exc)}")
            continue
        if parsed_host in trusted:
            errors.append(f"信任主機「{parsed_host}」在檔案裡重複")
            continue
        trusted.add(parsed_host)
    for item in document.models:
        key = item.name.lower()
        if key in seen:
            errors.append(f"模型「{item.name}」在檔案裡重複")
            continue
        seen.add(key)
        if key == PLATFORM_ROUTER_NAME:
            errors.append("平台入口由系統維護，不經匯入變更")
            continue
        if _endpoint_omitted(item):
            continue
        if item.endpoint_url in _REDACTED_ENDPOINTS:
            errors.append(f"模型「{item.name}」的位址已被遮罩，無法匯入")
            continue
        supplied = _supplied_credential(item.credential)
        if supplied is not None:
            try:
                _validate_gateway_key(supplied)
            except ValueError as exc:
                errors.append(f"模型「{item.name}」的金鑰：{exc}")
        owner_id = None
        if item.owner_department_path:
            if not (
                _find_department(db, item.owner_department_path)
                or _path_in_document(document, item.owner_department_path)
            ):
                errors.append(
                    f"模型「{item.name}」的所屬部門「{_path_label(item.owner_department_path)}」不在匯入檔裡"
                )
        try:
            ModelCreate(**_model_payload(item, owner_department_id=owner_id))
        except ValidationError as exc:
            errors.append(f"模型「{item.name}」：{_validation_message(exc)}")
            continue
        if item.base_model_name:
            base_key = item.base_model_name.lower()
            if base_key == key:
                errors.append(f"模型「{item.name}」不能把自己設為底層模型")
            elif base_key not in names_in_file and _model_by_name(db, item.base_model_name) is None:
                errors.append(f"模型「{item.name}」的底層模型「{item.base_model_name}」不存在")
        try:
            with _also_trust(trusted):
                _enforce_endpoint_url(item.endpoint_url)
                _enforce_protocol_endpoint(item.protocol, item.endpoint_url)
        except HTTPException as exc:
            errors.append(f"模型「{item.name}」的位址：{_detail_text(exc)}")


def _path_in_document(document: SettingsDocument, path: list[str]) -> bool:
    wanted = list(path)
    return any(item.path[: len(wanted)] == wanted for item in document.departments)


def _validate_departments(db: Session, document: SettingsDocument, errors: list[str]) -> None:
    cap = max_depth(db)
    seen: set[tuple[str, ...]] = set()
    for item in document.departments:
        key = tuple(item.path)
        if key in seen:
            errors.append(f"部門「{_path_label(item.path)}」在檔案裡重複")
            continue
        seen.add(key)
        if len(item.path) > cap:
            errors.append(
                f"部門「{_path_label(item.path)}」超過層級上限（最多 {cap} 層）"
            )
            continue
        if len(item.path) < 2:
            continue
        parent_path = item.path[:-1]
        parent_item = next(
            (other for other in document.departments if other.path == parent_path),
            None,
        )
        if parent_item is not None and not parent_item.is_active:
            errors.append(
                f"部門「{_path_label(item.path)}」的上層已在匯入檔中停用"
            )
            continue
        if parent_item is None:
            parent = _find_department(db, parent_path)
            if parent is not None and not parent.is_active:
                errors.append(
                    f"部門「{_path_label(item.path)}」的上層已停用，無法掛在底下"
                )


def _validate_roles(db: Session, document: SettingsDocument, errors: list[str]) -> None:
    seen: set[str] = set()
    grant_names = _grant_names(document)
    for item in document.model_roles:
        if item.role in seen:
            errors.append(f"模型角色「{item.role}」在檔案裡重複")
            continue
        seen.add(item.role)
        spec = get_spec(item.role)
        if spec is None:
            errors.append(f"未知的模型角色「{item.role}」")
            continue
        if _blocked_by_hidden_endpoint(db, document, item.model_name):
            continue
        target = _planned_model(db, document, item.model_name)
        if item.role == "platform_embedding":
            if target is None:
                errors.append(f"{spec.label}指到的模型「{item.model_name}」不存在")
            continue
        if target is None or (
            isinstance(target, ModelRegistry) and target.name == PLATFORM_ROUTER_NAME
        ):
            errors.append(f"{spec.label}指到的模型「{item.model_name}」不存在")
            continue
        active, model_type, router_enabled = _planned_active_llm(target)
        if not active:
            errors.append(inactive_assign_error(spec))
            continue
        if model_type not in spec.accepted_types:
            errors.append(type_error(spec))
            continue
        if item.role == "router_primary":
            if isinstance(target, ModelTransfer) and target.name == PLATFORM_ROUTER_NAME:
                errors.append("平台入口不能設為全院預設基礎模型")
                continue
            if not router_enabled:
                errors.append("請先開放此模型給 Router")
                continue
            name = target.name if isinstance(target, ModelTransfer) else target.name
            in_file = name.lower() in grant_names
            in_db = (
                isinstance(target, ModelRegistry)
                and has_active_all_users_grant(db, target.id)
            )
            if not in_file and not in_db:
                errors.append("全院預設必須具有有效的全院授權")


def _validate_grants(db: Session, document: SettingsDocument, errors: list[str]) -> None:
    seen: set[str] = set()
    for item in document.router_grants:
        key = item.model_name.lower()
        if key in seen:
            errors.append(f"模型「{item.model_name}」的全院授權在檔案裡重複")
            continue
        seen.add(key)
        if key == PLATFORM_ROUTER_NAME:
            errors.append("平台入口不是可授權的基礎模型")
            continue
        if _blocked_by_hidden_endpoint(db, document, item.model_name):
            continue
        if _planned_model(db, document, item.model_name) is None:
            errors.append(f"全院授權的模型「{item.model_name}」不存在")


def _validate_external(document: SettingsDocument, errors: list[str], trusted: set[str]) -> None:
    seen: set[str] = set()
    for item in document.external_services:
        if item.kind in seen:
            errors.append(f"外部服務「{item.kind}」在檔案裡重複")
            continue
        seen.add(item.kind)
        if item.kind == "speech" and item.protocol is not None:
            if item.protocol.strip().lower() not in ("native", "openai"):
                errors.append("語音協定只接受 native 或 openai")
        if item.kind == "speech" and item.openai_model is not None and not item.openai_model.strip():
            errors.append("語音模型名稱不可為空")
        try:
            with _also_trust(trusted):
                external_svc.enforce_base_url(item.base_url)
        except external_svc.ExternalServiceUrlError as exc:
            errors.append(f"{_EXTERNAL_LABELS.get(item.kind, item.kind)}：{exc.public_message}")
        except UnsafeEndpointError as exc:
            errors.append(f"{_EXTERNAL_LABELS.get(item.kind, item.kind)}的位址：{exc}")


def _validate_banners(document: SettingsDocument, errors: list[str]) -> None:
    for item in document.banners:
        try:
            _validate_level(item.level)
            BannerCreate(
                level=item.level,
                content=item.content,
                is_active=True,
                show_on_login=item.show_on_login,
                sort_order=item.sort_order,
            )
        except HTTPException as exc:
            errors.append(f"公告：{_detail_text(exc)}")
        except ValidationError as exc:
            errors.append(f"公告：{_validation_message(exc)}")


def _validation_message(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        parts.append(str(err.get("msg") or "格式不正確"))
    return "；".join(parts) or "格式不正確"


def _host_items(db: Session, document: SettingsDocument) -> list[PlanItem]:
    items: list[PlanItem] = []
    seen: set[str] = set()
    for raw in document.trusted_hosts:
        parsed = TrustedHostCreate(host=raw.host, note=raw.note)
        if parsed.host in seen:
            continue
        seen.add(parsed.host)
        existing = (
            db.query(TrustedHost)
            .filter(TrustedHost.host == parsed.host)
            .first()
        )
        if existing is None:
            action, message = "create", ""
        elif raw.note is not None and raw.note != existing.note:
            action, message = "update", ""
        else:
            action, message = "skip", ""
        items.append(PlanItem("trusted_host", parsed.host, parsed.host, action, message))
    return items


def _department_items(db: Session, document: SettingsDocument) -> list[PlanItem]:
    """檔案裡的部門，加上套用時會順便建出的上層。

    路徑 ["A","B"] 在空環境會先建 A，再掛 A／B。試算要把這兩筆都列出來。
    """
    explicit = {tuple(raw.path): raw for raw in document.departments}
    needed: set[tuple[str, ...]] = set()
    for raw in document.departments:
        for index in range(1, len(raw.path) + 1):
            needed.add(tuple(raw.path[:index]))
    items: list[PlanItem] = []
    for prefix in sorted(needed, key=lambda path: (len(path), path)):
        raw = explicit.get(prefix)
        label = _path_label(list(prefix))
        existing = _find_department(db, list(prefix))
        if raw is None:
            if existing is None:
                items.append(PlanItem("department", label, label, "create", ""))
            continue
        dirty = existing is not None and (
            (existing.description or None) != raw.description
            or bool(existing.is_active) != bool(raw.is_active)
        )
        if existing is None:
            action = "create"
        elif dirty:
            action = "update"
        else:
            action = "skip"
        items.append(PlanItem("department", label, label, action, ""))
    return items


def _wanted_owner_id(db: Session, row: ModelRegistry, item: ModelTransfer) -> tuple[int | None, bool]:
    """回傳（用來比對的部門 id，這欄是否不同）。

    部門若還沒建、但路徑會在這次匯入出現，路徑相同就不算變更。
    """
    if not item.owner_department_path:
        return None, row.owner_department_id is not None
    dept = _find_department(db, item.owner_department_path)
    if dept is not None:
        return dept.id, dept.id != row.owner_department_id
    current = db.get(Department, row.owner_department_id) if row.owner_department_id else None
    current_path = _department_path(db, current) if current is not None else None
    return row.owner_department_id, current_path != item.owner_department_path


def _wanted_base_id(db: Session, row: ModelRegistry, item: ModelTransfer) -> tuple[int | None, bool]:
    if not item.base_model_name:
        return None, row.base_model_id is not None
    base = _model_by_name(db, item.base_model_name)
    if base is not None:
        return base.id, base.id != row.base_model_id
    current = db.get(ModelRegistry, row.base_model_id) if row.base_model_id else None
    same = current is not None and current.name.lower() == item.base_model_name.lower()
    return row.base_model_id, not same


def _model_items(db: Session, document: SettingsDocument) -> list[PlanItem]:
    items: list[PlanItem] = []
    for raw in document.models:
        if raw.name.lower() == PLATFORM_ROUTER_NAME:
            continue
        if _endpoint_omitted(raw):
            items.append(PlanItem(
                "model",
                raw.name,
                raw.display_name or raw.name,
                "skip",
                _NEEDS_ENDPOINT,
            ))
            continue
        row = _model_by_name(db, raw.name)
        supplied = _supplied_credential(raw.credential)
        case = (
            _endpoint_userinfo_case(row.endpoint_url or "", raw.endpoint_url or "", supplied)
            if row is not None else ""
        )
        stored = bool(row and row.api_key_secret_ref)
        # 帳密還在原網址上，檔案的 credential_required 只是匯出時的記號。
        needs = (
            case != "keep"
            and _credential_state(raw.credential_required, stored, supplied) == "needs"
        )
        dirty = False
        if row is not None:
            owner_id, owner_dirty = _wanted_owner_id(db, row, raw)
            base_id, base_dirty = _wanted_base_id(db, row, raw)
            dirty = _model_dirty(row, raw, owner_id, base_id) or owner_dirty or base_dirty
        action, message = _action_for(
            row is not None, dirty, needs, writing_key=supplied is not None,
        )
        if case == "keep" and message in {"", _NEEDS_KEY}:
            message = _KEEP_ENDPOINT_SECRET
        elif case == "drop":
            if supplied is not None and message == _WRITE_KEY:
                message = f"{_DROPPED_ENDPOINT_SECRET}；{_WRITE_KEY}"
            else:
                message = _DROPPED_ENDPOINT_SECRET
        items.append(PlanItem("model", raw.name, raw.display_name or raw.name, action, message))
    return items


def _role_items(db: Session, document: SettingsDocument) -> list[PlanItem]:
    items: list[PlanItem] = []
    for raw in document.model_roles:
        spec = get_spec(raw.role)
        if spec is None:
            continue
        label = spec.label
        if _blocked_by_hidden_endpoint(db, document, raw.model_name):
            items.append(PlanItem("model_role", raw.role, label, "skip", _NEEDS_ENDPOINT))
            continue
        current = None
        for resolved in list_roles(db):
            if resolved.spec.role == raw.role:
                current = resolved.model
                break
        if raw.role == "platform_embedding":
            same = current is not None and current.name.lower() == raw.model_name.lower()
            items.append(PlanItem(
                "model_role",
                raw.role,
                label,
                "skip",
                "" if same else _PLATFORM_EMBEDDING_SKIP,
            ))
            continue
        same = current is not None and current.name.lower() == raw.model_name.lower()
        action = "skip" if same else ("update" if current is not None else "create")
        items.append(PlanItem("model_role", raw.role, label, action, ""))
    return items


def _grant_items(db: Session, document: SettingsDocument) -> list[PlanItem]:
    items: list[PlanItem] = []
    for raw in document.router_grants:
        row = _model_by_name(db, raw.model_name)
        label = raw.model_name
        if _blocked_by_hidden_endpoint(db, document, raw.model_name):
            items.append(PlanItem("router_grant", raw.model_name, label, "skip", _NEEDS_ENDPOINT))
            continue
        if row is not None and has_active_all_users_grant(db, row.id):
            action = "skip"
        elif row is not None:
            existing = (
                db.query(RouterModelGrant)
                .filter(
                    RouterModelGrant.model_id == row.id,
                    RouterModelGrant.scope_type == "all",
                )
                .first()
            )
            action = "update" if existing is not None else "create"
        else:
            action = "create"
        items.append(PlanItem("router_grant", raw.model_name, label, action, ""))
    return items


def _external_items(db: Session, document: SettingsDocument) -> list[PlanItem]:
    items: list[PlanItem] = []
    for raw in document.external_services:
        row = db.get(ExternalService, raw.kind)
        label = _EXTERNAL_LABELS.get(raw.kind, raw.kind)
        enabled = bool(row.enabled) if row is not None else False
        base_url = external_svc.display_base_url((row.base_url if row else "") or "")
        protocol = (row.protocol if row else None) or "native"
        openai_model = (row.openai_model if row else None) or "whisper-1"
        dirty = enabled != bool(raw.enabled) or base_url != external_svc.display_base_url(raw.base_url)
        if raw.kind == "speech" and raw.protocol is not None:
            dirty = dirty or protocol != raw.protocol.strip().lower()
        if raw.kind == "speech" and raw.openai_model is not None:
            dirty = dirty or openai_model != raw.openai_model.strip()
        supplied = _supplied_credential(raw.credential)
        stored = bool(row and row.credential_envelope)
        needs = _credential_state(raw.credential_required, stored, supplied) == "needs"
        exists = True  # 缺列時用預設值比，套用時才補列
        action, message = _action_for(
            exists, dirty, needs, writing_key=supplied is not None,
        )
        items.append(PlanItem("external_service", raw.kind, label, action, message))
    return items


def _banner_items(db: Session, document: SettingsDocument) -> list[PlanItem]:
    items: list[PlanItem] = []
    for raw in document.banners:
        row = (
            db.query(Banner)
            .filter(Banner.level == raw.level, Banner.content == raw.content)
            .order_by(Banner.id)
            .first()
        )
        label = raw.content[:40]
        if row is None:
            action = "create"
        elif (
            bool(row.is_active)
            and bool(row.show_on_login) == bool(raw.show_on_login)
            and int(row.sort_order) == int(raw.sort_order)
        ):
            action = "skip"
        else:
            action = "update"
        items.append(PlanItem("banner", f"{raw.level}:{raw.content}", label, action, ""))
    return items


def _address_change_needs_author(db: Session, document: SettingsDocument) -> bool:
    for item in document.models:
        if item.name.lower() == PLATFORM_ROUTER_NAME:
            continue
        if _endpoint_omitted(item):
            continue
        row = _model_by_name(db, item.name)
        if row is None:
            return True
        if _endpoint_userinfo_case(
            row.endpoint_url or "",
            item.endpoint_url or "",
            _supplied_credential(item.credential),
        ) == "keep":
            continue
        if (row.endpoint_url or "").strip() != item.endpoint_url:
            return True
    return False


def plan_import(db: Session, actor: User, document: SettingsDocument) -> list[PlanItem]:
    errors: list[str] = []
    _validate_departments(db, document, errors)
    _validate_models(db, document, errors)
    _validate_roles(db, document, errors)
    _validate_grants(db, document, errors)
    trusted: set[str] = set()
    for host in document.trusted_hosts:
        try:
            trusted.add(TrustedHostCreate(host=host.host, note=host.note).host)
        except ValidationError:
            pass
    _validate_external(document, errors, trusted)
    _validate_banners(document, errors)
    _fail(errors)
    if _address_change_needs_author(db, document):
        require_endpoint_address_author(db, actor)

    items: list[PlanItem] = []
    items.extend(_department_items(db, document))
    items.extend(_host_items(db, document))
    items.extend(_model_items(db, document))
    items.extend(_grant_items(db, document))
    items.extend(_role_items(db, document))
    items.extend(_external_items(db, document))
    items.extend(_banner_items(db, document))
    return items


def _apply_departments(db: Session, actor: User, document: SettingsDocument, decisions: dict[tuple[str, str], PlanItem]) -> None:
    ordered = sorted(document.departments, key=lambda row: (len(row.path), row.path))
    for raw in ordered:
        decision = decisions.get(("department", _path_label(raw.path)))
        if decision is None or decision.action == "skip":
            continue
        parent_id = None
        for index in range(1, len(raw.path)):
            prefix = raw.path[:index]
            node = _find_department(db, prefix)
            if node is None:
                create_department(
                    DepartmentCreate(name=prefix[-1], description=None, parent_id=parent_id),
                    actor,
                    db,
                    commit=False,
                )
                node = _find_department(db, prefix)
            parent_id = node.id if node is not None else None
        existing = _find_department(db, raw.path)
        if existing is None:
            parent = _find_department(db, raw.path[:-1]) if len(raw.path) > 1 else None
            create_department(
                DepartmentCreate(
                    name=raw.path[-1],
                    description=raw.description,
                    parent_id=parent.id if parent is not None else None,
                ),
                actor,
                db,
                commit=False,
            )
            existing = _find_department(db, raw.path)
        if existing is None:
            raise SettingsTransferError([f"部門「{_path_label(raw.path)}」沒有建立"])
        changed = {}
        if (existing.description or None) != raw.description:
            changed["description"] = raw.description
        if bool(existing.is_active) != bool(raw.is_active):
            changed["is_active"] = raw.is_active
        if changed:
            update_department(existing.id, DepartmentUpdate(**changed), actor, db, commit=False)


def _apply_hosts(db: Session, actor: User, document: SettingsDocument, decisions: dict[tuple[str, str], PlanItem]) -> None:
    for raw in document.trusted_hosts:
        parsed = TrustedHostCreate(host=raw.host, note=raw.note)
        decision = decisions.get(("trusted_host", parsed.host))
        if decision is None or decision.action == "skip":
            continue
        trusted_host_service.add_host(
            db,
            host=parsed.host,
            note=parsed.note,
            actor=actor,
            commit=False,
        )


def _apply_models(db: Session, actor: User, document: SettingsDocument, decisions: dict[tuple[str, str], PlanItem]) -> None:
    for raw in document.models:
        decision = decisions.get(("model", raw.name))
        if decision is None or decision.action == "skip":
            continue
        if decision.action == "needs-credential" and _model_by_name(db, raw.name) is not None:
            continue
        _enforce_endpoint_url(raw.endpoint_url)
        _enforce_protocol_endpoint(raw.protocol, raw.endpoint_url)
        owner_id = None
        if raw.owner_department_path:
            dept = _find_department(db, raw.owner_department_path)
            if dept is None:
                raise SettingsTransferError([
                    f"模型「{raw.name}」的所屬部門不存在"
                ])
            owner_id = dept.id
        created = ModelCreate(**_model_payload(raw, owner_department_id=owner_id))
        data = created.model_dump()
        api_key = (data.pop("api_key", None) or "").strip()
        row = _model_by_name(db, raw.name)
        creating = row is None
        if creating:
            row = ModelRegistry(**data)
            row.created_by_user_id = actor.id
            row.is_active = raw.is_active
            if api_key:
                row.api_key_secret_ref = encode_service_token_envelope(api_key)
            db.add(row)
            db.flush()
            log_audit_event_or_raise(
                db,
                actor=actor,
                action="create",
                resource_type="model",
                resource_id=row.id,
                detail=f"匯入建立模型「{row.display_name}」",
                commit=False,
            )
        else:
            data.pop("name", None)
            # 位址只差被拿掉的帳密時，不寫回端點，原網址上的帳密留著。
            if _endpoint_userinfo_case(
                row.endpoint_url or "",
                raw.endpoint_url or "",
                _supplied_credential(raw.credential),
            ) == "keep":
                data.pop("endpoint_url", None)
            deactivating = bool(row.is_active) and not raw.is_active
            for field, value in data.items():
                setattr(row, field, value)
            row.is_active = raw.is_active
            if not raw.is_active:
                row.is_router_primary = False
                row.is_slides_primary = False
                row.is_platform_embedding = False
                row.is_asr_primary = False
            if deactivating:
                mark_agents_base_model_offline(db, row)
            if api_key:
                row.api_key_secret_ref = encode_service_token_envelope(api_key)
            log_audit_event_or_raise(
                db,
                actor=actor,
                action="update",
                resource_type="model",
                resource_id=row.id,
                detail=f"匯入更新模型「{row.display_name}」",
                commit=False,
            )
    # 底層模型等這一輪都有列之後再掛。略過的列不改。
    for raw in document.models:
        if not raw.base_model_name:
            continue
        decision = decisions.get(("model", raw.name))
        if decision is None or decision.action == "skip":
            continue
        if decision.action == "needs-credential":
            row = _model_by_name(db, raw.name)
            if row is None or row.base_model_id is not None:
                continue
        row = _model_by_name(db, raw.name)
        base = _model_by_name(db, raw.base_model_name)
        if row is None or base is None:
            raise SettingsTransferError([f"模型「{raw.name}」的底層模型不存在"])
        if row.base_model_id != base.id:
            row.base_model_id = base.id
    db.flush()


def _apply_grants(db: Session, actor: User, document: SettingsDocument, decisions: dict[tuple[str, str], PlanItem]) -> None:
    for raw in document.router_grants:
        decision = decisions.get(("router_grant", raw.model_name))
        if decision is None or decision.action == "skip":
            continue
        row = _model_by_name(db, raw.model_name)
        if row is None or row.name == PLATFORM_ROUTER_NAME:
            raise SettingsTransferError([f"全院授權的模型「{raw.model_name}」不存在"])
        _validate_grant(db, RouterGrantIn(scope_type="all"))
        existing = (
            db.query(RouterModelGrant)
            .filter(
                RouterModelGrant.model_id == row.id,
                RouterModelGrant.scope_type == "all",
            )
            .first()
        )
        if existing is None:
            db.add(RouterModelGrant(
                model_id=row.id,
                scope_type="all",
                created_by=actor.id,
            ))
        else:
            existing.expires_at = None
        log_audit_event_or_raise(
            db,
            actor=actor,
            action="grant_model_all_users",
            resource_type="model",
            resource_id=row.id,
            detail=f"匯入全院授權「{row.name}」",
            commit=False,
        )
    db.flush()


def _apply_roles(db: Session, actor: User, document: SettingsDocument, decisions: dict[tuple[str, str], PlanItem]) -> None:
    for raw in document.model_roles:
        decision = decisions.get(("model_role", raw.role))
        if decision is None or decision.action == "skip":
            continue
        spec = ROLE_SPECS[raw.role]
        row = _model_by_name(db, raw.model_name)
        if row is None:
            raise SettingsTransferError([f"{spec.label}指到的模型不存在"])
        if spec.storage == "flag":
            if raw.role == "router_primary":
                set_router_primary(row.id, actor, db, commit=False)
            elif raw.role == "slides":
                set_slides_primary(row.id, actor, db, commit=False)
            continue
        if not row.is_active:
            raise HTTPException(status_code=400, detail=inactive_assign_error(spec))
        if (row.model_type or "") not in spec.accepted_types:
            raise HTTPException(status_code=400, detail=type_error(spec))
        assign_table_role(db, spec, row)
        log_audit_event_or_raise(
            db,
            actor=actor,
            action="set_model_role",
            resource_type="model_role",
            resource_id=raw.role,
            detail=f"將{spec.label}設為 {row.display_name}",
            metadata={"role": raw.role, "model_id": row.id, "model_name": row.name},
            commit=False,
        )
    db.flush()


def _apply_external(
    db: Session,
    actor: User,
    document: SettingsDocument,
    decisions: dict[tuple[str, str], PlanItem],
    ip_address: str | None,
) -> None:
    external_svc.ensure_rows(db)
    db.flush()
    for raw in document.external_services:
        decision = decisions.get(("external_service", raw.kind))
        if decision is None or decision.action in {"skip", "needs-credential"}:
            continue
        supplied = _supplied_credential(raw.credential)
        external_svc.update_service(
            db,
            raw.kind,
            enabled=raw.enabled,
            base_url=raw.base_url,
            credential_set=supplied is not None,
            credential=supplied,
            protocol=raw.protocol,
            openai_model=raw.openai_model,
            actor=actor,
            ip_address=ip_address,
            commit=False,
        )


def _apply_banners(db: Session, actor: User, document: SettingsDocument, decisions: dict[tuple[str, str], PlanItem]) -> None:
    for raw in document.banners:
        key = f"{raw.level}:{raw.content}"
        decision = decisions.get(("banner", key))
        if decision is None or decision.action == "skip":
            continue
        _validate_level(raw.level)
        BannerCreate(
            level=raw.level,
            content=raw.content,
            is_active=True,
            show_on_login=raw.show_on_login,
            sort_order=raw.sort_order,
        )
        row = (
            db.query(Banner)
            .filter(Banner.level == raw.level, Banner.content == raw.content)
            .order_by(Banner.id)
            .first()
        )
        if row is None:
            row = Banner(
                level=raw.level,
                content=raw.content,
                is_active=True,
                show_on_login=raw.show_on_login,
                sort_order=raw.sort_order,
                created_by_user_id=actor.id,
            )
            db.add(row)
            db.flush()
            log_audit_event_or_raise(
                db,
                actor=actor,
                action="create_banner",
                resource_type="banner",
                resource_id=row.id,
                detail=(
                    f"張貼公告 [{raw.level}]"
                    + ("(登入頁公開)" if raw.show_on_login else "")
                ),
                commit=False,
            )
        else:
            row.is_active = True
            row.show_on_login = raw.show_on_login
            row.sort_order = raw.sort_order
            log_audit_event_or_raise(
                db,
                actor=actor,
                action="update_banner",
                resource_type="banner",
                resource_id=row.id,
                detail=(
                    f"更新公告 [{raw.level}]"
                    + ("(登入頁公開)" if raw.show_on_login else "")
                ),
                commit=False,
            )
    db.flush()


def _summary(items: list[PlanItem], *, dry_run: bool) -> str:
    counts = {"create": 0, "update": 0, "skip": 0, "needs-credential": 0}
    for item in items:
        counts[item.action] = counts.get(item.action, 0) + 1
    mode = "試算" if dry_run else "套用"
    return (
        f"匯入設定（{mode}）：新增 {counts['create']}、更新 {counts['update']}、"
        f"略過 {counts['skip']}、需填金鑰 {counts['needs-credential']}"
    )


def _trusted_hosts_in_document(document: SettingsDocument) -> set[str]:
    """通過格式檢查的主機。套用當下快取讀的是另一條連線，同一請求還看不到剛寫入的列。"""
    trusted: set[str] = set()
    for host in document.trusted_hosts:
        try:
            trusted.add(TrustedHostCreate(host=host.host, note=host.note).host)
        except ValidationError:
            continue
    return trusted


def run_import(
    db: Session,
    actor: User,
    document: SettingsDocument,
    *,
    dry_run: bool,
    ip_address: str | None,
) -> dict:
    # 試算與套用都把檔案裡的信任主機算進出向檢查。主機仍先經 add_host 寫入。
    with _also_trust(_trusted_hosts_in_document(document)):
        items = plan_import(db, actor, document)
        decisions = {(item.entity, item.key): item for item in items}
        if dry_run:
            log_audit_event_or_raise(
                db,
                actor=actor,
                action="settings_import",
                resource_type="settings",
                resource_id="import",
                detail=_summary(items, dry_run=True),
                ip_address=ip_address,
                commit=True,
            )
        else:
            try:
                _apply_departments(db, actor, document, decisions)
                _apply_hosts(db, actor, document, decisions)
                _apply_models(db, actor, document, decisions)
                _apply_grants(db, actor, document, decisions)
                _apply_roles(db, actor, document, decisions)
                _apply_external(db, actor, document, decisions, ip_address)
                _apply_banners(db, actor, document, decisions)
                log_audit_event_or_raise(
                    db,
                    actor=actor,
                    action="settings_import",
                    resource_type="settings",
                    resource_id="import",
                    detail=_summary(items, dry_run=False),
                    ip_address=ip_address,
                    commit=False,
                )
                db.commit()
            except Exception:
                db.rollback()
                raise
            trusted_host_service._invalidate_cache()
            invalidate_docling_source_cache()
    return {
        "dry_run": dry_run,
        "applied": not dry_run,
        "items": [item.as_dict() for item in items],
    }
