import csv
import io
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.audit_log import AuditLog
from app.models.user import User
from app.schemas.audit_log import AuditLogResponse
from app.services import audit_ledger
from app.services.audit_service import serialize_audit_log
from app.services.auth_service import require_admin

router = APIRouter(prefix="/api/audit-logs", tags=["審計日誌"])

_EXPORT_COLUMNS = (
    "id", "created_at", "actor_user_id", "actor_username", "action",
    "resource_type", "resource_id", "status", "detail", "ip_address",
)


def _csv_safe(value) -> str:
    """試算表公式注入防護:``=``/``+``/``-``/``@`` 開頭的欄位前面加單引號。

    不含 ``\\t`` / ``\\r``。行為由測試鎖在這四個字元；其他匯出站點走
    ``app.utils.csv_formula.csv_formula_safe``（OWASP 六字元閉集）。
    """
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def _one_physical_line(value) -> str:
    """Export-time only: collapse CR/LF so a stored field cannot split rows.

    Does not change registration or username charset.
    """
    return ("" if value is None else str(value)).replace("\r", "").replace("\n", "")


@router.get("", response_model=list[AuditLogResponse])
def list_audit_logs(
    action: str | None = None,
    resource_type: str | None = None,
    actor_username: str | None = None,
    status: str | None = Query(None, regex="^(success|failure)$"),
    limit: int = Query(100, ge=1, le=500),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    query = db.query(AuditLog).order_by(AuditLog.created_at.desc())
    if action:
        query = query.filter(AuditLog.action == action)
    if resource_type:
        query = query.filter(AuditLog.resource_type == resource_type)
    if actor_username:
        query = query.filter(AuditLog.actor_username == actor_username)
    if status:
        query = query.filter(AuditLog.status == status)
    return [
        serialize_audit_log(log, caller=admin, db=db)
        for log in query.limit(limit).all()
    ]


@router.get("/export")
def export_audit_logs(
    limit: int = Query(10000, ge=1, le=200000),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """SYSTEM-MAP §8「要能匯出給稽核單位」的匯出檔 —— **同時是鏈頭的錨點**。

    P2.7:檔頭印出目前的稽核鏈鏈頭與涵蓋區間。這份檔案一旦交出去(給稽核
    單位、給長官的月報附件),就成為這台機器上的人**碰不到的東西** —— 之後
    任何對已錨定區間的改寫,``verify_audit_chain --head <那個鏈頭>`` 都會
    對不上,而且指得出是哪一天。

    操作者每個月要為此多做的事:**沒有**。匯出本來就要做,鏈頭是自動印上去的。
    """
    anchor = audit_ledger.current_anchor(db)
    buf = io.StringIO()
    buf.write("﻿")  # BOM：Excel 開繁中 CSV 不亂碼
    now = datetime.now(timezone.utc).isoformat()
    header_lines = [
        "# ANILA 稽核匯出（append-only 稽核帳）",
        f"# 匯出時間(UTC): {now}",
        f"# 匯出者: {_csv_safe(_one_physical_line(admin.username))}",
        f"# 稽核鏈鏈頭: {anchor.chain_head}",
    ]
    if anchor.anchored:
        header_lines += [
            f"# 涵蓋檢查點: {anchor.first_day} .. {anchor.last_day}"
            f"（{anchor.checkpoint_count} 天）",
            "# 已錨定列數: "
            + ", ".join(f"{t}={n}" for t, n in sorted(anchor.row_counts.items())),
        ]
    else:
        header_lines.append(
            "# ⚠ 尚無檢查點：這份匯出還沒有可比對的鏈頭，"
            "第一個檢查點會在明天 UTC 00:05 產生。"
        )
    header_lines += [
        "# 驗證方式（在 csp 容器內）:",
        "#   python scripts/verify_audit_chain.py --head <上面那串鏈頭>",
        "# 註：鏈頭只證明「已錨定區間沒有被改過」。持有本機 superuser 的人"
        "仍可改寫最後一個檢查點之後的資料（≤24h 盲區），此為單機拓撲的極限。",
    ]
    for line in header_lines:
        buf.write(_one_physical_line(line) + "\n")

    writer = csv.writer(buf)
    writer.writerow(_EXPORT_COLUMNS)
    rows = (
        db.query(AuditLog)
        .order_by(AuditLog.id.asc())
        .limit(limit)
        .all()
    )
    for log in rows:
        data = serialize_audit_log(log, caller=admin, db=db)
        writer.writerow([_csv_safe(data.get(c)) for c in _EXPORT_COLUMNS])

    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                'attachment; filename="anila_audit_export.csv"'
            ),
            # 讓下載端不必開檔就能記錄錨點。
            "X-Anila-Audit-Chain-Head": anchor.chain_head,
        },
    )
