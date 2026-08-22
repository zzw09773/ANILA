# -*- coding: utf-8 -*-
"""院內規章檢索的狀態機 —— 「搜了、沒命中」不可以是謊話。

這一支守的不是「有沒有檢索到東西」，而是**使用者被告知的那個狀態是不是真的**。
ANILA 的聊天會依這個狀態決定要不要跟使用者說「這個答案有院規當依據」。所以
五個狀態之間分錯一格，後果不是少一筆資料，是使用者相信了一句沒有依據的話：

* ``NOT_SEARCHED``  —— 一個庫都沒標記。沒搜過就不要顯示搜過。
* ``SEARCHED_MISS`` —— 搜了，門檻之上沒有東西。
* ``SEARCHED_HIT``  —— 搜了，有依據。
* ``PARTIAL_ERROR`` —— 有的庫成功、有的庫炸掉。顯示成 HIT 就是宣稱「這是全部
  的依據」，而事實上有一庫沒查成。
* ``SEARCH_ERROR``  —— 查不了。與「沒命中」是兩件完全不同的事。

⚠ **本檔刻意不需要 PostgreSQL。** ``ANILA_TEST_PG_DSN`` 沒設時也要整批跑，
因為這裡驗的是應用層的狀態決策與密等過濾，不是 pgvector 的 SQL。向量層依
``tests/test_chunk_search_index_mismatch.py`` 的既有做法以 stub 取代（SQLite
跑不動 asyncpg + halfvec）。真的沒被本檔驗到的是：``similarity_search`` 的
SQL 與 RLS GUC 在 PostgreSQL 上的行為——那一半由 anila-core 自己的測試負責。
但**「一庫一個 store 實例」**這個 RLS 前提有被驗（見
``test_each_collection_gets_its_own_store``）：跨庫單一查詢會讓資料庫**靜默**
只回一庫的資料，而那正是「搜了、沒命中」變成謊話的後門。

⚠ fixture 一律 function-scoped：這些測試會改同一列 collection 的欄位，共用
會讓前一支的殘留變成下一支的前置條件（Task 1 踩過）。

⚠ 「已標記的庫裡有機密文件」在真實系統是**可以發生的**，所以 fixture 直接
造這種狀態不是在造假：標記時的四道檢查（``collections.py:_guard_anila_searchable``）
是 T 時刻的檢查，文件是 T+1 才加進去的；DB CHECK 只鎖 collection 那一列的
密等，管不到文件。誰把這個 fixture「修正」成不含機密文件，就是把本檔最重要
的那條保護刪掉。
"""
from __future__ import annotations

import os
import uuid

os.environ.setdefault("ANILA_ALLOW_DEV_SECRET", "1")

import pytest

import app.services.institutional_kb as kb_mod
from anila_core.storage.adapters.pgvector_store import SourceModelCoverage
from app.models.ingestion import IngestionCollection, IngestionDocument
from app.models.user import User
from app.schemas.contracts.classification import ClassificationLevel
from app.services.institutional_kb import KbState, retrieve_institutional

_UNCLASSIFIED = ClassificationLevel.UNCLASSIFIED.to_storage()
# ⚠ 從 enum 推導，不是自己列一份清單。將來加一級密等時，這裡要自動跟著長出
# 一輪測試；手寫的清單只會停在寫的那一天，而且不會有任何東西提醒你。
_CLASSIFIED_LEVELS = [
    level.to_storage()
    for level in ClassificationLevel
    if level is not ClassificationLevel.UNCLASSIFIED
]
# enum 以外、但**存得進去**的密等字串（該欄位是 String(20) 且沒有 CHECK）。
# 每一個都是真的會出現的漂移形狀：英文拼法、加註記、他系統的字彙、以及
# 尾隨空白（匯入/backfill 最常見的那一種，肉眼還看不出來）。
_OFF_ENUM_VALUES = ["SECRET", "機密(限閱)", "top_secret", "無機密 "]
_EMBED_MODEL = "nvidia/nv-embed-v2"
_DIM = 8


