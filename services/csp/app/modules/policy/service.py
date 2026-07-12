# -*- coding: utf-8 -*-
"""Policy Engine — 裁決紀錄、ceiling 純函式與五級分類 latch core。

Slice 2b-B(裁決紀錄):

1. :func:`record_decision` —— 把一次政策裁決 **append** 進
   ``policy_decisions``(doc 03 §5)。fail-closed:``action`` 九值、
   ``decision`` 三值、``actor_type`` 二值皆以封閉 enum 驗證,非法即
   ``ValueError``、不落任何列;deny 必附可解釋 ``reason``(doc 03 Done
   Criteria 4)。與 audit 的 fail-soft 不同,policy decision 是治理
   Done Criteria 資料 —— 寫不進去就讓例外浮上來,不吞。
2. :func:`evaluate_classification_ceiling` —— doc 08 §10 的判定式
   ``allow if task.level <= ceiling`` 純函式(無 ceiling = 不設限)。

Slice 3a(五級分類 latch core,doc 08 §2/§5–§9/§12):

3. :func:`apply_classification` —— 單向閂鎖:effective = max(current,
   new),**絕不降級**;升級時寫 ClassificationEvent 並泛型更新資源的
   共通四欄位;資源帶舊 boolean latch 時同步鏡射
   (``classified = level >= 機密``,doc 08 §15 Step 3 舊欄位保留為
   compatibility read model)。降級嘗試 = no-op、不寫 event、回 None
   (doc 08 未規定降級嘗試要記 event)。
4. :func:`effective_level` —— 讀資源當前等級(未知資源 fail-closed)。
5. :func:`create_declassification_request` / :func:`decide_declassification`
   —— doc 08 §7 變體 A 最低保證:僅 Admin 可申請、申請人 ≠ 核准人/
   代錄人(雙人原則,無例外)、核准權來自「機密審批權責」指派
   (:func:`has_declassification_authority`,與 owner/admin 技術角色
   脫鉤)、無權責 → 申請維持 pending + audit ``supervisor_missing``
   (fail-closed);紙本核定＋代錄必附公文文號與核定者官職姓名。
   核准生效走 **唯一** 明示繞過單向閂鎖的內部路徑
   (:func:`_apply_approved_declassification`),事件 reason 採文件
   7 值 enum 中的 ``declassification_copy``。

Append-only:本模組 **不提供** 任何 update / delete 介面;
``policy_decisions`` / ``classification_events`` 只 INSERT。
"""

from __future__ import annotations

from datetime import datetime, timezone

from anila_contracts import Classification as ClassificationLevel
from sqlalchemy.orm import Session

from app.models.artifact import Artifact, ExportRecord
from app.models.classification import (
    ClassificationAuthorityAssignment,
    ClassificationEvent,
    DeclassificationRequest,
)
from app.models.conversation import Conversation
from app.models.ingestion import IngestionCollection, IngestionDocument
from app.models.message import Message
from app.models.policy_decision import PolicyDecision
from app.models.source_snapshot import SourceSnapshot
from app.models.task import Task, TaskRun
from app.models.user import User
from app.schemas.contracts.classification import (
    ClassificationEventReason,
    DeclassificationApprovedVia,
    DeclassificationStatus,
)
from app.schemas.contracts.policy import (
    PolicyAction,
    PolicyActorType,
    PolicyDecisionVerdict,
)
from app.services.audit_service import log_audit_event

# actor_id 欄位是 Integer(users.id / service client / agent id 都是整數);
# 非數值的 actor 識別字(理論上不該出現)不丟資料 —— 原文塞進 metadata。
_ACTOR_ID_RAW_KEY = "actor_id_raw"


def _validate_enum(kind: str, value: str, enum_cls) -> str:
    """fail-closed 驗證:非法值拋 ``ValueError``(列出合法值)。"""
    try:
        return enum_cls(value).value
    except ValueError:
        allowed = [member.value for member in enum_cls]
        raise ValueError(
            f"非法的 {kind}:{value!r};合法值為 {allowed}"
        ) from None


