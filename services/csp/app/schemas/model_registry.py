from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.schemas.contracts.classification import ClassificationLevel
from app.schemas.base import ApiResponseModel

# URL path prefix only — not a wire protocol. See docs/FAKE-CONTROLS.md.
ALLOWED_API_VERSIONS = frozenset({"v1", "v2"})
# App-level only (no DB CHECK). custom_adapter remains rejected at the API.
ALLOWED_PROTOCOLS = frozenset({"openai_compatible", "triton_grpc"})


def _validate_classification_ceiling(value: str | None) -> str | None:
    """None = 不設限;otherwise must be a ClassificationLevel storage value."""
    if value is None:
        return None
    # from_storage raises ValueError on unknown → Pydantic 422 at write time
    ClassificationLevel.from_storage(value)
    return value


def _validate_api_version(value: str | None) -> str | None:
    if value is None:
        return None
    if value not in ALLOWED_API_VERSIONS:
        raise ValueError(
            "api_version 僅接受 v1 或 v2（URL 路徑前綴，不是通訊協定）"
        )
    return value


def _validate_protocol(value: str | None) -> str | None:
    if value is None:
        return None
    if value not in ALLOWED_PROTOCOLS:
        raise ValueError(
            "protocol 僅接受 openai_compatible 或 triton_grpc"
        )
    return value


class ModelCreate(BaseModel):
    name: str
    display_name: str
    model_type: str  # 'llm' / 'vlm' / 'embedding' / 'agent' / 'image' / 'asr'
    endpoint_url: str
    # URL path prefix for OpenAI-compatible endpoints only (v1/v2).
    # Ignored for protocol=triton_grpc (gRPC has no versioned HTTP path).
    api_version: Literal["v1", "v2"] = "v1"
    description: str | None = None
    context_window: int | None = None
    base_model_id: int | None = None  # For agents: the underlying LLM model ID
    # Default True: new model registrations are expected to live on the
    # anila-models-net internal docker network (decoupled inference stack).
    # Admin can untick for external on-prem LAN endpoints. DB column default
    # (migration 0033) is False so historical rows aren't auto-flipped.
    is_internal: bool = True
    # Slice 6a (doc 04 §2): ModelEndpoint formalized fields.
    # openai_compatible = HTTP OpenAI shape; triton_grpc = Triton/KServe gRPC.
    protocol: str = "openai_compatible"
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

    @field_validator("api_version")
    @classmethod
    def _api_version(cls, v: str) -> str:
        return _validate_api_version(v)  # type: ignore[return-value]

    @field_validator("protocol")
    @classmethod
    def _protocol(cls, v: str) -> str:
        return _validate_protocol(v)  # type: ignore[return-value]


class ModelUpdate(BaseModel):
    display_name: str | None = None
    model_type: str | None = None
    endpoint_url: str | None = None
    api_version: Literal["v1", "v2"] | None = None
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

    @field_validator("api_version")
    @classmethod
    def _api_version(cls, v: str | None) -> str | None:
        return _validate_api_version(v)

    @field_validator("protocol")
    @classmethod
    def _protocol(cls, v: str | None) -> str | None:
        return _validate_protocol(v)


class ModelResponse(ApiResponseModel):
    id: int
    name: str
    display_name: str
    model_type: str
    endpoint_url: str
    api_version: str
    is_active: bool
    is_router_primary: bool = False
    is_image_primary: bool = False
    is_asr_primary: bool = False
    is_platform_embedding: bool = False
    embedding_native_dim: int | None = None
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


# ── P4.6 bulk import (SYSTEM-MAP §6 / PLAN 4.6 / OE-2 G5) ───────────────────


class ModelBulkImportRequest(BaseModel):
    """Import every model listed by an already-registered endpoint.

    ``source_model_id`` picks a registry row whose ``endpoint_url`` (and stored
    credential) are used to call upstream ``GET {endpoint}/models``. The
    natural identity key remains ``name`` — same unique column as single-record
    create.
    """

    source_model_id: int


class ModelBulkImportSkipped(BaseModel):
    name: str | None = None
    reason: str


class ModelBulkImportUnchanged(BaseModel):
    """An existing row matched by ``name``; local admin settings were kept."""

    name: str
    reason: str = "已登錄；本機設定已保留"


class ModelBulkImportCreated(BaseModel):
    """A newly inserted row; ``guessed_fields`` lists values not from upstream
    or endpoint-scoped inheritance (administrator should review before activate).
    """

    name: str
    guessed_fields: list[str] = Field(default_factory=list)


class ModelBulkImportMissing(BaseModel):
    """Registry row on the source endpoint that the upstream listing omitted.

    Report-only — nothing is deleted or deactivated.
    """

    name: str
    reason: str = "上游清單未再列出此模型"


class ModelBulkImportResponse(BaseModel):
    source_model_id: int
    endpoint_url: str
    created: int
    already_existed: int
    skipped: int
    # Count of create-eligible listing entries not written because the
    # per-run create cap was reached. Re-running the import progresses
    # through the remainder (already-created names become unchanged).
    truncated: int = 0
    created_names: list[str]
    created_entries: list[ModelBulkImportCreated] = Field(default_factory=list)
    unchanged: list[ModelBulkImportUnchanged]
    skipped_entries: list[ModelBulkImportSkipped]
    missing_from_listing: list[ModelBulkImportMissing] = Field(default_factory=list)


class ModelBulkActivateCreatedRequest(BaseModel):
    """Activate inactive rows created by a prior bulk import (single review step).

    Scoped to ``names`` that still point at the same endpoint as
    ``source_model_id``. Does not weaken the inactive-by-default fence —
    activation remains an explicit administrator action.
    """

    source_model_id: int
    names: list[str] = Field(..., min_length=1)


class ModelBulkActivateCreatedResponse(BaseModel):
    source_model_id: int
    activated: int
    already_active: int
    not_found: int
    wrong_endpoint: int
    activated_names: list[str]
