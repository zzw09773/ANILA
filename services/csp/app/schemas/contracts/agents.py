# -*- coding: utf-8 -*-
"""Agent Registry 契約(doc 05 §3/§4/§9,Slice 5a)。

本檔集中 Agent Registry 升級的**封閉字彙**與**Full Trace manifest 契約**,
DB 層一律存開放 String / JSON(SQLite create_all 相容,不用 PG 原生 enum),
契約層 fail-closed 把關(同 ``contracts.classification`` / ``contracts.policy``
模式)。

- :class:`RuntimeType`(doc 05 §3,5 值):agent runtime 種類;
  ``openwebui_pipe_compatible`` 為**預留值**(doc 05 §9 / doc 06 拍板:v1 不
  交付 bridge,既有 Pipe agent 人工重註冊)。
- :class:`ApprovalStatus`(doc 05 §3,7 值,v0.2 目標):審批拆三關 ——
  連得上(connection test)、trace 過(trace-test,Full Trace 是 approval
  blocker 非 enhancement)、安全審查。現況三值 pending/approved/rejected 由
  migration r1_0004 backfill 進七值。
- :class:`AuditLevel`:v1 policy「approved Agent 必為 full_trace」(doc 05 §2)。
- :class:`AgentManifest`(doc 05 §4):``GET /.well-known/anila-agent.json`` 的
  逐字欄位契約;fail-closed(``extra="forbid"``),未知欄位 / 型別錯 → 422。

狀態機(doc 05 §3 註解「拆三關把守」):

    draft
      → pending_connection_test   (註冊落地的第一關)
      → pending_trace_test        (連線測試過)
      → pending_security_review   (trace-test 過;trace_test_passed_at 落章)
      → approved                  (安全審查/admin 核准)

    任一非終態 → rejected / disabled 為合法旁支。
    approved / rejected / disabled 為終態(approve 只在 pending_security_review
    且 trace_test_passed_at 非空時允許)。
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.contracts.classification import ClassificationLevel


class RuntimeType(str, enum.Enum):
    """doc 05 §3 runtime_type 5 值(逐字,順序照文件)。"""

    ANILA_AGENT = "anila_agent"
    LANGCHAIN = "langchain"
    # 預留值:v1 不交付 bridge(doc 05 §9 / doc 06 拍板);既有 Pipe agent 人工重註冊。
    OPENWEBUI_PIPE_COMPATIBLE = "openwebui_pipe_compatible"
    OPENAI_COMPATIBLE_AGENT = "openai_compatible_agent"
    CUSTOM_HTTP = "custom_http"


# 現況(有 endpoint_url)agent 的 runtime_type backfill 預設(doc 05 §3 未另
# 指定 backfill;既有全為 OpenAI-compatible endpoint proxy → openai_compatible_agent)。
DEFAULT_RUNTIME_TYPE = RuntimeType.OPENAI_COMPATIBLE_AGENT.value


class ApprovalStatus(str, enum.Enum):
    """doc 05 §3 approval_status 7 值(v0.2 目標,逐字,順序照文件)。"""

    DRAFT = "draft"
    PENDING_CONNECTION_TEST = "pending_connection_test"
    PENDING_TRACE_TEST = "pending_trace_test"
    PENDING_SECURITY_REVIEW = "pending_security_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    DISABLED = "disabled"


# 現況三值 → 七值 backfill(migration r1_0004 upgrade;pending 是唯一需搬遷的值)。
LEGACY_APPROVAL_BACKFILL: dict[str, str] = {
    "pending": ApprovalStatus.PENDING_CONNECTION_TEST.value,
    "approved": ApprovalStatus.APPROVED.value,
    "rejected": ApprovalStatus.REJECTED.value,
}

# 七值 → 三值 downgrade(對 pending/approved/rejected 三者無損還原;新增的四值
# fail-closed 收斂:未核准者一律回 pending、disabled 併入 rejected)。
DOWNGRADE_APPROVAL_MAP: dict[str, str] = {
    ApprovalStatus.DRAFT.value: "pending",
    ApprovalStatus.PENDING_CONNECTION_TEST.value: "pending",
    ApprovalStatus.PENDING_TRACE_TEST.value: "pending",
    ApprovalStatus.PENDING_SECURITY_REVIEW.value: "pending",
    ApprovalStatus.APPROVED.value: "approved",
    ApprovalStatus.REJECTED.value: "rejected",
    ApprovalStatus.DISABLED.value: "rejected",
}

# 註冊落地的預設審批狀態(現況「pending」的七值等價;第一關 = 連線測試)。
REGISTER_DEFAULT_APPROVAL = ApprovalStatus.PENDING_CONNECTION_TEST.value

# 可執行 trace-test 的前置狀態(非終態、尚未核准);通過後一律推進至
# pending_security_review(doc 05 §3 狀態機:trace 過 → 安全審查關卡)。
TRACE_TEST_ELIGIBLE_STATES: frozenset[str] = frozenset(
    {
        ApprovalStatus.DRAFT.value,
        ApprovalStatus.PENDING_CONNECTION_TEST.value,
        ApprovalStatus.PENDING_TRACE_TEST.value,
        ApprovalStatus.PENDING_SECURITY_REVIEW.value,
    }
)
STATE_AFTER_TRACE_PASS = ApprovalStatus.PENDING_SECURITY_REVIEW.value


class AuditLevel(str, enum.Enum):
    """doc 05 §2 v1 policy:approved Agent 必為 full_trace。"""

    FULL_TRACE = "full_trace"


DEFAULT_AUDIT_LEVEL = AuditLevel.FULL_TRACE.value


class TraceCallbackMode(str, enum.Enum):
    """Full Trace 回報通道(doc 05 §4 manifest trace.callback_mode)。"""

    SSE_AND_POST = "sse_and_post"
    SSE = "sse"
    POST = "post"


# ── Manifest 契約(doc 05 §4,GET /.well-known/anila-agent.json 逐字欄位)──────


class ManifestCapabilities(BaseModel):
    """doc 05 §4 capabilities:retrieval / tools / streaming。"""

    retrieval: bool = False
    tools: list[str] = Field(default_factory=list)
    streaming: bool = False


class ManifestTrace(BaseModel):
    """doc 05 §4 trace:required / protocol / callback_mode(fail-closed 定值)。"""

    required: bool = True
    protocol: Literal["anila-full-trace-v1"] = "anila-full-trace-v1"
    callback_mode: TraceCallbackMode = TraceCallbackMode.SSE_AND_POST


class ManifestClassification(BaseModel):
    """doc 05 §4 classification:ceiling / default(五級,fail-closed)。"""

    ceiling: ClassificationLevel
    default: ClassificationLevel


class AgentManifest(BaseModel):
    """``GET /.well-known/anila-agent.json`` 逐字契約(doc 05 §4)。

    ``extra="forbid"`` —— 未知頂層欄位一律 422(fail-closed);型別 / enum 錯值
    由 Pydantic 擋下。``classification`` 無預設 → 必填(分類上限/預設不允許憑空
    落定)。
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


class TraceTestItemStatus(str, enum.Enum):
    """trace-test 單項檢查結果(doc 06 §8 檢核表逐項)。"""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class TraceTestItem(BaseModel):
    """trace-test 報告的單項(name / status / required / detail)。"""

    name: str
    status: TraceTestItemStatus
    required: bool
    detail: str = ""


class TraceTestReport(BaseModel):
    """``POST /api/agents/{id}/trace-test`` 回應 = 逐項報告 + 綜合判定。

    ``passed`` = 所有 ``required`` 項皆 PASSED(doc 06 §6:8 類 span 齊備才可
    離開 dev/test);非 required 的條件式/未可查項以 SKIPPED 記錄,不擋關。
    """

    passed: bool
    trace_id: str
    items: list[TraceTestItem]
    checked_at: datetime
