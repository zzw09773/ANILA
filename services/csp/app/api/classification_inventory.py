# -*- coding: utf-8 -*-
"""機敏分類盤點報表(Classification Inventory Before Cutover)。

依 docs/anila-redesign-docs/08-classified-latch-and-policy-engine.md §15
「Classification Inventory Before Cutover(✅ 已拍板 v0.2)」:切到五級分類前,
所有既有資源必須完成人工分類盤點並記錄。本端點提供切換前的盤點快照——
每個資源類型 × 五級分類的分佈、已閂鎖(``classification_latched_at`` 非空)
筆數,以及「舊 boolean latch 與新五級等級」的一致性檢查。

doc 08 §3 的 backfill 映射:``classified=true → 機密``、
``requires_encryption=true → 機密``。因此凡舊 boolean 為真、但五級等級卻
低於「機密」的列即為 **不一致**(``inconsistent``);backfill 完成後應為 0。
本報表把這個不變量做成可稽核的計數,供保密單位在切換前 attestation。

doc 08 §15 未規定明確的 wire 欄位/聚合格式,故採 Slice 3c 約定的 fallback
形狀::

    {
      "generated_at": ISO-8601,
      "resources": [
        {"resource_type", "levels": {級別: count...},
         "latched", "inconsistent", "total",
         "description", "manage_path"},
        ...
      ]
    }

``?format=csv`` 產出 UTF-8(含 BOM,供 Excel 正確辨識)的 text/csv 變體。
僅限 admin/owner(``require_admin`` 兼含 owner)。

W2-11 另增持續性抽查報表(``/sampling-report``)與複核動作
(``/sampling-reviews``):隨機取樣文件供權責人複核,複核寫入
``classification_sampling_reviews`` + audit。雙重 gate = admin **且**
該 collection 對呼叫者可見(``_require_collection_access``),避免報表
含文件標題卻變成新的洩漏面。
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from anila_contracts import Classification as ClassificationLevel
from app.api.ingestion.collections import _require_collection_access
from app.api.ingestion.surface import ANY_SURFACE
from app.database import get_db
from app.models.agent import Agent
from app.models.classification import ClassificationSamplingReview
from app.models.conversation import Conversation
from app.models.ingestion import IngestionCollection, IngestionDocument
from app.models.message import Message
from app.models.model_registry import ModelRegistry
from app.models.source_snapshot import SourceSnapshot
from app.models.task import Task
from app.models.user import User
from app.services.audit_service import log_audit_event
from app.services.auth_service import is_admin_tier, require_admin

router = APIRouter(prefix="/api/classification", tags=["機敏分類盤點"])

# 五級順序(單一事實來源 = 契約 enum 宣告順序);報表欄位固定用它。
_LEVELS: list[str] = [level.value for level in ClassificationLevel]

# 低於「機密」的等級集合;舊 boolean 為真卻落在這裡 = backfill 不一致。
_BELOW_CONFIDENTIAL: list[str] = [
    level.value
    for level in ClassificationLevel
    if level.rank < ClassificationLevel.CONFIDENTIAL.rank
]

# 各資源類型的繁中說明與(若有)治理面路由。
_AUTO_GOVERNED = "系統自動治理，無人工管理面"
_RESOURCE_META: dict[str, tuple[str, str | None]] = {
    "conversations": ("對話執行期分類等級與舊 latch 對照", None),
    "messages": ("訊息執行期分類等級(系統自動繼承/閂鎖)", None),
    "ingestion_collections": (
        "知識庫集合分類等級(文件上傳時繼承)",
        "/knowledge-collections",
    ),
    "ingestion_documents": ("已索引文件分類等級(繼承集合)", None),
    "agents": (
        "Agent 預設分級與加密旗標對照"
        "（分類上限未設定＝不可派工，非無上限）",
        "/developer/agents",
    ),
    "model_registry": (
        "模型分類上限許可(classification_ceiling；非資料實際等級)",
        "/models",
    ),
    "tasks": ("任務執行期分類等級", None),
    "source_snapshots": ("檢索來源快照分類等級", None),
}

_SamplingOutcome = Literal["confirmed", "mismatch", "needs_followup"]


class _ResourceSpec:
    """單一盤點資源的宣告式描述。

    ``level_attr`` / ``latched_attr`` / ``legacy_attr`` 為 None 時代表該模型
    尚無對應欄位。``legacy_attr`` 為 None 時 ``inconsistent`` 回傳 null
    (不適用),而非硬編 0。
    """

    __slots__ = ("resource_type", "model", "level_attr", "latched_attr", "legacy_attr")

    def __init__(self, resource_type, model, level_attr, latched_attr, legacy_attr):
        self.resource_type = resource_type
        self.model = model
        self.level_attr = level_attr
        self.latched_attr = latched_attr
        self.legacy_attr = legacy_attr


# doc 08 §5 掛載五級共通欄位的核心資源(順序照 Slice 3c 契約)。
# agents 用 ``default_classification_level`` 且無 latched 欄位;
# model_registry 用 ``classification_ceiling``(許可上限,非資料實際等級)。
_RESOURCES: list[_ResourceSpec] = [
    _ResourceSpec("conversations", Conversation,
                  "classification_level", "classification_latched_at", "classified"),
    _ResourceSpec("messages", Message,
                  "classification_level", "classification_latched_at", None),
    _ResourceSpec("ingestion_collections", IngestionCollection,
                  "classification_level", "classification_latched_at", None),
    _ResourceSpec("ingestion_documents", IngestionDocument,
                  "classification_level", "classification_latched_at", None),
    _ResourceSpec("agents", Agent,
                  "default_classification_level", None, "requires_encryption"),
    _ResourceSpec("model_registry", ModelRegistry,
                  "classification_ceiling", None, None),
    _ResourceSpec("tasks", Task,
                  "classification_level", "classification_latched_at", None),
    _ResourceSpec("source_snapshots", SourceSnapshot,
                  "classification_level", "classification_latched_at", None),
]

# resource_type → levels 計數語意。model_registry 的桶是 ceiling 許可上限,
# 其餘是實際資料分類等級。
_CEILING_RESOURCE_TYPES = frozenset({"model_registry"})


def _row_for(db: Session, spec: _ResourceSpec) -> dict:
    """計算單一資源的盤點列(levels / latched / inconsistent / total)。"""
    total = db.query(func.count()).select_from(spec.model).scalar() or 0
    levels = {value: 0 for value in _LEVELS}

    if spec.level_attr is None:
        # 無分類欄位:read-model floor = 全部視為無機密。
        levels[ClassificationLevel.UNCLASSIFIED.value] = total
    else:
        level_col = getattr(spec.model, spec.level_attr)
        grouped = (
            db.query(level_col, func.count())
            .group_by(level_col)
            .all()
        )
        for stored_value, count in grouped:
            # 未知/NULL 值仍計入 total,但不歸入任一已知級別欄位(fail-closed
            # 不臆測);合法五級才落格。
            if stored_value in levels:
                levels[stored_value] = count

    latched = 0
    if spec.latched_attr is not None:
        latched_col = getattr(spec.model, spec.latched_attr)
        latched = (
            db.query(func.count())
            .select_from(spec.model)
            .filter(latched_col.isnot(None))
            .scalar()
            or 0
        )

    # 無 legacy boolean 可比對時 inconsistent 不適用 → null(前端顯示「不適用」)。
    inconsistent: int | None
    if spec.legacy_attr is None or spec.level_attr is None:
        inconsistent = None
    else:
        legacy_col = getattr(spec.model, spec.legacy_attr)
        level_col = getattr(spec.model, spec.level_attr)
        inconsistent = (
            db.query(func.count())
            .select_from(spec.model)
            .filter(legacy_col.is_(True), level_col.in_(_BELOW_CONFIDENTIAL))
            .scalar()
            or 0
        )

    description, manage_path = _RESOURCE_META.get(
        spec.resource_type, ("", None)
    )
    if manage_path is None and spec.resource_type in _RESOURCE_META:
        # conversations / messages / tasks / source_snapshots / documents
        # 明確標示無人工管理面。
        if not description.endswith(_AUTO_GOVERNED):
            description = (
                f"{description}。{_AUTO_GOVERNED}"
                if description
                else _AUTO_GOVERNED
            )

    row = {
        "resource_type": spec.resource_type,
        "levels": levels,
        "latched": latched,
        "inconsistent": inconsistent,
        "total": total,
        "description": description,
        "manage_path": manage_path,
    }
    # Distinguish ceiling-permit buckets from actual data-classification counts.
    if spec.resource_type in _CEILING_RESOURCE_TYPES:
        row["ceiling"] = True
    return row


def _build_inventory(db: Session) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "resources": [_row_for(db, spec) for spec in _RESOURCES],
    }


def _format_inconsistent_csv(value: int | None) -> str:
    return "不適用" if value is None else str(value)


def _to_csv(inventory: dict) -> str:
    """展平成 CSV;首列 BOM 供 Excel 正確以 UTF-8 開啟。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "資源類型", "說明", "管理面", *_LEVELS, "已閂鎖", "不一致", "總計",
    ])
    for row in inventory["resources"]:
        writer.writerow([
            row["resource_type"],
            row.get("description") or "",
            row.get("manage_path") or _AUTO_GOVERNED,
            *[row["levels"][value] for value in _LEVELS],
            row["latched"],
            _format_inconsistent_csv(row["inconsistent"]),
            row["total"],
        ])
    return "﻿" + buffer.getvalue()


