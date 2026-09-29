"""平台出貨版本與更新稽核。

版本寫在映像裡的 ``/app/VERSION``（開發機打包時寫入）。沒有這個檔就顯示 dev。
內網更新腳本直接對資料庫寫稽核。這支 CLI 留給容器裡手動補記，操作者最長 100 字。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.conversation import Conversation
from app.models.ingestion import IngestionDocument
from app.models.message import Message

_ACTIONS = {"update", "rollback", "adopt"}
_RESULTS = {"success", "failure"}


# 容器裡的檔。測試可改這個路徑，不必寫進真正的 /app。
_CONTAINER_VERSION = Path("/app/VERSION")


def version_file_candidates() -> list[Path]:
    """映像裡的 /app/VERSION 優先。repo 根目錄只在路徑夠長時才算，短路徑不丟 IndexError。"""
    candidates = [_CONTAINER_VERSION]
    here = Path(__file__).resolve()
    # services/csp/app/services/platform_release.py → parents[4] 是 repo 根。
    # 容器布局是 /app/app/services/platform_release.py，parents 不夠長。
    if len(here.parents) > 4:
        candidates.append(here.parents[4] / "VERSION")
    return candidates


def read_platform_version() -> str:
    for path in version_file_candidates():
        try:
            if path.is_file():
                text = path.read_text(encoding="utf-8").strip()
                if text:
                    return text
        except OSError:
            continue
    return "dev"


def record_platform_event(
    db: Session,
    *,
    action: str,
    operator: str,
    from_version: str,
    to_version: str,
    result: str,
) -> AuditLog:
    """寫一筆更新、認領或回復。operator 是主機上的作業系統帳號，不是平台使用者。"""
    if action not in _ACTIONS or result not in _RESULTS:
        raise ValueError("更新紀錄的動作或結果不正確")
    operator = operator.strip()[:100]
    from_version = from_version.strip()
    to_version = to_version.strip()
    if not operator or not from_version or not to_version:
        raise ValueError("更新紀錄缺欄位")
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    event = AuditLog(
        actor_user_id=None,
        actor_username=operator,
        action=f"platform_{action}",
        resource_type="platform_release",
        resource_id=to_version,
        status=result,
        detail=f"{from_version} → {to_version}",
        metadata_json=json.dumps(
            {
                "operator": operator,
                "from_version": from_version,
                "to_version": to_version,
                "result": result,
                "recorded_at": now,
            },
            ensure_ascii=False,
        ),
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def count_created_since(db: Session, since: datetime) -> dict[str, int]:
    """更新之後新增的對話、訊息、文件。文件以入庫時間 ``uploaded_at`` 計算。"""
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    return {
        "conversations": db.query(Conversation).filter(Conversation.created_at > since).count(),
        "messages": db.query(Message).filter(Message.created_at > since).count(),
        "documents": db.query(IngestionDocument).filter(IngestionDocument.uploaded_at > since).count(),
    }
