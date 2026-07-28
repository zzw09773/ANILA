"""治理稽核查詢 + CSV 串流匯出。

寫法刻意對齊隔壁 ``app/api/admin_inference_audit.py``(同一張 ``audit_logs``
表的推論視圖):起訖時間、keyset cursor、串流批次、無日期範圍時的 row cap、
UTF-8 BOM、CSV formula 中和、owner-only 遮蔽 —— 全部沿用同一組 helper,不另
創一套風格,也不重抄一份(CSV 中和是安全相關,只能有一份 SSOT)。

**回應形狀刻意不變**:``GET /api/audit-logs`` 仍回裸 list。治理 UI
(``apps/csp-governance-ui``,本次改動範圍外)做 ``logs.value = data`` 後
``v-for``,換成 ``{rows, next_cursor}`` envelope 會直接壞掉。因此 cursor 是
**純輸入**:每一列都帶 ``id`` 與帶時區的 ``created_at``,client 拿最後一列的
這兩個值當下一頁的 ``cursor_created_at`` / ``cursor_id``。等 UI 能一起改時,
再考慮 envelope 或 ``X-Next-Cursor``(後者還需要動 CORS ``expose_headers``)。
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Iterator

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

# SSOT:tz 處理、ILIKE 轉義、CSV formula 中和與隔壁推論視圖共用同一份實作。
# 重抄一份的風險是兩邊漂移(尤其 formula 中和),所以這裡直接 import。
from app.api.admin_inference_audit import (
    _as_utc_aware,
    _csv_neutralize_cell,
    _ilike_literal_pattern,
    _require_tz_aware,
    _serialize_created_at,
)
from app.database import get_db
from app.models.audit_log import AuditLog
from app.models.user import User
from app.schemas.audit_log import AuditLogResponse
from app.services.audit_service import parse_metadata
from app.services.auth_service import is_owner, require_admin

router = APIRouter(prefix="/api/audit-logs", tags=["審計日誌"])

# Owner-only fields. Admins see the audit trail for moderation but the
# IP address and request metadata can leak deployment topology / token
# remnants and are reserved for the platform owner. Non-owner viewers
# get a literal sentinel so the column doesn't silently look "always
# blank" — they can still see who/what/when, just not where/how.
SENSITIVE_REDACTED = "<owner-only>"

# Streaming CSV export: batch size (monkeypatchable in tests) and hard
# cap when the caller omits both ``from`` and ``to`` date filters.
EXPORT_BATCH_SIZE = 1000
EXPORT_MAX_ROWS_WITHOUT_DATE = 50000

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


def _to_naive_utc(value: datetime | None) -> datetime | None:
    """把帶時區的輸入折成 naive-UTC —— 欄位存的就是 naive-UTC。

    直接拿 aware datetime 去比 naive 欄位在 PostgreSQL 上算得對(psycopg 會
    套 offset 再轉 timestamp),但 SQLite 的 DATETIME bind processor **無視
    tzinfo**:``2026-07-25T00:00+08:00`` 會被當成 UTC 去比,差 8 小時。稽核
    查詢的時間範圍不能因為後端不同而給不同答案,所以在進 SQL 前就折平。
    """
    if value is None:
        return None
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _serialize(log: AuditLog, *, caller: User) -> dict:
    show_sensitive = is_owner(caller)
    return {
        "id": log.id,
        "actor_user_id": log.actor_user_id,
        "actor_username": log.actor_username,
        "action": log.action,
        "resource_type": log.resource_type,
        "resource_id": log.resource_id,
        "status": log.status,
        "detail": log.detail,
        "ip_address": log.ip_address if show_sensitive else SENSITIVE_REDACTED,
        "metadata": parse_metadata(log.metadata_json) if show_sensitive else None,
        # 存的是 naive-UTC;不補 offset 的話瀏覽器會當成 local time(台灣差
        # 8 小時),而且 client 沒辦法把它當成合法的 cursor 值回傳。
        "created_at": _as_utc_aware(log.created_at),
    }


def _may_view_inference_rows(caller: User) -> bool:
    """推論列(detail = 使用者完整 prompt)是否可見。

    ``/api/admin/audit/inference`` 用 ``require_inference_audit_viewer`` 把
    prompt 內容鎖在 ``can_view_inference_audit`` 授權後面,而本端點只要
    ``require_admin`` —— 未授權的 admin 從這裡就能讀到同一批 prompt。既然這
    次要加**批次 CSV 匯出**,不補這道門等於把既有小洞放大成整表外流管道。
    因此:沒有授權就把 ``resource_type='inference'`` 濾掉,那些列走它們專屬
    的視圖與匯出。
    """
    return is_owner(caller) or bool(
        getattr(caller, "can_view_inference_audit", False)
    )


def _build_audit_query(
    db: Session,
    *,
    caller: User,
    action: str | None,
    resource_type: str | None,
    actor_username: str | None,
    status: str | None,
    q: str | None,
    from_dt: datetime | None,
    to_dt: datetime | None,
):
    query = db.query(AuditLog)
    if not _may_view_inference_rows(caller):
        query = query.filter(AuditLog.resource_type != "inference")
    if action:
        query = query.filter(AuditLog.action == action)
    if resource_type:
        query = query.filter(AuditLog.resource_type == resource_type)
    if actor_username:
        query = query.filter(AuditLog.actor_username == actor_username)
    if status:
        query = query.filter(AuditLog.status == status)
    if q:
        # ``audit_logs.detail`` 的 trgm GIN index 見 migration r1_0035。
        query = query.filter(
            AuditLog.detail.ilike(_ilike_literal_pattern(q), escape="\\")
        )
    if from_dt is not None:
        query = query.filter(AuditLog.created_at >= from_dt)
    if to_dt is not None:
        query = query.filter(AuditLog.created_at <= to_dt)
    # (created_at DESC, id DESC) 是 keyset cursor 的全序;只有 created_at
    # 的話同秒多列會在分頁邊界重複或漏掉。
    return query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())


def _assert_cursor_pair(
    cursor_created_at: datetime | None, cursor_id: int | None
) -> None:
    if (cursor_created_at is None) != (cursor_id is None):
        raise HTTPException(
            status_code=422,
            detail="cursor_created_at 與 cursor_id 必須成對提供",
        )


def _apply_cursor(
    query,
    *,
    cursor_created_at: datetime | None,
    cursor_id: int | None,
):
    """套 keyset 條件:嚴格小於 (created_at, id)。

    明寫 OR 展開(而非 row-value 比較)是為了 PostgreSQL 與 SQLite 都吃得下,
    與隔壁匯出用的同一套寫法。
    """
    if cursor_created_at is None or cursor_id is None:
        return query
    return query.filter(
        or_(
            AuditLog.created_at < cursor_created_at,
            and_(
                AuditLog.created_at == cursor_created_at,
                AuditLog.id < cursor_id,
            ),
        )
    )


@router.get("", response_model=list[AuditLogResponse])
def list_audit_logs(
    action: str | None = None,
    resource_type: str | None = None,
    actor_username: str | None = None,
    status: str | None = Query(None, regex="^(success|failure)$"),
    q: str | None = Query(None, description="detail 子字串搜尋(大小寫不敏感)"),
    from_: datetime | None = Query(
        None, alias="from", description="created_at >= (需帶時區)"
    ),
    to: datetime | None = Query(None, description="created_at <= (需帶時區)"),
    cursor_created_at: datetime | None = Query(
        None, description="上一頁最後一列的 created_at(需帶時區)"
    ),
    cursor_id: int | None = Query(None, ge=1, description="上一頁最後一列的 id"),
    limit: int = Query(100, ge=1, le=500),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """依條件列出稽核事件(新→舊)。

    分頁:傳回的最後一列的 ``created_at`` / ``id`` 就是下一頁的 cursor。
    keyset 而非 OFFSET —— 稽核表持續寫入,OFFSET 會在併發插入下重複/跳列。
    """
    _assert_cursor_pair(cursor_created_at, cursor_id)
    from_dt = _to_naive_utc(_require_tz_aware(from_, name="from"))
    to_dt = _to_naive_utc(_require_tz_aware(to, name="to"))
    cursor_dt = _to_naive_utc(
        _require_tz_aware(cursor_created_at, name="cursor_created_at")
    )
    query = _build_audit_query(
        db,
        caller=admin,
        action=action,
        resource_type=resource_type,
        actor_username=actor_username,
        status=status,
        q=q,
        from_dt=from_dt,
        to_dt=to_dt,
    )
    query = _apply_cursor(query, cursor_created_at=cursor_dt, cursor_id=cursor_id)
    return [_serialize(log, caller=admin) for log in query.limit(limit).all()]


@router.get("/export")
def export_audit_logs(
    action: str | None = None,
    resource_type: str | None = None,
    actor_username: str | None = None,
    status: str | None = Query(None, regex="^(success|failure)$"),
    q: str | None = Query(None),
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """把同一組篩選條件的稽核事件以 CSV **串流**匯出。

    不先組完整個 body:稽核表是實測最大的表,一次組完會把 worker 記憶體吃掉。
    無日期範圍時硬上限 ``EXPORT_MAX_ROWS_WITHOUT_DATE`` 列並在尾端註明截斷。
    """
    from_dt = _to_naive_utc(_require_tz_aware(from_, name="from"))
    to_dt = _to_naive_utc(_require_tz_aware(to, name="to"))
    query = _build_audit_query(
        db,
        caller=admin,
        action=action,
        resource_type=resource_type,
        actor_username=actor_username,
        status=status,
        q=q,
        from_dt=from_dt,
        to_dt=to_dt,
    )
    show_sensitive = is_owner(admin)
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
        # Keyset cursor on (created_at DESC, id DESC): stable under concurrent
        # inserts (OFFSET would duplicate/skip when new rows arrive mid-export).
        cursor_created_at: datetime | None = None
        cursor_id: int | None = None
        while True:
            batch_q = _apply_cursor(
                query,
                cursor_created_at=cursor_created_at,
                cursor_id=cursor_id,
            )
            batch = batch_q.limit(batch_size).all()
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
                            (row.metadata_json if show_sensitive else ""),
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
            last = batch[-1]
            cursor_created_at = last.created_at
            cursor_id = last.id

        if truncated:
            yield f"# truncated at {row_cap} — 請縮小日期範圍\n"

    return StreamingResponse(
        _iter(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="audit-logs.csv"',
        },
    )