# ── DB fixtures ─────────────────────────────────────────────────────────────


def _owner(db) -> User:
    user = User(
        username=f"institutional-owner-{uuid.uuid4().hex[:12]}",
        hashed_password="x",
        role="developer",
        is_approved=True,
    )
    db.add(user)
    db.flush()
    return user


def _collection(db, name: str, *, searchable: bool, status: str = "active") -> IngestionCollection:
    coll = IngestionCollection(
        name=name,
        chunking_config={"strategy": "fixed"},
        embedding_model=_EMBED_MODEL,
        embedding_dim=_DIM,
        status=status,
        created_by=_owner(db).id,
        origin="csp",
        classification_level=_UNCLASSIFIED,
        anila_searchable=searchable,
    )
    db.add(coll)
    db.commit()
    db.refresh(coll)
    return coll


def _document(db, coll: IngestionCollection, filename: str, level: str) -> IngestionDocument:
    doc = IngestionDocument(
        collection_id=coll.id,
        filename=filename,
        sha256=f"{abs(hash(filename)):064x}"[:64],
        mime_type="application/pdf",
        status="indexed",
        classification_level=level,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


@pytest.fixture
def user(db) -> User:
    """嵌入要計量到人身上，所以檢索一定要帶著一個 user。"""
    from tests.conftest import make_user

    return make_user(db, username="institutional-asker", role="user")


@pytest.fixture
def one_marked_collection(db):
    coll = _collection(db, "人事規章", searchable=True)
    doc = _document(db, coll, "獎懲作業要點.pdf", _UNCLASSIFIED)
    coll._doc_ids = [doc.id]
    return coll


@pytest.fixture
def two_collections(db):
    """一個標記、一個沒標記，兩個都是無機密。

    無機密是**必要條件不是觸發條件** —— 沒標記的無機密庫不會被 ANILA 搜到。
    """
    marked = _collection(db, "人事規章", searchable=True)
    _document(db, marked, "獎懲作業要點.pdf", _UNCLASSIFIED)
    unmarked = _collection(db, "某人的私人庫", searchable=False)
    _document(db, unmarked, "私人筆記.pdf", _UNCLASSIFIED)
    return marked, unmarked


@pytest.fixture
def two_marked_collections(db):
    first = _collection(db, "人事規章", searchable=True)
    _document(db, first, "獎懲作業要點.pdf", _UNCLASSIFIED)
    second = _collection(db, "差勤規章", searchable=True)
    _document(db, second, "請假規則.pdf", _UNCLASSIFIED)
    return first, second


def _mixed_collection(db, level: str) -> IngestionCollection:
    """已標記的庫，裡面混著一份 ``level`` 密等的文件（見模組 docstring 的說明）。

    ⚠ 這裡收 ``level`` 參數而不是寫死「機密」，是本專案第二次因為同一個形狀
    被抓：**只造一個密等的 fixture，會讓其餘每一級都沒有人看著，而測試全綠。**
    把白名單（``== 無機密``）改成黑名單（``!= 機密``）時，營業秘密與密會外洩，
    而寫死機密的測試不會有任何反應。所以呼叫端一律 parametrize 整個 enum。
    """
    coll = _collection(db, "人事規章", searchable=True)
    clean = _document(db, coll, "獎懲作業要點.pdf", _UNCLASSIFIED)
    classified = _document(db, coll, f"個案調查報告-{level}.pdf", level)
    coll._clean_doc_ids = [clean.id]
    coll._classified_doc_ids = [classified.id]
    return coll


# ── 向量層 stub ─────────────────────────────────────────────────────────────


class _StubChunk:
    def __init__(self, document_id: int, content: str, metadata: dict | None = None):
        self.id = document_id * 100
        self.document_id = document_id
        self.chunk_key = f"chunk:{document_id}"
        self.content = content
        self.metadata: dict = metadata if metadata is not None else {}
        self.parent_chunk_id = None
        self.chunk_type = "leaf"
        self.chunk_level = 0


class _StubHit:
    def __init__(
        self,
        document_id: int,
        score: float,
        content: str = "第三條 申誡三次視同記過一次。",
        metadata: dict | None = None,
    ):
        self.chunk = _StubChunk(document_id, content, metadata)
        self.score = score
        self.parent_content = None


class _Backend:
    """一個假的向量後端，逐庫回答。

    ``similarity_search`` **必須真的套用 min_score**：門檻沒被傳下去的話
    ``test_below_threshold_is_a_miss_not_a_hit`` 就抓不到，而那是「沒命中」
    這個狀態唯一的來源。
    """

    def __init__(self):
        self.hits: dict[int, list[_StubHit]] = {}
        self.failing: set[int] = set()
        self.coverage: dict[int, SourceModelCoverage] = {}
        self.constructed: list[int] = []

    def _store(self, pool, collection_id: int):
        self.constructed.append(collection_id)
        return _StubStore(self, collection_id)

    def install(self, monkeypatch) -> "_Backend":
        monkeypatch.setattr(kb_mod, "get_pool", lambda: object())
        monkeypatch.setattr(
            kb_mod,
            "CollectionScopedPgVectorStore",
            lambda pool, collection_id: self._store(pool, collection_id),
        )
        return self


class _StubStore:
    def __init__(self, backend: _Backend, collection_id: int):
        self._backend = backend
        self._collection_id = collection_id

    async def similarity_search(self, *, query_embedding, top_k, min_score, source_model=None):
        if self._collection_id in self._backend.failing:
            raise RuntimeError("pgvector 連線中斷")
        hits = self._backend.hits.get(self._collection_id, [])
        return [h for h in hits if h.score >= min_score][:top_k]

    async def source_model_coverage(self, source_model: str) -> SourceModelCoverage:
        return self._backend.coverage.get(
            self._collection_id,
            SourceModelCoverage(has_matching=True, has_other=False, sample_other_model=None),
        )


@pytest.fixture
def backend(monkeypatch) -> _Backend:
    return _Backend().install(monkeypatch)


@pytest.fixture(autouse=True)
def stub_embed(monkeypatch):
    """嵌入走 CSP proxy（計量），測試裡不打網路。"""

    async def fake_embed_query(db, user, model_name, embedding_dim, query):
        return [0.1] * embedding_dim

    monkeypatch.setattr(kb_mod, "_embed_query", fake_embed_query)


# ── 標記是觸發條件 ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unmarked_unclassified_collection_is_not_searched(
    db, user, two_collections, backend
):
    """無機密是必要條件不是觸發條件 —— 擁有者 08-07 特別交代的那條。

    兩個庫在後端都有東西可回；只有標記過的那個可以出現在結果裡。
    """
    marked, unmarked = two_collections
    backend.hits[marked.id] = [_StubHit(marked.documents[0].id, 0.42)]
    backend.hits[unmarked.id] = [_StubHit(unmarked.documents[0].id, 0.99)]

    result = await retrieve_institutional(db, user, "申誡", threshold=0.0)

    assert {h.collection_id for h in result.hits} == {marked.id}
    assert backend.constructed == [marked.id]


