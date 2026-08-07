# ANILA 檢索院內規章知識庫 — 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 ANILA 聊天在 Router 判斷「不需要 agent」時，自動檢索管理員指定的院內規章知識庫，並且使用者永遠分得出答案有沒有規章依據。

**Architecture:** 知識庫加一個 `anila_searchable` 標記（密等由 DB CHECK 鎖死）；Router 直答時多送一個 header 告訴 CSP；CSP 在 `/v1/chat/completions` 逐庫檢索（RLS 要求）、濾掉機密文件、套用可設定的分數門檻，把命中內容與五狀態之一放進 `anila_meta`；前端依狀態渲染徽章、原文泡泡與「改用院內規章重查」。

**Tech Stack:** FastAPI + SQLAlchemy + Alembic（csp）、pgvector（`CollectionScopedPgVectorStore`）、Vue 3（治理中心）、React/vitest（anila-shell）、pytest。

## Global Constraints

- 分支 `restart/from-redesign`；**migration 編號 `r1_0033`，`down_revision = "r1_0032"`**（偵察已確認 head 是 `r1_0032`，無撞車）。
- **絕不對活體資料庫跑 alembic**。測試用丟棄式容器：`docker run --rm -d --name plan33-pg -e POSTGRES_PASSWORD=<throwaway> -p 127.0.0.1:5550:5432 pgvector/pgvector:pg16`，用完 `docker rm -f plan33-pg`。
- **不得修改 `packages/anila-core/.../pgvector_store.py` 或任何共用檢索元件**。agent 與 ANILALM 路徑檢索機密內容是合法的；全域加過濾＝靜默弄壞別人。
- **不得修改 `_require_collection_access`**（`services/csp/app/api/ingestion/collections.py:61`）——它被 `documents.py:211`、`image_blob.py`、`conversations.py:471` 等 8 處共用，放寬它等於同時放寬灌文件與刪除。權限例外只開在 `search.py` 側。
- 祕密零外洩（PUBLIC repo）。不得在容器內 `env`／`printenv`／`env | grep`。
- 每個 Task 結束都 commit；commit message 用英文。
- 完整 csp 套件約 12 分鐘，**由指揮官在背景跑**；各 Task 只跑自己相關的子集。
- 驗收另派 fresh agent，驗收單必帶：**「Look for the shape this package exists to eliminate, in the package's own work.」** 本包要消滅的形狀是**「使用者以為答案有規章依據，其實沒有」**。

---

## File Structure

| 檔案 | 責任 |
|---|---|
| `services/csp/migrations/versions/r1_0033_anila_searchable.py` | 新欄位 + CHECK 約束（新建） |
| `services/csp/app/models/ingestion.py:52` | `IngestionCollection` 加 `anila_searchable` 欄位 |
| `services/csp/app/api/ingestion/collections.py` | 標記的 PATCH 端點 + 三道前置檢查 + CHECK 違反翻成人話 |
| `services/csp/app/services/institutional_kb.py` | **新** — 多庫檢索、文件密等過濾、門檻、五狀態判定。本包唯一的新模組 |
| `services/csp/app/api/proxy.py:803` | 注入點（照 `_inject_memory` / `_inject_attachments` 樣板） |
| `services/csp/app/services/proxy/service.py:388` | `anila_meta` 加狀態欄位 |
| `packages/anila-core/src/anila_core/api/router_server.py:1132` | 直答時送 `X-Anila-Route: direct` |
| `apps/csp-governance-ui/src/views/KnowledgeCollectionsView.vue:66` | 卡片 footer 加標記 toggle |
| `apps/anila-shell/src/chat.jsx:665` | 五狀態徽章 + 原文泡泡；`:884` 重新產生選單加一項 |
| `apps/anila-shell/src/app.jsx:898` | meta → message 欄位映射 |

---

## Task 1：資料模型與 migration

**Files:**
- Create: `services/csp/migrations/versions/r1_0033_anila_searchable.py`
- Modify: `services/csp/app/models/ingestion.py:52-110`
- Test: `services/csp/tests/test_anila_searchable_pg.py`（新）

**Interfaces:**
- Produces: `IngestionCollection.anila_searchable: bool`；DB 約束 `ck_ingestion_collections_anila_searchable_unclassified`

- [ ] **Step 1: 寫失敗測試（CHECK 約束）**

`services/csp/tests/test_anila_searchable_pg.py`：

```python
# -*- coding: utf-8 -*-
"""r1_0033 的 CHECK 約束：已標記的庫不可能是機密。

為什麼要 PG：SQLite 不執行 CHECK 約束，這支測的就是資料庫層擋不擋。
⚠ 這支刻意用 superuser 跑就好 —— CHECK 對 superuser 一樣會擋（不像 RLS 會被 bypass）。
"""
import os
import pytest

psycopg2 = pytest.importorskip("psycopg2")
_DSN = os.environ.get("ANILA_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(
    not _DSN, reason="ANILA_TEST_PG_DSN not set — needs PostgreSQL"
)


def test_raising_classification_on_a_marked_collection_fails(upgraded_conn):
    conn, cur, coll_id = upgraded_conn
    cur.execute(
        "UPDATE ingestion_collections SET anila_searchable = true WHERE id = %s",
        (coll_id,),
    )
    conn.commit()
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(
            "UPDATE ingestion_collections SET classification_level = '機密' WHERE id = %s",
            (coll_id,),
        )
    conn.rollback()


def test_marking_a_classified_collection_fails(upgraded_conn):
    conn, cur, coll_id = upgraded_conn
    cur.execute(
        "UPDATE ingestion_collections SET classification_level = '機密' WHERE id = %s",
        (coll_id,),
    )
    conn.commit()
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(
            "UPDATE ingestion_collections SET anila_searchable = true WHERE id = %s",
            (coll_id,),
        )
    conn.rollback()
```

