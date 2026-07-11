"""Agent 執行錯誤的安全 wire envelope。"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AGENT_ERROR_SCHEMA_VERSION = "agent-error/v1"


class AgentErrorCode(str, Enum):
    """F5 已拍板的最小錯誤 taxonomy。"""

    CONTRACT_ERROR = "contract_error"
    AUTHENTICATION_ERROR = "authentication_error"
    AUTHORIZATION_ERROR = "authorization_error"
    POLICY_DENIED = "policy_denied"
    CLASSIFICATION_VIOLATION = "classification_violation"
    AGENT_UNAVAILABLE = "agent_unavailable"
    TIMEOUT = "timeout"
    STREAM_BROKEN = "stream_broken"
    CANCELLED = "cancelled"
    DOWNSTREAM_ERROR = "downstream_error"
    INTERNAL_ERROR = "internal_error"


class AgentError(BaseModel):
    """可跨 CSP、Router 與 Agent 傳輸的標準化安全錯誤。

    ``safe_message`` 是唯一可直接投影到 UI 的訊息；raw exception、secret、
    command 或未遮罩 payload 不屬於此契約。
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    schema_version: Literal["agent-error/v1"] = "agent-error/v1"
    code: AgentErrorCode
    retryable: bool
    safe_message: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    invocation_id: str = Field(min_length=1)
    step_id: str | None = Field(default=None, min_length=1)


__all__ = ["AGENT_ERROR_SCHEMA_VERSION", "AgentError", "AgentErrorCode"]
