"""模型單價。一列一個生效時點，改價只新增，不覆寫。

金額是每百萬 token 的 micros（貨幣 × 1_000_000）。三個欄位都空代表
從那個時點起「未計價」，報表不當成 0。
"""

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Index, Integer

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ModelPrice(Base):
    __tablename__ = "model_prices"
    __table_args__ = (
        Index("ix_model_prices_model_effective", "model_id", "effective_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    model_id = Column(
        Integer,
        ForeignKey("model_registry.id", ondelete="CASCADE"),
        nullable=False,
    )
    input_micros = Column(BigInteger, nullable=True)
    output_micros = Column(BigInteger, nullable=True)
    reasoning_micros = Column(BigInteger, nullable=True)
    effective_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    created_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
