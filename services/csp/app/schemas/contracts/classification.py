# -*- coding: utf-8 -*-
"""五級分類契約(ClassificationLevel + 分類事件/降級申請 enum)。

依 docs/anila-redesign-docs/08-classified-latch-and-policy-engine.md:

- 五級:無機密(0) < 營業秘密(1) < 機密(2) < 極機密(3) < 絕對機密(4),
  排序不可變(doc 08 §1)。
- 單向閂鎖(doc 08 §2):effective level = 所有觀測到的分類取 max,
  只能維持或升級,不得自動降級 → 見 :meth:`ClassificationLevel.max_of`。
- 舊 boolean `classified` 的 backfill(doc 08 §3,v0.2 拍板):
  false → 無機密、true → 機密;此為 migration floor(最低安全起點),
  不是最終分類,最終等級以人工分類盤點為準 →
  見 :meth:`ClassificationLevel.from_legacy_classified`。
- Slice 3a 補三個封閉 enum(DB 層存開放 String,契約層 fail-closed 把關,
  同 ``contracts.policy`` 模式):
  :class:`ClassificationEventReason`(doc 08 §6 reason 7 值,逐字)、
  :class:`DeclassificationStatus`(doc 08 §8 status 5 值,逐字)、
  :class:`DeclassificationApprovedVia`(doc 08 §8 變體 A 核准路徑二選一)。

儲存格式:一律以繁中字串(enum value)落地,經
:meth:`ClassificationLevel.to_storage` / :meth:`ClassificationLevel.from_storage`
往返;未知字串 fail-closed 拋 ``ValueError``。
"""

from __future__ import annotations

import enum
from datetime import datetime
from functools import total_ordering
from typing import Iterable

from pydantic import BaseModel, Field


@total_ordering
class ClassificationLevel(enum.Enum):
    """五級分類等級;成員定義順序即由低到高的排序契約。"""

    UNCLASSIFIED = "無機密"
    TRADE_SECRET = "營業秘密"
    CONFIDENTIAL = "機密"
    SECRET = "極機密"
    TOP_SECRET = "絕對機密"

    @property
    def rank(self) -> int:
        """數值序(doc 08 §1 的 0–4);僅供排序/比較,不作儲存格式。"""
        return _RANKS[self]

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, ClassificationLevel):
            return NotImplemented
        return self.rank < other.rank

    @classmethod
    def max_of(cls, levels: Iterable["ClassificationLevel"]) -> "ClassificationLevel":
        """單向閂鎖 helper:effective level = 觀測到分類的最大值。

        空集合拋 ``ValueError``(分類判定不允許憑空預設,fail-closed)。
        """
        materialized = list(levels)
        if not materialized:
            raise ValueError("max_of() 需要至少一個分類等級,不允許空集合")
        return max(materialized, key=lambda level: level.rank)

    @classmethod
    def from_legacy_classified(cls, classified: bool) -> "ClassificationLevel":
        """舊 boolean classified → 五級的 floor backfill 映射(doc 08 §3)。

        true → 機密 只是 migration floor(最低安全起點),不是最終分類。
        """
        return cls.CONFIDENTIAL if classified else cls.UNCLASSIFIED

    def to_storage(self) -> str:
        """回傳落地儲存用的繁中字串(enum value)。"""
        return self.value

    @classmethod
    def from_storage(cls, value: str) -> "ClassificationLevel":
        """由儲存字串還原等級;未知值拋 ``ValueError``(fail-closed)。"""
        try:
            return cls(value)
        except ValueError:
            raise ValueError(
                f"未知的分類等級儲存值:{value!r};"
                f"合法值為 {[level.value for level in cls]}"
            ) from None


# 定義順序即排序:rank 由成員宣告順序推導,單一事實來源。
_RANKS: dict[ClassificationLevel, int] = {
    level: index for index, level in enumerate(ClassificationLevel)
}


class ClassificationEventReason(str, enum.Enum):
    """doc 08 §6 ClassificationEvent.reason 7 值(逐字,順序照文件)。

    注:文件的 7 值 enum 是封閉集合 —— 降級核准生效所寫的事件也必須
    落在其中,採 ``declassification_copy``(doc 08 §9 降密模式的事件
    reason;audit 面另記 ``classification.downgrade_approved`` 等事件)。
    """

    SOURCE_SELECTED = "source_selected"
    AGENT_POLICY = "agent_policy"
    MEMORY_INHERITED = "memory_inherited"
    MANUAL_ADMIN = "manual_admin"
    SERVICE_POLICY = "service_policy"
    CONTENT_DETECTION = "content_detection"
    DECLASSIFICATION_COPY = "declassification_copy"