fixture `upgraded_conn` 照抄 `services/csp/tests/test_embedding_model_canonical_pg.py:192`（`scratch_dsn`）與 `:401`（`upgraded`）的形狀，但**不需要 NOSUPERUSER 角色**——CHECK 不會被 superuser 繞過。種一列 collection（`classification_level='無機密'`、`embedding_model='nv-embed-v2'`、`embedding_dim=4000`、`origin='csp'`、`status='active'`）並回傳其 id。

- [ ] **Step 2: 跑測試確認失敗**

```bash
docker run --rm -d --name plan33-pg -e POSTGRES_PASSWORD=throwaway -p 127.0.0.1:5550:5432 pgvector/pgvector:pg16
sleep 5
cd services/csp && ANILA_TEST_PG_DSN="postgresql://postgres:throwaway@127.0.0.1:5550/postgres" \
  $PY -m pytest tests/test_anila_searchable_pg.py -q
```
Expected: FAIL —— `column "anila_searchable" does not exist`

- [ ] **Step 3: 寫 migration**

`services/csp/migrations/versions/r1_0033_anila_searchable.py`：

```python
# -*- coding: utf-8 -*-
"""ANILA 可直接檢索的知識庫標記。

SYSTEM-MAP §3 的主線圖寫著 Router 判斷「不需要 agent」時用院內知識庫直答，
這條路一直沒實作。本 migration 加上那個標記，並把「已標記的庫不可能是機密」
交給資料庫的 CHECK 約束閉合 —— 不是交給應用程式。

⚠ 那條 CHECK 的價值在於：升密的程式如果忘了先取消標記,UPDATE 會**當場失敗**,
而不是靜默留下一個被全院檢索的機密庫。耦合看得見、會叫。

⚠ 無機密是**必要條件不是觸發條件**：這條約束只禁止「機密＋已標記」這個組合,
不會讓任何庫因為密等低就自動變成已標記。

⚠ ``down_revision = r1_0032``：r1_0032 是 2026-08-07 的大小寫正規化。

Revision ID: r1_0033
Revises: r1_0032
Create Date: 2026-08-07
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r1_0033"
down_revision: Union[str, None] = "r1_0032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "ingestion_collections"
_COLUMN = "anila_searchable"
_CHECK = "ck_ingestion_collections_anila_searchable_unclassified"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            _COLUMN,
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_check_constraint(
        _CHECK,
        _TABLE,
        f"NOT {_COLUMN} OR classification_level = '無機密'",
    )


def downgrade() -> None:
    op.drop_constraint(_CHECK, _TABLE, type_="check")
    op.drop_column(_TABLE, _COLUMN)
```

- [ ] **Step 4: 加 ORM 欄位**

`services/csp/app/models/ingestion.py`：`Boolean` 加進 `:20` 起的 import；`IngestionCollection` 在 `classification_level`（:102）之後加：

```python
    # ANILA 聊天可直接檢索這個庫（SYSTEM-MAP §3 的「不需要 agent」那條路）。
    # ⚠ 只有「無機密」能開,由 DB CHECK ck_ingestion_collections_anila_searchable_unclassified
    # 閉合 —— 升密時忘了取消標記會讓 UPDATE 失敗,不是靜默留洞。
    # ⚠ 無機密是必要條件不是觸發條件:沒標記的無機密庫不會被 ANILA 搜到。
    anila_searchable = Column(
        Boolean, nullable=False, default=False, server_default="false"
    )
```

- [ ] **Step 5: 跑測試確認通過**

同 Step 2 的命令。Expected: 2 passed。然後 `docker rm -f plan33-pg`。

- [ ] **Step 6: Commit**

```bash
git add services/csp/migrations/versions/r1_0033_anila_searchable.py \
        services/csp/app/models/ingestion.py \
        services/csp/tests/test_anila_searchable_pg.py
git commit -m "feat(ingestion): mark collections ANILA may search, with the classification gate in the database"
```

---

## Task 2：標記端點與三道前置檢查

**Files:**
- Modify: `services/csp/app/api/ingestion/collections.py`
- Test: `services/csp/tests/test_anila_searchable_api.py`（新）

**Interfaces:**
- Consumes: Task 1 的 `IngestionCollection.anila_searchable`
- Produces: `PATCH /api/ingestion/collections/{id}` 接受 `anila_searchable: bool`；四種 400 訊息

- [ ] **Step 1: 寫失敗測試**

`services/csp/tests/test_anila_searchable_api.py`，照 `test_collection_classification_raise.py:23-37` 的 `login` / `_auth` / `_create_payload` 樣板：

