# -*- coding: utf-8 -*-
"""Artifact 契約四表(doc 01 §Artifact 群、doc 02 ArtifactJob、doc 08 §5,
Slice 8a)。

Studio 五類產出(slides / report / mindmap / infographic / datatable)的
持久化骨幹 —— doc 02 §8 明列 blocker:「Studio restart 後 job 不應丟失」,
現況五 pipeline 的 job 狀態全在 process memory。本檔把 job 生命週期與
產出物件搬進 CSP DB(Studio 經 HTTP + service token 回報,不直讀 CSP DB,
doc 10 §12 邊界)。

- :class:`ArtifactJob` — doc 02 ArtifactJob schema 逐欄(``job_id`` 為
  studio uuid PK;``artifact_type`` 對 doc 的 ``type``;``status`` 四值
  queued/running/completed/failed)。**不屬** doc 08 §5 的分類資源,故無
  四共通分類欄位;分類掛在成品 :class:`Artifact` 上。
- :class:`Artifact` — doc 01 Artifact(``artifact_type`` 五值、狀態四值、
  binding 規則:必綁 ``source_task_id`` 或 ``source_snapshot_id``,
  constitution §6 凍結未綁 Task 的 artifact 產出)+ doc 08 §5 四共通分類
  欄位。
- :class:`ArtifactVersion` — doc 01 ArtifactVersion(version 遞增、
  file_refs、citation_map、generated_by_*);每版記當時 effective 分類。
- :class:`ExportRecord` — doc 01 ExportRecord + doc 08 §5 四共通分類欄位;
  **只在** classification policy 核可後落列(deny 不落 allow 列,
  doc 00 §6「未通過 classification policy 的資料匯出 frozen」)。

循環相依處理(對齊 ``task.py`` 慣例):``Artifact.job_id`` 只存字串參照、
不掛 FK(``ArtifactJob.artifact_id`` 反向已有 FK,雙向 FK 會成環,
SQLite create_all 無法 use_alter);參照完整性由 service 層維護。
enum 欄位一律存開放 String,封閉 enum 在契約層
(``app.schemas.contracts.artifacts``)把關。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from app.database import Base

JSONValue = JSON().with_variant(JSONB, "postgresql")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Artifact(Base):
    """一件 Studio / task 產出物件;必綁 task 或 source_snapshot。"""

    __tablename__ = "artifacts"
    __table_args__ = (
        Index("ix_artifacts_source_task_id", "source_task_id"),
        Index("ix_artifacts_artifact_type", "artifact_type"),
        Index("ix_artifacts_owner_user_id", "owner_user_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    # doc 01 五值:report/slides/mindmap/infographic/datatable(契約層封閉)。
    artifact_type = Column(String(32), nullable=False)
    title = Column(String(500), nullable=False, default="未命名產出")
    # doc 01 四值:queued/generating/completed/failed;註冊 = 已產出成品。
    status = Column(String(20), nullable=False, default="completed",
                    server_default="completed")
    owner_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # binding 規則:兩者至少一(constitution §6);service 層 fail-closed
    # 驗證。SET NULL 保留成品史(刪 task/snapshot 不連帶抹除產出)。
    source_task_id = Column(
        Integer, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    source_snapshot_id = Column(
        Integer, ForeignKey("source_snapshots.id", ondelete="SET NULL"),
        nullable=True,
    )
    # studio job 參照;無 FK(避免與 artifact_jobs.artifact_id 成環)。
    job_id = Column(String(64), nullable=True, index=True)
    current_version = Column(Integer, nullable=False, default=1,
                             server_default="1")
    trace_id = Column(String(64), nullable=True, index=True)
    metadata_json = Column(JSONValue, nullable=True)
    # doc 08 §5 四共通分類欄位(effective = max(explicit, task, snapshot),
    # 單向閂鎖由 policy 核心維護)。
    classification_level = Column(String(20), nullable=False,
                                  default="無機密", server_default="無機密")
    classification_latched_at = Column(DateTime, nullable=True)
    classification_source = Column(String(50), nullable=True)
    classification_event_id = Column(
        Integer,
        ForeignKey("classification_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    updated_at = Column(DateTime, nullable=False, default=_utcnow,
                        onupdate=_utcnow)

    # ORM cascade(SQLite 測試不開 FK pragma;PG 另有 ON DELETE CASCADE)。
    versions = relationship(
        "ArtifactVersion", back_populates="artifact",
        cascade="all, delete-orphan",
        order_by="ArtifactVersion.version",
    )
    exports = relationship(
        "ExportRecord", back_populates="artifact",
        cascade="all, delete-orphan",
    )


class ArtifactVersion(Base):
    """Artifact 的一個版本(version 由 1 起遞增,同 artifact 內唯一)。"""

    __tablename__ = "artifact_versions"
    __table_args__ = (
        UniqueConstraint("artifact_id", "version",
                         name="uq_artifact_versions_artifact_version"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    artifact_id = Column(
        Integer, ForeignKey("artifacts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    version = Column(Integer, nullable=False, default=1)
    storage_ref = Column(String(1000), nullable=True)
    content_hash = Column(String(64), nullable=True)
    file_refs = Column(JSONValue, nullable=False, default=list)
    citation_map = Column(JSONValue, nullable=True)
    generated_by_model_id = Column(Integer, nullable=True)
    generated_by_agent_id = Column(Integer, nullable=True)
    generated_by_studio_job_id = Column(String(64), nullable=True)
    # 每版記當時 effective 分類(隨 artifact 單向閂鎖同步)。
    classification_level = Column(String(20), nullable=False,
                                  default="無機密", server_default="無機密")
    created_at = Column(DateTime, nullable=False, default=_utcnow)

    artifact = relationship("Artifact", back_populates="versions")


class ArtifactJob(Base):
    """Studio 五 pipeline 的持久化 job(doc 02 ArtifactJob schema 逐欄)。

    ``job_id`` = studio uuid(PK);``artifact_id`` 完成時回填。restart 後
    Studio 可自本表恢復,不再丟 job(doc 02 §8 failure model)。
    """

    __tablename__ = "artifact_jobs"
    __table_args__ = (
        Index("ix_artifact_jobs_status", "status"),
        Index("ix_artifact_jobs_task_id", "task_id"),
        Index("ix_artifact_jobs_artifact_type", "artifact_type"),
    )

    # doc 02:id: string(studio uuid)。
    job_id = Column(String(64), primary_key=True)
    owner_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # 卡登身分為員編(騎在 username);保留原始員編字串供稽核。
    requester_employee_id = Column(String(32), nullable=True)
    collection_id = Column(Integer, nullable=True)
    task_id = Column(
        Integer, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True
    )
    source_snapshot_id = Column(
        Integer, ForeignKey("source_snapshots.id", ondelete="SET NULL"),
        nullable=True,
    )
    # doc 02 type 五值(契約層封閉)。
    artifact_type = Column(String(32), nullable=False)
    # doc 02 status 四值:queued/running/completed/failed。
    status = Column(String(20), nullable=False, default="queued",
                    server_default="queued")
    progress = Column(Integer, nullable=False, default=0, server_default="0")
    message = Column(Text, nullable=True)
    result_metadata = Column(JSONValue, nullable=True)
    artifact_files = Column(JSONValue, nullable=False, default=list)
    # doc 02 error?: {code, message}。
    error = Column(JSONValue, nullable=True)
    params_digest = Column(String(64), nullable=True)
    trace_id = Column(String(64), nullable=True, index=True)
    # 完成時回填的成品參照(無 FK 環:見模組 docstring 反向由此掛 FK)。
    artifact_id = Column(
        Integer, ForeignKey("artifacts.id", ondelete="SET NULL"), nullable=True
    )
    created_at = Column(DateTime, nullable=False, default=_utcnow)
    updated_at = Column(DateTime, nullable=False, default=_utcnow,
                        onupdate=_utcnow)
    expires_at = Column(DateTime, nullable=True)


class ExportRecord(Base):
    """一次(已核可的)匯出(doc 01 ExportRecord + doc 08 §5 四共通分類欄位)。

    只在 classification policy 核可後落列(doc 00 §6);deny 只寫
    PolicyDecision、不落本表(不存在被標為 allow 的 deny 匯出)。
    """

    __tablename__ = "export_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    artifact_id = Column(
        Integer, ForeignKey("artifacts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    artifact_version_id = Column(
        Integer, ForeignKey("artifact_versions.id", ondelete="SET NULL"),
        nullable=True,
    )
    exporter_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    exporter_employee_id = Column(String(32), nullable=True)
    # 匯出目的地與其分類下限(doc 08 §10 匯出判定式的輸入)。
    target_space = Column(String(100), nullable=True)
    target_classification_floor = Column(String(20), nullable=True)
    export_format = Column(String(32), nullable=True)
    # 掛回核可的 PolicyDecision(allow)。
    policy_decision_id = Column(
        Integer, ForeignKey("policy_decisions.id", ondelete="SET NULL"),
        nullable=True,
    )
    decision = Column(String(20), nullable=False, default="allow",
                      server_default="allow")
    trace_id = Column(String(64), nullable=True)
    # doc 08 §5 四共通分類欄位(匯出當下 artifact 的 effective 分類)。
    classification_level = Column(String(20), nullable=False,
                                  default="無機密", server_default="無機密")
    classification_latched_at = Column(DateTime, nullable=True)
    classification_source = Column(String(50), nullable=True)
    classification_event_id = Column(
        Integer,
        ForeignKey("classification_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime, nullable=False, default=_utcnow)

    artifact = relationship("Artifact", back_populates="exports")
