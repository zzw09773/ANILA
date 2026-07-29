from datetime import datetime

from pydantic import BaseModel, field_validator

from app.schemas.contracts.classification import ClassificationLevel


def _validate_classification_ceiling(value: str | None) -> str | None:
    """None = 不設限;otherwise must be a ClassificationLevel storage value."""
    if value is None:
        return None
    # from_storage raises ValueError on unknown → Pydantic 422 at write time
    ClassificationLevel.from_storage(value)
    return value


class ModelCreate(BaseModel):
    name: str
    display_name: str
    model_type: str  # 'llm' / 'vlm' / 'embedding' / 'agent'
    endpoint_url: str
    api_version: str = "v1"
    description: str | None = None
    context_window: int | None = None
    base_model_id: int | None = None  # For agents: the underlying LLM model ID
    # Default True: new model registrations are expected to live on the
    # anila-models-net internal docker network (decoupled inference stack).
    # Admin can untick for external on-prem LAN endpoints. DB column default
    # (migration 0033) is False so historical rows aren't auto-flipped.
    is_internal: bool = True
    # Slice 6a (doc 04 §2): ModelEndpoint formalized fields.
    protocol: str = "openai_compatible"  # 'openai_compatible' / 'custom_adapter'
    classification_ceiling: str | None = None  # 四級字串;None = 不設限
    owner_department_id: int | None = None
    supports_streaming: bool = True
    supports_json_schema: bool = False
    supports_tools: bool = False
    # doc 04 §3: write-only per-model gateway key. Encrypted to
    # ``api_key_secret_ref`` on create; NEVER returned. Omit to use the
    # global MODEL_GATEWAY_API_KEY fallback.
    api_key: str | None = None

    @field_validator("classification_ceiling")
    @classmethod
    def _ceiling(cls, v: str | None) -> str | None:
        return _validate_classification_ceiling(v)


class ModelUpdate(BaseModel):
    display_name: str | None = None
    model_type: str | None = None
    endpoint_url: str | None = None
    api_version: str | None = None
    is_active: bool | None = None
    description: str | None = None
    context_window: int | None = None
    base_model_id: int | None = None
    is_internal: bool | None = None
    # Slice 6a (doc 04 §2/§3).
    protocol: str | None = None
    classification_ceiling: str | None = None
    owner_department_id: int | None = None
    supports_streaming: bool | None = None
    supports_json_schema: bool | None = None
    supports_tools: bool | None = None
    # Write-only: re-encrypt the per-model gateway key. Never returned.
    api_key: str | None = None

    @field_validator("classification_ceiling")
    @classmethod
    def _ceiling(cls, v: str | None) -> str | None:
        return _validate_classification_ceiling(v)


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
    is_internal: bool = False
    # Slice 6a (doc 04 §2): ModelEndpoint formalized fields.
    protocol: str = "openai_compatible"
    classification_ceiling: str | None = None
    owner_department_id: int | None = None
    supports_streaming: bool = True
    supports_json_schema: bool = False
    supports_tools: bool = False
    # doc 04 §3: only the presence of a per-model key is exposed — never the
    # ciphertext / secret ref, and never the plaintext.
    has_api_key: bool = False
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
