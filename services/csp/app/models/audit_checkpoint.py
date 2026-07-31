"""P2.7 稽核帳每日檢查點(day-granularity hash chain)。

一天一列,不是一列一鏈——熱路徑(``log_audit_event``)完全不碰這張表,
所以稽核寫入成本維持 0ms。鏈頭 (``chain_head``) 會被嵌進稽核匯出檔,
匯出檔一旦離開這台機器就成為資料庫外的錨點;之後任何人(含 superuser)
改寫已錨定區間的稽核列,重算出來的 digest 就對不上,而且對得出是哪一天。

這張表本身也在受保護集合裡(``audit_ledger.AUDIT_LEDGER_TABLES``):
runtime role ``csp_app`` 只有 INSERT / SELECT,不是 owner。
"""
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Column,
    Date,
    DateTime,
    Integer,
    SmallInteger,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.database import Base

JSONValue = JSON().with_variant(JSONB, "postgresql")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AuditCheckpoint(Base):
    """某一個 UTC 日的稽核列摘要 + 鏈頭;append-only。"""

    __tablename__ = "audit_checkpoints"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # 被摘要的那一天(UTC)。一天只會有一列。
    day = Column(Date, nullable=False, unique=True, index=True)
    # 摘要演算法版本;驗證時照這一列記的版本重算,舊檢查點不會因為
    # 日後加欄位而失效。
    digest_version = Column(SmallInteger, nullable=False, default=1)
    # 該日所有受保護稽核列的 SHA-256(不含鏈)。
    day_digest = Column(String(64), nullable=False)
    # 前一個檢查點的 chain_head;第一個檢查點為 64 個 '0'。
    prev_hash = Column(String(64), nullable=False)
    # H(prev_hash | day | day_digest)。
    chain_head = Column(String(64), nullable=False)
    # {"audit_logs": n, "policy_decisions": n, "classification_events": n}
    row_counts = Column(JSONValue, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
