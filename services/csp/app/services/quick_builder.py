"""快製流程的單位權限、版本審核與 Agent 發布。"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import uuid

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.models.agent import Agent, UserAgentPermission
from app.models.department import Department
from app.models.ingestion import IngestionCollection
from app.models.model_registry import ModelRegistry
from app.models.quick_builder import (
    QuickAssistant,
    QuickVersion,
    QuickCollectionPublication,
)
from app.models.user import User
from app.schemas.contracts.classification import ClassificationLevel
from app.services.audit_service import log_audit_event_or_raise
from app.services.auth_service import is_admin_tier
from app.services.collection_scope import can_read_collection
from app.services.unit_admin_service import get_unit_admin_scope_ids


def fail(status, code, message):
    raise HTTPException(status_code=status, detail={"type": code, "message": message})


def audit(db, user, action, assistant_id, **metadata):
    log_audit_event_or_raise(
        db,
        actor=user,
        action="quick_builder." + action,
        resource_type="quick_assistant",
        resource_id=assistant_id,
        metadata=metadata or None,
    )


def can_review(db, user, assistant):
    return is_admin_tier(user) or assistant.department_id in (
        get_unit_admin_scope_ids(db, user) or set()
    )


def can_edit(db, user, assistant):
    return is_admin_tier(user) or (
        assistant.owner_user_id == user.id
        and assistant.department_id == user.department_id
    )


def version_for(db, assistant, version_id):
    row = (
        db.query(QuickVersion)
        .filter_by(id=version_id, assistant_id=assistant.id)
        .first()
    )
    if row is None:
        fail(404, "NOT_FOUND", "找不到這個流程版本")
    return row


def published(db, assistant):
    if assistant.published_revision is None:
        return None
    return (
        db.query(QuickVersion)
        .filter_by(assistant_id=assistant.id, revision=assistant.published_revision)
        .first()
    )


def audience_allows(user, assistant, payload):
    if user.department_id == assistant.department_id:
        return True
    return payload.get("scope") == "public" or (
        payload.get("scope") == "departments"
        and user.department_id in payload.get("target_department_ids", [])
    )


def visible(db, user, assistant):
    if (
        is_admin_tier(user)
        or user.department_id == assistant.department_id
        or can_review(db, user, assistant)
    ):
        return True
    live = published(db, assistant)
    return bool(
        live
        and not assistant.disabled
        and audience_allows(user, assistant, live.payload)
    )


def load(db, user, assistant_id, *, edit=False, review=False, lock=False):
    query = db.query(QuickAssistant).filter_by(id=assistant_id)
    if lock:
        query = query.with_for_update()
    row = query.first()
    if row is None or not visible(db, user, row):
        fail(404, "NOT_FOUND", "找不到這個助手，或目前沒有存取權限")
    if edit and not can_edit(db, user, row):
        fail(403, "EDIT_DENIED", "只有仍在所屬單位的建立者或管理員可以修改")
    if review and not can_review(db, user, row):
        fail(403, "REVIEW_DENIED", "請由所屬單位主管審核")
    return row


def _graph_sources(graph):
    collections, tools = set(), set()
    for node in graph.get("nodes", []):
        config = node.get("config", {})
        if node["kind"] == "rag":
            collections.update(config.get("collection_ids", []))
        if node["kind"] == "mcp" and config.get("tool_id"):
            tools.add(config["tool_id"])
        if node["kind"] == "agent":
            tools.update(config.get("tool_ids", []))
    return collections, tools


def collection_usable(db, user, coll):
    if is_admin_tier(user) or (
        coll.department_id is not None and coll.department_id == user.department_id
    ):
        return can_read_collection(db, user, coll)
    publication = db.get(QuickCollectionPublication, coll.id, populate_existing=True)
    return bool(publication and can_read_collection(db, user, coll))


def validate_sources(db, user, payload, *, complete=False, check_model_access=True):
    from app.services.api_key_service import check_model_permission
    from app.services.quick_mcp import list_tools

    graph = payload["graph"]
    source_ids, tool_ids = _graph_sources(graph)
    declared = set(payload["collection_ids"])
    if not source_ids.issubset(declared):
        fail(422, "SOURCE_NOT_DECLARED", "請先將檢索文件集加入助手的資料來源")
    level = ClassificationLevel.from_storage(payload["classification_level"])
    for source_id in declared:
        # A run retains its Session across awaited services. Never authorize
        # from an identity-map row loaded before a source was revoked/raised.
        coll = db.get(IngestionCollection, source_id, populate_existing=True)
        if (
            coll is None
            or not collection_usable(db, user, coll)
            or coll.status != "active"
        ):
            fail(404, "SOURCE_UNAVAILABLE", "文件集不存在、已停用或目前沒有存取權限")
        if (
            ClassificationLevel.from_storage(coll.classification_level or "無機密")
            > level
        ):
            fail(422, "SOURCE_CLASSIFICATION", "助手密等必須涵蓋所選文件集的密等")
        if complete and (
            coll.department_id is None
            or db.get(QuickCollectionPublication, coll.id, populate_existing=True) is None
        ):
            fail(
                422,
                "SOURCE_GOVERNANCE_REQUIRED",
                "請管理員先確認文件集所屬單位與開放範圍，再使用於助手",
            )
    approved_tools = {item["id"]: item for item in list_tools(db, user)}
    for tool_id in tool_ids:
        if tool_id not in approved_tools:
            fail(404, "TOOL_UNAVAILABLE", "查詢工具不存在、尚未核准或目前沒有存取權限")
        expected = payload.get("tool_fingerprints", {}).get(str(tool_id))
        if expected and expected != approved_tools[tool_id]["fingerprint"]:
            fail(409, "TOOL_CHANGED", "查詢工具的參數已變更，請另存版本並重新送審")
    model = (
        db.get(ModelRegistry, payload.get("model_id"), populate_existing=True)
        if payload.get("model_id")
        else None
    )
    if model is not None:
        if not model.is_active or model.name == "anila-router":
            fail(422, "MODEL_UNAVAILABLE", "請選擇治理中心已啟用的底層模型")
        if check_model_access and not check_model_permission(
            db, user=user, api_key_id=None, model_id=model.id
        ):
            fail(403, "MODEL_DENIED", "目前沒有這個模型的使用權限")
        from app.modules.policy import evaluate_classification_ceiling

        if not evaluate_classification_ceiling(
            task_level=payload["classification_level"],
            ceiling=model.classification_ceiling,
        ):
            fail(403, "CLASSIFICATION_CEILING", "助手密等超過所選模型的分類上限")
    elif complete:
        fail(422, "MODEL_REQUIRED", "請先選擇助手使用的模型")
    if complete:
        for node in graph["nodes"]:
            config = node["config"]
            if (
                (node["kind"] == "rag" and not config.get("collection_ids"))
                or (node["kind"] == "mcp" and not config.get("tool_id"))
                or (node["kind"] == "agent" and not config.get("tool_ids"))
            ):
                fail(
                    422,
                    "CONFIGURATION_REQUIRED",
                    "請先設定檢索文件集與查詢工具，再送審或執行",
                )
    return {
        str(tool_id): approved_tools[tool_id]["fingerprint"] for tool_id in tool_ids
    }


def make_payload(db, user, body):
    from app.services.quick_flow import validate_graph

    raw = (
        body.model_dump(exclude={"expected_revision"})
        if hasattr(body, "model_dump")
        else deepcopy(body)
    )
    try:
        raw["graph"] = validate_graph(raw["graph"])
    except ValueError as exc:
        fail(422, "INVALID_FLOW", str(exc))
    raw["name"] = raw["name"].strip()
    if not raw["name"]:
        fail(422, "NAME_REQUIRED", "請填寫助手名稱")
    for key in ("collection_ids", "target_department_ids"):
        values = raw[key]
        if any(type(v) is not int or v <= 0 for v in values):
            fail(422, "INVALID_SOURCE", "資料來源與單位編號必須是正整數")
        raw[key] = sorted(set(values))
    if raw["scope"] == "departments" and not raw["target_department_ids"]:
        fail(422, "DEPARTMENT_REQUIRED", "請選擇開放使用的單位")
    if raw["scope"] != "departments":
        raw["target_department_ids"] = []
    if raw["target_department_ids"]:
        count = (
            db.query(Department)
            .filter(
                Department.id.in_(raw["target_department_ids"]),
                Department.is_active.is_(True),
            )
            .count()
        )
        if count != len(raw["target_department_ids"]):
            fail(422, "INVALID_DEPARTMENT", "部分指定單位不存在或已停用")
    raw.pop("tool_fingerprints", None)
    raw["tool_fingerprints"] = validate_sources(db, user, raw)
    return raw


def _new_version(db, user, assistant, payload, expected):
    result = db.execute(
        update(QuickAssistant)
        .where(
            QuickAssistant.id == assistant.id, QuickAssistant.draft_revision == expected
        )
        .values(draft_revision=expected + 1)
    )
    if result.rowcount != 1:
        db.rollback()
        fail(409, "VERSION_CONFLICT", "另一個視窗已儲存新版本，請重新載入後再儲存")
    fingerprint = hashlib.sha256(
        json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    version = QuickVersion(
        assistant_id=assistant.id,
        revision=expected + 1,
        payload=deepcopy(payload),
        payload_hash=fingerprint,
        created_by=user.id,
    )
    db.add(version)
    db.flush()
    audit(
        db,
        user,
        "save",
        assistant.id,
        version_id=version.id,
        revision=version.revision,
        payload_hash=fingerprint,
    )
    db.commit()
    db.refresh(assistant)
    return version


def create(db, user, body):
    if not user.department_id or not db.get(Department, user.department_id).is_active:
        fail(403, "DEPARTMENT_REQUIRED", "請先在 ANILA 設定所屬單位")
    payload = make_payload(db, user, body)
    row = QuickAssistant(
        owner_user_id=user.id, department_id=user.department_id, draft_revision=0
    )
    db.add(row)
    db.flush()
    audit(db, user, "create", row.id)
    _new_version(db, user, row, payload, 0)
    return row


def save(db, user, assistant_id, body):
    row = load(db, user, assistant_id, edit=True)
    payload = make_payload(db, user, body)
    _new_version(db, user, row, payload, body.expected_revision)
    return row


def restore(db, user, assistant_id, body):
    row = load(db, user, assistant_id, edit=True)
    original = version_for(db, row, body.version_id)
    payload = make_payload(db, user, original.payload)
    _new_version(db, user, row, payload, body.expected_revision)
    audit(db, user, "restore", row.id, source_version_id=original.id)
    db.commit()
    return row


def submit(db, user, assistant_id, version_id):
    row = load(db, user, assistant_id, edit=True, lock=True)
    version = version_for(db, row, version_id)
    if version.status == "pending" and row.pending_revision == version.revision:
        return row
    if version.status != "draft":
        fail(409, "VERSION_STATE", "這個版本已送審，修改請另存新版本")
    if row.pending_revision is not None:
        fail(409, "REVIEW_PENDING", "已有版本正在審核，請先完成或退回該版本")
    validate_sources(db, user, version.payload, complete=True)
    version.status = "pending"
    row.pending_revision = version.revision
    audit(
        db,
        user,
        "submit",
        row.id,
        version_id=version.id,
        payload_hash=version.payload_hash,
    )
    db.commit()
    return row


def review(db, user, assistant_id, body):
    row = load(db, user, assistant_id, review=True, lock=True)
    version = version_for(db, row, body.version_id)
    target = "approved" if body.approve else "rejected"
    if version.status == target:
        return row
    if row.owner_user_id == user.id:
        fail(403, "SELF_REVIEW", "請由另一位所屬單位主管審核，不能自行核准")
    if version.status != "pending" or row.pending_revision != version.revision:
        fail(409, "VERSION_STATE", "這個版本目前不在待審清單")
    if not body.approve and not body.reason.strip():
        fail(422, "REASON_REQUIRED", "退回時請說明需要調整的地方")
    if body.approve:
        owner = db.get(User, row.owner_user_id)
        if (
            owner is None
            or not owner.is_active
            or owner.department_id != row.department_id
        ):
            fail(409, "OWNER_CHANGED", "建立者已離開原單位，請先處理助手移交")
        validate_sources(db, owner, version.payload, complete=True)
    version.status = target
    version.review_reason = body.reason.strip()
    version.reviewed_by = user.id
    version.reviewed_at = datetime.now(timezone.utc)
    row.pending_revision = None
    audit(
        db,
        user,
        "approve" if body.approve else "reject",
        row.id,
        version_id=version.id,
        payload_hash=version.payload_hash,
    )
    db.commit()
    return row


def publish(db, user, assistant_id, version_id):
    row = load(db, user, assistant_id, edit=True, lock=True)
    version = version_for(db, row, version_id)
    if version.agent_id:
        return row
    if row.disabled and version.revision <= (row.disabled_revision or 0):
        fail(409, "NEW_REVIEW_REQUIRED", "停用後請另存新版本並重新取得主管核准")
    if version.status != "approved":
        fail(409, "REVIEW_REQUIRED", "請先取得單位主管核准，再註冊為 Agent")
    validate_sources(db, user, version.payload, complete=True)
    payload = version.payload
    from app.services.agent_collection_bindings import set_bound_collection_ids

    level = ClassificationLevel.from_storage(payload["classification_level"])
    agent_name = f"quick-{row.id}-v{version.revision}"
    if db.query(Agent.id).filter_by(name=agent_name).first() is not None:
        agent_name += "-" + uuid.uuid4().hex[:12]
    agent = Agent(
        name=agent_name,
        owner_user_id=row.owner_user_id,
        owner_department_id=row.department_id,
        endpoint_url=f"anila://quick-builder/{version.id}",
        runtime_type="custom_http",
        base_model_id=payload["model_id"],
        description_for_router=payload["description"] or payload["name"],
        capabilities={
            "quick_builder_version_id": version.id,
            "display_name": payload["name"],
        },
        approval_status="registered",
        agent_version=str(version.revision),
        default_classification_level=level.to_storage(),
        requires_encryption=level >= ClassificationLevel.RESTRICTED,
    )
    db.add(agent)
    db.flush()
    set_bound_collection_ids(db, agent, payload["collection_ids"])
    version.agent_id = agent.id
    version.status = "registered"
    for member in (
        db.query(User)
        .filter(User.is_active.is_(True), User.is_approved.is_(True))
        .all()
    ):
        if audience_allows(member, row, payload):
            db.add(UserAgentPermission(user_id=member.id, agent_id=agent.id))
    audit(db, user, "register", row.id, version_id=version.id, agent_id=agent.id)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        fail(409, "PUBLICATION_CONFLICT", "這個版本已由另一個視窗註冊，請重新載入")
    return row


def activate_agent(db, user, agent):
    version = db.query(QuickVersion).filter_by(agent_id=agent.id).first()
    if version is None:
        return
    row = (
        db.query(QuickAssistant)
        .filter_by(id=version.assistant_id)
        .populate_existing()
        .with_for_update()
        .one()
    )
    version = (
        db.query(QuickVersion)
        .filter_by(id=version.id)
        .populate_existing()
        .with_for_update()
        .one()
    )
    owner = db.get(User, row.owner_user_id)
    if not owner or not owner.is_active or owner.department_id != row.department_id:
        fail(409, "OWNER_CHANGED", "建立者已離開原單位，請先處理助手移交")
    if (
        version.status == "published"
        and row.published_revision == version.revision
        and not row.disabled
    ):
        validate_sources(db, owner, version.payload, complete=True)
        return
    if version.status != "registered" or (
        row.disabled and version.revision <= (row.disabled_revision or 0)
    ):
        fail(409, "VERSION_STATE", "這個助手版本已停用或不再等待平台核准")
    if row.published_revision is not None and version.revision < row.published_revision:
        fail(409, "STALE_APPROVAL", "已有更新版本發布，請勿核准較舊的候選版本")
    validate_sources(db, owner, version.payload, complete=True)
    previous = published(db, row)
    if previous and previous.agent_id != agent.id:
        old_agent = db.get(Agent, previous.agent_id)
        if old_agent:
            old_agent.approval_status = "disabled"
    version.status = "published"
    row.published_revision = version.revision
    row.disabled = False
    audit(db, user, "publish", row.id, version_id=version.id, agent_id=agent.id)


def quick_agent_allowed(db, user, agent_id):
    version = db.query(QuickVersion).filter_by(agent_id=agent_id).first()
    if version is None:
        return None
    row = db.get(QuickAssistant, version.assistant_id)
    return bool(
        row
        and not row.disabled
        and row.published_revision == version.revision
        and audience_allows(user, row, version.payload)
    )


def disable(db, user, assistant_id):
    row = load(db, user, assistant_id, lock=True)
    if not can_edit(db, user, row) and not can_review(db, user, row):
        fail(403, "DISABLE_DENIED", "只有建立者、主管或管理員可以停用")
    if not row.disabled:
        row.disabled = True
        row.disabled_revision = row.draft_revision
        for version in db.query(QuickVersion).filter_by(assistant_id=row.id).all():
            if version.agent_id:
                db.get(Agent, version.agent_id).approval_status = "disabled"
        audit(db, user, "disable", row.id)
        db.commit()
    return row


def version_out(version):
    return {
        "id": version.id,
        "revision": version.revision,
        **deepcopy(version.payload),
        "status": version.status,
        "agent_id": version.agent_id,
        "review_reason": version.review_reason,
        "created_at": version.created_at.isoformat(),
        "payload_hash": version.payload_hash,
    }


def to_out(db, user, row, *, summary=False):
    internal = (
        is_admin_tier(user)
        or row.department_id == user.department_id
        or can_review(db, user, row)
    )
    query = (
        db.query(QuickVersion)
        .filter_by(assistant_id=row.id)
        .order_by(QuickVersion.revision.desc())
    )
    versions = (
        ([query.first()] if summary else query.all())
        if internal
        else [published(db, row)]
    )
    latest = next((v for v in versions if v), None)
    return {
        "id": row.id,
        "owner_user_id": row.owner_user_id,
        "department_id": row.department_id,
        "name": latest.payload["name"] if latest else "",
        "description": latest.payload["description"] if latest else "",
        "draft_revision": row.draft_revision if internal else None,
        "pending_revision": row.pending_revision if internal else None,
        "published_revision": row.published_revision,
        "disabled": row.disabled,
        "can_edit": can_edit(db, user, row),
        "can_review": can_review(db, user, row) and user.id != row.owner_user_id,
        "versions": [version_out(v) for v in versions if v]
        if internal and not summary
        else [],
    }
