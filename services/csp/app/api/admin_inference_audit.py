"""Admin-only inference audit query and CSV export."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Iterator

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.audit_log import AuditLog
from app.models.user import User
from app.services.auth_service import is_owner, require_inference_audit_viewer
from app.services.inference_audit import INFERENCE_ACTIONS

router = APIRouter(prefix="/api/admin/audit", tags=["推論審計"])

# Mirror ``app.api.audit_logs.SENSITIVE_REDACTED`` / ``_serialize``:
# granted non-owner admins may list inference rows, but IP + request
# metadata remain owner-only. ``detail`` stays visible (product choice).
SENSITIVE_REDACTED = "<owner-only>"

# Streaming CSV export: batch size (monkeypatchable in tests) and hard
# cap when the caller omits both ``from`` and ``to`` date filters.
EXPORT_BATCH_SIZE = 1000
EXPORT_MAX_ROWS_WITHOUT_DATE = 50000


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


def _serialize_created_at(value: datetime | None) -> str | None:
    """Serialize AuditLog.created_at as tz-aware UTC ISO8601.

    Stored values are naive-UTC; browsers treat naive ISO as *local* time,
    so we always emit an explicit UTC offset (``+00:00``).
    """
    if value is None:
        return None
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        aware = value.replace(tzinfo=timezone.utc)
    else:
        aware = value.astimezone(timezone.utc)
    return aware.isoformat()


def _as_utc_aware(value: datetime | None) -> datetime | None:
    """Attach UTC tzinfo to naive datetimes for JSON responses."""
    if value is None:
        return None
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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


def _csv_neutralize_cell(value: object) -> str:
    """Neutralize CSV formula injection for every cell.

    Values starting with ``=``, ``+``, ``-``, ``@``, TAB, or CR get a leading
    apostrophe so spreadsheet apps treat them as text.
    """
    text = "" if value is None else str(value)
    if text[:1] in {"=", "+", "-", "@", "\t", "\r"}:
        return "'" + text
    return text


def _serialize_inference_row(row: AuditLog, *, caller: User) -> InferenceAuditRow:
    show_sensitive = is_owner(caller)
    return InferenceAuditRow(
        id=row.id,
        actor_user_id=row.actor_user_id,
        actor_username=row.actor_username,
        action=row.action,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        status=row.status,
        detail=row.detail,
        ip_address=row.ip_address if show_sensitive else SENSITIVE_REDACTED,
        metadata_json=row.metadata_json if show_sensitive else None,
        created_at=_as_utc_aware(row.created_at),
    )


def _reject_non_owner_ip_filter(*, caller: User, ip: str | None) -> None:
    """IP is owner-only; granted admins must not oracle via total/rows."""
    if ip is not None and not is_owner(caller):
        raise HTTPException(
            status_code=422,
            detail="僅擁有者可使用 IP 篩選",
        )


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
    admin: User = Depends(require_inference_audit_viewer),
    db: Session = Depends(get_db),
):
    from_dt = _require_tz_aware(from_, name="from")
    to_dt = _require_tz_aware(to, name="to")
    _reject_non_owner_ip_filter(caller=admin, ip=ip)
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
        rows=[_serialize_inference_row(row, caller=admin) for row in rows],
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
    admin: User = Depends(require_inference_audit_viewer),
    db: Session = Depends(get_db),
):
    from_dt = _require_tz_aware(from_, name="from")
    to_dt = _require_tz_aware(to, name="to")
    _reject_non_owner_ip_filter(caller=admin, ip=ip)
    query = _build_inference_query(
        db,
        username=username,
        ip=ip,
        action=action,
        q=q,
        from_dt=from_dt,
        to_dt=to_dt,
    )
    show_sensitive = is_owner(admin)
    # Unscoped exports (no from/to) are hard-capped so a full-table pull
    # cannot OOM the worker. Date-bounded exports stream without a row cap.
    row_cap = (
        None
        if (from_dt is not None or to_dt is not None)
        else EXPORT_MAX_ROWS_WITHOUT_DATE
    )
    batch_size = EXPORT_BATCH_SIZE

    def _iter() -> Iterator[str]:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        # UTF-8 BOM so Excel opens zh-TW correctly.
        yield "\ufeff"
        writer.writerow(_CSV_FIELDS)
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)

        emitted = 0
        truncated = False
        # Windowed offset batches: stream rows without materialising .all().
        offset = 0
        while True:
            batch = query.offset(offset).limit(batch_size).all()
            if not batch:
                break
            for row in batch:
                if row_cap is not None and emitted >= row_cap:
                    truncated = True
                    break
                writer.writerow(
                    [
                        _csv_neutralize_cell(cell)
                        for cell in (
                            row.id,
                            _serialize_created_at(row.created_at) or "",
                            row.actor_user_id if row.actor_user_id is not None else "",
                            row.actor_username or "",
                            row.action,
                            row.resource_type,
                            row.resource_id or "",
                            row.status,
                            row.detail or "",
                            (
                                row.ip_address
                                if show_sensitive
                                else SENSITIVE_REDACTED
                            ),
                            (
                                row.metadata_json
                                if show_sensitive
                                else ""
                            ),
                        )
                    ]
                )
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate(0)
                emitted += 1
            if truncated:
                break
            if len(batch) < batch_size:
                break
            offset += batch_size

        if truncated:
            yield f"# truncated at {row_cap} — 請縮小日期範圍\n"

    return StreamingResponse(
        _iter(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="inference-audit.csv"',
        },
    )
