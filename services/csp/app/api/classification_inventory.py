# -*- coding: utf-8 -*-
"""機敏分類盤點報表(Classification Inventory Before Cutover)。

依 SYSTEM-MAP §8 四級字彙與舊 boolean 相容讀模型:提供盤點快照——
每個資源類型 × 四級分類的分佈、已閂鎖(``classification_latched_at`` 非空)
筆數,以及「舊 boolean latch 與等級」的一致性檢查。

backfill 映射:``classified=true → 機密``(SECRET)、
``requires_encryption=true → 機密``。一致性檢查的「受控集合」門檻保留
舊行為 rank >= 2 → 現為 RESTRICTED(密)(SYSTEM-MAP §8);凡舊 boolean
為真、但等級卻低於「密」的列即為 **不一致**(``inconsistent``)。

``ingestion_documents`` 的等級分佈用**有效密等**
(``max(文件欄位, 知識庫欄位)``,與 documents API 同一讀模型),不是原始
欄位——否則升密前就建立的舊文件會在報表裡報低一級。``latched`` 仍看
文件自身的 ``classification_latched_at``(那是「這一列有沒有被閂鎖過」,
是另一個問題,不套讀模型)。

本報表把這個不變量做成可稽核的計數。Wire 形狀::

    {
      "generated_at": ISO-8601,
      "resources": [
        {"resource_type", "levels": {級別: count...},
         "latched", "inconsistent", "total"},
        ...
      ]
    }

``?format=csv`` 產出 UTF-8(含 BOM,供 Excel 正確辨識)的 text/csv 變體。
僅限 admin/owner(``require_admin`` 兼含 owner)。
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.agent import Agent
from app.models.conversation import Conversation
from app.models.ingestion import IngestionCollection, IngestionDocument
from app.models.message import Message
from app.models.model_registry import ModelRegistry
from app.models.source_snapshot import SourceSnapshot
from app.models.task import Task
from app.models.user import User
from app.schemas.contracts.classification import ClassificationLevel
from app.services.auth_service import require_admin

router = APIRouter(prefix="/api/classification", tags=["機敏分類盤點"])

# 四級順序(單一事實來源 = 契約 enum 宣告順序;SYSTEM-MAP §8);報表欄位固定用它。
_LEVELS: list[str] = [level.value for level in ClassificationLevel]

# 低於「密」(RESTRICTED,rank 2)的等級集合;舊 boolean 為真卻落在這裡 =
# backfill 不一致。意圖保留舊「controlled set = rank >= 2」語意(SYSTEM-MAP §8)。
_BELOW_RESTRICTED: list[str] = [
    level.value
    for level in ClassificationLevel
    if level.rank < ClassificationLevel.RESTRICTED.rank
]


class _ResourceSpec:
    """單一盤點資源的宣告式描述。

    ``level_attr`` / ``latched_attr`` / ``legacy_attr`` 為 None 時代表該模型
    尚無對應欄位(例:``model_registry`` 現況無分類欄位;``agents`` 無
    ``classification_latched_at``),以 fail-safe 方式視為 floor / 無資料。
    """

    __slots__ = ("resource_type", "model", "level_attr", "latched_attr", "legacy_attr")

    def __init__(self, resource_type, model, level_attr, latched_attr, legacy_attr):
        self.resource_type = resource_type
        self.model = model
        self.level_attr = level_attr
        self.latched_attr = latched_attr
        self.legacy_attr = legacy_attr


# doc 08 §5 掛載四級共通欄位的核心資源(順序照 Slice 3c 契約;字彙=SYSTEM-MAP §8)。
# agents 用 ``default_classification_level`` 且無 latched 欄位;
# model_registry(=ModelEndpoint)現況無分類欄位 → 全數視為 floor 無機密。
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
    _ResourceSpec("model_registry", ModelRegistry, None, None, None),
    _ResourceSpec("tasks", Task,
                  "classification_level", "classification_latched_at", None),
    _ResourceSpec("source_snapshots", SourceSnapshot,
                  "classification_level", "classification_latched_at", None),
]


def _document_levels(db: Session) -> dict[str, int]:
    """文件的**有效**密等分佈 = max(文件欄位, 所屬知識庫欄位)。

    必須與 ``GET /api/ingestion/documents/{id}`` 用同一個讀模型
    (``services.ingestion_classification.effective_document_classification_level``)
    ——否則升密前就存在的舊文件會在盤點裡報成「無機密」、在 API 裡報成
    「機密」,而這份報表是治理交付物,兩邊不能各說各話。

    做法:以 (文件欄位, 知識庫欄位) 分組取回計數,在 Python 端折成 max。
    無法解讀的儲存值仍計入 total 但不落格(與其他資源一致的 fail-closed)。
    """
    levels = {value: 0 for value in _LEVELS}
    grouped = (
        db.query(
            IngestionDocument.classification_level,
            IngestionCollection.classification_level,
            func.count(),
        )
        .outerjoin(
            IngestionCollection,
            IngestionDocument.collection_id == IngestionCollection.id,
        )
        .group_by(
            IngestionDocument.classification_level,
            IngestionCollection.classification_level,
        )
        .all()
    )
    for doc_stored, coll_stored, count in grouped:
        try:
            doc_level = ClassificationLevel.from_storage(doc_stored or "無機密")
        except ValueError:
            continue
        effective = doc_level
        if coll_stored is not None:
            try:
                effective = ClassificationLevel.max_of(
                    [doc_level, ClassificationLevel.from_storage(coll_stored)]
                )
            except ValueError:
                # 知識庫欄位壞掉:退回文件自身等級,不臆測。
                effective = doc_level
        levels[effective.value] += count
    return levels


def _row_for(db: Session, spec: _ResourceSpec) -> dict:
    """計算單一資源的盤點列(levels / latched / inconsistent / total)。"""
    total = db.query(func.count()).select_from(spec.model).scalar() or 0
    levels = {value: 0 for value in _LEVELS}

    if spec.resource_type == "ingestion_documents":
        # 唯一用讀模型而非原欄位的資源(見 _document_levels)。
        levels = _document_levels(db)
    elif spec.level_attr is None:
        # 無分類欄位(model_registry):read-model floor = 全部視為無機密。
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
            # 不臆測);合法四級才落格。
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

    inconsistent = 0
    if spec.legacy_attr is not None and spec.level_attr is not None:
        legacy_col = getattr(spec.model, spec.legacy_attr)
        level_col = getattr(spec.model, spec.level_attr)
        inconsistent = (
            db.query(func.count())
            .select_from(spec.model)
            .filter(legacy_col.is_(True), level_col.in_(_BELOW_RESTRICTED))
            .scalar()
            or 0
        )

    return {
        "resource_type": spec.resource_type,
        "levels": levels,
        "latched": latched,
        "inconsistent": inconsistent,
        "total": total,
    }


def _build_inventory(db: Session) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "resources": [_row_for(db, spec) for spec in _RESOURCES],
    }


def _to_csv(inventory: dict) -> str:
    """展平成 CSV;首列 BOM 供 Excel 正確以 UTF-8 開啟。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["資源類型", *_LEVELS, "已閂鎖", "不一致", "總計"])
    for row in inventory["resources"]:
        writer.writerow([
            row["resource_type"],
            *[row["levels"][value] for value in _LEVELS],
            row["latched"],
            row["inconsistent"],
            row["total"],
        ])
    return "﻿" + buffer.getvalue()


@router.get("/inventory")
def get_classification_inventory(
    format: str = Query("json", pattern="^(json|csv)$"),
    _admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """四級分類盤點快照(admin/owner;SYSTEM-MAP §8)。

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