def _visible_collection_ids(db: Session, admin: User) -> list[int]:
    """Collections the caller may see under the ownership/admin ACL.

    Sampling rows carry document titles — never return a collection the
    caller could not open via the normal ingestion ACL.
    """
    q = db.query(IngestionCollection.id)
    if not is_admin_tier(admin):
        q = q.filter(IngestionCollection.created_by == admin.id)
    return [row[0] for row in q.all()]


class SamplingReviewCreate(BaseModel):
    """POST /api/classification/sampling-reviews 請求體。"""

    document_id: int = Field(..., gt=0)
    attested_level: ClassificationLevel
    outcome: _SamplingOutcome = "confirmed"
    note: str | None = Field(default=None, max_length=2000)


class SamplingReportDocument(BaseModel):
    """One row in GET /api/classification/sampling-report."""

    document_id: int
    title: str | None = None
    filename: str | None = None
    classification_level: str | None = None
    classification_level_valid: bool
    uploader_user_id: int | None = None
    uploader_username: str | None = None
    collection_id: int
    collection_name: str | None = None
    collection_classification_level: str | None = None


class SamplingReportResponse(BaseModel):
    """GET /api/classification/sampling-report response."""

    generated_at: str
    sample_size: int
    requested_n: int
    documents: list[SamplingReportDocument]