```python
def test_non_admin_cannot_mark(client, db):
    """標記是密等相鄰操作,只有 admin 能動。"""
    token = login(client, "plain_user")
    coll = _create_collection(client, token, "一般庫")
    r = client.patch(f"/api/ingestion/collections/{coll['id']}",
                     json={"anila_searchable": True}, headers=_auth(token))
    assert r.status_code == 403


def test_cannot_mark_an_anilalm_personal_notebook(client, db, admin_token):
    """個人知識庫變全院可搜不是本功能的本意。"""
    coll = _create_collection(client, admin_token, "我的筆記", origin="anilalm")
    r = client.patch(f"/api/ingestion/collections/{coll['id']}",
                     json={"anila_searchable": True}, headers=_auth(admin_token))
    assert r.status_code == 400
    assert "個人知識庫" in r.json()["detail"]


def test_cannot_mark_when_embedding_model_differs_from_the_marked_set(
    client, db, admin_token
):
    """不同嵌入空間的分數不能互比;錯誤訊息要給做法,不能只說不行。"""
    a = _create_collection(client, admin_token, "甲", embedding_model="nv-embed-v2")
    client.patch(f"/api/ingestion/collections/{a['id']}",
                 json={"anila_searchable": True}, headers=_auth(admin_token))
    b = _create_collection(client, admin_token, "乙", embedding_model="other-model")
    r = client.patch(f"/api/ingestion/collections/{b['id']}",
                     json={"anila_searchable": True}, headers=_auth(admin_token))
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "other-model" in detail and "nv-embed-v2" in detail
    assert "重新嵌入" in detail   # 給做法,不是只說不行


def test_cannot_mark_when_the_library_holds_a_classified_document(
    client, db, admin_token
):
    """CHECK 只鎖 collection 那一列,管不到文件 —— 標記時先把話講明。"""
    coll = _create_collection(client, admin_token, "混雜庫")
    _add_document(db, coll["id"], classification_level="機密")
    r = client.patch(f"/api/ingestion/collections/{coll['id']}",
                     json={"anila_searchable": True}, headers=_auth(admin_token))
    assert r.status_code == 400
    assert "機密" in r.json()["detail"]


def test_raising_classification_while_marked_says_what_to_do(
    client, db, admin_token
):
    """撞到 CHECK 的人要看得懂、知道怎麼自救,不是 500。"""
    coll = _create_collection(client, admin_token, "要升密的庫")
    client.patch(f"/api/ingestion/collections/{coll['id']}",
                 json={"anila_searchable": True}, headers=_auth(admin_token))
    r = client.patch(f"/api/ingestion/collections/{coll['id']}",
                     json={"classification_level": "機密"}, headers=_auth(admin_token))
    assert r.status_code == 400
    assert "取消" in r.json()["detail"] and "ANILA" in r.json()["detail"]
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
cd services/csp && $PY -m pytest tests/test_anila_searchable_api.py -q
```
Expected: FAIL（欄位不被接受 / 回 200 而非 400）

- [ ] **Step 3: 實作端點與檢查**

`services/csp/app/api/ingestion/collections.py` — `CollectionUpdate` schema 加 `anila_searchable: Optional[bool] = None`，並在 PATCH handler 內：

```python
_MARK_ERRORS = {
    "not_admin": "只有管理員可以設定 ANILA 檢索標記",
    "anilalm": "個人知識庫（origin=anilalm）不可標記為 ANILA 可檢索",
}


def _guard_anila_searchable(db: Session, coll: IngestionCollection) -> None:
    """開標記前的三道檢查。⚠ 全部要給做法,不能只說不行。"""
    if coll.origin == "anilalm":
        raise HTTPException(400, detail=_MARK_ERRORS["anilalm"])

    marked_model = (
        db.query(IngestionCollection.embedding_model)
        .filter(
            IngestionCollection.anila_searchable.is_(True),
            IngestionCollection.id != coll.id,
        )
        .first()
    )
    if marked_model and marked_model[0] != coll.embedding_model:
        raise HTTPException(
            400,
            detail=(
                f"此庫用 {coll.embedding_model}，已標記集用 {marked_model[0]}；"
                f"跨嵌入空間的分數不能互相比較。請先用 {marked_model[0]} 重新嵌入再標記。"
            ),
        )

    classified = (
        db.query(IngestionDocument.classification_level)
        .filter(
            IngestionDocument.collection_id == coll.id,
            IngestionDocument.classification_level != "無機密",
        )
        .first()
    )
    if classified:
        raise HTTPException(
            400,
            detail=(
                f"此庫內含密等「{classified[0]}」的文件。標記後這些文件不會被檢索，"
                "但請先確認它們是否應該留在這個庫裡。"
            ),
        )
```

升密撞 CHECK 的翻譯（同檔 PATCH handler 內，包住 `db.commit()`）：

```python
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if _CHECK_NAME in str(exc.orig):
            raise HTTPException(
                400,
                detail=(
                    "此庫已標記為 ANILA 可檢索，不能升密。"
                    "請先取消 ANILA 檢索標記，再調整密等。"
                ),
            ) from exc
        raise
```

標記翻轉寫稽核（照同檔既有 `log_audit_event` 用法）。

- [ ] **Step 4: 跑測試確認通過**

Expected: 5 passed。

- [ ] **Step 5: Commit**

```bash
git add services/csp/app/api/ingestion/collections.py services/csp/tests/test_anila_searchable_api.py
git commit -m "feat(ingestion): admin-only marking, with every refusal naming a way out"
```

---

## Task 3：檢索服務（本包唯一的新模組）

**Files:**
- Create: `services/csp/app/services/institutional_kb.py`
- Test: `services/csp/tests/test_institutional_kb.py`（新）

**Interfaces:**
- Consumes: Task 1 的欄位；既有 `CollectionScopedPgVectorStore`、`SearchRequest.document_ids`
- Produces:
  ```python
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

  @dataclass
  class KbResult:
      state: KbState
      hits: list[KbHit]
      failed_collections: list[int]

  async def retrieve_institutional(db, query: str, *, threshold: float) -> KbResult
  ```

- [ ] **Step 1: 寫失敗測試**