def record_decision(
    db: Session,
    *,
    action: str,
    resource_type: str,
    resource_id: str | None,
    decision: str,
    actor_type: str,
    actor_id: str,
    task_id: int | None = None,
    reason: str | None = None,
    matched_policy_ids: list[str] | None = None,
    policy_version: str = "r1",
    metadata: dict | None = None,
    commit: bool = True,
) -> PolicyDecision:
    """追加一筆政策裁決(append-only;doc 03 §5)。

    - ``action`` / ``decision`` / ``actor_type`` 非法 → ``ValueError``,
      不落任何列(fail-closed)。
    - ``decision == "deny"`` 必附非空 ``reason``(doc 03 Done Criteria 4:
      所有 deny 都有可解釋原因)。``matched_policy_ids`` 在 r1 紀錄階段
      允許空(規則引擎未建,hardcoded guard 沒有 policy id 可填)。
    - 預設立即 commit；治理編排器可傳 ``commit=False``，把裁決與
      Task／Audit／Artifact 納入同一個資料庫交易。此時仍會 ``flush``，
      因此任何裁決寫入錯誤都會 fail-closed，而不是延後到回應送出後。
    - 不改寫、不刪除既有列;本模組沒有任何 mutator。
    """
    action_value = _validate_enum("action", action, PolicyAction)
    decision_value = _validate_enum("decision", decision, PolicyDecisionVerdict)
    actor_type_value = _validate_enum("actor_type", actor_type, PolicyActorType)

    if decision_value == PolicyDecisionVerdict.DENY.value and not (
        reason and reason.strip()
    ):
        raise ValueError(
            "policy deny 必須附上可解釋的 reason(doc 03 Done Criteria 4)"
        )

    # 不變式:不動 caller 的物件 —— metadata / matched_policy_ids 一律複本。
    metadata_json: dict | None = dict(metadata) if metadata is not None else None

    actor_id_value: int | None
    try:
        actor_id_value = int(str(actor_id).strip())
    except (TypeError, ValueError):
        actor_id_value = None
        metadata_json = dict(metadata_json or {})
        metadata_json.setdefault(_ACTOR_ID_RAW_KEY, actor_id)

    row = PolicyDecision(
        task_id=task_id,
        actor_type=actor_type_value,
        actor_id=actor_id_value,
        action=action_value,
        resource_type=resource_type,
        resource_id=resource_id,
        decision=decision_value,
        reason=reason,
        matched_policy_ids=list(matched_policy_ids or []),
        policy_version=policy_version,
        metadata_json=metadata_json,
    )
    db.add(row)
    db.flush()
    if commit:
        db.commit()
        db.refresh(row)
    return row


def evaluate_classification_ceiling(
    *, task_level: str, ceiling: str | None
) -> bool:
    """doc 08 §10 判定式:``allow if task.level <= ceiling``。

    純函式,無副作用。``ceiling is None`` = 該資源不設分類上限 → True。
    等級字串一律經 :meth:`ClassificationLevel.from_storage` 解析,
    未知值 fail-closed 拋 ``ValueError``(不得默默放行)。
    """
    level = ClassificationLevel.from_storage(task_level)
    if ceiling is None:
        return True
    return level <= ClassificationLevel.from_storage(ceiling)


# ── Slice 3a:五級分類 latch core(doc 08 §2/§5–§9/§12)───────────────────────

# resource_type → ORM model 的封閉派發表(doc 08 §5 的 11 種資源中,
# 現存表的對應;整數 PK)。未列型別一律 ValueError fail-closed:
# chunk(document_chunks 為 PG-only、非 ORM 建模,走 worker SDK)、
# service_launch(表在 Slice 7)。artifact / export_record 於 Slice 8a 補上。
_RESOURCE_MODELS: dict[str, type] = {
    "task": Task,
    "task_run": TaskRun,  # doc 08 的 AgentRun 現制對應表
    "conversation": Conversation,
    "message": Message,
    "source_snapshot": SourceSnapshot,
    "collection": IngestionCollection,
    "document": IngestionDocument,
    "artifact": Artifact,  # doc 08 §5(Slice 8a)
    "export_record": ExportRecord,  # doc 08 §5(Slice 8a)
}

