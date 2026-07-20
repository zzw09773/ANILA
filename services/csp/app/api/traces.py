# -*- coding: utf-8 -*-
"""Trace REST surface (Slice 4a — Full Trace foundation).

doc 05 §6/§13、doc 09 §10、doc 03 §P0(Full Trace gap)。兩條路由,和 ``/v1``
proxy router 同樣「不帶 APIRouter prefix、寫完整路徑」掛載 —— nginx 的 ``/v1``
直通才吃得到 ingest 端:

- ``POST /v1/traces/{trace_id}/spans`` —— data-plane span 收攏。路徑之爭
  (doc 03 寫 ``POST /v1/traces`` vs doc 10 寫 ``…/{trace_id}/spans``)以 doc 09
  收斂:doc 09 §10 逐字列 ``POST /v1/traces/{trace_id}/spans``,故採此。收一批
  TraceSpanIn(1..256)。認證 = 任一 data-plane 憑證(agent integration key /
  service client token / 使用者 JWT / sk- API key);匿名 → 401。
  (trace_id, span_id) 冪等 —— 重複 upsert-ignore。fail-safe:收攏錯誤只侷限本
  端點,不外溢打斷其他流程。
- ``GET /api/traces/{trace_id}`` —— control-plane 讀。認證 = admin/owner 或
  「擁有此 trace 的任務」之申請人(經 ``ensure_task_access``)。trace 既無 span
  又無對應任務 → 404。

邊界:本檔在 ``app.api``,可單向 import ``app.modules.tasks``(api → modules);
不觸犯 modules → api 禁令。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.middleware.caller import _extract_bearer, get_caller
from app.models.source_snapshot import SourceSnapshot
from app.models.task import Task
from app.models.trace_span import TraceSpan
from app.models.user import User
from app.modules.clearance.service import (
    ClearancePolicyDataError,
    resolve_effective_classification_clearance,
    resolve_and_evaluate_data_access_batch,
)
from app.modules.tasks import ensure_task_access
from app.schemas.contracts.traces import SpanProducer, TraceSpanIn, TraceSpanOut
from app.schemas.contracts.tasks import SourceScope
from app.services import agent_credential_service
from app.services.auth_service import get_current_user, is_admin_tier
from anila_contracts import Classification as ClassificationLevel

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Trace"])

# doc 05 §6 Full Trace callback 一次一批的上限。超過 → 413(語義:payload 過大)。
_MAX_SPANS_PER_BATCH = 256


def _positive_id_set(value: object, *, field: str) -> set[int]:
    if not isinstance(value, list) or any(
        isinstance(item, bool) or not isinstance(item, int) or item <= 0
        for item in value
    ):
        raise HTTPException(status_code=403, detail=f"trace {field} 資料無效")
    result = set(value)
    if len(result) != len(value):
        raise HTTPException(status_code=403, detail=f"trace {field} 資料重複")
    return result


def _trace_classification_floor(levels: list[object]) -> ClassificationLevel:
    try:
        return ClassificationLevel.max_of(
            [ClassificationLevel.from_storage(level) for level in levels]
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=403, detail="trace classification 資料無效"
        ) from exc


def _ensure_trace_classification_access(
    db: Session, *, user: User, required_level: ClassificationLevel
) -> None:
    """Require active classification authority without inventing source grants."""

    try:
        authorized = resolve_effective_classification_clearance(
            db,
            user_id=user.id,
        )
    except (ClearancePolicyDataError, ValueError) as exc:
        raise HTTPException(status_code=403, detail="trace clearance 資料無效") from exc
    if authorized is None or authorized < required_level:
        raise HTTPException(
            status_code=403, detail="trace classification clearance 拒絕"
        )


def _ensure_trace_data_access(
    db: Session, *, task: Task, rows: list[TraceSpan], user: User
) -> None:
    """Re-derive source compartments before returning a task-owned trace."""

    try:
        source_scope = SourceScope(task.source_scope)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=403, detail="trace Task source scope 無效"
        ) from exc
    selected_ids = _positive_id_set(
        task.selected_collection_ids, field="Task collection scope"
    )
    classification_levels: list[object] = [
        task.classification_level,
        *(row.classification_level for row in rows),
    ]
    if task.source_snapshot_id is None:
        if source_scope is not SourceScope.NONE or selected_ids:
            raise HTTPException(
                status_code=403, detail="trace 缺少 SourceSnapshot binding"
            )
        _ensure_trace_classification_access(
            db,
            user=user,
            required_level=_trace_classification_floor(classification_levels),
        )
        return

    snapshot = db.get(SourceSnapshot, task.source_snapshot_id)
    if snapshot is None or snapshot.task_id != task.id:
        raise HTTPException(status_code=403, detail="trace Task/Snapshot binding 不符")
    if snapshot.source_scope != source_scope.value:
        raise HTTPException(
            status_code=403, detail="trace Task/Snapshot source scope 不符"
        )
    classification_levels.append(snapshot.classification_level)

    collection_ids = _positive_id_set(
        snapshot.collection_ids, field="Snapshot collection scope"
    )
    document_ids = _positive_id_set(
        snapshot.document_ids, field="Snapshot document scope"
    )
    if not collection_ids:
        if (
            source_scope is not SourceScope.NONE
            or snapshot.origin != "none"
            or document_ids
            or selected_ids
        ):
            raise HTTPException(
                status_code=403, detail="trace Snapshot source scope 不完整"
            )
        _ensure_trace_classification_access(
            db,
            user=user,
            required_level=_trace_classification_floor(classification_levels),
        )
        return
    if source_scope is SourceScope.NONE or snapshot.origin == "none":
        raise HTTPException(status_code=403, detail="trace Snapshot source scope 不符")
    if not collection_ids.issubset(selected_ids):
        raise HTTPException(
            status_code=403, detail="trace Snapshot collection scope 不符"
        )

    required_level = _trace_classification_floor(classification_levels)

    try:
        decisions = resolve_and_evaluate_data_access_batch(
            db,
            user_id=user.id,
            collection_ids=sorted(collection_ids),
            document_ids=sorted(document_ids),
        )
        for decision in decisions.values():
            if (
                not decision.allowed
                or decision.authorized_classification is None
                or decision.authorized_classification < required_level
            ):
                raise HTTPException(
                    status_code=403,
                    detail="trace clearance/compartment 拒絕",
                )
    except (LookupError, ClearancePolicyDataError, ValueError) as exc:
        raise HTTPException(status_code=403, detail="trace clearance 資料無效") from exc


def _naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize to naive UTC (see ``proxy.spans._naive_utc`` — same reason:
    keep persisted timestamps comparable for the trace read ordering)."""
    if dt is not None and dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