@pytest.mark.asyncio
async def test_inactive_marked_collection_is_not_searched(db, user, backend):
    """標記還在、庫已停用 —— 停用的庫不該被檢索，而且不算失敗。"""
    coll = _collection(db, "已封存的規章", searchable=True, status="archived")
    doc = _document(db, coll, "舊版要點.pdf", _UNCLASSIFIED)
    backend.hits[coll.id] = [_StubHit(doc.id, 0.9)]

    result = await retrieve_institutional(db, user, "申誡", threshold=0.0)

    assert result.state is KbState.NOT_SEARCHED
    assert backend.constructed == []


@pytest.mark.asyncio
async def test_image_pks_on_chunk_metadata_reach_kb_hit(
    db, user, one_marked_collection, backend
):
    """Cited figures ride chunk metadata through retrieve — not the embed text."""
    coll = one_marked_collection
    doc_id = coll._doc_ids[0]
    backend.hits[coll.id] = [
        _StubHit(
            doc_id,
            0.91,
            content="見 [圖片描述：轉換區示意圖] 如圖。",
            metadata={"image_pks": [42], "strategy": "hierarchical"},
        )
    ]

    result = await retrieve_institutional(db, user, "轉換區", threshold=0.0)

    assert result.state is KbState.SEARCHED_HIT
    assert result.hits[0].image_pks == [42]
    assert "http" not in result.hits[0].content
    assert "/api/" not in result.hits[0].content