_DECLASSIFICATION_SOURCE = "declassification_approved"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_resource(db: Session, resource_type: str, resource_id: str):
    """派發 + 取列;未知型別 / 非整數 id / 查無列 一律 ValueError。"""
    model, pk = _resolve_resource_identity(resource_type, resource_id)
    row = db.get(model, pk)
    if row is None:
        raise ValueError(
            f"找不到分類資源 {resource_type}#{resource_id}(fail-closed)"
        )
    return row


def _resolve_resource_identity(resource_type: str, resource_id: str):
    """驗證分類資源型別與主鍵，供 read / locked mutation 共用。"""
    model = _RESOURCE_MODELS.get(resource_type)
    if model is None:
        raise ValueError(
            f"未知的分類資源型別:{resource_type!r};"
            f"合法值為 {sorted(_RESOURCE_MODELS)}(fail-closed)"
        )
    try:
        pk = int(str(resource_id).strip())
    except (TypeError, ValueError):
        raise ValueError(
            f"分類資源 id 必須是整數字串,實得 {resource_id!r}"
        ) from None
    return model, pk


def _resolve_resource_for_update(
    db: Session, resource_type: str, resource_id: str
):
    """鎖住並強制重讀分類資源，避免 identity-map stale lost update。

    ``populate_existing`` 是安全不變量的一部分：呼叫者可能在同一
    Session 先讀過資源；單純 ``db.get`` 會直接回 identity map 的舊值，
    即使 PostgreSQL 已讓另一交易完成更高分類，也可能以舊 current 覆寫。
    mutation path 一律在 ``SELECT ... FOR UPDATE`` 取得列鎖後覆寫現存 ORM
    狀態，再計算 monotonic max。
    """
    model, pk = _resolve_resource_identity(resource_type, resource_id)
    row = (
        db.query(model)
        .populate_existing()
        .filter(model.id == pk)
        .with_for_update()
        .one_or_none()
    )
    if row is None:
        raise ValueError(
            f"找不到分類資源 {resource_type}#{resource_id}(fail-closed)"
        )
    return row


def _mirror_legacy_boolean(row, level: ClassificationLevel) -> None:
    """同步舊 boolean latch(doc 08 §15 Step 3 compatibility read model)。

    鏡射規則:``classified = level >= 機密``。升級方向由
    :func:`apply_classification` 走到這裡;降級方向只有核准生效的
    :func:`_apply_approved_declassification` 會走到(降到機密以下時
    boolean 一併回 false,並清 inherited 旗標,避免舊 read model 殘留
    「已上鎖」假象)。
    """
    if not hasattr(row, "classified"):
        return
    now_classified = level >= ClassificationLevel.CONFIDENTIAL
    row.classified = now_classified
    if now_classified:
        if getattr(row, "classified_at", None) is None:
            row.classified_at = _utcnow()
    else:
        if hasattr(row, "classification_inherited"):
            row.classification_inherited = False


def _write_event(
    db: Session,
    *,
    resource_type: str,
    resource_id: str,
    previous: ClassificationLevel,
    new: ClassificationLevel,
    reason: str,
    actor_type: str,
    actor_id: str,
    task_id: int | None,
) -> ClassificationEvent:
    """落一筆 ClassificationEvent(append-only)並 flush 取 id。"""
    actor_user_id: int | None = None
    if actor_type == PolicyActorType.USER.value:
        try:
            actor_user_id = int(str(actor_id).strip())
        except (TypeError, ValueError):
            actor_user_id = None
    trace_id: str | None = None
    if task_id is not None:
        trace_id = db.query(Task.trace_id).filter(
            Task.id == task_id
        ).scalar()
    event = ClassificationEvent(
        resource_type=resource_type,
        resource_id=str(resource_id),
        previous_level=previous.to_storage(),
        new_level=new.to_storage(),
        reason=reason,
        actor_user_id=actor_user_id,
        trace_id=trace_id,
    )
    db.add(event)
    db.flush()
    return event