```python
async def test_unmarked_unclassified_collection_is_not_searched(db, two_collections):
    """無機密是必要條件不是觸發條件 —— 擁有者 08-07 特別交代的那條。"""
    marked, unmarked = two_collections
    result = await retrieve_institutional(db, "申誡", threshold=0.0)
    assert {h.collection_id for h in result.hits} == {marked.id}


async def test_classified_documents_never_appear(db, mixed_collection):
    """CHECK 管不到文件層 —— 真正會外洩的東西在這裡。"""
    result = await retrieve_institutional(db, "任何字", threshold=0.0)
    assert all(h.document_id not in mixed_collection.classified_doc_ids
               for h in result.hits)


async def test_below_threshold_is_a_miss_not_a_hit(db, one_marked_collection):
    """top-k 永遠會回東西;沒有門檻就沒有「沒命中」這個狀態。"""
    result = await retrieve_institutional(db, "完全無關的問題", threshold=0.99)
    assert result.state is KbState.SEARCHED_MISS
    assert result.hits == []


async def test_one_collection_failing_is_partial_not_hit(db, two_collections, monkeypatch):
    """一庫成功一庫炸掉卻顯示「有依據」= 昨晚才修掉那個謊的多庫版。"""
    _make_second_collection_raise(monkeypatch)
    result = await retrieve_institutional(db, "申誡", threshold=0.0)
    assert result.state is KbState.PARTIAL_ERROR
    assert result.failed_collections


async def test_all_collections_failing_is_error_not_miss(db, two_collections, monkeypatch):
    """「沒命中」與「查不了」必須是兩個不同的狀態。"""
    _make_all_collections_raise(monkeypatch)
    result = await retrieve_institutional(db, "申誡", threshold=0.0)
    assert result.state is KbState.SEARCH_ERROR


async def test_empty_marked_set_is_not_searched_not_miss(db):
    """一個庫都沒標時不要顯示搜過。"""
    result = await retrieve_institutional(db, "申誡", threshold=0.0)
    assert result.state is KbState.NOT_SEARCHED
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
cd services/csp && $PY -m pytest tests/test_institutional_kb.py -q
```
Expected: FAIL — `ModuleNotFoundError: app.services.institutional_kb`

- [ ] **Step 3: 實作**

`services/csp/app/services/institutional_kb.py`。要點（逐條對應設計）：

```python
async def retrieve_institutional(db, query: str, *, threshold: float) -> KbResult:
    """檢索所有已標記的院內規章知識庫。

    ⚠ **逐庫檢索,不可寫成跨庫單一查詢。** document_chunks 的 RLS 靠
    ``SET LOCAL anila.collection_id`` 放行,而那個 GUC 綁在
    CollectionScopedPgVectorStore 的建構子裡(pgvector_store.py:138)——
    一庫一個 store 實例。誰要是繞過 store 直接寫跨庫 SQL,資料庫會**靜默**
    濾到只剩 GUC 指的那一庫,「搜了、沒命中」的謊就從後門回來,而且不報錯。

    ⚠ **文件密等過濾用既有的 document_ids 機制**(SearchRequest.document_ids),
    不改共用的 pgvector_store —— agent 與 ANILALM 檢索機密內容是合法的。
    """
    colls = (
        db.query(IngestionCollection)
        .filter(
            IngestionCollection.anila_searchable.is_(True),
            IngestionCollection.status == "active",
        )
        .all()
    )
    if not colls:
        return KbResult(state=KbState.NOT_SEARCHED, hits=[], failed_collections=[])

    hits: list[KbHit] = []
    failed: list[int] = []
    for coll in colls:
        allowed_docs = [
            d.id
            for d in db.query(IngestionDocument.id)
            .filter(
                IngestionDocument.collection_id == coll.id,
                IngestionDocument.classification_level == "無機密",
            )
            .all()
        ]
        if not allowed_docs:
            continue
        try:
            store = CollectionScopedPgVectorStore(pool, collection_id=coll.id)
            rows = await store.similarity_search(
                query_embedding=vec, top_k=_TOP_K, min_score=threshold,
                source_model=coll.embedding_model,
            )
        except Exception:
            logger.warning("institutional_kb: collection %s 檢索失敗", coll.id)
            failed.append(coll.id)
            continue
        hits.extend(
            _to_hit(coll, r) for r in rows if r.chunk.document_id in set(allowed_docs)
        )

    hits.sort(key=lambda h: h.score, reverse=True)
    if failed and not hits:
        return KbResult(state=KbState.SEARCH_ERROR, hits=[], failed_collections=failed)
    if failed:
        return KbResult(state=KbState.PARTIAL_ERROR, hits=hits, failed_collections=failed)
    state = KbState.SEARCHED_HIT if hits else KbState.SEARCHED_MISS
    return KbResult(state=state, hits=hits, failed_collections=[])
```

- [ ] **Step 4: 跑測試確認通過**

Expected: 6 passed。

- [ ] **Step 5: 突變檢查（本 Task 的守衛必須會叫）**

手動施加三個突變，每個都必須讓測試轉紅，然後還原：

```
突變 A：把 SEARCHED_MISS 改成 SEARCHED_HIT      → test_below_threshold… 必須紅
突變 B：把 PARTIAL_ERROR 改成 SEARCHED_HIT      → test_one_collection_failing… 必須紅
突變 C：拿掉 allowed_docs 過濾                   → test_classified_documents… 必須紅
```

還原後 `git diff` 必須為空。**任何一個突變沒讓測試轉紅，那條保護就是不存在的。**

- [ ] **Step 6: Commit**

```bash
git add services/csp/app/services/institutional_kb.py services/csp/tests/test_institutional_kb.py
git commit -m "feat(csp): institutional knowledge retrieval, per collection because RLS says so"
```

---

## Task 4：分數門檻設定 + 校準視圖

**Files:**
- Create: `services/csp/app/models/platform_setting.py`
- Create: `services/csp/migrations/versions/r1_0034_platform_settings.py`
- Create: `services/csp/app/api/institutional_kb.py`（設定端點 + 校準端點）
- Modify: `services/csp/app/main.py`（掛新 router）
- Test: `services/csp/tests/test_kb_threshold_setting.py`（新）

⚠ **前提查驗（指揮官已做）**：這個系統**沒有**平台層級設定表。唯一的 `ui_settings`
（`app/models/user.py:52`）是**掛在使用者身上**的 JSON blob、整包取代，交接文件記著
「寫第三個 key 會在下次存檔被洗掉」——**不可拿來當前例**。所以本 Task 要新建。

