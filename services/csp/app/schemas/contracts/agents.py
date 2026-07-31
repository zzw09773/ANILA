# -*- coding: utf-8 -*-
"""Agent Registry 契約(OE-1 三態 + 自我描述 / 診斷殘餘)。

SYSTEM-MAP §「知識庫怎麼運作」八步表:註冊拿 key → admin 指派誰能用 → 使用者選它。
無連線／trace／安全審查三關。``approval_status`` 只認三值:

    registered → approved → disabled

DB 層一律存開放 String / JSON(SQLite create_all 相容,不用 PG 原生 enum),
契約層 fail-closed 把關(同 ``contracts.classification`` / ``contracts.policy``
模式)。

- :class:`RuntimeType`:agent runtime 種類。
- :class:`ApprovalStatus`(OE-1,3 值):registered / approved / disabled。
- :class:`AuditLevel`:殘餘欄位字彙(不再是核准硬閘;D1 前仍可能出現在列上)。
- :class:`AgentManifest`:``GET /.well-known/anila-agent.json`` 的選填自我描述契約
  (仍 fail-closed 驗證;不再驅動核准狀態機)。
"""

from __future__ import annotations

import enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.contracts.classification import ClassificationLevel


class RuntimeType(str, enum.Enum):
    """agent runtime 種類。"""

    ANILA_AGENT = "anila_agent"
    LANGCHAIN = "langchain"
    # 預留值:既有 Pipe agent 人工重註冊。
    OPENWEBUI_PIPE_COMPATIBLE = "openwebui_pipe_compatible"
    OPENAI_COMPATIBLE_AGENT = "openai_compatible_agent"
    CUSTOM_HTTP = "custom_http"


# 現況(有 endpoint_url)agent 的 runtime_type backfill 預設。
DEFAULT_RUNTIME_TYPE = RuntimeType.OPENAI_COMPATIBLE_AGENT.value


class ApprovalStatus(str, enum.Enum):
    """OE-1:approval_status 三值(SYSTEM-MAP 註冊→指派→可用)。"""

    REGISTERED = "registered"
    APPROVED = "approved"
    DISABLED = "disabled"


# 七值(及更舊三值)→ OE-1 三值。usable(approved) 不得變 unusable。
OE1_APPROVAL_UPGRADE_MAP: dict[str, str] = {
    "draft": ApprovalStatus.REGISTERED.value,
    "pending": ApprovalStatus.REGISTERED.value,
    "pending_connection_test": ApprovalStatus.REGISTERED.value,
    "pending_trace_test": ApprovalStatus.REGISTERED.value,
    "pending_security_review": ApprovalStatus.REGISTERED.value,
    "registered": ApprovalStatus.REGISTERED.value,
    "approved": ApprovalStatus.APPROVED.value,
    "rejected": ApprovalStatus.DISABLED.value,
    "disabled": ApprovalStatus.DISABLED.value,
}

# OE-1 三值 → 七值 downgrade(還原到 r1_0004 形狀;registered 回第一關)。
OE1_APPROVAL_DOWNGRADE_MAP: dict[str, str] = {
    ApprovalStatus.REGISTERED.value: "pending_connection_test",
    ApprovalStatus.APPROVED.value: "approved",
    ApprovalStatus.DISABLED.value: "disabled",
}

# 註冊落地的預設審批狀態。
REGISTER_DEFAULT_APPROVAL = ApprovalStatus.REGISTERED.value


class AuditLevel(str, enum.Enum):
    """殘餘 audit_level 字彙(不再是核准硬閘)。"""

    FULL_TRACE = "full_trace"


DEFAULT_AUDIT_LEVEL = AuditLevel.FULL_TRACE.value


class TraceCallbackMode(str, enum.Enum):
    """Full Trace 回報通道殘餘字彙(診斷用)。"""

    SSE_AND_POST = "sse_and_post"
    SSE = "sse"
    POST = "post"


# ── Manifest 契約(選填自我描述;不再驅動核准)──────────────────────────────────


class ManifestCapabilities(BaseModel):
    """capabilities:retrieval / tools / streaming。"""

    retrieval: bool = False
    tools: list[str] = Field(default_factory=list)
    streaming: bool = False


class ManifestTrace(BaseModel):
    """trace:required / protocol / callback_mode。"""

    required: bool = True
    protocol: Literal["anila-full-trace-v1"] = "anila-full-trace-v1"
    callback_mode: TraceCallbackMode = TraceCallbackMode.SSE_AND_POST


class ManifestClassification(BaseModel):
    """classification:ceiling / default(四級,fail-closed)。"""

    ceiling: ClassificationLevel
    default: ClassificationLevel


class AgentManifest(BaseModel):
    """``GET /.well-known/anila-agent.json`` 選填契約。

    ``extra="forbid"`` —— 未知頂層欄位一律 422;型別 / enum 錯值由 Pydantic 擋下。
    """

    model_config = {"extra": "forbid"}

    agent_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    runtime_type: RuntimeType
    api_version: Literal["v1"] = "v1"
    supported_task_types: list[str] = Field(default_factory=list)
    description_for_router: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    capabilities: ManifestCapabilities = Field(default_factory=ManifestCapabilities)
    trace: ManifestTrace = Field(default_factory=ManifestTrace)
    classification: ManifestClassification
