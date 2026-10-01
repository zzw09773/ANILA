"""設定匯出／匯入的檔案形狀。

金鑰只允許出現在 ``credential``。匯出永遠寫 null。多出來的欄位
（例如 api_key、credential_envelope）直接拒絕，避免密文混進別欄。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

SCHEMA_VERSION = 1

_MAX_ITEMS = 500


def _cap(items: list, label: str) -> list:
    if len(items) > _MAX_ITEMS:
        raise ValueError(f"{label}超過 {_MAX_ITEMS} 筆")
    return items


class TrustedHostTransfer(BaseModel):
    host: str
    note: str | None = None

    model_config = {"extra": "forbid"}


class ModelTransfer(BaseModel):
    name: str
    display_name: str
    model_type: str
    endpoint_url: str | None = None
    # 匯出者看不到位址時為 true，endpoint_url 留空。匯入略過該模型。
    endpoint_hidden: bool = False
    api_version: Literal["v1", "v2"] = "v1"
    protocol: str = "openai_compatible"
    router_enabled: bool = False
    max_concurrent: int | None = 16
    is_active: bool = True
    is_internal: bool = True
    description: str | None = None
    context_window: int | None = None
    classification_ceiling: str | None = None
    supports_streaming: bool = True
    supports_json_schema: bool = False
    supports_tools: bool = False
    thinking_effort: str | None = None
    thinking_user_selectable: bool = True
    temperature: float | None = None
    top_p: float | None = None
    presence_penalty: float | None = None
    max_tokens: int | None = None
    base_model_name: str | None = None
    owner_department_path: list[str] | None = None
    credential: str | None = None
    credential_required: bool = False

    model_config = {"extra": "forbid"}

    @field_validator("name", "display_name")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise ValueError("名稱不可為空")
        return value

    @field_validator("endpoint_url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @model_validator(mode="after")
    def _endpoint_required(self):
        if self.endpoint_hidden:
            return self
        if not self.endpoint_url:
            raise ValueError("端點位址不可為空")
        return self

    @field_validator("base_model_name")
    @classmethod
    def _base_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("owner_department_path")
    @classmethod
    def _dept_path(cls, value: list[str] | None) -> list[str] | None:
        if not value:
            return None
        cleaned: list[str] = []
        for part in value:
            name = (part or "").strip()
            if not name:
                raise ValueError("部門路徑不可有空名稱")
            cleaned.append(name)
        return cleaned


class ModelRoleTransfer(BaseModel):
    role: str
    model_name: str

    model_config = {"extra": "forbid"}

    @field_validator("role", "model_name")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise ValueError("角色或模型名稱不可為空")
        return value


class RouterGrantTransfer(BaseModel):
    model_name: str
    scope: Literal["all"]

    model_config = {"extra": "forbid"}

    @field_validator("model_name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise ValueError("模型名稱不可為空")
        return value


class ExternalServiceTransfer(BaseModel):
    kind: Literal["document_parser", "speech"]
    enabled: bool
    base_url: str = ""
    protocol: str | None = None
    openai_model: str | None = None
    credential: str | None = None
    credential_required: bool = False

    model_config = {"extra": "forbid"}

    @field_validator("base_url")
    @classmethod
    def _url(cls, value: str) -> str:
        return (value or "").strip()


class DepartmentTransfer(BaseModel):
    path: list[str] = Field(min_length=1)
    description: str | None = None
    is_active: bool = True

    model_config = {"extra": "forbid"}

    @field_validator("path")
    @classmethod
    def _path(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for part in value:
            name = (part or "").strip()
            if not name:
                raise ValueError("部門名稱不可為空")
            if len(name) > 100:
                raise ValueError("部門名稱過長")
            cleaned.append(name)
        if len(cleaned) > 8:
            raise ValueError("部門路徑過長")
        return cleaned

    @field_validator("description")
    @classmethod
    def _description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        if len(value) > 255:
            raise ValueError("部門說明過長")
        return value


class BannerTransfer(BaseModel):
    level: str = "info"
    content: str
    show_on_login: bool = False
    sort_order: int = 0

    model_config = {"extra": "forbid"}


class SettingsDocument(BaseModel):
    schema_version: Literal[1]
    trusted_hosts: list[TrustedHostTransfer] = Field(default_factory=list)
    models: list[ModelTransfer] = Field(default_factory=list)
    model_roles: list[ModelRoleTransfer] = Field(default_factory=list)
    router_grants: list[RouterGrantTransfer] = Field(default_factory=list)
    external_services: list[ExternalServiceTransfer] = Field(default_factory=list)
    departments: list[DepartmentTransfer] = Field(default_factory=list)
    banners: list[BannerTransfer] = Field(default_factory=list)

    model_config = {"extra": "forbid"}

    @field_validator("trusted_hosts")
    @classmethod
    def _hosts(cls, value: list) -> list:
        return _cap(value, "信任主機")

    @field_validator("models")
    @classmethod
    def _models(cls, value: list) -> list:
        return _cap(value, "模型")

    @field_validator("model_roles")
    @classmethod
    def _roles(cls, value: list) -> list:
        return _cap(value, "模型角色")

    @field_validator("router_grants")
    @classmethod
    def _grants(cls, value: list) -> list:
        return _cap(value, "全院授權")

    @field_validator("external_services")
    @classmethod
    def _services(cls, value: list) -> list:
        return _cap(value, "外部服務")

    @field_validator("departments")
    @classmethod
    def _departments(cls, value: list) -> list:
        return _cap(value, "部門")

    @field_validator("banners")
    @classmethod
    def _banners(cls, value: list) -> list:
        return _cap(value, "公告")


class SettingsImportRequest(BaseModel):
    dry_run: bool = False
    document: SettingsDocument

    model_config = {"extra": "forbid"}
