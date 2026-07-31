"""OW-3 message-action Pydantic contracts
(docs/plans/ow3-message-actions-blueprint.md §4 / §Q5)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator
from app.schemas.base import ApiResponseModel

# Server-owned icon allow-list (governance picker + SPA ACTION_ICONS keys).
ALLOWED_ACTION_ICONS: frozenset[str] = frozenset(
    {
        "translate",
        "summarize",
        "file-text",
        "wand",
        "sparkles",
        "languages",
        "rewrite",
        "bolt",
        "code",
        "clipboard",
        "list",
        "pen",
        "search",
        "share",
        "star",
    }
)

_CHOICE_ID_RE = re.compile(r"^[a-z0-9_-]{1,40}$")


class ChoiceSpec(BaseModel):
    id: str = Field(..., min_length=1, max_length=40)
    label: str = Field(..., min_length=1, max_length=60)
    prompt: str = Field(default="", max_length=4000)
    input: bool = False
    input_label: Optional[str] = Field(default=None, max_length=40)

    @field_validator("id")
    @classmethod
    def _id_shape(cls, value: str) -> str:
        if not _CHOICE_ID_RE.match(value):
            raise ValueError(
                "選項 id 須符合 ^[a-z0-9_-]{1,40}$"
            )
        return value


class BindingSpec(BaseModel):
    scope_type: Literal["role", "department", "user"]
    role: Optional[str] = Field(default=None, max_length=40)
    department_id: Optional[int] = None
    user_id: Optional[int] = None

    @model_validator(mode="after")
    def _exactly_one_target(self) -> BindingSpec:
        st = self.scope_type
        if st == "role":
            if not self.role or self.department_id is not None or self.user_id is not None:
                raise ValueError("scope_type=role 時必須只填 role")
        elif st == "department":
            if (
                self.department_id is None
                or self.role is not None
                or self.user_id is not None
            ):
                raise ValueError("scope_type=department 時必須只填 department_id")
        elif st == "user":
            if (
                self.user_id is None
                or self.role is not None
                or self.department_id is not None
            ):
                raise ValueError("scope_type=user 時必須只填 user_id")
        return self


class MessageActionCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    label: str = Field(..., min_length=1, max_length=120)
    icon: str = Field(..., min_length=1, max_length=40)
    body: str = Field(..., min_length=1)
    choices: list[ChoiceSpec] = Field(default_factory=list)
    notes: Optional[str] = None
    is_enabled: bool = True


class MessageActionUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    label: Optional[str] = Field(default=None, min_length=1, max_length=120)
    icon: Optional[str] = Field(default=None, min_length=1, max_length=40)
    body: Optional[str] = Field(default=None, min_length=1)
    choices: Optional[list[ChoiceSpec]] = None
    notes: Optional[str] = None
    is_enabled: Optional[bool] = None


class MessageActionOut(BaseModel):
    """User-facing visible (pressable) action — includes template body.

    Callers of ``/visible`` are already bound-or-authored and enabled, so
    the template is disclosed under the pressable-or-may-modify read rule.
    """

    id: int
    name: str
    label: str
    icon: str
    body: str
    choices: list[Any] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class MessageActionAdminOut(ApiResponseModel):
    """Management read — body when caller may press (enabled) or may modify."""

    id: int
    name: str
    label: str
    icon: str
    body: Optional[str] = None
    body_sha256: str
    choices: list[Any] = Field(default_factory=list)
    notes: Optional[str] = None
    version: int
    is_enabled: bool
    created_by_user_id: Optional[int] = None
    updated_by_user_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class BindingOut(ApiResponseModel):
    id: int
    action_id: int
    scope_type: str
    role: Optional[str] = None
    department_id: Optional[int] = None
    user_id: Optional[int] = None
    created_by: Optional[int] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class BindingsReplaceRequest(BaseModel):
    bindings: list[BindingSpec] = Field(default_factory=list)


class InvokeRequest(BaseModel):
    conversation_id: int
    message_id: int
    choice_id: Optional[str] = None
    # Length gated in service → 413 (not pydantic 422).
    input: Optional[str] = None


class InvokeResponse(BaseModel):
    invocation_id: str
    action_id: int
    version: int
    prompt: str
