# -*- coding: utf-8 -*-
"""使用者自訂 skill 的請求與回應。長度在這裡擋，服務層再擋一次。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.schemas.base import ApiResponseModel

NAME_MAX = 40
DESCRIPTION_MAX = 200
BODY_MAX = 8000
REJECT_REASON_MAX = 500


class SkillWrite(BaseModel):
    name: str = Field(min_length=1, max_length=NAME_MAX)
    description: str = Field(min_length=1, max_length=DESCRIPTION_MAX)
    body: str = Field(min_length=1, max_length=BODY_MAX)
    scope: str = Field(pattern="^(personal|unit|campus)$")
    department_id: int | None = None
    auto_apply: bool = False
    submit: bool = False


class SkillUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=NAME_MAX)
    description: str = Field(min_length=1, max_length=DESCRIPTION_MAX)
    body: str = Field(min_length=1, max_length=BODY_MAX)
    auto_apply: bool = False


class SkillSubmit(BaseModel):
    scope: str = Field(pattern="^(unit|campus)$")
    department_id: int | None = None


class SkillReject(BaseModel):
    reason: str = Field(min_length=1, max_length=REJECT_REASON_MAX)


class SkillOut(ApiResponseModel):
    id: int
    version_id: int
    version: int
    name: str
    description: str
    body: str
    scope: str
    department_id: int | None = None
    auto_apply: bool
    status: str
    reject_reason: str | None = None
    owner_user_id: int
    published_version: int | None = None
    usable: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SkillListOut(ApiResponseModel):
    skills: list[SkillOut]


class SkillReviewItem(ApiResponseModel):
    version_id: int
    lineage_id: int
    version: int
    name: str
    description: str
    body: str
    scope: str
    department_id: int | None = None
    department_name: str | None = None
    owner_user_id: int
    owner_username: str | None = None
    auto_apply: bool
    status: str
    reject_reason: str | None = None


class SkillReviewListOut(ApiResponseModel):
    pending: list[SkillReviewItem]
    published: list[SkillReviewItem]


class SkillPublishUnit(ApiResponseModel):
    id: int
    name: str


class SkillPublishTargets(ApiResponseModel):
    campus: bool
    units: list[SkillPublishUnit]
    submit_units: list[SkillPublishUnit] = Field(default_factory=list)


class SkillAssistIn(BaseModel):
    """協助撰寫。草稿可空，過長在這裡截斷，不把整段送進模型。"""

    goal: str = Field(min_length=1, max_length=1000)
    name: str | None = None
    description: str | None = None
    body: str | None = None
    mode: Literal["create", "improve"]

    @field_validator("goal")
    @classmethod
    def _goal_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("請說明想讓 skill 做什麼")
        return text

    @field_validator("name", "description", "body")
    @classmethod
    def _clip_draft(cls, value: str | None, info) -> str:
        if not value:
            return ""
        limit = {
            "name": NAME_MAX,
            "description": DESCRIPTION_MAX,
            "body": BODY_MAX,
        }[info.field_name]
        return value.strip()[:limit]


class SkillAssistOut(BaseModel):
    name: str = Field(max_length=NAME_MAX)
    description: str = Field(max_length=DESCRIPTION_MAX)
    body: str = Field(max_length=BODY_MAX)
    notes: list[str]