⚠ **migration 編號由指揮官分配：`r1_0034`，`down_revision = "r1_0033"`。**
（已知：`origin/main` 上也有 r1_0033/r1_0034 之類的編號，兩條線在 r1_0030 之後已經各走各的；
本分支照本地線編號，不要為了避開 main 而跳號。）

**Interfaces:**
- Consumes: Task 3 的 `retrieve_institutional(...)`
- Produces:
  - `PlatformSetting` model：`key: str` (PK)、`value: str`、`updated_at`、`updated_by`
  - `get_kb_threshold(db) -> float` / `set_kb_threshold(db, value, *, actor) -> None`
  - `GET/PUT /api/institutional-kb/threshold`
  - `POST /api/institutional-kb/preview` → `{"hits": [{"content", "score", "filename", "collection_id"}], "threshold": float, "state": str}`

- [ ] **Step 1: 寫失敗測試**

```python
def test_threshold_change_takes_effect_without_restart(client, admin_token, marked_collection):
    """⚠ 這是「設定頁」那件大工程的第一塊磚。改了畫面卻不影響行為 = 假控制項。"""
    client.put("/api/institutional-kb/threshold", json={"value": 0.0}, headers=_auth(admin_token))
    before = client.post("/api/institutional-kb/preview", json={"query": "申誡"},
                         headers=_auth(admin_token)).json()
    assert before["hits"], "門檻 0 應該有命中"

    client.put("/api/institutional-kb/threshold", json={"value": 0.99}, headers=_auth(admin_token))
    after = client.post("/api/institutional-kb/preview", json={"query": "申誡"},
                        headers=_auth(admin_token)).json()
    assert after["hits"] == [], "同一個行程內就要生效,不重啟"


def test_preview_returns_scores_so_an_admin_can_calibrate(client, admin_token, marked_collection):
    """給數字輸入框而不給證據,等於叫人猜。"""
    r = client.post("/api/institutional-kb/preview", json={"query": "申誡"},
                    headers=_auth(admin_token))
    assert all("score" in h and "content" in h for h in r.json()["hits"])


def test_threshold_is_admin_only(client, plain_token):
    r = client.put("/api/institutional-kb/threshold", json={"value": 0.5},
                   headers=_auth(plain_token))
    assert r.status_code == 403


def test_default_is_declared_uncalibrated(client, admin_token):
    """PLAN.md:77 —— 手上的 0.3 是用替代模型量的,對真 nv-embed 必須重校。
    不准假裝它是已知數。"""
    r = client.get("/api/institutional-kb/threshold", headers=_auth(admin_token))
    body = r.json()
    assert body["value"] == 0.3
    assert body["calibrated"] is False


def test_out_of_range_is_refused_with_a_usable_message(client, admin_token):
    for bad in (-0.1, 1.1):
        r = client.put("/api/institutional-kb/threshold", json={"value": bad},
                       headers=_auth(admin_token))
        assert r.status_code == 422 or r.status_code == 400


def test_preview_never_leaks_a_classified_document(client, admin_token, marked_collection_with_classified_doc):
    """校準視圖跟正式檢索走同一條路,不可以有自己的較寬鬆版本。"""
    r = client.post("/api/institutional-kb/preview", json={"query": "任何字"},
                    headers=_auth(admin_token))
    assert all(h["document_id"] != marked_collection_with_classified_doc.classified_doc_id
               for h in r.json()["hits"])
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
cd services/csp && $PY -m pytest tests/test_kb_threshold_setting.py -q
```
Expected: FAIL — 404（端點不存在）

- [ ] **Step 3: 實作 model + migration**

`platform_setting.py`：key/value 單列設定，`key` 為主鍵。migration `r1_0034` 建表，
`down_revision = "r1_0033"`，`downgrade()` drop table。

- [ ] **Step 4: 實作端點**

⚠ **讀取不可快取成行程生命期**——否則「改了立刻生效」就是假的。若要快取，必須在
`set_kb_threshold` 時失效，並且要有測試證明同一行程內改完即生效（Step 1 第一支）。

⚠ **校準端點必須呼叫 Task 3 的 `retrieve_institutional`**，不可以自己寫一份較寬鬆的檢索——
一旦兩條路徑分岔，校準看到的就不是正式檢索會看到的。

- [ ] **Step 5: 跑測試確認通過**

Expected: 6 passed。

- [ ] **Step 6: 突變檢查**

```
突變 A：讀取改成行程啟動時讀一次           → test_threshold_change_takes_effect… 必須紅
突變 B：preview 自己寫一份不濾密等的檢索    → test_preview_never_leaks…        必須紅
突變 C：admin 檢查拿掉                     → test_threshold_is_admin_only      必須紅
突變 D：calibrated 硬寫 True               → test_default_is_declared_uncalibrated 必須紅
```

- [ ] **Step 7: Commit**

```bash
git commit -m "feat(csp): the retrieval threshold is a setting with a calibration view, not a constant"
```

---

## Task 5：Router 直答訊號

**Files:**
- Modify: `packages/anila-core/src/anila_core/api/router_server.py`（直答分支 `:1133` 非串流、`:1853` 串流）
- Test: `packages/anila-core/tests/test_router_direct_header.py`（新）

⚠ **前提查驗（指揮官已做）**：
- CSP 端**不存在**「Router 決定直答」的訊號。判定在 router 服務內（`:1133` / `:1853`），
  而 router 直答時**自己回給前端**；只有 LLM 呼叫會打到 CSP，CSP 看到的是一個普通模型呼叫。
  `routed_agent_id` 在非測試 Python 碼中 **0 命中**。
