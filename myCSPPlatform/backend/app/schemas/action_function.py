"""Pydantic request/response models for the ANILA Functions v1 API.

Mirrors the SQLAlchemy models in ``app.models.action_function`` and the
endpoint contracts in spec §4. ``code`` is exposed conditionally — the
:func:`api.action_function.crud._can_view_code` gate decides whether to
include it in :class:`FunctionReadWithCode` for a given caller.

Slug regex matches the migration-level constraint: lowercase alphanumeric
plus hyphens, starting with alphanumeric, max 64 chars. The 128 KB code
size cap is a soft limit — large user code is almost always a sign of
abuse rather than a legitimate Function (the 同事's example is 30 lines).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"
MAX_CODE_BYTES = 128 * 1024  # 128 KB
MAX_TAG_COUNT = 20
MAX_TAG_LEN = 32


# ── Function CRUD ───────────────────────────────────────────────────────


class FunctionCreate(BaseModel):
    slug: str = Field(pattern=SLUG_PATTERN)
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    icon_data_url: str | None = Field(default=None, max_length=20000)
    code: str = Field(min_length=1, max_length=MAX_CODE_BYTES)
    tags: list[str] = Field(default_factory=list, max_length=MAX_TAG_COUNT)


class FunctionPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    icon_data_url: str | None = Field(default=None, max_length=20000)
    status: Literal["draft", "enabled", "disabled"] | None = None
    tags: list[str] | None = Field(default=None, max_length=MAX_TAG_COUNT)


class VersionCreate(BaseModel):
    code: str = Field(min_length=1, max_length=MAX_CODE_BYTES)
    commit_message: str | None = Field(default=None, max_length=500)


class FunctionRead(BaseModel):
    """Metadata-only view returned to all roles."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    title: str
    description: str | None
    icon_data_url: str | None
    author_user_id: int
    status: str
    disabled_reason: str | None
    latest_version_id: int | None
    forked_from_id: int | None
    tags: list[str]
    created_at: datetime
    updated_at: datetime


class FunctionReadWithCode(FunctionRead):
    """View returned when caller is allowed to see the code (developer+
    for enabled/disabled, author/admin for draft, admin always).

    For callers without code-viewing rights, ``code`` /
    ``valves_schema_json`` / ``actions_meta_json`` come back ``None``.
    """

    code: str | None = None
    valves_schema_json: dict[str, Any] | None = None
    actions_meta_json: list[dict[str, Any]] | None = None


class VersionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    version_no: int
    editor_user_id: int
    commit_message: str | None
    created_at: datetime


# ── Valves ──────────────────────────────────────────────────────────────


class ValvesValues(BaseModel):
    """Admin-set Valves payload. Encrypted at rest; never logged."""

    values: dict[str, Any]


class ValvesValuesRead(BaseModel):
    """GET response — secret-tagged fields are stripped to ``has_value``
    boolean (per spec §7.3); plain fields pass through.
    """

    fields: dict[str, Any]


# ── Marketplace / abuse ─────────────────────────────────────────────────


class ForkRequest(BaseModel):
    new_slug: str | None = Field(default=None, pattern=SLUG_PATTERN)


class ReportRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class QuarantineRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


# ── Run / audit ─────────────────────────────────────────────────────────


class RunContext(BaseModel):
    conversation_id: int | None = None
    message_id: int | None = None
    selected_text: str | None = Field(default=None, max_length=10000)


class RunRequest(BaseModel):
    action_id: str = Field(min_length=1, max_length=100)
    context: RunContext
    test_mode: bool = False


class RunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    function_id: int
    version_no: int
    action_id: str
    triggered_by_user_id: int
    context_type: str
    conversation_id: int | None
    message_id: int | None
    status: str
    error_message: str | None
    duration_ms: int | None
    started_at: datetime
    ended_at: datetime | None


class RunDetail(RunRead):
    request_payload_json: dict[str, Any]
    events_json: list[dict[str, Any]]


# ── ChatRuntime integration ─────────────────────────────────────────────


class EnabledAction(BaseModel):
    function_slug: str
    action_id: str
    name: str
    icon_data_url: str | None
    function_version: int


class EnabledActionsResponse(BaseModel):
    actions: list[EnabledAction]
