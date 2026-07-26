# -*- coding: utf-8 -*-
"""五級分類治理三表(doc 08 §6/§7/§8/§12,Slice 3a)。

- :class:`ClassificationEvent` — 分類異動事件(doc 08 §6 欄位逐字)。
  append-only:只 INSERT,不 UPDATE / DELETE;``reason`` 7 值封閉 enum
  在契約層(``app.schemas.contracts.classification``)把關,DB 存開放
  String(SQLite create_all 相容,同 policy_decision.py 模式)。
- :class:`DeclassificationRequest` — 降級申請(doc 08 §8 欄位逐字)。
  ``status`` 5 值,fail-closed 預設 ``pending_supervisor``;變體 A:
  ``approved_via`` 二選一,``recorded_paper_decision`` 必附
  ``authority_reference``(公文文號/簽呈)＋``authority_title_name``
  (核定者官職＋姓名)＋``recorded_by_user_id``(代錄人)。不變量
  「申請人 ≠ 核准人/代錄人」由 service 層
  (``app.modules.policy.service``)強制。
- :class:`ClassificationAuthorityAssignment` —「機密審批權責」指派
  (doc 08 §7 第 2–3 點、§12):核准權與平台 owner/admin 技術角色脫鉤,
  指派必附核定依據(公文文號/簽呈)、雙人控制(登錄人＋確認人或
  bootstrap 見證)。Slice 3a 只由 migration / seed 管理列,admin UI
  在 3b;``has_declassification_authority`` hook 讀本表。

``resource_type`` / ``resource_id`` 一律字串參照(doc 08 §6),不掛
資源表 FK —— 事件橫跨 11 種資源,反向由各資源表的
``classification_event_id`` FK 指回本表。
"""

from __future__ import annotations

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
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.database import Base

JSONValue = JSON().with_variant(JSONB, "postgresql")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── W2-10 批次 1(migration r1_0040):本檔三張表的時間欄一律 timezone=True ────
#
# 這三張表是**法律證據性質**的治理帳(分類異動 ledger、雙人降密核准、公文文號
# 權責指派)。原本 `Column(DateTime)` + PG `timestamp without time zone` →
# 「一次分類異動發生在哪個絕對時點」在資料裡沒有答案,只能靠「寫入端都是
# `datetime.now(timezone.utc)`」這個外部知識推斷。r1_0040 把 DB 側轉成
# timestamptz,這裡同步宣告 —— 只改一邊會讓 drift gate 告警(見 r1_0040 檔頭)。
#
# ⚠ 既有 naive 值的判讀是 `AT TIME ZONE 'Asia/Taipei'`(2026-07-26 user 拍板),
# 所以遷移前後的紀錄在絕對時點上有 8 小時不連續。完整脈絡(含實作者的反對意見)
# 逐字寫在 `migrations/versions/r1_0040_governance_ledger_timestamptz.py` 檔頭。
class ClassificationEvent(Base):
    """一次分類異動(latch / 升級 / 降級生效);append-only。"""

    __tablename__ = "classification_events"
    __table_args__ = (
        Index(
            "ix_classification_events_resource",
            "resource_type",
            "resource_id",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    resource_type = Column(String(50), nullable=False)
    resource_id = Column(String(100), nullable=False)
    previous_level = Column(String(20), nullable=False)
    new_level = Column(String(20), nullable=False)
    # doc 08 §6 七值:source_selected / agent_policy / memory_inherited /
    # manual_admin / service_policy / content_detection /
    # declassification_copy(契約層封閉)。
    reason = Column(String(32), nullable=False)
    # service actor(router / worker)非 users FK → NULL,細節入 audit。
    actor_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    inherited_from_resource_type = Column(String(50), nullable=True)
    inherited_from_resource_id = Column(String(100), nullable=True)
    trace_id = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class DeclassificationRequest(Base):
    """Admin 降級申請單(doc 08 §8);主管核准/紙本代錄後才生效。"""

    __tablename__ = "declassification_requests"
    __table_args__ = (
        Index("ix_declassification_requests_status", "status"),
        Index(
            "ix_declassification_requests_resource",
            "resource_type",
            "resource_id",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    resource_type = Column(String(50), nullable=False)
    resource_id = Column(String(100), nullable=False)
    from_level = Column(String(20), nullable=False)
    to_level = Column(String(20), nullable=False)
    # 申請人必為 Admin(doc 08 §7 規則 4;service 層強制)。不掛
    # ondelete:降級治理紀錄不得因刪帳號而連帶蒸發(fail-closed)。
    requested_by_admin_id = Column(
        Integer, ForeignKey("users.id"), nullable=False
    )
    reason = Column(Text, nullable=False)
    proposed_redaction_summary = Column(Text, nullable=True)
    # doc 08 §8 五值(契約層封閉);fail-closed 預設 pending_supervisor。
    status = Column(
        String(32),
        nullable=False,
        default="pending_supervisor",
        server_default="pending_supervisor",
    )
    supervisor_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    supervisor_comment = Column(Text, nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    # 變體 A:in_system / recorded_paper_decision(契約層封閉)。
    approved_via = Column(String(32), nullable=True)
    # recorded_paper_decision 必填三欄(service 層強制):
    authority_reference = Column(String(255), nullable=True)  # 公文文號/簽呈
    authority_title_name = Column(String(255), nullable=True)  # 官職＋姓名
    recorded_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # 降密副本模式的新資源參照(doc 08 §9;in-place 生效時留 NULL)。
    resulting_resource_id = Column(String(100), nullable=True)
    audit_event_ids = Column(JSONValue, nullable=False, default=list)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class ClassificationAuthorityAssignment(Base):
    """「機密審批權責」指派(doc 08 §7 第 2–3 點、§12)。

    - ``department_id`` NULL = 全域權責;非 NULL = per-department。
    - ``authority_reference``(核定依據:公文文號/簽呈)必附 —— 權責
      來自行政程序,系統只負責記錄與鎖定(信任錨)。
    - 雙人控制:``granted_by_user_id``(登錄人)＋``confirmed_by_user_id``
      (確認人;部署 bootstrap 見證時可為 NULL,由 seed 記錄緣由)。
    - 指派異動 = 高敏 audit event(``classification.
      authority_assignment_changed``);Slice 3a 列僅由 migration / seed
      管理,admin UI 與治理中心公告在 3b。
    """

    __tablename__ = "classification_authority_assignments"
    __table_args__ = (
        Index("ix_classification_authority_assignments_user", "user_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    department_id = Column(
        Integer, ForeignKey("departments.id", ondelete="SET NULL"),
        nullable=True,
    )
    authority_reference = Column(String(255), nullable=False)
    granted_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    confirmed_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    is_active = Column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
