from datetime import datetime
from pydantic import BaseModel


class ModelCreate(BaseModel):
    name: str
    display_name: str
    model_type: str  # 'llm' / 'vlm' / 'embedding' / 'agent'
    endpoint_url: str
    api_version: str = "v1"
    description: str | None = None
    context_window: int | None = None
    base_model_id: int | None = None  # For agents: the underlying LLM model ID


class ModelUpdate(BaseModel):
    display_name: str | None = None
    model_type: str | None = None
    endpoint_url: str | None = None
    api_version: str | None = None
    is_active: bool | None = None
    description: str | None = None
    context_window: int | None = None
    base_model_id: int | None = None


class ModelResponse(BaseModel):
    id: int
    name: str
    display_name: str
    model_type: str
    endpoint_url: str
    api_version: str
    is_active: bool
    is_router_primary: bool = False
    health_status: str
    health_checked_at: datetime | None
    description: str | None
    context_window: int | None
    base_model_id: int | None = None
    base_model_name: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
