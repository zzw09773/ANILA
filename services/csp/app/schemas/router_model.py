"""Request/response shapes for Router model selection."""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class RouterModelOut(BaseModel):
    id: int
    name: str
    display_name: str
    health_status: str = "unknown"
    grant_sources: list[str] = []
    thinking_effort: str | None = None
    thinking_levels_supported: list[str] | None = None
    thinking_user_selectable: bool = True


class RouterModelListOut(BaseModel):
    models: list[RouterModelOut]
    default_model_id: Optional[int] = None


class RouterModelChoiceIn(BaseModel):
    router_model_id: int
    expected_version: int = 0


class RouterModelChoiceOut(BaseModel):
    router_model_id: int
    router_model_name: str
    router_selection_version: int


class CampusDefaultIn(BaseModel):
    model_id: int


class RouterGrantIn(BaseModel):
    scope_type: Literal["all", "department", "group", "user"]
    department_id: Optional[int] = None
    group_id: Optional[int] = None
    user_id: Optional[int] = None
    include_descendants: bool = False
    expires_at: Optional[datetime] = None


class RouterGrantOut(RouterGrantIn):
    id: int
    model_id: int
    department_name: Optional[str] = None
    group_name: Optional[str] = None
    username: Optional[str] = None


class RouterGrantsReplaceIn(BaseModel):
    grants: list[RouterGrantIn] = Field(default_factory=list)


class ModelAccessGroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    is_active: bool = True


class ModelAccessGroupOut(BaseModel):
    id: int
    name: str
    is_active: bool
    member_count: int = 0


class ModelAccessGroupMembersIn(BaseModel):
    user_ids: list[int] = Field(default_factory=list)


class GroupMemberOut(BaseModel):
    id: int
    username: str
    department: Optional[str] = None


class GroupLinkedModelOut(BaseModel):
    id: int
    name: str
    display_name: str


class UserRouterModelOut(BaseModel):
    id: int
    name: str
    display_name: str
    grant_sources: list[str] = Field(default_factory=list)
