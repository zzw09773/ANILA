from datetime import datetime
from pydantic import BaseModel
from app.schemas.base import ApiResponseModel


class AlertResponse(ApiResponseModel):
    id: int
    category: str
    severity: str
    source_type: str | None = None
    source_id: str | None = None
    title: str
    message: str
    status: str
    metadata: dict | None = None
    first_seen_at: datetime
    last_seen_at: datetime
    acknowledged_at: datetime | None = None
    acknowledged_by_user_id: int | None = None
    acknowledged_by_username: str | None = None
    resolved_at: datetime | None = None


class InactivityNotice(BaseModel):
    """最近一次閒置排程要給治理頁的藍色通知。count 為 0 時整段省略。"""

    phase: str
    count: int
    days: int


class AlertSummary(BaseModel):
    open_count: int
    acknowledged_count: int
    resolved_count: int
    high_count: int
    highest_open_severity: str | None = None
    # 跟警報同一支摘要，治理中心橫幅不用再多打一輪。
    unread_feedback_count: int = 0
    inactivity_notice: InactivityNotice | None = None


class AlertStatusUpdate(BaseModel):
    note: str | None = None