- **已有現成通道**：`router_server.py:907-910` 的 `anila_headers` 會把進來的 `x-anila-*`
  header 轉發給 CSP，四個呼叫點在用（`:1052`、`:1083`、`:1099`、`:1432`）。
  訊號搭這條，**不要新建機制**。

**Interfaces:**
- Produces: router 直答時，對 CSP 的 LLM 呼叫帶 `X-ANILA-Route: direct`；派工路徑**不帶**。

### ⚠ 這個 Task 要決定並在報告寫清楚的一件事

`anila_headers` 是**從 inbound 請求複製**的，所以**前端也能送 `X-ANILA-Route`**。

- 這不是安全漏洞：檢索範圍只有「已標記且無機密」的庫，全院本來就看得到。
- 而且 **Task 9 的「改用院內規章重查」按鈕正需要一條使用者強制檢索的路**。
- 但 CSP 會**分不出「router 判斷要查」與「使用者按了重查」**。

**決定怎麼區分並實作**：建議兩個不同的值（例如 `direct` 與 `forced`），
router 只會送前者，前端送後者。理由：兩者在五狀態機裡的意義不同——
router 判斷錯時使用者按重查，稽核上要看得出來是人救的還是機器決定的。
如果你有更好的做法，做你的，但**必須在報告裡說明前端可偽造這件事怎麼處理**。

- [ ] **Step 1: 寫失敗測試**

```python
def test_direct_answer_forwards_the_route_header():
    """Router 判斷不需要 agent 時,CSP 要知道這是它在直答。"""
    captured = _capture_downstream_headers(dispatch=None)
    assert captured.get("X-ANILA-Route") == "direct"


def test_dispatch_path_does_not_forward_it():
    """派給 agent 的路徑不加規章檢索 —— agent 自己搜自己的庫。"""
    captured = _capture_downstream_headers(dispatch={"agent": "image-generator"})
    assert "X-ANILA-Route" not in captured


def test_streaming_direct_answer_forwards_it_too():
    """payload 有兩條路,訊號必須兩條都騎(串流 :1853 / 非串流 :1133)。"""
    captured = _capture_downstream_headers(dispatch=None, stream=True)
    assert captured.get("X-ANILA-Route") == "direct"


def test_a_client_supplied_route_header_is_distinguishable():
    """前端可以送(Task 9 的重查按鈕要用),但不可以冒充成 router 的判斷。"""
    captured = _capture_downstream_headers(
        dispatch=None, inbound_headers={"X-ANILA-Route": "forced"}
    )
    assert captured.get("X-ANILA-Route") in ("forced", "direct")
    # 斷言兩者可分辨——實作者決定確切語意後,把這條寫成明確斷言。
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
cd packages/anila-core && $PY -m pytest tests/test_router_direct_header.py -q
```
Expected: FAIL — header 不存在

- [ ] **Step 3: 實作**（兩條路徑都要）

- [ ] **Step 4: 跑測試確認通過**

- [ ] **Step 5: 突變檢查**

```
突變 A：只在非串流路徑加 header        → 串流那條測試必須紅
突變 B：派工路徑也加 header            → dispatch 那條測試必須紅
突變 C：把 client 送的值原樣當 router 的判斷 → 可分辨那條必須紅
```

- [ ] **Step 6: Commit**

```bash
git commit -m "feat(router): tell CSP when the router is answering directly"
```

---

## Task 6：CSP 注入與五狀態 payload

> **裁決脈絡（Q39，2026-08-07，取代本計畫原先的「收到直答訊號才檢索」前提）**：
> 注入掛在 router 的**答案通道**呼叫上——`X-ANILA-Route` header 在就檢索，不等「直答判定」
> （那個判定在時序上晚於這通呼叫；header 語意以 `task-5-report.md` §1 為準，不是本計畫
> Interfaces 原句）。離題靠分數門檻擋；派工收尾的回合白做一次檢索是擁有者接受的代價。
> **新出現的危險型**：派工回合裡答案通道那通的檢索命中，絕不能漏進使用者看到的 payload
> ——那正是本包要消滅的形狀（「以為有依據，其實沒有」）在 Task 6 自己地盤上的長法。

**Files:**（行號 2026-08-07 偵察逐一驗過；計畫先前引的 :803/:965/:1177 都落在註解上）
- Modify: `services/csp/app/api/proxy.py` — 注入 seam 在 **:797–:807 視窗**（`_inject_memory` 呼叫 :797 之後、`captured_user_text` :829 之前）；回應出口**四個都要騎**：agent SSE **:966**、agent 非串流 **:1019**（合流回傳 :1040）、model SSE **:1178**、model 非串流 **:1214**
- Modify: `services/csp/app/services/proxy/service.py:388`（`build_default_anila_meta` 增列 `kb_state` 明帶預設）＋兩個 chat 建構點 `:638`（非串流）、`:1010`（SSE）。⚠ `service.py:279` 是 embedding 路徑，不碰
- Test: `services/csp/tests/test_institutional_kb_injection.py`（新）

**Interfaces（真實簽章，偵察已驗）:**
- Consumes: `retrieve_institutional(db, user, query, *, threshold, top_k=...)`（`institutional_kb.py:83`）——
  **async，必須 await**（seam 現場是 sync 呼叫形，別照抄 `_inject_attachments` 的形狀）；
  `user` 必填（嵌入計量歸戶）用 `caller.user`（`proxy.py:776`）；`threshold` 是必填 keyword-only，
  模組自己**不讀設定**——呼叫端用 `get_kb_threshold(db)`（**`app/models/platform_setting.py:128`**，
  key `institutional_kb.score_threshold`；⚠ 不存在任何 `services/*settings*` 模組，別寫錯 import）
