"""每日閒置停用的試算狀態。只有一列。

第一次排程只記試算人數，不停用任何人。下一個 UTC 日才停用。
同一天重跑（含 background leader 重新當選）不覆寫通知人數。
"""
from datetime import datetime

from sqlalchemy import Boolean, Column, Date, DateTime, Integer, String

from app.database import Base


class AccountInactivityState(Base):
    __tablename__ = "account_inactivity_state"

    id = Column(Integer, primary_key=True)
    preview_completed = Column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # pending | previewing | preview | applying | applied
    phase = Column(String(16), nullable=False, default="pending", server_default="pending")
    notice_count = Column(Integer, nullable=False, default=0, server_default="0")
    notice_days = Column(Integer, nullable=False, default=180, server_default="180")
    # 最近一次開始的 UTC 日。做完之後同一天不再重跑；中斷則同一天接著做。
    last_run_on = Column(Date, nullable=True)
    updated_at = Column(DateTime(timezone=True), nullable=True)
