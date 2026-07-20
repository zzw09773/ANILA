# -*- coding: utf-8 -*-
"""Artifact 契約(型別 + 封閉 enum)—— Slice 8a。

依 docs/anila-redesign-docs/02-system-architecture.md(ArtifactJob schema
逐欄)、01-domain-model.md(Artifact / ArtifactVersion / ExportRecord)、
09-api-event-contracts.md(Artifact API 回應契約)、08(分類匯出判定)。

DB 層存開放 String,封閉 enum 在本契約層 fail-closed 把關(同
``contracts.tasks`` / ``contracts.policy`` 模式)。五類產出名稱以 doc 02/01
逐字為準:``slides / report / mindmap / infographic / datatable``(task 提示
的 ``deck`` 是佔位,doc 用 ``slides`` → 從文件)。
"""

from __future__ import annotations

import enum
from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from anila_contracts import Classification as ClassificationLevel


class ArtifactType(str, enum.Enum):
    """doc 02/01 五類產出(順序照 doc 02 ArtifactJob.type)。"""

    SLIDES = "slides"
    REPORT = "report"
    MINDMAP = "mindmap"
    INFOGRAPHIC = "infographic"
    DATATABLE = "datatable"


class ArtifactJobStatus(str, enum.Enum):
    """ArtifactJob runtime and terminal states shared with Studio."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ArtifactStatus(str, enum.Enum):
    """doc 01 Artifact.status 四值(逐字)。"""

    QUEUED = "queued"
    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"


# ── /v1 service 面(Studio 經 service token 回報)─────────────────────────────


class ArtifactJobIn(BaseModel):
    """``POST /v1/artifact-jobs`` body(冪等 upsert on ``job_id``)。

    ``requester_user_id`` 或 ``employee_id`` 至少一(owner 解析);model
    endpoint 拿不到 service token,身分由 studio 目的地驅動填入。
    """

    job_id: str = Field(..., min_length=1, max_length=64)
    artifact_type: ArtifactType
    task_id: int | None = None
    source_snapshot_id: int | None = None
    requester_user_id: int | None = None
    employee_id: str | None = Field(default=None, max_length=32)
    collection_id: int | None = None
    trace_id: str | None = Field(default=None, max_length=64)
    status: ArtifactJobStatus = ArtifactJobStatus.QUEUED
    progress: int = Field(default=0, ge=0, le=100)
    params_digest: str | None = Field(default=None, max_length=64)
    message: str | None = None
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def _require_requester(self) -> "ArtifactJobIn":
        if self.requester_user_id is None and not (self.employee_id or "").strip():
            raise ValueError(
                "requester_user_id 或 employee_id 至少填一(job owner 解析)"
            )
        return self


class ArtifactJobPatch(BaseModel):
    """``PATCH /v1/artifact-jobs/{job_id}`` body(status 轉移 + 進度/回填)。"""

    status: ArtifactJobStatus
    progress: int | None = Field(default=None, ge=0, le=100)
    message: str | None = None
    error: dict | None = None
    artifact_id: int | None = None
    result_metadata: dict | None = None
    artifact_files: list[dict] | None = None


class ArtifactJobLeaseIn(BaseModel):
    attempt: int = Field(..., ge=1, le=100)
    lease_token: str = Field(..., min_length=16, max_length=128)
    lease_seconds: int = Field(..., ge=15, le=900)


class ArtifactIn(BaseModel):
    """``POST /v1/artifacts`` body。

    binding 規則(``task_id`` 或 ``source_snapshot_id`` 至少一)在 service 層
    以 422 把關(constitution §6);此處只定形狀,``metadata`` 落
    ``metadata_json``。
    """

    artifact_type: ArtifactType
    title: str = Field(..., min_length=1, max_length=500)
    storage_ref: str = Field(..., min_length=1, max_length=1000)
    job_id: str | None = Field(default=None, max_length=64)
    task_id: int | None = None
    source_snapshot_id: int | None = None
    content_hash: str | None = Field(default=None, max_length=64)
    classification_level: ClassificationLevel | None = None
    file_refs: list[str] = Field(default_factory=list)
    citation_map: dict | None = None
    generated_by_model_id: int | None = None
    generated_by_agent_id: int | None = None
    metadata: dict | None = None


class ArtifactUploadMetadata(BaseModel):
    """Metadata form part for CSP-owned immutable artifact bytes."""

    artifact_type: ArtifactType
    title: str = Field(..., min_length=1, max_length=500)
    job_id: str | None = Field(default=None, max_length=64)
    task_id: int = Field(..., gt=0)
    source_snapshot_id: int = Field(..., gt=0)
    content_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    content_size: int = Field(..., gt=0)
    media_type: str = Field(..., min_length=1, max_length=200)
    original_filename: str = Field(..., min_length=1, max_length=255)
    classification_level: ClassificationLevel | None = None
    citation_map: dict | None = None
    generated_by_model_id: int | None = None
    generated_by_agent_id: int | None = None
    metadata: dict | None = None


class ArtifactVersionIn(BaseModel):
    """``POST /v1/artifacts/{artifact_id}/versions`` body(新版 + 分類重驗)。"""

    storage_ref: str = Field(..., min_length=1, max_length=1000)
    content_hash: str | None = Field(default=None, max_length=64)
    classification_level: ClassificationLevel | None = None
    file_refs: list[str] = Field(default_factory=list)
    citation_map: dict | None = None
    generated_by_model_id: int | None = None
    generated_by_agent_id: int | None = None
    generated_by_studio_job_id: str | None = Field(default=None, max_length=64)


class ArtifactExportIn(BaseModel):
    """``POST /v1/artifacts/{artifact_id}/exports`` body(匯出 policy gate)。

    doc 08 §10 匯出判定式:``allow if target_space.classification_floor >=
    artifact.level``。``target_classification_floor`` = 目的地空間的分類下限。
    """

    target_classification_floor: ClassificationLevel
    target_space: str | None = Field(default=None, max_length=100)
    export_format: str | None = Field(default=None, max_length=32)
    artifact_version_id: int | None = None
    employee_id: str | None = Field(default=None, max_length=32)


# ── 回應契約 ──────────────────────────────────────────────────────────────────


class ArtifactRegisterResult(BaseModel):
    """``POST /v1/artifacts`` → 201 回應(effective 分類為繼承後結果)。"""

    artifact_id: int
    version_id: int
    classification_level: ClassificationLevel
    download_url: str | None = None


class ArtifactVersionRevokeIn(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500)


class ArtifactLegalHoldIn(BaseModel):
    held: bool
    reason: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def _reason_matches_hold(self) -> "ArtifactLegalHoldIn":
        if self.held and not (self.reason or "").strip():
            raise ValueError("啟用 legal hold 必須提供 reason")
        if not self.held and self.reason is not None:
            raise ValueError("解除 legal hold 不可保留 reason")
        return self


class ArtifactVersionResult(BaseModel):
    """``POST /v1/artifacts/{id}/versions`` → 201 回應。"""

    artifact_id: int
    version_id: int
    version: int
    classification_level: ClassificationLevel


class ExportResult(BaseModel):
    """``POST /v1/artifacts/{id}/exports`` → 201(allow)回應。"""

    export_id: int
    classification_level: ClassificationLevel
    decision: str


class ArtifactJobOut(BaseModel):
    job_id: str
    owner_user_id: int | None = None
    requester_employee_id: str | None = None
    collection_id: int | None = None
    task_id: int | None = None
    source_snapshot_id: int | None = None
    artifact_type: ArtifactType
    status: ArtifactJobStatus
    progress: int
    message: str | None = None
    result_metadata: dict | None = None
    artifact_files: list = Field(default_factory=list)
    error: dict | None = None
    params_digest: str | None = None
    trace_id: str | None = None
    artifact_id: int | None = None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None
    durable_attempt: int | None = None

    model_config = {"from_attributes": True}


class ArtifactVersionOut(BaseModel):
    id: int
    artifact_id: int
    version: int
    storage_ref: str | None = None
    content_hash: str | None = None
    blob_size_bytes: int | None = None
    media_type: str | None = None
    original_filename: str | None = None
    is_active: bool = True
    revoked_at: datetime | None = None
    revoked_by_user_id: int | None = None
    revocation_reason: str | None = None
    lifecycle_state: str = "active"
    archive_due_at: datetime | None = None
    archived_at: datetime | None = None
    erase_due_at: datetime | None = None
    erased_at: datetime | None = None
    legal_hold: bool = False
    legal_hold_reason: str | None = None
    file_refs: list = Field(default_factory=list)
    citation_map: dict | None = None
    generated_by_model_id: int | None = None
    generated_by_agent_id: int | None = None
    generated_by_studio_job_id: str | None = None
    classification_level: ClassificationLevel
    created_at: datetime

    model_config = {"from_attributes": True}


class ExportRecordOut(BaseModel):
    id: int
    artifact_id: int
    artifact_version_id: int | None = None
    exporter_user_id: int | None = None
    exporter_employee_id: str | None = None
    target_space: str | None = None
    target_classification_floor: ClassificationLevel
    export_format: str | None = None
    policy_decision_id: int | None = None
    decision: str
    trace_id: str | None = None
    classification_level: ClassificationLevel
    created_at: datetime

    model_config = {"from_attributes": True}


class ArtifactOut(BaseModel):
    id: int
    artifact_type: ArtifactType
    title: str
    status: ArtifactStatus
    owner_user_id: int | None = None
    source_task_id: int | None = None
    source_snapshot_id: int | None = None
    job_id: str | None = None
    current_version: int
    trace_id: str | None = None
    classification_level: ClassificationLevel
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ArtifactDetailOut(ArtifactOut):
    """``GET /api/artifacts/{id}`` —— artifact + versions + exports。"""

    versions: list[ArtifactVersionOut] = Field(default_factory=list)
    exports: list[ExportRecordOut] = Field(default_factory=list)