class SamplingReviewResponse(BaseModel):
    """POST /api/classification/sampling-reviews response."""

    id: int
    document_id: int
    collection_id: int
    document_level_at_review: str
    attested_level: str
    outcome: _SamplingOutcome
    note: str | None = None
    reviewer_user_id: int
    created_at: str | None = None


@router.get("/inventory")
def get_classification_inventory(
    format: str = Query("json", pattern="^(json|csv)$"),
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """切換五級分類前的盤點快照(admin/owner)。

    ``?format=csv`` 回傳含 BOM 的 UTF-8 text/csv;否則回 JSON。
    """
    inventory = _build_inventory(db)
    if format == "csv":
        return StreamingResponse(
            iter([_to_csv(inventory)]),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": "attachment; filename=classification-inventory.csv",
            },
        )
    return inventory


@router.get("/sampling-report", response_model=SamplingReportResponse)
def get_classification_sampling_report(
    sample_size: int = Query(20, ge=1, le=200, alias="n"),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> SamplingReportResponse:
    """W2-11 continuous sampling report for authorized reviewers.

    Returns a random sample of up to ``n`` documents the caller can see,
    with title + current level + uploader + collection. Double-gated:
    ``require_admin`` **and** collection visibility.
    """
    visible = _visible_collection_ids(db, admin)
    if not visible:
        return SamplingReportResponse(
            generated_at=datetime.now(timezone.utc).isoformat(),
            sample_size=0,
            requested_n=sample_size,
            documents=[],
        )

    # Prefer documents not yet attested; fall back to any visible doc so the
    # report stays useful after the first full pass.
    reviewed_ids = {
        row[0]
        for row in db.query(ClassificationSamplingReview.document_id).distinct().all()
    }
    base = (
        db.query(IngestionDocument, IngestionCollection, User)
        .join(
            IngestionCollection,
            IngestionCollection.id == IngestionDocument.collection_id,
        )
        .outerjoin(User, User.id == IngestionDocument.uploaded_by)
        .filter(IngestionDocument.collection_id.in_(visible))
    )
    unreviewed = (
        base.filter(~IngestionDocument.id.in_(reviewed_ids))
        if reviewed_ids
        else base
    )
    rows = (
        unreviewed.order_by(func.random())
        .limit(sample_size)
        .all()
    )
    if len(rows) < sample_size:
        already = {doc.id for doc, _coll, _user in rows}
        filler_q = base
        if already:
            filler_q = filler_q.filter(~IngestionDocument.id.in_(already))
        filler = (
            filler_q.order_by(func.random())
            .limit(sample_size - len(rows))
            .all()
        )
        rows = list(rows) + list(filler)

    documents: list[SamplingReportDocument] = []
    for doc, coll, uploader in rows:
        # Fail-closed on unknown stored levels — surface as null + flag rather
        # than inventing a bucket.
        level_ok = True
        try:
            level = ClassificationLevel.from_storage(
                doc.classification_level
            ).to_storage()
        except ValueError:
            level = None
            level_ok = False
        documents.append(
            SamplingReportDocument(
                document_id=doc.id,
                title=doc.title or doc.filename,
                filename=doc.filename,
                classification_level=level,
                classification_level_valid=level_ok,
                uploader_user_id=doc.uploaded_by,
                uploader_username=uploader.username if uploader else None,
                collection_id=coll.id,
                collection_name=coll.name,
                collection_classification_level=coll.classification_level,
            )
        )

    return SamplingReportResponse(
        generated_at=datetime.now(timezone.utc).isoformat(),
        sample_size=len(documents),
        requested_n=sample_size,
        documents=documents,
    )


@router.post(
    "/sampling-reviews",
    response_model=SamplingReviewResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_classification_sampling_review(
    payload: SamplingReviewCreate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> SamplingReviewResponse:
    """Attest one sampled document; writes ledger row + audit event.

    Double gate: admin **and** collection visibility for the document.
    """
    doc = db.get(IngestionDocument, payload.document_id)
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found",
        )
    # Visibility gate — raises 403/404 if the admin cannot see this collection.
    _require_collection_access(
        db, admin, doc.collection_id, origin=ANY_SURFACE
    )

    try:
        document_level = ClassificationLevel.from_storage(
            doc.classification_level
        ).to_storage()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Document classification state is invalid",
        ) from exc

    attested = payload.attested_level.to_storage()
    review = ClassificationSamplingReview(
        document_id=doc.id,
        collection_id=doc.collection_id,
        reviewer_user_id=admin.id,
        document_level_at_review=document_level,
        attested_level=attested,
        outcome=payload.outcome,
        note=payload.note,
    )
    db.add(review)
    db.flush()

    log_audit_event(
        db,
        commit=False,
        actor=admin,
        action="classification.sampling_review",
        resource_type="ingestion_document",
        resource_id=doc.id,
        detail=(
            f"抽查複核文件 #{doc.id}:outcome={payload.outcome},"
            f"stored={document_level},attested={attested}"
        ),
        metadata={
            "review_id": review.id,
            "collection_id": doc.collection_id,
            "document_id": doc.id,
            "document_level_at_review": document_level,
            "attested_level": attested,
            "outcome": payload.outcome,
        },
    )
    db.commit()
    db.refresh(review)

    return SamplingReviewResponse(
        id=review.id,
        document_id=review.document_id,
        collection_id=review.collection_id,
        document_level_at_review=review.document_level_at_review,
        attested_level=review.attested_level,
        outcome=review.outcome,  # type: ignore[arg-type]
        note=review.note,
        reviewer_user_id=review.reviewer_user_id,
        created_at=(
            review.created_at.isoformat() if review.created_at else None
        ),
    )
