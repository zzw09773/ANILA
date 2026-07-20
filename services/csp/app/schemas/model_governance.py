"""CSP-facing schemas for Gate 5 model-governance runtime evidence."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


_SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"


class _FrozenSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ObservedArtifactFacts(_FrozenSchema):
    """Immutable artifact identity observed from the deployed model runtime."""

    artifact_id: str = Field(min_length=2, max_length=128)
    digest: str = Field(pattern=_SHA256_PATTERN)
    revision: str = Field(min_length=1, max_length=256)


class ObservedGpuTopology(_FrozenSchema):
    vendor: str = Field(min_length=1, max_length=128)
    count: int = Field(gt=0, le=4096)
    memory_gib: int = Field(gt=0, le=1_000_000)
    compute_capability: str = Field(min_length=1, max_length=128)


class ObservedHealthReadiness(_FrozenSchema):
    health_url: str = Field(min_length=1, max_length=512)
    readiness_url: str = Field(min_length=1, max_length=512)
    freshness_seconds: int = Field(gt=0, le=86_400)
    last_check: datetime
    healthy: bool
    ready: bool

    @field_validator("last_check")
    @classmethod
    def _aware_last_check(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("last_check must include timezone")
        return value


class ObservedDeploymentFacts(_FrozenSchema):
    """Observed deployment shape matching the signed profile deployment."""

    deployment_id: str = Field(min_length=2, max_length=128)
    artifact_id: str = Field(min_length=2, max_length=128)
    image_digest: str = Field(pattern=_SHA256_PATTERN)
    gpu_topology: ObservedGpuTopology
    health_readiness: ObservedHealthReadiness


class ObservedGovernanceFacts(_FrozenSchema):
    """Explicit file contract mounted by deployment for live evidence."""

    schema_version: Literal["anila.gate5.model-governance.observed.v1"]
    artifacts: tuple[ObservedArtifactFacts, ...] = ()
    deployments: tuple[ObservedDeploymentFacts, ...] = ()


class ModelGovernanceReadiness(_FrozenSchema):
    """Public, non-secret readiness projection for health endpoints."""

    status: Literal["ready", "not_ready", "not_configured"]
    ready: bool
    reason: str = Field(min_length=1, max_length=512)
    checked_at: datetime
    profile_id: str | None = None
    profile_version: str | None = None
    profile_content_sha256: str | None = None

    @field_validator("checked_at")
    @classmethod
    def _aware_checked_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("checked_at must include timezone")
        return value


__all__ = [
    "ModelGovernanceReadiness",
    "ObservedArtifactFacts",
    "ObservedDeploymentFacts",
    "ObservedGovernanceFacts",
    "ObservedGpuTopology",
    "ObservedHealthReadiness",
]
