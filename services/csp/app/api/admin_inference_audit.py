"""Admin-only inference audit query and CSV export."""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Iterator

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.audit_log import AuditLog
from app.models.user import User
from app.services.auth_service import require_admin
from app.services.inference_audit import INFERENCE_ACTIONS

router = APIRouter(prefix="/api/admin/audit", tags=["推論審計"])


class InferenceAuditRow(BaseModel):
    id: int
    actor_user_id: int | None
    actor_username: str | None
    action: str
    resource_type: str
    resource_id: str | None
    status: str
    detail: str | None
    ip_address: str | None
    metadata_json: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class InferenceAuditListResponse(BaseModel):
    rows: list[InferenceAuditRow]
    total: int


def _require_tz_aware(value: datetime | None, *, name: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise HTTPException(
            status_code=422,
            detail=f"{name} 必須是帶時區的 ISO8601 時間",
        )
    return value


def _ilike_literal_pattern(q: str) -> str:
    """Escape ``%``, ``_``, and ``\\`` so ILIKE matches the literal substring."""
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _build_inference_query(
    db: Session,
    *,
    username: str | None,
    ip: str | None,
    action: str | None,
    q: str | None,
    from_dt: datetime | None,
    to_dt: datetime | None,
):
    query = db.query(AuditLog).filter(
        AuditLog.resource_type == "inference",
        AuditLog.action.in_(sorted(INFERENCE_ACTIONS)),
    )
    if username is not None:
        query = query.filter(AuditLog.actor_username == username)
    if ip is not None:
        query = query.filter(AuditLog.ip_address == ip)
    if action is not None:
        if action not in INFERENCE_ACTIONS:
            raise HTTPException(status_code=422, detail="不支援的 action 過濾值")
        query = query.filter(AuditLog.action == action)
    if q is not None and q != "":
        query = query.filter(
            AuditLog.detail.ilike(_ilike_literal_pattern(q), escape="\\")
        )
    if from_dt is not None:
        query = query.filter(AuditLog.created_at >= from_dt)
    if to_dt is not None:
        query = query.filter(AuditLog.created_at <= to_dt)
    return query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())


@router.get("/inference", response_model=InferenceAuditListResponse)
def list_inference_audit(
    username: str | None = Query(None),
    ip: str | None = Query(None),
    action: str | None = Query(None),
    q: str | None = Query(None),
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    del admin  # authorization side effect only
    from_dt = _require_tz_aware(from_, name="from")
    to_dt = _require_tz_aware(to, name="to")
    base = _build_inference_query(
        db,
        username=username,
        ip=ip,
        action=action,
        q=q,
        from_dt=from_dt,
        to_dt=to_dt,
    )
    total = base.count()
    rows = base.offset(offset).limit(limit).all()
    return InferenceAuditListResponse(
        rows=[InferenceAuditRow.model_validate(row) for row in rows],
        total=total,
    )


_CSV_FIELDS = (
    "id",
    "created_at",
    "actor_user_id",
    "actor_username",
    "action",
    "resource_type",
    "resource_id",
    "status",
    "detail",
    "ip_address",
    "metadata_json",
)


@router.get("/inference/export")
def export_inference_audit(
    username: str | None = Query(None),
    ip: str | None = Query(None),
    action: str | None = Query(None),
    q: str | None = Query(None),
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    del admin
    from_dt = _require_tz_aware(from_, name="from")
    to_dt = _require_tz_aware(to, name="to")
    rows = _build_inference_query(
        db,
        username=username,
        ip=ip,
        action=action,
        q=q,
        from_dt=from_dt,
        to_dt=to_dt,
    ).all()

    def _iter() -> Iterator[str]:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        # UTF-8 BOM so Excel opens zh-TW correctly.
        yield "\ufeff"
        writer.writerow(_CSV_FIELDS)
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        for row in rows:
            writer.writerow(
                [
                    row.id,
                    row.created_at.isoformat() if row.created_at else "",
                    row.actor_user_id if row.actor_user_id is not None else "",
                    row.actor_username or "",
                    row.action,
                    row.resource_type,
                    row.resource_id or "",
                    row.status,
                    row.detail or "",
                    row.ip_address or "",
                    row.metadata_json or "",
                ]
            )
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)

    return StreamingResponse(
        _iter(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="inference-audit.csv"',
        },
    )