class SpanIngestRequest(BaseModel):
    """``POST /v1/traces/{trace_id}/spans`` body —— 一批 span。

    ``min_length=1`` → 空批次由 Pydantic 擋成 422;上限不在此設,改由 handler
    手動判 413(語義區分:結構非法 vs payload 過大)。
    """

    spans: list[TraceSpanIn] = Field(min_length=1)


@dataclass(frozen=True)
class _TracePrincipal:
    producer: str
    user_id: int | None = None
    service_kind: str | None = None


def _ingest_principal(
    request: Request, db: Session = Depends(get_db)
) -> _TracePrincipal:
    """Resolve the data-plane caller and map its type to a span producer role.

    順序:先試 agent integration key / service client token(csk-,經
    ``verify_service_token``),命中就依 kind 給 ``agent`` / ``router``;否則落到
    ``get_caller``(JWT / sk- API key)—— 匿名或無效由 ``get_caller`` fail-closed
    丟 401。``producer`` 一律由 ingest 端依呼叫者身分設定,不收 client 自報
    (與 TraceSpanIn 契約一致)。

    對映:
      agent credential   → ``agent``
      service client     → ``router``(Router / worker 等 s2s 呼叫)
      user JWT / sk- key  → ``proxy``(經 CSP data plane 首方注入,無 user 腳色)
    """
    token = _extract_bearer(request.headers.get("Authorization"))
    if token and not token.startswith("sk-"):
        identity = agent_credential_service.verify_service_token(db, token=token)
        if identity is not None:
            return _TracePrincipal(
                producer=(SpanProducer.AGENT.value
                if identity.kind == "agent"
                else SpanProducer.ROUTER.value),
                service_kind=identity.kind,
            )
    # JWT / sk- API key path — raises 401 when anonymous / invalid.
    caller = get_caller(request, db)
    return _TracePrincipal(
        producer=SpanProducer.PROXY.value, user_id=caller.user.id
    )


