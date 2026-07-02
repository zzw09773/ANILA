from datetime import datetime
from pydantic import BaseModel


class AlertResponse(BaseModel):
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


class AlertSummary(BaseModel):
    open_count: int
    acknowledged_count: int
    resolved_count: int
    high_count: int


class AlertStatusUpdate(BaseModel):
    note: str | None = None
