from datetime import datetime
from pydantic import BaseModel


class ApiKeyCreate(BaseModel):
    name: str
    model_ids: list[int] = []
    expires_at: datetime | None = None  # None = no expiration


class ApiKeyUpdate(BaseModel):
    name: str | None = None
    model_ids: list[int] | None = None
    is_active: bool | None = None


class ApiKeyResponse(BaseModel):
    id: int
    user_id: int
    name: str
    key_prefix: str
    key_suffix: str
    is_active: bool
    expires_at: datetime | None
    created_at: datetime
    last_used_at: datetime | None
    allowed_model_ids: list[int] = []
    allowed_model_names: list[str] = []

    model_config = {"from_attributes": True}


class ApiKeyCreatedResponse(ApiKeyResponse):
    full_key: str  # Only returned once at creation
