"""Versioned, internal-only Agent registry snapshot response models."""

from __future__ import annotations

from app.schemas.base import ApiResponseModel

from datetime import datetime
import re
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationInfo,
    field_validator,
    model_validator,
)

from anila_contracts import AgentManifest, Classification


_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SHA256_REVISION = re.compile(r"^sha256:[0-9a-f]{64}$")


def _require_hex64(value: str, *, field_name: str) -> str:
    if not _HEX64.fullmatch(value):
        raise ValueError(f"{field_name} 必須是 64 位小寫 SHA-256 hex")
    return value


def _require_manifest_revision(value: str, *, field_name: str) -> str:
    if not _SHA256_REVISION.fullmatch(value):
        raise ValueError(f"{field_name} 必須是 sha256:<64 位小寫 hex>")
    return value


def _require_aware(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} 必須是 timezone-aware datetime")
    return value


class AgentRegistryBaseModel(ApiResponseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: StrictInt = Field(gt=0)
    name: StrictStr
    display_name: StrictStr
    is_active: StrictBool
    health_status: StrictStr
    health_checked_at: datetime | None = None
    classification_ceiling: StrictStr

    @field_validator("health_checked_at")
    @classmethod
    def _health_timestamp_timezone(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_aware(value, field_name="health_checked_at")


class AgentRegistryEntry(ApiResponseModel):
    """Safe governance projection; intentionally has no endpoint URL field."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: StrictStr
    registry_id: StrictInt = Field(gt=0)
    name: StrictStr
    snapshot_id: StrictStr
    is_active: StrictBool
    manifest: AgentManifest | None = None
    # Empty strings are explicit gap sentinels for legacy/unregistered rows;
    # omission/coercion is not allowed at this service boundary.
    manifest_sha256: StrictStr
    manifest_revision: StrictStr
    approval_status: StrictStr
    health_status: StrictStr
    health_checked_at: datetime | None = None
    audit_level: StrictStr
    trace_callback_mode: StrictStr
    trace_test_passed_at: datetime | None = None
    trace_test_governance_fingerprint: StrictStr
    classification_ceiling: StrictStr
    default_classification_level: StrictStr
    base_model: AgentRegistryBaseModel | None = None
    ready_for_dispatch: StrictBool
    approved: StrictBool
    health_ready: StrictBool
    trace_test_passed: StrictBool
    manifest_valid: StrictBool
    endpoint_via_csp: StrictBool
    approval_required: StrictBool
    required_obligations: tuple[StrictStr, ...] = ()
    model_gateway: StrictStr
    reason_codes: tuple[StrictStr, ...] = ()

    @field_validator("snapshot_id")
    @classmethod
    def _snapshot_id_format(cls, value: str) -> str:
        return _require_hex64(value, field_name="snapshot_id")

    @field_validator("manifest_sha256")
    @classmethod
    def _manifest_hash_format(cls, value: str) -> str:
        # Legacy rows deliberately use an empty gap sentinel.  Any populated
        # identity must be a raw, lowercase SHA-256 digest.
        if value == "":
            return value
        return _require_hex64(value, field_name="manifest_sha256")

    @field_validator("manifest_revision")
    @classmethod
    def _manifest_revision_format(cls, value: str) -> str:
        # Legacy rows deliberately use an empty gap sentinel.  A populated
        # revision is always the CSP-owned content revision, never manifest.version.
        if value == "":
            return value
        return _require_manifest_revision(value, field_name="manifest_revision")

    @field_validator("trace_test_governance_fingerprint")
    @classmethod
    def _trace_fingerprint_format(cls, value: str) -> str:
        if value == "":
            return value
        return _require_hex64(value, field_name="trace_test_governance_fingerprint")

    @field_validator("health_checked_at", "trace_test_passed_at")
    @classmethod
    def _entry_timestamp_timezone(
        cls, value: datetime | None, info: ValidationInfo
    ) -> datetime | None:
        return None if value is None else _require_aware(value, field_name=info.field_name)

    @model_validator(mode="after")
    def _ready_requires_canonical_evidence(self) -> "AgentRegistryEntry":
        if self.ready_for_dispatch:
            if not self.is_active:
                raise ValueError("ready_for_dispatch=true 的 Agent 必須啟用")
            if not self.approved or self.approval_status != "approved":
                raise ValueError("ready_for_dispatch=true 必須是 approved")
            if self.health_status != "healthy" or not self.health_ready:
                raise ValueError("ready_for_dispatch=true 必須有 healthy health evidence")
            if not self.trace_test_passed:
                raise ValueError("ready_for_dispatch=true 必須有 trace-test evidence")
            if not self.manifest_valid or not self.endpoint_via_csp:
                raise ValueError("ready_for_dispatch=true 必須通過 manifest/endpoint gate")
            if self.model_gateway != "csp":
                raise ValueError("ready_for_dispatch=true 必須使用 CSP model gateway")
            if self.base_model is None:
                raise ValueError("ready_for_dispatch=true 必須包含 base_model")
            if not self.base_model.is_active or self.base_model.health_status != "healthy":
                raise ValueError("ready_for_dispatch=true 的 base_model 必須 active/healthy")
            if self.manifest is None:
                raise ValueError("ready_for_dispatch=true 必須包含 canonical manifest")
            if not self.manifest_sha256:
                raise ValueError("ready_for_dispatch=true 必須包含 manifest_sha256")
            if not self.manifest_revision:
                raise ValueError("ready_for_dispatch=true 必須包含 manifest_revision")
            if self.health_checked_at is None or self.trace_test_passed_at is None:
                raise ValueError("ready_for_dispatch=true 必須包含 health/trace timestamp")
            if not self.trace_test_governance_fingerprint:
                raise ValueError("ready_for_dispatch=true 必須包含 trace fingerprint")
            try:
                Classification.from_storage(self.classification_ceiling)
                Classification.from_storage(self.default_classification_level)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "ready_for_dispatch=true 必須包含有效 classification ceiling/default"
                ) from exc
        return self


class AgentRegistrySnapshot(ApiResponseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["agent-registry/v1"]
    snapshot_id: StrictStr
    snapshot_revision: StrictStr
    snapshot_hash: StrictStr
    registry_snapshot_id: StrictStr
    fetched_at: datetime
    expires_at: datetime
    caller_user_id: StrictInt = Field(gt=0)
    # A tuple is required here: ``frozen=True`` prevents assigning a new list
    # but does not make a nested list immutable.  Router consumers must not be
    # able to mutate the authority snapshot in place.
    agents: tuple[AgentRegistryEntry, ...]

    @field_validator("snapshot_id", "snapshot_revision", "snapshot_hash", "registry_snapshot_id")
    @classmethod
    def _snapshot_hash_format(cls, value: str, info: ValidationInfo) -> str:
        return _require_hex64(value, field_name=info.field_name)

    @field_validator("fetched_at", "expires_at")
    @classmethod
    def _snapshot_timestamp_timezone(
        cls, value: datetime, info: ValidationInfo
    ) -> datetime:
        return _require_aware(value, field_name=info.field_name)

    @model_validator(mode="after")
    def _snapshot_identity_is_consistent(self) -> "AgentRegistrySnapshot":
        if self.registry_snapshot_id != self.snapshot_id:
            raise ValueError("registry_snapshot_id 必須等於 snapshot_id")
        if self.snapshot_revision != self.snapshot_id:
            raise ValueError("snapshot_revision 必須等於 snapshot_id")
        if self.snapshot_hash != self.snapshot_id:
            raise ValueError("snapshot_hash 必須等於 snapshot_id")
        if self.expires_at <= self.fetched_at:
            raise ValueError("registry snapshot expires_at 必須晚於 fetched_at")
        return self


__all__ = ["AgentRegistryBaseModel", "AgentRegistryEntry", "AgentRegistrySnapshot"]
