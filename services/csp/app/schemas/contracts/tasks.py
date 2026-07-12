# -*- coding: utf-8 -*-
"""Task / TaskRun / SourceSnapshot / Citation 契約(doc 01 §3–5,Slice 2a)。

DB 層(app/models/task.py、source_snapshot.py)存開放 String;封閉 enum
在這裡把關 —— API 進出一律走本模組型別,未知值 fail-closed 拋驗證錯誤。
分類等級沿用 ``anila_contracts.Classification``（在 CSP 內以舊名稱
``ClassificationLevel`` 相容引用；五級繁中字串），
不另定義。
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from anila_contracts import Classification as ClassificationLevel


class TaskType(str, enum.Enum):
    """doc 01 Task.task_type 八值。"""

    QUERY = "query"
    SUMMARIZE = "summarize"
    ANALYZE = "analyze"
    COMPARE = "compare"
    DRAFT = "draft"
    GENERATE_ARTIFACT = "generate_artifact"
    LAUNCH_SERVICE = "launch_service"
    GOVERNANCE = "governance"


class TaskStatus(str, enum.Enum):
    """doc 01 Task.status 十值狀態機;宣告順序即典型生命週期。"""

    DRAFT = "draft"
    SUBMITTED = "submitted"
    POLICY_CHECKING = "policy_checking"
    SOURCE_RESOLVING = "source_resolving"
    RUNNING = "running"
    WAITING_FOR_USER = "waiting_for_user"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED_BY_POLICY = "blocked_by_policy"


class SourceScope(str, enum.Enum):
    """doc 01 Task.source_scope 五值(Collection 三層 + none +
    registered_service)。"""

    NONE = "none"
    PERSONAL = "personal"
    PROJECT = "project"
    ORGANIZATION = "organization"
    REGISTERED_SERVICE = "registered_service"


class RequestedOutputType(str, enum.Enum):
    """doc 01 Task.requested_output_type 七值。"""

    ANSWER = "answer"
    REPORT = "report"
    SLIDES = "slides"
    MINDMAP = "mindmap"
    INFOGRAPHIC = "infographic"
    DATATABLE = "datatable"
    SERVICE_LAUNCH = "service_launch"


class TaskRunStatus(str, enum.Enum):
    """doc 01 TaskRun.status 五值。"""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DispatchTarget(str, enum.Enum):
    """TaskRun 派發目的地四值(Slice 2a;語意承 doc 01 run_type)。"""

    MODEL = "model"
    AGENT = "agent"
    STUDIO = "studio"
    SERVICE = "service"


class SnapshotOrigin(str, enum.Enum):
    """SourceSnapshot 來源型態五值。"""

    COLLECTION = "collection"
    DOCUMENT = "document"
    UPLOAD = "upload"
    NONE = "none"
    SERVICE = "service"


class CitationUsedBy(str, enum.Enum):
    """doc 01 Citation.used_by 三值。"""

    ANSWER = "answer"
    ARTIFACT = "artifact"
    AGENT_TOOL = "agent_tool"


class TaskCreate(BaseModel):
    """POST /api/tasks 的建立 payload(doc 09 §Task API;端點屬後續
    slice,本契約先行)。"""

    title: str = Field(min_length=1, max_length=255)
    task_type: TaskType
    source_scope: SourceScope = SourceScope.NONE
    selected_collection_ids: list[int] = Field(default_factory=list)
    selected_service_id: str | None = None
    requested_output_type: RequestedOutputType | None = None
    conversation_id: int | None = None
    classification_level: ClassificationLevel = ClassificationLevel.UNCLASSIFIED


class TaskOut(BaseModel):
    """Task 讀出契約(from ORM)。"""

    id: int
    title: str
    task_type: TaskType
    status: TaskStatus
    source_scope: SourceScope
    requester_user_id: int
    department_id: int | None = None
    conversation_id: int | None = None
    selected_collection_ids: list[int]
    selected_service_id: str | None = None
    requested_output_type: RequestedOutputType | None = None
    source_snapshot_id: int | None = None
    policy_decision_id: int | None = None
    legacy_runtime_call: bool
    classification_level: ClassificationLevel
    trace_id: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class TaskRunOut(BaseModel):
    """TaskRun 讀出契約(from ORM)。"""

    id: int
    task_id: int
    run_sequence: int
    dispatch_target: DispatchTarget
    status: TaskRunStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    usage_record_id: int | None = None
    error: dict[str, Any] | None = None
    classification_level: ClassificationLevel
    created_at: datetime

    model_config = {"from_attributes": True}


class SourceSnapshotIn(BaseModel):
    """建立 snapshot 的輸入契約。

    ``classification_level`` 缺省 ``None``:三規則之 3(snapshot 分類 =
    來源最高分類)由 service 層以 max 規則計算,呼叫端不得指定低於
    來源的等級 —— 指定值僅作 floor 參與 max。
    """

    origin: SnapshotOrigin = SnapshotOrigin.NONE
    source_scope: SourceScope = SourceScope.NONE
    collection_ids: list[int] = Field(default_factory=list)
    document_ids: list[int] = Field(default_factory=list)
    chunk_ids: list[str] = Field(default_factory=list)
    document_versions: dict[str, str] | None = None
    retrieval_queries: list[str] = Field(default_factory=list)
    content_hash: str | None = None
    payload_ref: str | None = None
    classification_level: ClassificationLevel | None = None


class SourceSnapshotOut(BaseModel):
    """SourceSnapshot 讀出契約(from ORM)。"""

    id: int
    task_id: int
    origin: SnapshotOrigin
    source_scope: SourceScope
    collection_ids: list[int]
    document_ids: list[int]
    chunk_ids: list[str]
    document_versions: dict[str, str] | None = None
    retrieval_queries: list[str]
    content_hash: str | None = None
    payload_ref: str | None = None
    classification_level: ClassificationLevel
    created_at: datetime

    model_config = {"from_attributes": True}


class CitationOut(BaseModel):
    """Citation 讀出契約(from ORM);只指 snapshot 內 chunk(規則 2)。"""

    id: int
    source_snapshot_id: int
    document_id: int | None = None
    chunk_id: str
    quote_preview: str | None = None
    page: int | None = None
    score: float | None = None
    span_start: int | None = None
    span_end: int | None = None
    used_by: CitationUsedBy
    classification_level: ClassificationLevel
    created_at: datetime

    model_config = {"from_attributes": True}