def apply_classification(
    db: Session,
    *,
    resource_type: str,
    resource_id: str,
    new_level: str,
    actor_type: str,
    actor_id: str,
    reason: str,
    task_id: int | None = None,
    source: str = "propagation",
    commit: bool = True,
) -> ClassificationEvent | None:
    """單向閂鎖(doc 08 §2):effective = max(current, new),絕不降級。

    - 升級(含首次 latch):寫 ClassificationEvent、更新資源共通四欄位、
      鏡射舊 boolean latch(資源有的話),回傳事件。
    - 維持 / 降級嘗試:no-op —— 不寫 event、不動資源,回 ``None``
      (doc 08 未規定降級嘗試要記 event;降級唯一合法路徑是
      :func:`decide_declassification` 核准後的內部生效)。
    - fail-closed:未知 resource_type / reason / level / actor_type、
      查無資源列 → ``ValueError``,不落任何列。
    - ``reason == "memory_inherited"`` 時同步鏡射舊
      ``classification_inherited`` 旗標(doc 08 §3 bridge)。
    """
    reason_value = _validate_enum("reason", reason, ClassificationEventReason)
    actor_type_value = _validate_enum(
        "actor_type", actor_type, PolicyActorType
    )
    target = ClassificationLevel.from_storage(new_level)
    row = _resolve_resource_for_update(db, resource_type, resource_id)

    current = ClassificationLevel.from_storage(row.classification_level)
    effective = ClassificationLevel.max_of([current, target])
    if effective == current:
        # 維持或降級嘗試:單向閂鎖,不動資源、不寫 event。
        return None

    event = _write_event(
        db,
        resource_type=resource_type,
        resource_id=resource_id,
        previous=current,
        new=effective,
        reason=reason_value,
        actor_type=actor_type_value,
        actor_id=actor_id,
        task_id=task_id,
    )
    row.classification_level = effective.to_storage()
    row.classification_latched_at = _utcnow()
    row.classification_source = source
    row.classification_event_id = event.id
    _mirror_legacy_boolean(row, effective)
    if (
        reason_value == ClassificationEventReason.MEMORY_INHERITED.value
        and hasattr(row, "classification_inherited")
    ):
        row.classification_inherited = True
    db.flush()
    if commit:
        db.commit()
        db.refresh(event)
    return event


def effective_level(
    db: Session, *, resource_type: str, resource_id: str
) -> ClassificationLevel:
    """讀資源當前分類等級;未知型別 / 查無列 fail-closed ``ValueError``。"""
    row = _resolve_resource(db, resource_type, resource_id)
    return ClassificationLevel.from_storage(row.classification_level)


def has_declassification_authority(db: Session, user_id: int) -> bool:
    """「機密審批權責」查核 hook(doc 08 §7 第 2 點,與技術角色脫鉤)。

    讀 ``classification_authority_assignments``(Slice 3a 只由 migration /
    seed 管理列;admin UI 與 per-department scope 細分在 3b)。無有效
    指派 → False(fail-closed;owner/admin 技術角色不自動取得核准權)。
    """
    return (
        db.query(ClassificationAuthorityAssignment.id)
        .filter(
            ClassificationAuthorityAssignment.user_id == user_id,
            ClassificationAuthorityAssignment.is_active.is_(True),
            ClassificationAuthorityAssignment.revoked_at.is_(None),
        )
        .first()
        is not None
    )


