from datetime import datetime
from pydantic import BaseModel, Field, field_validator
from app.schemas.base import ApiResponseModel


class DepartmentCreate(BaseModel):
    name: str
    description: str | None = None
    parent_id: int | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("部門名稱不可為空")
        return value


class DepartmentUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    is_active: bool | None = None
    parent_id: int | None = None

    @field_validator("name")
    @classmethod
    def validate_optional_name(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("部門名稱不可為空")
        return value


class DepartmentResponse(ApiResponseModel):
    id: int
    name: str
    description: str | None = None
    parent_id: int | None = None
    is_active: bool
    user_count: int = 0
    active_user_count: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class DepartmentTreeNode(BaseModel):
    """嵌套樹節點（院 → 所 → 組）。"""

    id: int
    name: str
    is_active: bool
    parent_id: int | None = None
    user_count: int = 0
    children: list["DepartmentTreeNode"] = Field(default_factory=list)


DepartmentTreeNode.model_rebuild()
