"""使用者上傳的附件，滿 30 天刪掉檔案與抽出的文字。

對話留著。平台產出的長文（origin=generated）跟知識庫文件不在這裡。
30 天是固定常數，不是主控台設定。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session, defer

from app.models.attachment import Attachment
from app.services.audit_service import log_audit_event

logger = logging.getLogger(__name__)

ATTACHMENT_RETENTION_DAYS = 30
ATTACHMENT_EXPIRED_MESSAGE = "已超過保存期限（30 天），檔案已刪除"


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _seconds_until_next_utc_midnight(now: datetime) -> float:
    aware = _aware(now)
    nxt = (aware + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    )
    return max(1.0, (nxt - aware).total_seconds())


def purge_expired_uploads(db: Session, *, now: datetime | None = None) -> int:
    """刪掉滿 30 天的使用者上傳。列留著，檔名還在，內容沒了。

    回傳這一輪清除的筆數。同一列再跑一次不會再算進去。
    稽核只記筆數，不記檔名。
    """
    from app.services.attachment_service import _storage_root

    moment = _aware(now or datetime.now(timezone.utc))
    cutoff = moment - timedelta(days=ATTACHMENT_RETENTION_DAYS)
    rows = (
        db.query(Attachment)
        .options(defer(Attachment.extracted_text))
        .filter(Attachment.extract_status != "expired")
        .filter(or_(Attachment.origin.is_(None), Attachment.origin != "generated"))
        .all()
    )
    root = _storage_root()
    purged = 0
    for row in rows:
        created = getattr(row, "created_at", None)
        if created is None or _aware(created) > cutoff:
            continue
        rel = row.storage_path or ""
        if rel:
            try:
                (root / rel).unlink(missing_ok=True)
            except OSError:
                logger.warning("attachment retention unlink failed id=%s", row.id)
        row.extracted_text = None
        row.token_count = None
        row.page_count = None
        row.extract_status = "expired"
        row.extract_error = ATTACHMENT_EXPIRED_MESSAGE
        purged += 1
    db.commit()
    log_audit_event(
        db,
        action="attachment_retention_purge",
        resource_type="attachment",
        status="success",
        detail=f"已清除 {purged} 份超過保存期限的上傳附件",
        metadata={"purged_count": purged},
        commit=True,
    )
    logger.info("attachment retention purged count=%s", purged)
    return purged


def _purge_once() -> int:
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        return purge_expired_uploads(db)
    finally:
        db.close()


def start_attachment_retention():
    """每天清除一次。啟動時先跑一輪，之後等到下一個 UTC 午夜。"""
    import asyncio

    async def _loop() -> None:
        await asyncio.sleep(0)
        while True:
            try:
                await asyncio.to_thread(_purge_once)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("attachment retention purge failed")
            try:
                delay = _seconds_until_next_utc_midnight(datetime.now(timezone.utc))
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise

    logger.info(
        "附件保存期限背景任務已啟動（上傳滿 %s 天刪檔，產出文件保留）",
        ATTACHMENT_RETENTION_DAYS,
    )
    return asyncio.create_task(_loop())
