# -*- coding: utf-8 -*-
"""Policy Engine 治理 API(doc 03 §11、doc 08 §7/§8/§12、doc 09 §11)。

三組面(掛在同一個 package 根 ``router`` 下,各帶自己的前綴):

1. ``GET /api/policy-decisions`` —— 政策裁決唯讀查詢(admin tier;沿
   audit-logs 慣例)。append-only:只有 GET,永遠不新增 PUT/PATCH/DELETE。
2. ``/api/classification/declassification-requests`` —— 降級申請三段式
   (doc 09 §11 路由形狀:建立 / 列表 / approve / reject)。申請僅 Admin
   (ADR-0005「上鎖後僅 Admin 可申請降級」);裁決繞開平台角色,改由
   「機密審批權責」把關(§7.2 脫鉤)。API 層在 service guard 之上再明確
   暴露:申請人 ≠ 核准/駁回人(403)、核准人無權責 → 申請維持 pending
   + 403(fail-closed)、紙本核定缺文號/官職姓名 → 422。每次裁決落一筆
   ``PolicyDecision``(action=``classification.downgrade_request`` —— 九值
   enum 中唯一的分類動作,無 ``declassify`` 這種值)。
3. ``/api/classification-authorities`` —— 「機密審批權責」指派管理
   (doc 08 §7.3 信任錨、§12)。授予/撤銷 owner-only(§7.2 owner 管理指派)、
   授予必附核定依據(公文文號/簽呈)、雙人控制(owner 登錄 → 另一名
   admin 以 ``/{id}/confirm`` 確認才生效;登錄人 ≠ 確認人)。撤銷為 soft
   (寫 ``revoked_at`` + ``is_active=false``)。指派生效 = ``is_active and
   revoked_at is None`` —— 與 3a ``has_declassification_authority`` 一致
   (登錄後未確認的列 ``is_active=false``,故 hook 自然視為未生效)。

邊界:本 module 內部的 sub-router 皆以 ``_`` 前綴(不外露);處理函式命名
避開 mutator 標記(append-only 公開面約束,見 tests/test_policy_module.py)。
service 的降級三件組與裁決紀錄由本 module 內部 import;audit / auth 走
``app.services``(與 service.py 同慣例,不觸犯 modules→api 分層)。
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.classification import (
    ClassificationAuthorityAssignment,
    DeclassificationRequest,
)
from app.models.policy_decision import PolicyDecision
from app.models.user import User
from app.modules.policy.service import (
    create_declassification_request,
    decide_declassification,
    record_decision,
)
from app.schemas.contracts.classification import (
    ClassificationAuthorityCreate,
    ClassificationAuthorityOut,
    DeclassificationApproveBody,
    DeclassificationApprovedVia,
    DeclassificationRejectBody,
    DeclassificationRequestCreate,
    DeclassificationRequestOut,
    DeclassificationStatus,
)
from app.schemas.contracts.policy import (
    PolicyAction,
    PolicyActorType,
    PolicyDecisionOut,
    PolicyDecisionVerdict,
)
from app.services.audit_service import log_audit_event
from app.services.auth_service import (
    get_current_user,
    require_admin,
    require_owner,
)

# package 根 router(無前綴);三組面各自帶前綴掛進來。
router = APIRouter()


# ── GET /api/policy-decisions(既有,唯讀 append-only)────────────────────────

_policy_decisions_router = APIRouter(
    prefix="/api/policy-decisions", tags=["政策裁決"]
)


@_policy_decisions_router.get("", response_model=list[PolicyDecisionOut])
def list_policy_decisions(
    action: PolicyAction | None = None,
    task_id: int | None = None,
    decision: PolicyDecisionVerdict | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[PolicyDecision]:
    """列出政策裁決,新到舊;支援 action / task / decision / 時間範圍過濾。"""
    query = db.query(PolicyDecision).order_by(
        PolicyDecision.created_at.desc(), PolicyDecision.id.desc(),
    )
    if action is not None:
        query = query.filter(PolicyDecision.action == action.value)
    if task_id is not None:
        query = query.filter(PolicyDecision.task_id == task_id)
    if decision is not None:
        query = query.filter(PolicyDecision.decision == decision.value)
    if created_from is not None:
        query = query.filter(PolicyDecision.created_at >= created_from)
    if created_to is not None:
        query = query.filter(PolicyDecision.created_at <= created_to)
    return query.offset(offset).limit(limit).all()


# ── 降級申請(doc 08 §7/§8/§12、doc 09 §11)─────────────────────────────────

_declassification_router = APIRouter(
    prefix="/api/classification/declassification-requests",
    tags=["機敏分類降級"],
)


@_declassification_router.post(
    "", response_model=DeclassificationRequestOut, status_code=201
)
def submit_declassification_request(
    body: DeclassificationRequestCreate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> DeclassificationRequest:
    """建立降級申請(ADR-0005:上鎖後僅 Admin 可申請)。

    fail-closed 預設 ``pending_supervisor``;service 再驗申請資格 / 目標等級
    嚴格低於現行等級 / reason 必填,任一不合 → 422。
    """
    try:
        return create_declassification_request(
            db,
            resource_type=body.resource_type,
            resource_id=body.resource_id,
            to_level=body.requested_level.value,
            requested_by_admin_id=admin.id,
            reason=body.reason,
            proposed_redaction_summary=body.proposed_redaction_summary,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@_declassification_router.get(
    "", response_model=list[DeclassificationRequestOut]
)
def list_declassification_requests(
    status_filter: DeclassificationStatus | None = Query(None, alias="status"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[DeclassificationRequest]:
    """列出降級申請,新到舊;可依 ``status`` 過濾(非法值 FastAPI 422)。"""
    query = db.query(DeclassificationRequest).order_by(
        DeclassificationRequest.created_at.desc(),
        DeclassificationRequest.id.desc(),
    )
    if status_filter is not None:
        query = query.filter(
            DeclassificationRequest.status == status_filter.value
        )
    return query.offset(offset).limit(limit).all()


def _decide_and_record(
    db: Session,
    *,
    request_id: int,
    approver: User,
    approve: bool,
    via: DeclassificationApprovedVia,
    authority_reference: str | None,
    authority_title_name: str | None,
    comment: str | None,
) -> DeclassificationRequest:
    """approve / reject 共用裁決 + 記帳,把 service guard 明確暴露成 HTTP 碼。"""
    request = db.get(DeclassificationRequest, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="找不到降級申請")
    if request.status != DeclassificationStatus.PENDING_SUPERVISOR.value:
        raise HTTPException(
            status_code=409,
            detail=f"降級申請已裁決(狀態 {request.status}),不可重複裁決",
        )
    # 雙人原則:申請人 ≠ 核准/駁回人(doc 08 §7 變體 A,無例外)。
    if approver.id == request.requested_by_admin_id:
        raise HTTPException(
            status_code=403,
            detail="申請人不得核准或駁回自己的降級申請(雙人原則)",
        )
    # 紙本核定必附核定依據公文文號/簽呈與核定者官職＋姓名(fail-closed 422)。
    if (
        approve
        and via == DeclassificationApprovedVia.RECORDED_PAPER_DECISION
    ):
        if not (authority_reference and authority_reference.strip()):
            raise HTTPException(
                status_code=422,
                detail="紙本核定必附核定依據公文文號/簽呈(authority_reference)",
            )
        if not (authority_title_name and authority_title_name.strip()):
            raise HTTPException(
                status_code=422,
                detail="紙本核定必附核定者官職＋姓名(authority_title_name)",
            )
    try:
        updated = decide_declassification(
            db,
            request_id=request_id,
            approver_user_id=approver.id,
            approve=approve,
            via=via.value,
            comment=comment,
            paper_doc_no=authority_reference,
            authority_title_name=authority_title_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    # fail-closed:核准/駁回人未持「機密審批權責」→ service 維持 pending 並
    # audit ``supervisor_missing``;此處把它明確暴露為 403(申請維持待核准)。
    if updated.status == DeclassificationStatus.PENDING_SUPERVISOR.value:
        raise HTTPException(
            status_code=403,
            detail="核准者未持「機密審批權責」,申請維持待核准(fail-closed)",
        )
    verdict = (
        PolicyDecisionVerdict.ALLOW if approve else PolicyDecisionVerdict.DENY
    )
    record_decision(
        db,
        action=PolicyAction.CLASSIFICATION_DOWNGRADE_REQUEST.value,
        resource_type=request.resource_type,
        resource_id=str(request.resource_id),
        decision=verdict.value,
        actor_type=PolicyActorType.USER.value,
        actor_id=str(approver.id),
        reason=comment or ("核准降級申請" if approve else "駁回降級申請"),
        metadata={
            "declassification_request_id": request_id,
            "approved_via": via.value,
        },
    )
    return updated


@_declassification_router.post(
    "/{request_id}/approve", response_model=DeclassificationRequestOut
)
def approve_declassification_request(
    request_id: int,
    body: DeclassificationApproveBody,
    approver: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DeclassificationRequest:
    """核准降級(§7.2:核准權來自「機密審批權責」,與平台角色脫鉤)。"""
    return _decide_and_record(
        db,
        request_id=request_id,
        approver=approver,
        approve=True,
        via=body.via,
        authority_reference=body.authority_reference,
        authority_title_name=body.authority_title_name,
        comment=body.comment,
    )


@_declassification_router.post(
    "/{request_id}/reject", response_model=DeclassificationRequestOut
)
def reject_declassification_request(
    request_id: int,
    body: DeclassificationRejectBody,
    approver: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DeclassificationRequest:
    """駁回降級;資源等級不動(單向閂鎖)。駁回同受權責/雙人把關。"""
    return _decide_and_record(
        db,
        request_id=request_id,
        approver=approver,
        approve=False,
        via=DeclassificationApprovedVia.IN_SYSTEM,
        authority_reference=None,
        authority_title_name=None,
        comment=body.reason,
    )


# ── 「機密審批權責」指派(doc 08 §7.3 信任錨、§12)──────────────────────────

_authorities_router = APIRouter(
    prefix="/api/classification-authorities", tags=["機密審批權責"]
)


def _authority_out(
    row: ClassificationAuthorityAssignment,
) -> ClassificationAuthorityOut:
    """組讀出契約,補算 ``is_effective``(不 mutate ORM 列)。"""
    out = ClassificationAuthorityOut.model_validate(row)
    return out.model_copy(
        update={
            "is_effective": bool(row.is_active) and row.revoked_at is None
        }
    )


@_authorities_router.get("", response_model=list[ClassificationAuthorityOut])
def list_classification_authorities(
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[ClassificationAuthorityOut]:
    """列出權責指派清單(admin tier;供保密單位定期覆核 attestation)。"""
    rows = (
        db.query(ClassificationAuthorityAssignment)
        .order_by(ClassificationAuthorityAssignment.id.desc())
        .all()
    )
    return [_authority_out(row) for row in rows]


@_authorities_router.post(
    "", response_model=ClassificationAuthorityOut, status_code=201
)
def grant_classification_authority(
    body: ClassificationAuthorityCreate,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
) -> ClassificationAuthorityOut:
    """登錄一筆權責指派(owner-only);待另一名 admin 確認才生效(雙人控制)。

    授予必附核定依據(公文文號/簽呈);登錄後 ``is_active=false`` →
    ``has_declassification_authority`` 尚未視為生效(§7.3)。
    """
    if not (body.authority_reference and body.authority_reference.strip()):
        raise HTTPException(
            status_code=422,
            detail="指派機密審批權責必附核定依據(公文文號/簽呈)",
        )
    target = db.get(User, body.user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="指派對象使用者不存在")
    row = ClassificationAuthorityAssignment(
        user_id=body.user_id,
        department_id=body.department_id,
        authority_reference=body.authority_reference,
        granted_by_user_id=owner.id,
        confirmed_by_user_id=None,
        is_active=False,  # 待第二人確認才生效(雙人控制)
    )
    db.add(row)
    db.flush()
    log_audit_event(
        db,
        action="classification.authority_assignment_changed",
        resource_type="classification_authority",
        actor=owner,
        resource_id=row.id,
        detail=(
            f"登錄機密審批權責指派(待確認)user#{body.user_id}"
            f" 依據 {body.authority_reference}"
        ),
        metadata={"assignment_id": row.id, "state": "pending_confirm"},
    )
    db.commit()
    db.refresh(row)
    return _authority_out(row)


@_authorities_router.post(
    "/{assignment_id}/confirm", response_model=ClassificationAuthorityOut
)
def confirm_classification_authority(
    assignment_id: int,
    confirmer: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> ClassificationAuthorityOut:
    """第二人確認權責指派(§7.3 雙人控制:登錄人 ≠ 確認人),確認後生效。"""
    row = db.get(ClassificationAuthorityAssignment, assignment_id)
    if row is None:
        raise HTTPException(status_code=404, detail="找不到權責指派")
    if row.revoked_at is not None:
        raise HTTPException(status_code=409, detail="權責指派已撤銷,無法確認")
    if row.confirmed_by_user_id is not None:
        raise HTTPException(status_code=409, detail="權責指派已確認")
    if row.granted_by_user_id == confirmer.id:
        raise HTTPException(
            status_code=403,
            detail="建立權責指派者不得確認自己建立的指派(雙人控制)",
        )
    row.confirmed_by_user_id = confirmer.id
    row.is_active = True
    log_audit_event(
        db,
        action="classification.authority_assignment_changed",
        resource_type="classification_authority",
        actor=confirmer,
        resource_id=row.id,
        detail=f"確認機密審批權責指派(生效)assignment#{row.id}",
        metadata={"assignment_id": row.id, "state": "confirmed"},
    )
    db.commit()
    db.refresh(row)
    return _authority_out(row)


@_authorities_router.delete(
    "/{assignment_id}", response_model=ClassificationAuthorityOut
)
def deactivate_classification_authority(
    assignment_id: int,
    owner: User = Depends(require_owner),
    db: Session = Depends(get_db),
) -> ClassificationAuthorityOut:
    """撤銷權責指派(owner-only,soft:寫 ``revoked_at`` + ``is_active=false``)。

    冪等:已撤銷的列再打一次是 no-op。撤銷後
    ``has_declassification_authority`` 立即視為未生效(fail-closed)。
    """
    row = db.get(ClassificationAuthorityAssignment, assignment_id)
    if row is None:
        raise HTTPException(status_code=404, detail="找不到權責指派")
    if row.revoked_at is None:
        row.revoked_at = datetime.now(timezone.utc)
        row.is_active = False
        log_audit_event(
            db,
            action="classification.authority_assignment_changed",
            resource_type="classification_authority",
            actor=owner,
            resource_id=row.id,
            detail=f"撤銷機密審批權責指派 assignment#{row.id}",
            metadata={"assignment_id": row.id, "state": "revoked"},
        )
        db.commit()
        db.refresh(row)
    return _authority_out(row)


router.include_router(_policy_decisions_router)
router.include_router(_declassification_router)
router.include_router(_authorities_router)