# ── 文件密等 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("level", _CLASSIFIED_LEVELS)
@pytest.mark.asyncio
async def test_classified_documents_never_appear(db, user, backend, level):
    """CHECK 管不到文件層 —— 真正會外洩的東西在這裡。

    後端同時回無機密與帶密等兩份文件的 chunk。兩筆是刻意的：只回帶密等那一筆
    的話，一個「什麼都不回」的壞實作也會綠。

    ⚠ **逐級跑遍整個 enum**（而不是只測「機密」）：過濾寫成黑名單時，只有被
    列舉到的那一級會被擋，其餘各級照樣出得去。密等的級數是 enum 的事，不是
    這支測試可以自己挑的。
    """
    mixed_collection = _mixed_collection(db, level)
    clean_id = mixed_collection._clean_doc_ids[0]
    classified_id = mixed_collection._classified_doc_ids[0]
    backend.hits[mixed_collection.id] = [
        _StubHit(classified_id, 0.99, content=f"{level}：個案當事人姓名"),
        _StubHit(clean_id, 0.42),
    ]

    result = await retrieve_institutional(db, user, "任何字", threshold=0.0)

    assert all(h.document_id != classified_id for h in result.hits)
    assert [h.document_id for h in result.hits] == [clean_id]
    assert all("個案當事人姓名" not in h.content for h in result.hits)
    assert result.state is KbState.SEARCHED_HIT


@pytest.mark.parametrize("stored_value", _OFF_ENUM_VALUES)
@pytest.mark.asyncio
async def test_a_classification_value_outside_the_enum_never_passes(
    db, user, backend, stored_value
):
    """白名單要被釘成**白名單**，不能只是「一個列得比較齊的黑名單」。

    讀完上一輪的 K（「要跑遍整個 enum」），下一個人最自然的修法是把過濾寫成
    ``classification_level.notin_([enum 裡每一個帶密等的值])``——列得很齊、
    parametrize 全綠、而且**是黑名單**。這一支就是為了讓那個修法當場死掉。

    ⚠ 事實根據，不是假想：``ingestion_documents.classification_level`` 是
    ``String(20), nullable=False``，**沒有 CHECK 約束**（``app/models/ingestion.py``
    裡唯一的 CHECK 是 Task 1 加在 collection 那一列的）。也就是說 enum 以外的
    字串是**存得進去的**——匯入、backfill、外部工具、手動 SQL、或哪天有人把
    「機密」寫成 ``'SECRET'`` 或「機密(限閱)」。黑名單對這些一律放行。

    這就是本專案寫在 memory 裡的那條教訓：**黑名單防護永遠補不完。** 判準只有
    一條——不是**明確**標為「無機密」的，一律不給。
    """
    coll = _collection(db, "人事規章", searchable=True)
    clean = _document(db, coll, "獎懲作業要點.pdf", _UNCLASSIFIED)
    off_enum = _document(db, coll, f"來路不明-{stored_value}.pdf", stored_value)
    backend.hits[coll.id] = [
        _StubHit(off_enum.id, 0.99, content="這份文件的密等沒有人認得"),
        _StubHit(clean.id, 0.42),
    ]

    result = await retrieve_institutional(db, user, "任何字", threshold=0.0)

    assert all(h.document_id != off_enum.id for h in result.hits)
    assert [h.document_id for h in result.hits] == [clean.id]
    assert all("沒有人認得" not in h.content for h in result.hits)


