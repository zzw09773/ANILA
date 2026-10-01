# -*- coding: utf-8 -*-
"""使用者自訂 skill 的請求與回應。長度在這裡擋，服務層再擋一次。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

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
