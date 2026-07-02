# -*- coding: utf-8 -*-
"""SourceSnapshot / Citation — 穩定引用的核心(doc 01 §4–5,Slice 2a)。

SourceSnapshot 三規則(doc 01 拍板):
1. 回答 / artifact / GUI service launch 必指向 snapshot 或明確宣告無來源。
2. Citation 只指向 snapshot 內的 chunk,不指向 live document —— 因此
   ``citations.document_id`` 刻意不掛 ingestion_documents FK(那會把
   citation 綁回 live 資料);它只是 snapshot ``document_ids`` 清單內
   的參照值。
3. snapshot 的 ``classification_level`` = 所有來源的最高分類(max 規則),
   由 Slice 2b 的 service 層計算與強制;本表僅落地結果,預設 無機密。

enum 欄位(origin / source_scope / used_by)存開放 String,封閉 enum 在
Pydantic 契約層把關(SQLite create_all 相容)。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from app.database import Base

JSONValue = JSON().with_variant(JSONB, "postgresql")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SourceSnapshot(Base):
    """Task 來源在檢索當下的不可變快照。"""

    __tablename__ = "source_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(
        Integer, ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # 五值:collection/document/upload/none/service(Pydantic 層封閉)。
    origin = Column(String(32), nullable=False, default="none",
                    server_default="none")
    # 對齊 Task.source_scope 五值(doc 01 SourceSnapshot.source_scope)。
    source_scope = Column(String(32), nullable=False, default="none",
                          server_default="none")
    collection_ids = Column(JSONValue, nullable=False, default=list)
    document_ids = Column(JSONValue, nullable=False, default=list)
    chunk_ids = Column(JSONValue, nullable=False, default=list)
    # document_id → 版本指紋(doc 01 document_versions)。
    document_versions = Column(JSONValue, nullable=True)
    retrieval_queries = Column(JSONValue, nullable=False, default=list)
    # 快照內容整體 sha256(不可變性驗證用)。
    content_hash = Column(String(64), nullable=True)
    # 不可變 payload 落地位置(object-store key / 檔案路徑)。
    payload_ref = Column(String(1000), nullable=True)
    # 三規則之 3:max(來源分類);service 層計算,這裡只存結果。
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
    created_at = Column(DateTime, nullable=False, default=_utcnow)

    task = relationship("Task", back_populates="source_snapshots")
    # 不設 passive_deletes:SQLite 測試不開 FK pragma,由 ORM 刪子列;
    # PG 上另有 ON DELETE CASCADE 當後盾。
    citations = relationship(
        "Citation", back_populates="source_snapshot",
        cascade="all, delete-orphan",
    )


class Citation(Base):
    """指向 snapshot 內 chunk 的引用(絕不指 live document,規則 2)。"""

    __tablename__ = "citations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_snapshot_id = Column(
        Integer, ForeignKey("source_snapshots.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # snapshot document_ids 內的參照;無 live FK(規則 2,見模組 docstring)。
    document_id = Column(Integer, nullable=True)
    # snapshot chunk_ids 內的 chunk 參照鍵。
    chunk_id = Column(String(128), nullable=False)
    quote_preview = Column(Text, nullable=True)
    page = Column(Integer, nullable=True)
    score = Column(Float, nullable=True)
    # 被引文字在 chunk 內的字元位移(span 資訊);未知時 NULL。
    span_start = Column(Integer, nullable=True)
    span_end = Column(Integer, nullable=True)
    # doc 01 三值:answer/artifact/agent_tool(Pydantic 層封閉)。
    used_by = Column(String(20), nullable=False, default="answer",
                     server_default="answer")
    classification_level = Column(String(20), nullable=False,
                                  default="無機密", server_default="無機密")
    created_at = Column(DateTime, nullable=False, default=_utcnow)

    source_snapshot = relationship("SourceSnapshot", back_populates="citations")
