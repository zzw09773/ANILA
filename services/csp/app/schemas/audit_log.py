from datetime import datetime
from pydantic import BaseModel


class AuditLogResponse(BaseModel):
    id: int
    actor_user_id: int | None = None
    actor_username: str | None = None
    action: str
    resource_type: str
    resource_id: str | None = None
    status: str
    detail: str | None = None
    ip_address: str | None = None
    metadata: dict | None = None
    created_at: datetime
