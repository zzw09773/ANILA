# -*- coding: utf-8 -*-
"""PolicyDecision — 高價值動作的政策裁決紀錄(doc 03 §5,Slice 2a)。

Append-only:本表只 INSERT,不 UPDATE / DELETE(無 updated_at 欄位;
service 層與 API 面不得提供改寫路徑)。同時要可查詢 —— 依 doc 03 建
(action, created_at) 與 (task_id) 兩組索引。

- ``action`` 九值 enum(doc 03 逐字)與 ``decision`` 三值
  (allow/deny/require_approval)存開放 String,封閉 enum 在 Pydantic
  契約層(``app.schemas.contracts.policy``)把關。
- ``task_id`` 可空(registry.create 之類的治理動作不掛 task);
  ON DELETE SET NULL —— 刪 task 不得連帶抹除裁決史(append-only)。
- ``actor_type`` = user / service;``actor_id`` 依型別指 users.id 或
  service client / agent id,故不掛單一 FK,細節放 ``metadata_json``。
- 所有 policy deny 必須有可解釋原因(reason + matched_policy_ids,
  doc 03 Done Criteria 4)—— service 層強制,schema 允許 allow 留空。
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
)
from sqlalchemy.dialects.postgresql import JSONB

from app.database import Base

JSONValue = JSON().with_variant(JSONB, "postgresql")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PolicyDecision(Base):
    """一次政策裁決;不可改寫,只可追加。"""

    __tablename__ = "policy_decisions"
    __table_args__ = (
        Index("ix_policy_decisions_action_created_at", "action", "created_at"),
        Index("ix_policy_decisions_task_id", "task_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(
        Integer, ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True,
    )
    # user / service(Pydantic 層封閉)。
    actor_type = Column(String(16), nullable=False)
    actor_id = Column(Integer, nullable=True)
    # doc 03 九值:task.run / model.invoke / agent.invoke / service.launch /
    # artifact.export / collection.read / classification.downgrade_request /
    # registry.create / registry.approve。
    action = Column(String(64), nullable=False)
    resource_type = Column(String(50), nullable=False)
    resource_id = Column(String(100), nullable=True)
    # allow / deny / require_approval(Pydantic 層封閉)。
    decision = Column(String(20), nullable=False)
    reason = Column(Text, nullable=True)
    matched_policy_ids = Column(JSONValue, nullable=False, default=list)
    policy_version = Column(String(32), nullable=True)
    # 補充脈絡(policy inputs、actor 細節);attribute 名不能叫 metadata
    # (SQLAlchemy 保留),沿 audit_log 慣例取 metadata_json。
    metadata_json = Column(JSONValue, nullable=True)
    classification_level = Column(String(20), nullable=False,
                                  default="無機密", server_default="無機密")
    created_at = Column(DateTime, nullable=False, default=_utcnow)
