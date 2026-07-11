# -*- coding: utf-8 -*-
"""Pydantic contracts for the Service Registry (doc 07 §3/§6/§10/§13).

Enums mirror doc §3's closed value sets and are validated at the API boundary
(FastAPI 422 on a bad value). ``required_roles`` reuses the same allow-list as
``platform_link`` so the compat façade and the registry stay consistent.
"""

from __future__ import annotations

import enum
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from anila_contracts import Classification as ClassificationLevel
from app.schemas.platform_link import _validate_required_roles


class ServiceType(str, enum.Enum):
    PROJECT_PORTAL = "project_portal"
    ANALYSIS_GUI = "analysis_gui"
    ARTIFACT_TOOL = "artifact_tool"
    DATA_SYSTEM = "data_system"
    WORKFLOW_SYSTEM = "workflow_system"


class LaunchMode(str, enum.Enum):
    IFRAME = "iframe"
    NEW_TAB = "new_tab"


class SsoMode(str, enum.Enum):
    CARD_SSO = "card_sso"
    OIDC = "oidc"
    LAUNCH_JWT = "launch_jwt"


class ConfigSource(str, enum.Enum):
    DB = "db"
    ENV_SEEDED = "env_seeded"


# doc §9 value domains (validated soft — unknown values are rejected).
_DATA_INGRESS = {"uploaded_file", "task_result", "manual_input", "none"}
_DATA_EGRESS = {"artifact", "report", "table", "none"}


class RegisteredServiceCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    slug: str | None = Field(None, max_length=120)
    description: str | None = None
    icon: str | None = None
    owner_department_id: int | None = None
    owner_admin_user_id: int | None = None
    service_admin_user_ids: list[int] = Field(default_factory=list)
    service_type: ServiceType = ServiceType.PROJECT_PORTAL
    project_entry: bool = False
    project_id: str | None = None
    entry_url: str = Field(..., min_length=1, max_length=500)
    allowed_origins: list[str] = Field(default_factory=list)
    launch_mode: LaunchMode = LaunchMode.NEW_TAB
    iframe_allowed: bool = False
    sso_mode: SsoMode = SsoMode.CARD_SSO
    supports_launch_token: bool = False
    data_ingress: list[str] = Field(default_factory=list)
    data_egress: list[str] = Field(default_factory=list)
    healthcheck_url: str | None = None
    audit_callback_url: str | None = None
    trace_callback_url: str | None = None
    # R-SEC (ADR-0008): audit-callback client binding. Admin-tier only.
    service_client_id: int | None = None
    classification_ceiling: ClassificationLevel | None = None
    required_roles: list[str] = Field(default_factory=list)
    is_public: bool = False
    sort_order: int = 0

    @field_validator("required_roles")
    @classmethod
    def _roles(cls, v: list[str]) -> list[str]:
        return _validate_required_roles(v) or []

    @field_validator("data_ingress")
    @classmethod
    def _ingress(cls, v: list[str]) -> list[str]:
        bad = [x for x in v if x not in _DATA_INGRESS]
        if bad:
            raise ValueError(f"data_ingress unknown: {bad}; allowed {sorted(_DATA_INGRESS)}")
        return v

    @field_validator("data_egress")
    @classmethod
    def _egress(cls, v: list[str]) -> list[str]:
        bad = [x for x in v if x not in _DATA_EGRESS]
        if bad:
            raise ValueError(f"data_egress unknown: {bad}; allowed {sorted(_DATA_EGRESS)}")
        return v


class RegisteredServiceUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    icon: str | None = None
    owner_department_id: int | None = None
    owner_admin_user_id: int | None = None
    service_admin_user_ids: list[int] | None = None
    service_type: ServiceType | None = None
    project_entry: bool | None = None
    project_id: str | None = None
    entry_url: str | None = None
    allowed_origins: list[str] | None = None
    launch_mode: LaunchMode | None = None
    iframe_allowed: bool | None = None
    sso_mode: SsoMode | None = None
    supports_launch_token: bool | None = None
    data_ingress: list[str] | None = None
    data_egress: list[str] | None = None
    healthcheck_url: str | None = None
    audit_callback_url: str | None = None
    trace_callback_url: str | None = None
    # R-SEC (ADR-0008): audit-callback client binding. Admin-tier only — the
    # API layer rejects a per-service admin who tries to set this even when it
    # is whitelisted in ``db_editable_fields``.
    service_client_id: int | None = None
    classification_ceiling: ClassificationLevel | None = None
    required_roles: list[str] | None = None
    is_public: bool | None = None
    is_active: bool | None = None
    sort_order: int | None = None

    @field_validator("required_roles")
    @classmethod
    def _roles(cls, v: list[str] | None) -> list[str] | None:
        return _validate_required_roles(v)


class RegisteredServiceResponse(BaseModel):
    id: int
    name: str
    slug: str
    description: str | None
    icon: str | None
    owner_department_id: int | None
    owner_admin_user_id: int | None
    service_admin_user_ids: list[int]
    service_type: str
    project_entry: bool
    project_id: str | None
    entry_url: str
    allowed_origins: list[str]
    launch_mode: str
    iframe_allowed: bool
    sso_mode: str
    supports_launch_token: bool
    data_ownership: str
    data_ingress: list[str]
    data_egress: list[str]
    healthcheck_url: str | None
    audit_callback_url: str | None
    trace_callback_url: str | None
    service_client_id: int | None
    classification_ceiling: str | None
    required_roles: list[str]
    is_public: bool
    is_active: bool
    config_source: str
    env_seed_key: str | None
    db_editable_fields: list[str]
    last_seeded_at: datetime | None
    sort_order: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ── Launch contract (doc §5/§6/§13) ─────────────────────────────────────────


class LaunchRequest(BaseModel):
    """Launch a registered service. When ``task_id`` is given the launch
    inherits that task's classification / trace / snapshot; otherwise the
    caller may declare a floor ``classification_level`` (default 無機密)."""

    task_id: int | None = None
    source_snapshot_id: int | None = None
    classification_level: ClassificationLevel | None = None
    project_id: str | None = None


class LaunchResponse(BaseModel):
    launch_id: str
    launch_token: str
    launch_url: str
    mode: str
    expires_at: datetime


# ── Audit callback (doc §10) ────────────────────────────────────────────────


class AuditCallbackActor(BaseModel):
    employee_id: str | None = None


class AuditCallbackResource(BaseModel):
    type: str | None = None
    id: str | None = None


class AuditCallbackPayload(BaseModel):
    event_type: str = Field(..., min_length=1, max_length=100)
    launch_id: str | None = None
    trace_id: str | None = None
    timestamp: datetime | None = None
    actor: AuditCallbackActor | None = None
    resource: AuditCallbackResource | None = None
    classification_level: str | None = None
    metadata: dict | None = None

    @field_validator("event_type")
    @classmethod
    def _event_type(cls, v: str) -> str:
        import re

        if not re.fullmatch(r"[a-z0-9]+(?:[._][a-z0-9]+)*", v):
            raise ValueError(
                "event_type 需為 dot/underscore 分隔的小寫識別字 (如 session.started)"
            )
        return v


class AuditCallbackResponse(BaseModel):
    id: int
    service_id: int | None
    launch_id: str | None
    event_type: str
    received_at: datetime

    model_config = {"from_attributes": True}


# ── Project bindings (doc §13) ──────────────────────────────────────────────


class ProjectBindingCreate(BaseModel):
    project_id: str = Field(..., min_length=1, max_length=100)
    is_primary_entry: bool = False


class ProjectBindingResponse(BaseModel):
    id: int
    service_id: int
    project_id: str
    is_primary_entry: bool
    created_at: datetime

    model_config = {"from_attributes": True}
