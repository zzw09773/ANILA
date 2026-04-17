from datetime import datetime
from pydantic import BaseModel


class PlatformLinkCreate(BaseModel):
    name: str
    url: str
    icon: str | None = None
    description: str | None = None
    sort_order: int = 0


class PlatformLinkUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    icon: str | None = None
    description: str | None = None
    sort_order: int | None = None
    is_active: bool | None = None


class PlatformLinkResponse(BaseModel):
    id: int
    name: str
    url: str
    icon: str | None
    description: str | None
    sort_order: int
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}