@pytest.mark.asyncio
async def test_a_hit_on_an_unknown_document_is_dropped(db, user, one_marked_collection, backend):
    """查無此文件 = 不知道密等 = 不給。密等過濾是白名單，不是黑名單。"""
    backend.hits[one_marked_collection.id] = [_StubHit(999_999, 0.9)]

    result = await retrieve_institutional(db, user, "申誡", threshold=0.0)

    assert result.hits == []
    assert result.state is KbState.SEARCHED_MISS


# ── 門檻 ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_below_threshold_is_a_miss_not_a_hit(db, user, one_marked_collection, backend):
    """top-k 永遠會回東西;沒有門檻就沒有「沒命中」這個狀態。"""
    backend.hits[one_marked_collection.id] = [
        _StubHit(one_marked_collection._doc_ids[0], 0.42)
    ]

    result = await retrieve_institutional(db, user, "完全無關的問題", threshold=0.99)

    assert result.state is KbState.SEARCHED_MISS
    assert result.hits == []


@pytest.mark.asyncio
async def test_above_threshold_is_a_hit(db, user, one_marked_collection, backend):
    """門檻的另一半：同一筆資料在門檻之下要是 HIT，否則上一支可以靠「永遠空」作弊。"""
    backend.hits[one_marked_collection.id] = [
        _StubHit(one_marked_collection._doc_ids[0], 0.42)
    ]

    result = await retrieve_institutional(db, user, "申誡", threshold=0.1)

    assert result.state is KbState.SEARCHED_HIT
    assert [h.filename for h in result.hits] == ["獎懲作業要點.pdf"]


# ── 失敗與部分失敗 ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_collection_failing_is_partial_not_hit(
    db, user, two_marked_collections, backend
):
    """一庫成功一庫炸掉卻顯示「有依據」= 昨晚才修掉那個謊的多庫版。"""
    first, second = two_marked_collections
    backend.hits[first.id] = [_StubHit(first.documents[0].id, 0.42)]
    backend.failing.add(second.id)

    result = await retrieve_institutional(db, user, "申誡", threshold=0.0)

    assert result.state is KbState.PARTIAL_ERROR
    assert result.failed_collections == [second.id]
    assert [h.collection_id for h in result.hits] == [first.id]


@pytest.mark.asyncio
async def test_all_collections_failing_is_error_not_miss(
    db, user, two_marked_collections, backend
):
    """「沒命中」與「查不了」必須是兩個不同的狀態。"""
    first, second = two_marked_collections
    backend.failing.update({first.id, second.id})

    result = await retrieve_institutional(db, user, "申誡", threshold=0.0)

    assert result.state is KbState.SEARCH_ERROR
    assert result.hits == []
    assert sorted(result.failed_collections) == sorted([first.id, second.id])


@pytest.mark.asyncio
async def test_one_failing_and_one_empty_is_error_not_miss(
    db, user, two_marked_collections, backend
):
    """一庫查不了、另一庫真的沒東西 —— 這時**不可以**說「搜過了，沒有」。

    沒查成的那一庫裡有沒有依據，沒有人知道。這一支釘的是實作裡兩個 if 的
    先後順序（先判 ``failed and not hits``），把它們對調就會變成 PARTIAL_ERROR。
    """
    first, second = two_marked_collections
    backend.failing.add(second.id)  # first 回空

    result = await retrieve_institutional(db, user, "申誡", threshold=0.0)

    assert result.state is KbState.SEARCH_ERROR
    assert result.failed_collections == [second.id]


