# -*- coding: utf-8 -*-
"""院內規章知識庫檢索（SYSTEM-MAP §3 的「不需要 agent」那條路）。

ANILA 聊天在回答前先問這裡：有沒有已標記的院內規章庫講到這件事。回傳的
不只是命中內容，還有一個**狀態**——因為呼叫端要據以決定跟使用者說哪一句話，
而那幾句話的差別是「這個答案有依據」與「這個答案沒有依據」。

五個狀態的分界（``KbState``）：

* ``NOT_SEARCHED``  一個庫都沒標記（或標記的都不是 active）。沒搜過。
* ``SEARCHED_MISS`` 搜了，門檻之上沒有東西。
* ``SEARCHED_HIT``  搜了，有依據。
* ``PARTIAL_ERROR`` 有的庫成功、有的庫失敗。顯示成 HIT 等於宣稱「這就是全部
  的依據」，而其實有一庫沒查成。
* ``SEARCH_ERROR``  查不了。與「沒命中」是兩件事，不可以合併。

⚠ **有失敗且沒有命中 → ``SEARCH_ERROR``，不是 ``PARTIAL_ERROR``。** 兩個 if
的先後順序是刻意的：一庫查不了、另一庫真的沒東西時，「搜過了，沒有」這句話
沒有人能保證。空手而回時只要有一庫沒查成，就當作查不了。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from types import SimpleNamespace

from sqlalchemy.orm import Session

from anila_core.storage.adapters.pgvector_store import CollectionScopedPgVectorStore

# ⚠ service → api 方向的 import，是刻意的，不是層次搞反：``_embed_query`` 是
# 這個平台**唯一**一條會把 embedding 計量進 ``token_usage``、並且走 model_registry
# 解析端點的查詢嵌入路徑（``search.py:330``）。在這裡複製一份 = 複製一條會漂開的
# 計量規則，而漂開的那一天沒有人會發現（計量少算不會報錯）。
# ``_assert_index_matches_designation`` 同理：索引落在另一個嵌入空間時，
# similarity_search 會**靜默**回空集合，那正是本模組存在要防的那種謊。
from app.api.ingestion.search import (  # noqa: E402
    _assert_index_matches_designation,
    _embed_query,
)
from app.models.ingestion import IngestionCollection, IngestionDocument
from app.models.user import User
from app.schemas.contracts.classification import ClassificationLevel
from app.services.ingestion_pool import get_pool
from app.services.platform_embedding import resolve_platform_embedding
from app.services.search_expansion import expand_query

logger = logging.getLogger(__name__)

# 每庫取幾筆，以及合併排序後最多留幾筆。聊天的上下文是有限的，跨庫串起來
# 不設上限等於讓庫的數量決定 prompt 長度。
_TOP_K = 8

_UNCLASSIFIED = ClassificationLevel.UNCLASSIFIED.to_storage()


def _image_pks_from_metadata(metadata: object) -> list[int]:
    """Pull integer ``ingestion_images.id`` values off chunk metadata.

    Anything that is not a list of ints is dropped — a polluted or
    pre-feature metadata blob must not become a URL or a 500.
    """
    if not isinstance(metadata, dict):
        return []
    raw = metadata.get("image_pks")
    if not isinstance(raw, list):
        return []
    out: list[int] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, int):
            continue
        if item > 0:
            out.append(item)
    return out


class KbState(str, Enum):
    SEARCHED_HIT = "searched_hit"
    SEARCHED_MISS = "searched_miss"
    SEARCH_ERROR = "search_error"
    PARTIAL_ERROR = "partial_error"
    NOT_SEARCHED = "not_searched"


@dataclass
class KbHit:
    collection_id: int
    document_id: int
    filename: str
    content: str
    score: float
    # ``ingestion_images.id`` PKs stamped on the cited chunk. Empty when
    # the chunk has no figure, or the document was indexed before this
    # field existed (those stay empty until re-ingest — not a migration).
    image_pks: list[int] = field(default_factory=list)


@dataclass
class KbResult:
    state: KbState
    hits: list[KbHit] = field(default_factory=list)
    failed_collections: list[int] = field(default_factory=list)


async def retrieve_institutional(
    db: Session,
    user: User,
    query: str,
    *,
    threshold: float,
    top_k: int = _TOP_K,
) -> KbResult:
    """檢索所有已標記的院內規章知識庫。

    ``user`` 是必要參數而不是方便參數：查詢的 embedding 要透過 CSP proxy 打
    出去，而 proxy 會把用量記到人身上（``token_usage``），並且沿路檢查該人的
    密等天花板。沒有人可歸屬的嵌入呼叫在這個平台上不存在。這裡帶的是**發問
    的使用者**——是他問的、算他的。

    ⚠ **逐庫檢索,不可寫成跨庫單一查詢。** document_chunks 的 RLS 靠
    ``SET LOCAL anila.collection_id`` 放行,而那個 GUC 綁在
    CollectionScopedPgVectorStore 的建構子裡(pgvector_store.py:138)——
    一庫一個 store 實例。誰要是繞過 store 直接寫跨庫 SQL,資料庫會**靜默**
    濾到只剩 GUC 指的那一庫,「搜了、沒命中」的謊就從後門回來,而且不報錯。

    ⚠ **文件密等過濾在本模組做,不改共用的 pgvector_store** —— agent 與
    ANILALM 檢索機密內容是合法的,在共用元件裡加過濾會靜默弄壞它們。做法照
    ``search.py:802-804`` 既有的 in-app document_ids 後過濾。過濾是**白名單**
    （只留查得到、且密等為「無機密」的文件）:查不到文件列 = 不知道密等 = 不給。

    ⚠ **標記時的密等檢查不是不變式。** ``_guard_anila_searchable`` 擋的是
    標記那一刻；文件是之後才加進來的，DB CHECK 也只鎖 collection 那一列。
    所以這裡的過濾必須存在，即使「照理說」標記過的庫裡不會有機密文件。
    """
    collections = (
        db.query(IngestionCollection)
        .filter(
            IngestionCollection.anila_searchable.is_(True),
            IngestionCollection.status == "active",
        )
        .order_by(IngestionCollection.id)
        .all()
    )
    if not collections:
        return KbResult(state=KbState.NOT_SEARCHED)

    # ⚠ 先把要用的欄位抄成純物件再進迴圈：``_embed_query`` 中途會 ``db.commit()``，
    # ORM 實例在 expire_on_commit 下會過期，之後每一次屬性存取都是 lazy reload
    # （search.py 的 SimpleNamespace 快照就是為了這件事）。
    targets = [
        SimpleNamespace(
            id=coll.id,
            embedding_model=coll.embedding_model,
            embedding_dim=coll.embedding_dim,
        )
        for coll in collections
    ]

    # 與 search.py 一致：有指定平台主 embedding 時，查詢與過濾都用它，這樣
    # 跨庫的分數才在同一個向量空間裡（跨空間比大小是沒有意義的排序）。
    designated = resolve_platform_embedding(db)
    source_filter = designated.name if designated is not None else None

    # 民國紀年／域內同義擴展 —— 院規正是它存在的理由（「105 年函頒」）。
    search_query = expand_query(db, query)

    try:
        pool = get_pool()
    except RuntimeError:
        # 連線池起不來 = 一庫都查不了。這不是「沒命中」。
        logger.warning("institutional_kb: 取不到 pgvector 連線池，整批檢索視為失敗")
        return KbResult(
            state=KbState.SEARCH_ERROR,
            failed_collections=[t.id for t in targets],
        )

    hits: list[KbHit] = []
    failed: list[int] = []
    # 同一個 (模型, 維度) 只嵌入一次。標記時的檢查要求已標記集共用同一個嵌入
    # 模型，但那同樣是 T 時刻的檢查，所以這裡按實際欄位取用而不是假設只有一種。
    vectors: dict[tuple[str, int], list[float]] = {}

    for target in targets:
        embed_model = source_filter or target.embedding_model
        cache_key = (embed_model, target.embedding_dim)
        try:
            vector = vectors.get(cache_key)
            if vector is None:
                vector = await _embed_query(
                    db, user, embed_model, target.embedding_dim, search_query
                )
                vectors[cache_key] = vector

            store = CollectionScopedPgVectorStore(pool, collection_id=target.id)
            raw = await store.similarity_search(
                query_embedding=vector,
                top_k=top_k,
                min_score=threshold,
                source_model=source_filter,
            )
            # 空集合的兩種意思要分開：真的沒有 vs 整庫索引在另一個嵌入空間。
            # 後者在單庫端點是 409；在這裡 409 只能弄死這一庫，不能中斷整批
            # ——所以它落在 try 裡面，變成 failed，而不是被吞成「沒命中」。
            await _assert_index_matches_designation(
                store, hits=raw, designated=source_filter, collection_id=target.id
            )
        except Exception as exc:  # noqa: BLE001 — 一庫的失敗不得中斷其餘的庫
            logger.warning(
                "institutional_kb: collection %s 檢索失敗 (%s)",
                target.id,
                type(exc).__name__,
            )
            failed.append(target.id)
            continue

        if not raw:
            continue

        # 密等白名單 + filename 一次查完（命中的文件數 ≤ top_k，是一次小查詢）。
        allowed = {
            row.id: row.filename
            for row in db.query(IngestionDocument.id, IngestionDocument.filename)
            .filter(
                IngestionDocument.collection_id == target.id,
                IngestionDocument.id.in_({h.chunk.document_id for h in raw}),
                IngestionDocument.classification_level == _UNCLASSIFIED,
            )
            .all()
        }
        hits.extend(
            KbHit(
                collection_id=target.id,
                document_id=hit.chunk.document_id,
                filename=allowed[hit.chunk.document_id],
                content=hit.chunk.content,
                score=hit.score,
                image_pks=_image_pks_from_metadata(
                    getattr(hit.chunk, "metadata", None)
                ),
            )
            for hit in raw
            if hit.chunk.document_id in allowed
        )

    # 分數高的在前；同分時用 (庫, 文件) 收斂成穩定順序，免得同一個問題兩次
    # 問出不同的引用排列。
    hits.sort(key=lambda h: (-h.score, h.collection_id, h.document_id))
    del hits[top_k:]

    if failed and not hits:
        # ⚠ 這個 if 一定要在 PARTIAL_ERROR 前面，理由見模組 docstring。
        return KbResult(state=KbState.SEARCH_ERROR, failed_collections=failed)
    if failed:
        return KbResult(
            state=KbState.PARTIAL_ERROR, hits=hits, failed_collections=failed
        )
    state = KbState.SEARCHED_HIT if hits else KbState.SEARCHED_MISS
    return KbResult(state=state, hits=hits)
