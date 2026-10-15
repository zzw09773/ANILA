"""MCP 治理請求；身分與權限一律由登入狀態取得。"""
from pydantic import BaseModel, ConfigDict, Field, model_validator


class MCPServiceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    endpoint_url: str = Field(min_length=1, max_length=2048)
    department_id: int | None = Field(default=None, gt=0)
    is_public: bool = False

    @model_validator(mode="after")
    def require_scope(self):
        if not self.is_public and self.department_id is None:
            raise ValueError("未公開的服務必須指定所屬單位")
        return self


class MCPServiceApprove(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    approved_tool_ids: list[int] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def valid_ids(self):
        if any(isinstance(i, bool) or i <= 0 for i in self.approved_tool_ids):
            raise ValueError("工具編號必須是正整數")
        if len(set(self.approved_tool_ids)) != len(self.approved_tool_ids):
            raise ValueError("工具編號不能重複")
        return self
