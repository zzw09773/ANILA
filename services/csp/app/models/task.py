# -*- coding: utf-8 -*-
"""Task / TaskRun — 新系統主脊椎(doc 01 §3,Slice 2a)。

- Task 是主脊椎;Conversation 降級為互動容器(``conversation_id`` 可空,
  ON DELETE SET NULL:刪對話不動任務)。
- 每個 Task 建立時必產生 ``trace_id``(唯一,doc 01 驗收 2);每次執行/
  重跑/handoff 產生一筆 TaskRun(``run_sequence`` 遞增,同 task 內唯一)。
- 無 task_id 的舊流量相容標記 ``legacy_runtime_call``(doc 10)。
- enum 欄位(task_type / status / source_scope / requested_output_type /
  dispatch_target)一律存開放 String,封閉 enum 在 Pydantic 契約層
  (``app.schemas.contracts.tasks``)把關 — 測試套件在 SQLite 上跑
  create_all,不用 PG 原生 enum。
- ``source_snapshot_id`` / ``policy_decision_id`` 刻意不掛 DB FK:
  source_snapshots / policy_decisions 都有 task_id FK 指回本表,雙向
  FK 會形成循環相依(SQLite create_all 無法用 use_alter 補約束);
  參照完整性由 Slice 2b 的 service 層維護。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from app.database import Base

# Postgres 用 JSONB(可索引),SQLite 測試路徑退回 JSON —
# 與 message.py / agent.py / ingestion.py 同一 with_variant 模式。
JSONValue = JSON().with_variant(JSONB, "postgresql")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_trace_id() -> str:
    return uuid.uuid4().hex


class Task(Base):
    """一件正式工作單位;所有 runtime 呼叫最終都應掛在某個 Task 上。"""

    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(255), nullable=False, default="新任務")
    # doc 01 八值:query/summarize/analyze/compare/draft/generate_artifact/
    # launch_service/governance(Pydantic 層封閉)。
    task_type = Column(String(32), nullable=False)
    requester_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    department_id = Column(
        Integer, ForeignKey("departments.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Conversation 只是互動容器(doc 01 拍板);刪對話保留任務。
    conversation_id = Column(
        Integer, ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )
    # doc 01 十值狀態機:draft/submitted/policy_checking/source_resolving/
    # running/waiting_for_user/completed/failed/cancelled/blocked_by_policy。
    status = Column(String(32), nullable=False, default="draft",
                    server_default="draft", index=True)
    # doc 01 五值:none/personal/project/organization/registered_service。
    source_scope = Column(String(32), nullable=False, default="none",
                          server_default="none")
    selected_collection_ids = Column(JSONValue, nullable=False, default=list)
    selected_service_id = Column(String(100), nullable=True)
    # doc 01 七值:answer/report/slides/mindmap/infographic/datatable/
    # service_launch。
    requested_output_type = Column(String(32), nullable=True)
    # 無 DB FK(循環相依,見模組 docstring);service 層維護。
    source_snapshot_id = Column(Integer, nullable=True)
    policy_decision_id = Column(Integer, nullable=True)
    legacy_runtime_call = Column(Boolean, nullable=False, default=False,
                                 server_default="false")
    # 五級分類(ClassificationLevel)繁中字串落地;預設 無機密。
    classification_level = Column(String(20), nullable=False,
                                  default="無機密", server_default="無機密")
    # doc 08 §5 其餘三共通欄位(Slice 3a 補齊)。
    classification_latched_at = Column(DateTime, nullable=True)
    classification_source = Column(String(50), nullable=True)
    classification_event_id = Column(
        Integer,
        ForeignKey("classification_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    # 建立即產生;每 task 一條 trace(doc 02 observability ID)。
    trace_id = Column(String(64), nullable=False, unique=True, index=True,
                      default=_new_trace_id)
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    updated_at = Column(DateTime, nullable=False, default=_utcnow,
                        onupdate=_utcnow)

    requester = relationship("User", foreign_keys=[requester_user_id])
    conversation = relationship("Conversation", foreign_keys=[conversation_id])
    # ORM 層 cascade(不設 passive_deletes:SQLite 測試路徑不開 FK
    # pragma,須由 ORM 刪子列;PG 上另有 ON DELETE CASCADE 當後盾)。
    runs = relationship(
        "TaskRun", back_populates="task",
        cascade="all, delete-orphan",
        order_by="TaskRun.run_sequence",
    )
    source_snapshots = relationship(
        "SourceSnapshot", back_populates="task",
        cascade="all, delete-orphan",
    )


class TaskRun(Base):
    """Task 的一次派發執行(model / agent / studio / service)。"""

    __tablename__ = "task_runs"
    __table_args__ = (
        UniqueConstraint("task_id", "run_sequence",
                         name="uq_task_runs_task_sequence"),
        Index(
            "uq_task_runs_one_active_per_task",
            "task_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
            sqlite_where=text("status IN ('queued', 'running')"),
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(
        Integer, ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # 同一 task 內由 1 起遞增;重跑/handoff 各佔一筆。
    run_sequence = Column(Integer, nullable=False, default=1)
    # 四值:model/agent/studio/service(Pydantic 層封閉)。
    dispatch_target = Column(String(32), nullable=False)
    # doc 01 TaskRun 五值:queued/running/completed/failed/cancelled。
    status = Column(String(32), nullable=False, default="queued",
                    server_default="queued")
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    # 對應 token_usage 一列(doc 02 usage_record_id);usage 晚於 run 落地,
    # 可空、SET NULL。
    usage_record_id = Column(
        Integer, ForeignKey("token_usage.id", ondelete="SET NULL"),
        nullable=True,
    )
    # 結構化錯誤 {code, message, ...};成功時 NULL。
    error = Column(JSONValue, nullable=True)
    classification_level = Column(String(20), nullable=False,
                                  default="無機密", server_default="無機密")
    # doc 08 §5 其餘三共通欄位(Slice 3a;AgentRun 的現制對應表)。
    classification_latched_at = Column(DateTime, nullable=True)
    classification_source = Column(String(50), nullable=True)
    classification_event_id = Column(
        Integer,
        ForeignKey("classification_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=_utcnow)

    task = relationship("Task", back_populates="runs")