@router.post("/v1/traces/{trace_id}/spans", status_code=202)
def ingest_spans(
    trace_id: str,
    request: Request,
    payload: SpanIngestRequest,
    principal: _TracePrincipal = Depends(_ingest_principal),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    """Ingest a batch of spans for ``trace_id`` (idempotent, fail-safe)."""
    spans_in = payload.spans
    if len(spans_in) > _MAX_SPANS_PER_BATCH:
        raise HTTPException(
            status_code=413,
            detail=(
                f"單批 span 數量上限 {_MAX_SPANS_PER_BATCH},"
                f"收到 {len(spans_in)}"
            ),
        )

    # 語義驗證(Pydantic 層做不到):body 若帶 trace_id,必須等於 path trace_id
    # (契約:path 為準)。不符者列出索引,zh-TW detail。
    mismatched = [
        i
        for i, s in enumerate(spans_in)
        if s.trace_id is not None and s.trace_id != trace_id
    ]
    if mismatched:
        raise HTTPException(
            status_code=422,
            detail=f"span 的 trace_id 與路徑不一致(索引 {mismatched})",
        )

    # Formal trace ingest is task-owned.  Free-floating trace ids cannot prove
    # owner/classification and are rejected rather than persisted at 無機密.
    task = db.query(Task).filter(Task.trace_id == trace_id).first()
    if task is None:
        raise HTTPException(status_code=404, detail="trace 不屬於任何 Task")
    if principal.user_id is not None:
        if principal.user_id != task.requester_user_id:
            raise HTTPException(status_code=403, detail="trace owner 不符")
    else:
        raw_task_id = request.headers.get("X-ANILA-Task-Id")
        forwarded_user = request.headers.get("X-ANILA-User-Id")
        owner = db.get(User, task.requester_user_id)
        if (
            raw_task_id != str(task.id)
            or owner is None
            or forwarded_user != owner.username
        ):
            raise HTTPException(
                status_code=403,
                detail="service trace 必須綁定相同 Task 與申請人",
            )
    task_level = ClassificationLevel.from_storage(task.classification_level)
    task_id = task.id

    accepted = 0
    duplicates = 0
    seen: set[str] = set()
    try:
        for s in spans_in:
            if s.span_id in seen:
                duplicates += 1
                continue
            seen.add(s.span_id)
            row = TraceSpan(
                trace_id=trace_id,
                span_id=s.span_id,
                parent_span_id=s.parent_span_id,
                task_id=task_id,
                span_type=s.span_type,
                name=s.name,
                started_at=_naive_utc(s.started_at),
                ended_at=_naive_utc(s.ended_at),
                status=s.status.value,
                attributes=s.attributes,
                producer=principal.producer,
                classification_level=ClassificationLevel.max_of([
                    task_level,
                    s.classification_level or ClassificationLevel.UNCLASSIFIED,
                ]).to_storage(),
            )
            try:
                with db.begin_nested():
                    db.add(row)
                    db.flush()
                accepted += 1
            except IntegrityError:
                # (trace_id, span_id) 已存在 —— 冪等忽略(含並發競態)。
                duplicates += 1
        db.commit()
    except HTTPException:
        raise
    except Exception:
        db.rollback()
        logger.exception("span ingest 失敗 trace_id=%s", trace_id)
        raise HTTPException(status_code=500, detail="span 寫入失敗")

    return {"accepted": accepted, "duplicates": duplicates}


@router.get("/api/traces/{trace_id}")
def get_trace(
    trace_id: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Return the flat, ordered span list for ``trace_id`` (+ owning task)."""
    task = db.query(Task).filter(Task.trace_id == trace_id).first()
    rows = db.query(TraceSpan).filter(TraceSpan.trace_id == trace_id).all()

    if task is None and not rows:
        raise HTTPException(status_code=404, detail="找不到此 trace")

    # 存取控制:有任務走 ensure_task_access(admin/owner 全域 bypass + 申請人);
    # 無任務的 trace 僅 admin/owner 可讀(沒有申請人可比對)。
    if task is not None:
        if any(row.task_id != task.id for row in rows):
            raise HTTPException(status_code=403, detail="trace span/Task binding 不符")
        try:
            ensure_task_access(db, task_id=task.id, user_id=user.id)
        except PermissionError:
            raise HTTPException(status_code=403, detail="無權存取此 trace") from None
        _ensure_trace_data_access(db, task=task, rows=rows, user=user)
    else:
        if any(row.task_id is not None for row in rows):
            raise HTTPException(
                status_code=403, detail="taskless trace span binding 不符"
            )
        if not is_admin_tier(user):
            raise HTTPException(status_code=403, detail="無權存取此 trace")
        _ensure_trace_classification_access(
            db,
            user=user,
            required_level=_trace_classification_floor(
                [row.classification_level for row in rows]
            ),
        )

    # Flat list, sorted by started_at then span_id. started_at 已於寫入端
    # 正規化為 naive UTC;None 排最前(穩定、跨 DB 一致)。
    ordered = sorted(
        rows,
        key=lambda s: (
            s.started_at is None,
            s.started_at or datetime.min,
            s.span_id,
        ),
    )
    return {
        "trace_id": trace_id,
        "task_id": task.id if task is not None else None,
        "spans": [
            TraceSpanOut.model_validate(s).model_dump(mode="json")
            for s in ordered
        ],
    }
