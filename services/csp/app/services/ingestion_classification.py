# -*- coding: utf-8 -*-
"""知識庫／文件密等讀取與升密協調。

不變式：文件的有效密等永遠 ≥ 所屬知識庫密等。選定做法是
**升密時透過 ``apply_classification`` 級聯寫入文件欄位**（並在上傳時
繼承知識庫密等），而不是在每個讀點臨時算 max——理由：

1. ``apply_classification`` 是單向閂鎖唯一入口；級聯會寫
   ClassificationEvent，降級流程與稽核才能看見真實狀態。
2. 今日幾乎沒有「只讀文件欄位」的政策路徑（見下方
   ``effective_document_classification_level``）；若改走純 max 讀模型，
   文件欄位會長期停在較低值，降級申請與事件對帳會失真。

本模組的 helper 是文件密等的**唯一公開讀取入口**——禁止在呼叫端直接
讀 ``IngestionDocument.classification_level`` 當有效密等（
``apply_classification`` 內部對該列做閂鎖寫入除外）。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.agent import Agent, AgentCollectionBinding
from app.models.ingestion import IngestionCollection, IngestionDocument
from app.schemas.contracts.classification import ClassificationLevel


def effective_document_classification_level(
    document: IngestionDocument,
    collection: IngestionCollection | None = None,
) -> ClassificationLevel:
    """文件有效密等 = max(文件欄位, 知識庫欄位)。

    級聯寫入後兩邊應已對齊；此函式是防呆讀模型，避免未來有人漏級聯
    時又從文件欄位單獨讀出較低值。
    """
    doc_level = ClassificationLevel.from_storage(
        getattr(document, "classification_level", None) or "無機密"
    )
    coll = collection
    if coll is None:
        coll = getattr(document, "collection", None)
    if coll is None:
        return doc_level
    coll_level = ClassificationLevel.from_storage(
        getattr(coll, "classification_level", None) or "無機密"
    )
    return ClassificationLevel.max_of([doc_level, coll_level])


def agents_bound_below_level(
    db: Session,
    collection_id: int,
    target: ClassificationLevel,
) -> list[Agent]:
    """回傳已綁定此知識庫、且有效密等 < target 的 agents。

    同時看 junction（``agent_collection_bindings``）與舊單欄鏡像
    （``agents.bound_collection_id``），避免只清一邊時漏檢。
    """
    from app.api.agents._common import effective_agent_policy_level

    junction_ids = {
        int(aid)
        for (aid,) in db.query(AgentCollectionBinding.agent_id)
        .filter(AgentCollectionBinding.collection_id == collection_id)
        .all()
    }
    legacy_ids = {
        int(aid)
        for (aid,) in db.query(Agent.id)
        .filter(Agent.bound_collection_id == collection_id)
        .all()
    }
    agent_ids = junction_ids | legacy_ids
    if not agent_ids:
        return []
    agents = db.query(Agent).filter(Agent.id.in_(agent_ids)).all()
    return [
        a
        for a in agents
        if effective_agent_policy_level(a) < target
    ]


def cascade_raise_documents(
    db: Session,
    *,
    collection_id: int,
    new_level: ClassificationLevel,
    actor_user_id: int,
) -> list[int]:
    """將知識庫內低於 ``new_level`` 的文件經 ``apply_classification`` 升密。

    回傳實際升級的文件 id 清單。須在知識庫本身升密**之前**呼叫，
    避免知識庫已升、文件級聯中斷時留下「庫高於文件」的窗口。
    """
    from app.modules.policy import apply_classification

    docs = (
        db.query(IngestionDocument)
        .filter(IngestionDocument.collection_id == collection_id)
        .all()
    )
    raised: list[int] = []
    for doc in docs:
        current = ClassificationLevel.from_storage(
            getattr(doc, "classification_level", None) or "無機密"
        )
        if current >= new_level:
            continue
        event = apply_classification(
            db,
            resource_type="document",
            resource_id=str(doc.id),
            new_level=new_level.to_storage(),
            actor_type="user",
            actor_id=str(actor_user_id),
            reason="manual_admin",
            source="collection_raise",
        )
        if event is not None:
            raised.append(int(doc.id))
    return raised