def create_declassification_request(
    db: Session,
    *,
    resource_type: str,
    resource_id: str,
    to_level: str,
    requested_by_admin_id: int,
    reason: str,
    proposed_redaction_summary: str | None = None,
) -> DeclassificationRequest:
    """建立降級申請(doc 08 §7–§8)。

    - 僅 Admin 可申請(§7 規則 1–4:一般使用者 / Developer / Service
      Admin 皆不可;本 codebase 角色階層 owner > admin,故 owner 視同
      具 Admin)→ 否則 ``ValueError``。
    - ``to_level`` 必須嚴格低於資源現行等級(這是降級申請)。
    - fail-closed 預設 ``status=pending_supervisor``;生效必經
      :func:`decide_declassification`。
    - audit ``classification.downgrade_requested``,id 掛回
      ``audit_event_ids``。
    """
    requester = db.get(User, requested_by_admin_id)
    if requester is None or requester.role not in ("admin", "owner"):
        raise ValueError(
            "僅 Admin 可建立降級申請(doc 08 §7);"
            f"user#{requested_by_admin_id} 不具 Admin 角色"
        )
    target = ClassificationLevel.from_storage(to_level)
    row = _resolve_resource_for_update(
        db, resource_type=resource_type, resource_id=resource_id
    )
    current = ClassificationLevel.from_storage(row.classification_level)
    if not target < current:
        raise ValueError(
            f"降級申請的目標等級必須低於現行等級:"
            f"{current.to_storage()} → {target.to_storage()} 不是降級"
        )
    if not (reason and reason.strip()):
        raise ValueError("降級申請必須附上理由(doc 08 §8 reason 必填)")

    request = DeclassificationRequest(
        resource_type=resource_type,
        resource_id=str(resource_id),
        from_level=current.to_storage(),
        to_level=target.to_storage(),
        requested_by_admin_id=requested_by_admin_id,
        reason=reason,
        proposed_redaction_summary=proposed_redaction_summary,
        status=DeclassificationStatus.PENDING_SUPERVISOR.value,
        audit_event_ids=[],
    )
    db.add(request)
    db.flush()
    audit = log_audit_event(
        db,
        action="classification.downgrade_requested",
        resource_type=resource_type,
        actor=requester,
        resource_id=resource_id,
        detail=f"申請 {current.to_storage()} → {target.to_storage()}",
        metadata={"declassification_request_id": request.id},
    )
    if audit is not None:
        db.flush()
        request.audit_event_ids = [str(audit.id)]
    db.commit()
    db.refresh(request)
    return request


def _apply_approved_declassification(
    db: Session, *, request: DeclassificationRequest, actor_user_id: int
) -> ClassificationEvent:
    """核准後生效 —— **唯一** 明示繞過單向閂鎖的內部降級路徑。

    doc 08 §9:首選降密副本;通用資源的副本機制與 Artifact 一起在
    後續 slice 落地,3a 先支援 in-place 生效(前提已由
    :func:`decide_declassification` 把關:supervisor approval 完成 +
    audit 明確記錄)。事件 reason 用文件 7 值 enum 的
    ``declassification_copy``;in-place 不產生新資源,
    ``resulting_resource_id`` 留 NULL。
    """
    row = _resolve_resource_for_update(
        db, request.resource_type, request.resource_id
    )
    previous = ClassificationLevel.from_storage(row.classification_level)
    requested_from = ClassificationLevel.from_storage(request.from_level)
    target = ClassificationLevel.from_storage(request.to_level)
    if previous != requested_from:
        raise ValueError(
            "資源分類已在降級申請後變更，原申請失效；"
            f"request#{request.id} 預期 {requested_from.to_storage()}，"
            f"現為 {previous.to_storage()}，必須依現況重新申請(fail-closed)"
        )
    if not target < previous:
        raise ValueError(
            "核准降級的目標必須嚴格低於鎖定後的現行分類；"
            f"實得 {previous.to_storage()} → {target.to_storage()}"
        )
    event = ClassificationEvent(
        resource_type=request.resource_type,
        resource_id=str(request.resource_id),
        previous_level=previous.to_storage(),
        new_level=target.to_storage(),
        reason=ClassificationEventReason.DECLASSIFICATION_COPY.value,
        actor_user_id=actor_user_id,
    )
    db.add(event)
    db.flush()
    row.classification_level = target.to_storage()
    row.classification_latched_at = _utcnow()
    row.classification_source = _DECLASSIFICATION_SOURCE
    row.classification_event_id = event.id
    _mirror_legacy_boolean(row, target)
    return event


