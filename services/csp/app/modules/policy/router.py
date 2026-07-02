# -*- coding: utf-8 -*-
"""GET /api/policy-decisions — 政策裁決查詢(doc 03 §11 目標新增)。

唯讀、admin tier(admin / owner)限定;沿 audit-logs 的治理查詢慣例。
append-only:本 router 只有 GET,永遠不得新增 PUT / PATCH / DELETE。
過濾參數 ``action`` / ``decision`` 直接用封閉契約 enum 型別,
非法值由 FastAPI 422 擋下(fail-closed,與 record 端同一套字彙)。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.policy_decision import PolicyDecision
from app.models.user import User
from app.schemas.contracts.policy import (
    PolicyAction,
    PolicyDecisionOut,
    PolicyDecisionVerdict,
)
from app.services.auth_service import require_admin

router = APIRouter(prefix="/api/policy-decisions", tags=["政策裁決"])


@router.get("", response_model=list[PolicyDecisionOut])
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