@pytest.mark.asyncio
async def test_pool_unavailable_is_error_not_miss(
    db, user, two_marked_collections, backend, monkeypatch
):
    """連線池起不來 = 一庫都沒查成。這**不是**「搜過了，沒有」。

    這是整個功能存在的理由的最短版本：檢索根本沒有跑，而使用者被告知它跑過、
    而且院內規章沒有講到這件事。把這個分支的狀態改成 SEARCHED_MISS，本檔其餘
    每一支都還是綠的——所以這一支必須存在。

    ``get_pool`` 起不來是真的會發生的（``ingestion_pool.py`` 在 pool 尚未初始化
    時丟 RuntimeError），而且它是**全域**失敗：兩個庫要全數列進 failed，不是
    只列第一個。
    """
    first, second = two_marked_collections

    def _no_pool():
        raise RuntimeError("ingestion pool 尚未初始化")

    monkeypatch.setattr(kb_mod, "get_pool", _no_pool)

    result = await retrieve_institutional(db, user, "申誡", threshold=0.0)

    assert result.state is KbState.SEARCH_ERROR
    assert result.hits == []
    assert sorted(result.failed_collections) == sorted([first.id, second.id])
    assert backend.constructed == []


@pytest.mark.asyncio
async def test_stranded_index_is_a_failure_not_a_miss(
    db, user, one_marked_collection, backend, monkeypatch
):
    """索引在另一個嵌入空間 = 查不到不是因為沒資料。

    ``_assert_index_matches_designation`` 對這種庫丟 409。在單庫端點那是整個
    請求的錯誤；在這裡它必須降級成「這一庫失敗」，而不是被吞成「沒命中」——
    也不可以讓一個 409 把整批檢索中斷掉。
    """
    from app.models.model_registry import ModelRegistry

    db.add(
        ModelRegistry(
            name=_EMBED_MODEL,
            display_name=_EMBED_MODEL,
            model_type="embedding",
            endpoint_url="http://embed.test/v1",
            is_active=True,
            is_platform_embedding=True,
            embedding_native_dim=_DIM,
        )
    )
    db.commit()
    backend.hits[one_marked_collection.id] = []
    backend.coverage[one_marked_collection.id] = SourceModelCoverage(
        has_matching=False, has_other=True, sample_other_model="legacy/old-embedder"
    )

    result = await retrieve_institutional(db, user, "申誡", threshold=0.0)

    assert result.state is KbState.SEARCH_ERROR
    assert result.failed_collections == [one_marked_collection.id]


# ── 沒有標記過任何庫 ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_marked_set_is_not_searched_not_miss(db, user, backend):
    """一個庫都沒標時不要顯示搜過。"""
    coll = _collection(db, "某人的私人庫", searchable=False)
    _document(db, coll, "私人筆記.pdf", _UNCLASSIFIED)

    result = await retrieve_institutional(db, user, "申誡", threshold=0.0)

    assert result.state is KbState.NOT_SEARCHED
    assert result.hits == []
    assert result.failed_collections == []


# ── RLS 的前提 ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_each_collection_gets_its_own_store(db, user, two_marked_collections, backend):
    """逐庫檢索，一庫一個 store 實例 —— 這是 RLS 放行的方式，不是風格偏好。

    ``document_chunks`` 的 RLS 靠 ``SET LOCAL anila.collection_id`` 放行，而那個
    GUC 綁在 store 的建構子裡。誰把這裡改成跨庫單一查詢，資料庫會**靜默**濾到
    只剩一庫，而錯誤訊息一個都不會有。
    """
    first, second = two_marked_collections
    backend.hits[first.id] = [_StubHit(first.documents[0].id, 0.42)]
    backend.hits[second.id] = [_StubHit(second.documents[0].id, 0.55)]

    result = await retrieve_institutional(db, user, "申誡", threshold=0.0)

    assert sorted(backend.constructed) == sorted([first.id, second.id])
    assert len(backend.constructed) == 2
    # 跨庫結果依分數排序後合併（不是照庫的順序串起來）。
    assert [h.collection_id for h in result.hits] == [second.id, first.id]
