"""Formal Agent manifest contract shared by CSP, Router and Agent hosts."""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal

from pydantic import Field, StrictBool, StrictInt, field_validator, model_validator

from ._v2 import (
    MAX_DESCRIPTION_LENGTH,
    IdentifierField,
    JsonObject,
    StrictContractModel,
    TokenField,
    ensure_text,
    validate_json_object,
    validate_unique_tokens,
)
from .classification import ClassificationLevel

AGENT_MANIFEST_SCHEMA_VERSION = "agent-manifest/v1"

AgentIdentifier = Annotated[str, IdentifierField]
AgentToken = Annotated[str, TokenField]
ModelReference = StrictInt | AgentIdentifier


class RuntimeType(str, Enum):
    """Known Agent host/runtime families from the CSP registry contract."""

    ANILA_AGENT = "anila_agent"
    LANGCHAIN = "langchain"
    OPENWEBUI_PIPE_COMPATIBLE = "openwebui_pipe_compatible"
    OPENAI_COMPATIBLE_AGENT = "openai_compatible_agent"
    CUSTOM_HTTP = "custom_http"


class ManifestCapabilities(StrictContractModel):
    """Compatibility projection of the existing CSP capabilities object."""

    retrieval: StrictBool = False
    tools: tuple[AgentToken, ...] = ()
    streaming: StrictBool = False

    @field_validator("tools")
    @classmethod
    def _tools_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return validate_unique_tokens(value, field_name="capabilities.tools")


class ManifestClassification(StrictContractModel):
    """Default level and hard ceiling for an Agent."""

    ceiling: ClassificationLevel
    default: ClassificationLevel

    @model_validator(mode="after")
    def _default_cannot_exceed_ceiling(self) -> ManifestClassification:
        if self.default > self.ceiling:
            raise ValueError("classification.default 不得高於 classification.ceiling")
        return self


class ModelBinding(StrictContractModel):
    """CSP model-gateway identity bound to an Agent or execution target.

    A model name/id alone is not enough to authorize inference.  The gateway
    and optional deployed revision are retained so admission code can reject
    a manifest that tries to point at a raw/non-CSP endpoint.
    """

    model_id: ModelReference
    model_revision: AgentIdentifier | None = None
    gateway: Literal["csp"] = "csp"
    classification_ceiling: ClassificationLevel | None = None

    @field_validator("model_id")
    @classmethod
    def _model_id_is_positive_or_named(cls, value: ModelReference) -> ModelReference:
        if isinstance(value, bool):
            raise ValueError("model_id 不得是 bool")
        if isinstance(value, int) and value <= 0:
            raise ValueError("model_id 整數必須大於 0")
        return value


class AgentManifest(StrictContractModel):
    """Canonical Agent-supplied declaration consumed by CSP admission.

    ``capabilities`` accepts either the canonical token list or the legacy
    CSP object projection.  Both forms are closed at their own schema
    boundary; this is an additive migration aid, not a second authority.

    Approval, health/readiness, trace-test evidence, registry snapshot and
    ``ready_for_dispatch`` are deliberately absent.  CSP computes those
    authority fields in its registry snapshot instead of trusting an Agent's
    self-report.
    """

    schema_version: Literal["agent-manifest/v1"]
    agent_id: AgentIdentifier
    name: str = Field(min_length=1, max_length=255)
    version: AgentIdentifier
    runtime_type: RuntimeType
    runtime_version: AgentIdentifier | None = None
    api_version: Literal["v1"]
    supported_task_types: tuple[AgentToken, ...] = Field(min_length=1)
    description_for_router: str = Field(min_length=1, max_length=MAX_DESCRIPTION_LENGTH)
    input_schema: JsonObject
    output_schema: JsonObject
    capabilities: tuple[AgentToken, ...] | ManifestCapabilities
    event_protocols: tuple[AgentToken, ...] = Field(min_length=1)
    required_scopes: tuple[AgentToken, ...] = Field(min_length=1)
    classification: ManifestClassification
    full_trace_required: StrictBool
    supports_streaming: StrictBool
    supports_resume: StrictBool
    supports_cancel: StrictBool
    supports_idempotency: StrictBool
    model_binding: ModelBinding | None = None
    base_model_id: StrictInt | None = Field(default=None, gt=0)

    @field_validator("name", "description_for_router")
    @classmethod
    def _human_text_is_bounded(cls, value: str, info) -> str:
        return ensure_text(
            value,
            field_name=info.field_name or "manifest_text",
            max_length=MAX_DESCRIPTION_LENGTH,
        )

    @field_validator("runtime_version")
    @classmethod
    def _runtime_version_is_nonempty(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return ensure_text(value, field_name="runtime_version", max_length=128)

    @field_validator("supported_task_types", "event_protocols", "required_scopes")
    @classmethod
    def _token_lists_are_unique(cls, value: tuple[str, ...], info) -> tuple[str, ...]:
        return validate_unique_tokens(value, field_name=info.field_name or "manifest_tokens")

    @field_validator("input_schema", "output_schema")
    @classmethod
    def _schemas_are_frozen(cls, value: JsonObject, info):
        return validate_json_object(value, field_name=info.field_name or "schema")

    @field_validator("capabilities")
    @classmethod
    def _capabilities_are_unique(cls, value):
        if isinstance(value, tuple):
            return validate_unique_tokens(value, field_name="capabilities")
        # An Agent may honestly declare no special capabilities.  The
        # dispatchable contract is still non-empty because task types, event
        # protocol and scopes are required above.
        return value

    @model_validator(mode="after")
    def _manifest_governance_invariants(self) -> AgentManifest:
        if self.supports_resume and not self.supports_streaming:
            raise ValueError("supports_resume=true 必須同時 supports_streaming=true")

        if self.model_binding is not None and self.base_model_id is not None:
            if isinstance(self.model_binding.model_id, int):
                if self.model_binding.model_id != self.base_model_id:
                    raise ValueError("model_binding.model_id 必須與 base_model_id 一致")
            else:
                raise ValueError("base_model_id 不得與非數值 model_binding.model_id 並列")

        if (
            self.model_binding is not None
            and self.model_binding.classification_ceiling is not None
            and self.model_binding.classification_ceiling < self.classification.ceiling
        ):
            raise ValueError("Agent classification.ceiling 不得高於 model binding ceiling")

        return self


__all__ = [
    "AGENT_MANIFEST_SCHEMA_VERSION",
    "AgentManifest",
    "ManifestCapabilities",
    "ManifestClassification",
    "ModelBinding",
    "RuntimeType",
]