- Consumes: `request.headers.get("X-ANILA-Route")`（`request` 在 :805 在 scope，Starlette 大小寫不敏感；
  現成樣板 :785–786）
- Produces: `anila_meta["kb_state"]`（`KbState` 五值字串，enum 在 `institutional_kb.py:59–64`，
  名稱與本計畫完全一致）、`anila_meta["kb_hits"]`（`KbHit`：collection_id/document_id/filename/content/score）、
  命中時同步填 `anila_meta["citations"]`（**沿用既有 drawer 契約：`{id, title, score?, snippet?}`，
  `id` 必填**——空 id 會弄壞 CitationsDrawer 不只是樣式）、partial 時 `anila_meta["kb_failed_collections"]`

**硬規則（設計 §5 ＋ Q39 重推）：**
1. `kb_state` 必須**明帶**在全部四個 chat 出口上，缺席≠`not_searched`——渲染管線一壞、
   所有答案靜默降級成「沒查過」是本專案頭號家賊。
2. 檢索觸發 ＝ header 存在（`direct`／`forced`）。**無 header 的呼叫（agent 派工、ANILALM）
   一律明帶 `not_searched` 且不做檢索**——這條就是「派工回合命中不外漏」的防線。
3. 狀態一律取 `KbResult.state`，**呼叫端不得自行重推**——`SEARCH_ERROR` 優先於 `PARTIAL_ERROR`
   的次序是模組內釘死的不變式（`institutional_kb.py:225–227`），重推等於把修掉的缺陷再蓋回來。
4. `searched_miss` 注入的系統指示必含「不得以條號格式引用」且明令以一般知識口吻作答；
   `searched_hit` 注入段落編 `[1]..[N]`，對應 `citations[N-1]`（router 提示詞既有的 `[N]` 指令因此活過來）。
5. 檢索失敗**絕不擋回答**（設計 §5：`search_error` 明示但照答）。
6. 檢索 query ＝ 使用者最新一則 user 訊息，與 `captured_user_text`（:829）同一定義——共用抽取，不複製邏輯。
7. 注入的訊息變形照 `_inject_memory`（:251，system msg prepend）樣板；trace 合流沿用
   `_merge_attachment_trace`（:447）／`_sse_with_attachment_trace`（:466）的既有機制。

- [ ] **Step 1: 寫失敗測試**

```python
def test_state_is_always_present_in_the_payload(client, ...):
    """⚠ 第四狀態絕不能靠「payload 裡沒資料」表示。
    若缺席即 not_searched,渲染管線一壞,**所有答案都會靜默降級成沒查過** ——
    這是本專案的頭號家賊。四個出口(agent SSE/agent 非串流/model SSE/model 非串流)都要斷言。"""
    payload = _chat(client, "你好", header=None).json()
    assert payload["anila_meta"]["kb_state"] == "not_searched"   # 明帶,不是缺席


def test_no_header_means_no_retrieval_at_all(client, ...):
    """Q39 的洩漏防線:無 header(派工/ANILALM)不只標 not_searched,
    retrieve_institutional 根本不得被呼叫——派工回合的命中絕不能漏進 payload。"""


def test_miss_prompt_forbids_article_style_citation(client, ...):
    """使用者信的是正文不是標記。"""
    body = _captured_upstream_body()
    assert "不得以條號格式引用" in body["messages"][0]["content"]


def test_hit_injects_numbered_passages_matching_citations(client, ...):
    """[1]..[N] 與 citations[N-1] 對齊;每筆 citation 有非空 id 與 title(drawer 契約)。"""


def test_all_four_payload_exits_carry_the_state(client, ...):
    """payload 有三個建構點、四個出口(偵察修正:不是計畫原寫的兩條),狀態必須全部騎到。
    只接兩條會讓 agent 分支與一條串流分支靜默無狀態。"""


def test_search_error_does_not_block_the_answer(client, ...):
    """設計 §5:檢索失敗明示,但照答。"""


def test_threshold_change_takes_effect_next_request(client, ...):
    """設計 §7 的驗收釘:從設定改完,檢索行為真的變了(不重啟)——
    否則門檻設定就是假控制項的第一塊磚。"""
```

- [ ] **Step 2–5**：跑失敗 → 實作 → 跑通過 → commit

```bash
git commit -m "feat(csp): five retrieval states, carried explicitly on every payload exit"
```

---

## Task 7：治理中心的標記 toggle

**Files:**（行號 2026-08-07 偵察驗過）
- Modify: `apps/csp-governance-ui/src/views/KnowledgeCollectionsView.vue` — footer 是 `:65–74`
  （`cc__foot` 開 :65 收 :74；計畫原引的 66–73 是裡面那排動作鈕：檢視器 :66、評測器 :68、
  封存/還原 :70–71、刪除 :73）
- Modify: `apps/csp-governance-ui/src/api/ingestionCollections.js` — `updateCollection` 的 JSDoc
  `@param` 漏了 `anila_searchable`（碼是通的，但文件讀起來像「不支援」——順手補上，別讓審查者誤判）
- Test: `apps/csp-governance-ui/tests/anilaSearchableToggle.test.mjs`（新）
  ⚠ **這個 app 的測試是 `node --test` + `*.test.mjs`（`package.json:9`），不是 vitest**；
  照 `tests/platformEmbedding.test.mjs` 的形式寫

**已有的地基（不要重建）：**
- `CollectionResponse` **已經帶** `classification_level`（schemas/ingestion.py:216）與
  `anila_searchable`（:219）；LIST 端點（collections.py:345）用它——前端拿得到，不用改後端
- PATCH 後端已在 Task 2 關板：`collections.py:655`，守門 `_guard_anila_searchable`（:143）；
  拒絕訊息會指一條走得通的路（Task 2 裁決過的措辭），前端**原樣呈現 detail，不要改寫**
