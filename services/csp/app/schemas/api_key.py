from datetime import datetime
from pydantic import BaseModel, Field, field_validator


class ApiKeyCreate(BaseModel):
    # Name must be non-empty after trim — without this the admin UI lets
    # whitespace-only names through and the list ends up with keys whose
    # "delete" confirmation prompt says 'Revoke API Key「 」?'.
    name: str = Field(..., min_length=1, max_length=128)
    # Require at least one model; a zero-model key is indistinguishable
    # from a revoked key and can't call any `/v1/*` endpoint anyway.
    model_ids: list[int] = Field(..., min_length=1)
    expires_at: datetime | None = None  # None = no expiration

    @field_validator("name")
    @classmethod
    def _name_not_whitespace(cls, v: str) -> str:
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("名稱不可為空白")
        return trimmed


class ApiKeyUpdate(BaseModel):
    name: str | None = Field(None, max_length=128)
    model_ids: list[int] | None = None
    is_active: bool | None = None

    @field_validator("name")
    @classmethod
    def _name_not_whitespace(cls, v: str | None) -> str | None:
        if v is None:
            return None
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("名稱不可為空白")
        return trimmed


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