class DeclassificationStatus(str, enum.Enum):
    """doc 08 §8 DeclassificationRequest.status 5 值(逐字,順序照文件)。

    fail-closed 預設 = ``pending_supervisor``(doc 08 §12:無主管資料且
    無可用權責者時,申請維持 pending,不升級、不自動放行)。
    """

    PENDING_SUPERVISOR = "pending_supervisor"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    APPLIED = "applied"


class DeclassificationApprovedVia(str, enum.Enum):
    """doc 08 §8 approved_via 二選一(變體 A,2026-07-02 拍板)。

    ``recorded_paper_decision``(紙本核定＋代錄)時必填
    ``authority_reference``(公文文號/簽呈)、``authority_title_name``
    (核定者官職＋姓名)、``recorded_by_user_id``(代錄人,須持
    「機密審批權責」且 ≠ 申請人)。系統內沒有自我核准欄位或碼路徑。
    """

    IN_SYSTEM = "in_system"
    RECORDED_PAPER_DECISION = "recorded_paper_decision"


# ── Slice 3b：降級申請 / 權責指派 API 契約(doc 08 §7/§8/§12、doc 09 §11)───────
#
# 邊界 fail-closed:``requested_level`` / ``from_level`` / ``to_level`` /
# ``status`` / ``approved_via`` 皆以封閉 enum 型別把關,非法值由 FastAPI 422
# 擋下(與 record/latch 端同一套字彙)。降級的三段式(申請→主管批核→audit)
# 與雙人原則(申請人 ≠ 核准人/代錄人)在 service + router 層強制,契約層只
# 定形狀。


class DeclassificationRequestCreate(BaseModel):
    """POST /api/classification/declassification-requests 請求體。

    ``requested_level`` = 目標等級(service 的 ``to_level``),必須嚴格低於
    資源現行等級(service 層驗證);``reason`` 必填(doc 08 §8)。
    """

    resource_type: str = Field(..., min_length=1, max_length=50)
    resource_id: str = Field(..., min_length=1, max_length=100)
    requested_level: ClassificationLevel
    reason: str = Field(..., min_length=1)
    proposed_redaction_summary: str | None = None


class DeclassificationApproveBody(BaseModel):
    """POST .../{id}/approve 請求體(變體 A)。

    ``via=recorded_paper_decision`` 時 ``authority_reference``(公文文號/
    簽呈)與 ``authority_title_name``(核定者官職＋姓名)必填 —— router 層
    以 422 把關(fail-closed);``comment`` 落 ``supervisor_comment``。
    """

    via: DeclassificationApprovedVia = DeclassificationApprovedVia.IN_SYSTEM
    authority_reference: str | None = None
    authority_title_name: str | None = None
    comment: str | None = None


class DeclassificationRejectBody(BaseModel):
    """POST .../{id}/reject 請求體;``reason`` 落 ``supervisor_comment``。"""

    reason: str = Field(..., min_length=1)


class DeclassificationRequestOut(BaseModel):
    """DeclassificationRequest 讀出契約(from ORM;doc 08 §8 欄位)。"""

    id: int
    resource_type: str
    resource_id: str
    from_level: ClassificationLevel
    to_level: ClassificationLevel
    requested_by_admin_id: int
    reason: str
    proposed_redaction_summary: str | None = None
    status: DeclassificationStatus
    supervisor_user_id: int | None = None
    supervisor_comment: str | None = None
    decided_at: datetime | None = None
    approved_via: DeclassificationApprovedVia | None = None
    authority_reference: str | None = None
    authority_title_name: str | None = None
    recorded_by_user_id: int | None = None
    resulting_resource_id: str | None = None
    audit_event_ids: list[str] = Field(default_factory=list)
    created_at: datetime

    model_config = {"from_attributes": True}


class ClassificationAuthorityCreate(BaseModel):
    """POST /api/classification-authorities 請求體(doc 08 §7.3 信任錨)。

    ``authority_reference``(核定依據:公文文號/簽呈)必填 —— 權責來自行政
    程序,系統只負責記錄與鎖定;``department_id`` 為 None = 全域權責。
    """

    user_id: int
    department_id: int | None = None
    authority_reference: str = Field(..., min_length=1, max_length=255)


class ClassificationAuthorityOut(BaseModel):
    """ClassificationAuthorityAssignment 讀出契約(from ORM)。

    ``is_effective`` = 生效與否 = ``is_active and revoked_at is None``
    (雙人控制:登錄後未確認 → is_active=False → 不生效;撤銷 →
    revoked_at 有值 → 不生效);由 router 層計算填入。
    """

    id: int
    user_id: int
    department_id: int | None = None
    authority_reference: str
    granted_by_user_id: int | None = None
    confirmed_by_user_id: int | None = None
    is_active: bool
    is_effective: bool = False
    revoked_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}