- 客戶端 `updateCollection(collectionId, patch)` 是 passthrough；照 `archiveCollection`
  （view.vue:231–233）的「PATCH → `e.response?.data?.detail` → reload」慣用形

**硬規則：**
1. 密等非無機密時 toggle **停用但仍然顯示**，且顯示停用原因——**不是藏起來**
   （藏起來的控制項是本專案付過代價的形狀；同 Task 9 的「沒有 handler 就不要畫」是一體兩面：
   畫了就要真的能用，不能用就要說為什麼）。
2. 切換成功後 reload 清單，顯示值一律來自後端回應——**不做樂觀更新**
   （顯示值≠生效值是 Task 4 抓過的形狀）。
3. 錯誤走既有 `e.response?.data?.detail` 通道原樣呈現（後端訊息含自救路徑）。

- [ ] **Step 1: 寫失敗測試**（node --test）——**密等測試必須跑過整個 ClassificationLevel 列舉**
  （常設要求，這類缺陷出現過兩次）：只有「無機密」啟用 toggle，**其餘每一級都**停用＋顯示原因；
  另測：切換成功發出 PATCH 且 reload、後端 detail 原樣上畫面、未標記→標記與標記→未標記雙向。
- [ ] **Step 2–5**：跑失敗 → 實作 → 跑通過 → commit

```bash
git commit -m "feat(governance-ui): mark a collection ANILA-searchable, and say why when you cannot"
```

---

## Task 8：前端五狀態徽章與原文泡泡

**Files:**
- Modify: `apps/anila-shell/src/chat.jsx:665`（助理分支）、`apps/anila-shell/src/app.jsx:898`／`:1552`（meta 映射）
- Test: `apps/anila-shell/src/__tests__/kbStateBadge.test.jsx`（新，vitest）

- [ ] **Step 1: 寫失敗測試**

```jsx
it("四種狀態互相分得出來", () => {
  // ⚠ 不能只測有命中那條。
  for (const state of ["searched_hit", "searched_miss", "search_error", "partial_error"]) {
    const { container } = render(<MessageBubble msg={{ ...base, kbState: state }} />);
    expect(container.querySelector(`[data-testid="kb-state-${state}"]`)).toBeTruthy();
  }
  const plain = render(<MessageBubble msg={{ ...base, kbState: "not_searched" }} />);
  expect(plain.container.querySelector('[data-testid^="kb-state-"]')).toBeNull();
});

it("沒命中與檢索失敗是兩句不同的話", () => {
  const miss = render(<MessageBubble msg={{ ...base, kbState: "searched_miss" }} />);
  const err = render(<MessageBubble msg={{ ...base, kbState: "search_error" }} />);
  expect(miss.container.textContent).not.toBe(err.container.textContent);
});
```

- [ ] **Step 2–5**：跑失敗 → 實作（徽章照 `chat.jsx:676-691` 的 `action-agent-name` 樣板；原文泡泡用 `msg.citations` + 既有 `renderTextWithCitations`）→ 跑通過 → commit

```bash
git commit -m "feat(shell): show whether an answer is backed by a regulation, and which"
```

---

## Task 9：「改用院內規章重查」

**Files:**
- Modify: `apps/anila-shell/src/chat.jsx:884-930`（既有「重新產生」選單）
- Test: `apps/anila-shell/src/__tests__/kbRetry.test.jsx`（新）

⚠ **不必新建機制**：`chat.jsx:884` 已有 guided regenerate 選單（重試／更詳細／更簡潔／換個說法 + 自由輸入），加一個選項即可。注意 `chat.jsx:880-883` 的既有註解：**沒有 handler 就不要畫這顆按鈕**。

- [ ] **Step 1: 寫失敗測試** — 點該選項會以同一問句重送且**強制檢索**（略過 Router 判斷）
- [ ] **Step 2–5**：跑失敗 → 實作 → 跑通過 → commit

```bash
git commit -m "feat(shell): let the reader force a regulation search when the router did not"
```

---

## Task 10：文件與待辦收尾

**Files:**
- Modify: `SYSTEM-MAP.md`（§5「general 知識庫**一個**」→ 可標記多個）
- Modify: `PLAN.md`（門檻校準加入內網量測清單）
- Modify: `docs/HANDOFF-2026-08-07.md`（本包留下什麼要長期照顧）

- [ ] **Step 1: 改 SYSTEM-MAP §5**（規格變更，擁有者 08-07 裁決）
- [ ] **Step 2: PLAN 加一條**：對真 `nv-embed` 校準門檻
- [ ] **Step 3: 交接寫「留下什麼要長期照顧」**：門檻是**唯一**需要隨模型更換重新校準的數字；標記集的同模型限制在標記時擋、換模型時要重新檢視；`kb_state` 的五值是前後端契約
- [ ] **Step 4: Commit**

```bash
git commit -m "docs: the spec now allows several institutional libraries, and what needs recalibrating"
```

---

## 驗收（另派 fresh agent，不自驗）

驗收單必帶：**「Look for the shape this package exists to eliminate, in the package's own work.」**
本包要消滅的形狀：**「使用者以為答案有規章依據，其實沒有」**。

要求驗收特別攻擊：

1. 有沒有哪條路徑會讓**沒有依據的答案看起來像有依據**
2. 機密文件會不會從任何角度漏出（多庫、部分失敗、門檻為 0、`document_ids` 空集合）
3. 那個過濾有沒有**污染共用元件**（agent／ANILALM 檢索機密內容必須仍然成功）
4. 跨庫單一查詢的突變是否讓測試轉紅（RLS 靜默濾成一庫）
5. 門檻從畫面改完，檢索行為**是否真的變了**（否則它是假控制項的第一塊磚）
6. 五狀態是否**明帶**而非靠缺席