def decide_declassification(
    db: Session,
    *,
    request_id: int,
    approver_user_id: int,
    approve: bool,
    via: str,
    comment: str | None = None,
    paper_doc_no: str | None = None,
    authority_title_name: str | None = None,
) -> DeclassificationRequest:
    """裁決降級申請(doc 08 §7 變體 A + §12)。

    最低保證(全部 fail-closed):

    - 申請單必須仍在 ``pending_supervisor``(核准生效恰好一次)。
    - ``via`` 二選一(``in_system`` / ``recorded_paper_decision``)。
    - 雙人原則:申請人 ≠ 核准人 / 代錄人,**系統內無例外**。
    - 核准人必須持「機密審批權責」(:func:`has_declassification_authority`,
      與 owner/admin 技術角色脫鉤);無權責 → 申請 **維持 pending**、
      audit ``supervisor_missing``、原封不動回傳(不升級 owner、
      不同儕核准、不自動放行)。
    - ``recorded_paper_decision`` 必附 ``paper_doc_no``(公文文號/簽呈,
      落 ``authority_reference``)與 ``authority_title_name``
      (核定者官職＋姓名);代錄人落 ``recorded_by_user_id``。
    - 核准:status → approved → 生效(唯一內部降級路徑)→ applied;
      駁回:status → rejected,資源等級不動。
    """
    request = (
        db.query(DeclassificationRequest)
        .populate_existing()
        .filter(DeclassificationRequest.id == request_id)
        .with_for_update()
        .one_or_none()
    )
    if request is None:
        raise ValueError(f"找不到降級申請 #{request_id}(fail-closed)")
    if request.status != DeclassificationStatus.PENDING_SUPERVISOR.value:
        raise ValueError(
            f"降級申請 #{request_id} 已裁決(status={request.status}),"
            "不可重複裁決(核准生效恰好一次)"
        )
    via_value = _validate_enum("approved_via", via, DeclassificationApprovedVia)
    if approver_user_id == request.requested_by_admin_id:
        raise ValueError(
            "申請人 ≠ 核准人/代錄人(doc 08 §7 變體 A 雙人原則,無例外)"
        )
    is_paper = (
        via_value == DeclassificationApprovedVia.RECORDED_PAPER_DECISION.value
    )
    if is_paper:
        if not (paper_doc_no and paper_doc_no.strip()):
            raise ValueError(
                "recorded_paper_decision 必附核定依據公文文號/簽呈"
                "(authority_reference,doc 08 §8)"
            )
        if not (authority_title_name and authority_title_name.strip()):
            raise ValueError(
                "recorded_paper_decision 必附核定者官職＋姓名"
                "(authority_title_name,doc 08 §8)"
            )

    approver = db.get(User, approver_user_id)
    if not has_declassification_authority(db, approver_user_id):
        # fail-closed:維持 pending,不升級、不放行(doc 08 §12)。
        log_audit_event(
            db,
            action="supervisor_missing",
            resource_type="declassification_request",
            actor=approver,
            resource_id=request.id,
            status="denied",
            detail="裁決者未持「機密審批權責」,申請維持 pending_supervisor",
            commit=True,
        )
        return request

    audit_ids = list(request.audit_event_ids or [])

    def _audit(action: str, detail: str) -> None:
        entry = log_audit_event(
            db,
            action=action,
            resource_type=request.resource_type,
            actor=approver,
            resource_id=request.resource_id,
            detail=detail,
            metadata={
                "declassification_request_id": request.id,
                "approved_via": via_value,
            },
        )
        if entry is not None:
            db.flush()
            audit_ids.append(str(entry.id))

    request.decided_at = _utcnow()
    request.supervisor_comment = comment
    if is_paper:
        request.recorded_by_user_id = approver_user_id
        request.authority_reference = paper_doc_no
        request.authority_title_name = authority_title_name
    else:
        request.supervisor_user_id = approver_user_id

    if not approve:
        request.status = DeclassificationStatus.REJECTED.value
        _audit(
            "classification.downgrade_rejected",
            f"駁回 {request.from_level} → {request.to_level}",
        )
        request.audit_event_ids = audit_ids
        db.commit()
        db.refresh(request)
        return request

    request.approved_via = via_value
    request.status = DeclassificationStatus.APPROVED.value
    _audit(
        "classification.downgrade_approved",
        f"核准 {request.from_level} → {request.to_level}",
    )
    if is_paper:
        _audit(
            "classification.downgrade_recorded_paper_decision",
            f"紙本核定代錄:{paper_doc_no}({authority_title_name})",
        )
    _apply_approved_declassification(
        db, request=request, actor_user_id=approver_user_id
    )
    request.status = DeclassificationStatus.APPLIED.value
    _audit(
        "classification.in_place_downgrade_applied",
        f"降級生效:{request.resource_type}#{request.resource_id} → "
        f"{request.to_level}",
    )
    request.audit_event_ids = audit_ids
    db.commit()
    db.refresh(request)
    return request
