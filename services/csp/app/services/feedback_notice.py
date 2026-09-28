"""使用者回饋的未讀筆數。已讀水位依管理員分開記。

清單頁仍用訊息的 created_at 當時間窗。未讀看的是 rated_at：
沒有這個時間的舊評分不算新回饋。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.models.feedback_read import FeedbackReadState
from app.models.message import Message
from app.models.user import User


def _insert(session: Session):
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
        return insert
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
        return insert
    raise RuntimeError(f"不支援的資料庫：{dialect}")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def count_unread_feedback(db: Session, admin: User) -> int:
    query = db.query(func.count(Message.id)).filter(
        Message.rating.isnot(None),
        Message.rated_at.isnot(None),
    )
    state = db.get(FeedbackReadState, admin.id)
    if state is not None:
        query = query.filter(Message.rated_at > state.read_at)
    return int(query.scalar() or 0)


def mark_feedback_read(db: Session, admin: User, read_at: datetime) -> int:
    """把已讀水位推到 read_at，已有更晚的水位就留著。回傳推完後的未讀筆數。

    第一次寫入用 INSERT ... ON CONFLICT，同時兩筆請求不會撞主鍵，
    也不會讓較早的時間蓋掉較晚的。
    """
    read_at = _as_utc(read_at)
    stmt = _insert(db)(FeedbackReadState).values(user_id=admin.id, read_at=read_at)
    incoming = stmt.excluded.read_at
    stmt = stmt.on_conflict_do_update(
        index_elements=["user_id"],
        set_={
            "read_at": case(
                (FeedbackReadState.read_at >= incoming, FeedbackReadState.read_at),
                else_=incoming,
            )
        },
    )
    db.execute(stmt)
    db.flush()
    # SQL 更新不經過身份映射，接著數未讀前先丟掉快取的舊水位。
    db.expire_all()
    return count_unread_feedback(db, admin)
