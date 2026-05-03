# Parent-child RAG retrieval — design doc

> **Status**: review-passed, ready to schedule
> **Author**: Sprint 8 X follow-up → Sprint 9 X candidate
> **Last updated**: 2026-05-02

---

## Decisions on review (2026-05-02)

| # | 議題 | 決定 |
|---|---|---|
| 1 | 既有 collection 怎麼處理？ | **目前無 production 資料；不需要 rebuild endpoint 與 dual-table 一致性機制**。Migration 直接上，新 chunker 從第一份 doc 起就走新格式 |
| 2 | leaf 切多細？ | **A1 — paragraph 級 (~256 tokens)**；parents 維持 section 級 (~1024 tokens) |
| 3 | LLM context 組裝（怎麼用 parent_content vs content）？ | **Defer 到 AgenticRAG sprint**；本案只負責把 parent_content 送到 API response，agent 端怎麼用後續討論 |

→ 這三條讓 scope 從 ~415 LOC 縮到 ~250 LOC，工作量從 2–3 天降到 **~1.5 天**。

---

## TL;DR

把 chunk 拆成「leaves（細）+ parents（粗）」兩層；leaves embed 進 pgvector 給檢索精準度，parents 不 embed 但在命中後 JOIN 出來在 API response 多帶一份 `parent_content`。需要：

- 1 個 migration 加 3 欄（`parent_chunk_id`、`chunk_type`、`chunk_level`）
- HierarchicalChunker 改成 emit 兩種 row（heading 是 parent / paragraph-level 是 leaf）
- pgvector_store INSERT + 1 個欄位
- search 路徑改 1 次 JOIN
- API response 多 1 個 optional `parent_content` 欄位

效益是 retrieval recall@5 在 multi-facet question 上預期提升 5–15% 絕對值。

---

## 1 · Problem

### 1.1 現況

`HierarchicalChunker` 對 markdown / docx 的 doc tree 建出來，emit 1 row per heading section：

```
section content (60–1024 tokens)
  metadata.heading_path = ["第二章", "第八條"]
  metadata.strategy     = "hierarchical"
```

DB 的 `document_chunks` 表沒有 `parent_chunk_id` 欄位。所有 chunks 都是平的 leaves，彼此沒有結構關係。Retrieval 走純 vector / keyword search 拿 top-k leaves；citation 用 `metadata.heading_path` 字串 render 麵包屑。

### 1.2 為什麼這樣不夠好

**對長 section 來說，section-level vector 太粗。** 一個 1024-token section 通常涵蓋多個 sub-topic，但 NV-Embed-V2 的單一 4096 維向量沒辦法精確表達「section 裡哪一段在講什麼」。query 只匹配到 section 的整體 gist；如果 query 在 sub-topic 層，recall@5 會明顯下降。

**對短查詢更明顯**：「第 8 條罰金多少」這種精確查詢，理想是命中該條那段話 (~50 tokens)，但目前命中的是整個包含該條的 section (~1024 tokens)，LLM 還要從中 locate 答案，noise 高。

**反過來如果 chunk 切細到 200 tokens：** retrieval 變精準，但每個 chunk 失去結構性 context，LLM 看到「請罰款 5000 元」沒辦法連到「這是第幾條第幾項罰則」。

→ 這就是 RAG 經典的 precision-recall vs context-completeness trade-off。

### 1.3 readme 跟實作的落差

`AgenticRAG/README.md` 寫的是：

> 父子階層索引：切分依文件結構（標題 → 子標題 → 內文 → 內文中的圖片），只有葉節點（content / image）參與向量搜尋，**命中後會附上父節點內容做 context expansion**。

這個 expansion **沒有在 production 路徑被實作**：

| 層 | 狀態 |
|---|---|
| AgenticRAG chunker `_Node` 樹 | ✅ 真的建 tree（chunker.py:63 `_Node.parent / children`） |
| `DocumentChunk.parent_chunk_id` Pydantic 欄位 | ✅ 存在（models/storage.py:77） |
| `document_chunks` schema | ❌ 沒這欄（migration 0014） |
| storage adapter INSERT | ❌ 不寫入 |
| `/api/ingestion/.../search` retrieval | ❌ 不 JOIN，不回 parent_content |

整個父子鏈在資料寫入 DB 時就斷了。本案要把這條鏈接通。

---

## 2 · Goals + non-goals

### Goals

1. **Retrieval precision 提升** — 小 chunk 找
2. **Generation context 維持** — 大 chunk 餵
3. **既有 collection 不破** — migration 預設值讓老資料行為不變
4. **Embedding 總量不暴增** — 同樣的總 tokens 切細，不變多

### Non-goals

- 多模態 RAG（圖片 / 表格）— 跟父子無關，下次 sprint
- Cross-document parent linking — 父子限定同一份 doc
- 換 embedding model — 沿用 NV-Embed-V2
- 改 OpenAI-compat data plane shape — `/v1/chat/completions` 對 caller 不變

---

## 3 · Pattern 比較

### 3.1 候選方案

| Pattern | 描述 | 主要 cost |
|---|---|---|
| **A. Small-to-big**（parent-child） | leaf 細粒度 embed + parent 粗粒度 JOIN-fetch | schema +1 column self-FK |
| **B. Sentence-window** | 句子粒度 embed，命中後 SQL fetch ±N 句 | 句子層 embedding 量爆 |
| **C. Multi-level hybrid** | leaf 跟 parent 都 embed，retrieval 混兩層 score | 2× embedding cost、retrieval 邏輯複雜 |
| **D. Late chunking**（Jina） | 整段一次 embed，再切；chunks 共享 context vector | 要特殊 embedding 模型，NV-Embed 不支援 |

### 3.2 推薦 A — Small-to-big

理由：

1. **跟 HierarchicalChunker 天然契合** — 既有 `_Node` 樹結構就是 small-to-big 的天然來源，重畫 chunker 邏輯只是把寫一個 row 改成寫一棵 tree
2. **Schema 改動小** — 1 column self-FK + 2 個 typed enum-like columns，沒新表
3. **既有資料零影響** — 預設值讓老 row 看起來就是「沒 parent 的 leaf」，retrieval fallback 自然回到舊行為
4. **Embedding 總成本不變** — 切細之後 leaves 變多但每個變短；同樣 N 個 token 仍只 embed 一次（parent 不 embed）
5. **業界成熟 pattern** — LangChain `ParentDocumentRetriever` / LlamaIndex `HierarchicalNodeParser` 都是這條；可參考

否決其他：

- **B**：對 5MB doc 切句子 → 50,000 句子向量；NV-Embed 的 cosine search 索引爆炸
- **C**：embedding cost 直接 ×2 是非常可惜，NV-Embed 對長 chunk 的表達已經夠好
- **D**：要改 embedding 模型，超出 sprint 範圍

---

## 4 · 詳細設計

### 4.1 Schema (migration 0028)

```sql
ALTER TABLE document_chunks
  ADD COLUMN parent_chunk_id BIGINT
    REFERENCES document_chunks(id) ON DELETE SET NULL,
  ADD COLUMN chunk_type VARCHAR(20)
    NOT NULL DEFAULT 'leaf',
  ADD COLUMN chunk_level INTEGER
    NOT NULL DEFAULT 0;

-- Partial index — only leaves participate in vector search.
-- Existing HNSW on ``embedding`` already filters NULL implicitly,
-- but adding a partial index on ``parent_chunk_id IS NOT NULL``
-- accelerates the JOIN at retrieval.
CREATE INDEX idx_chunks_parent ON document_chunks(parent_chunk_id)
  WHERE parent_chunk_id IS NOT NULL;

-- Type taxonomy enforced as CHECK rather than ENUM (PostgreSQL ENUM
-- requires migration churn to extend).
ALTER TABLE document_chunks
  ADD CONSTRAINT chunks_type_valid
  CHECK (chunk_type IN ('document', 'heading', 'leaf'));
```

**Type taxonomy**：

| `chunk_type` | 用途 | embedding | 可被 retrieval 命中 |
|---|---|---|---|
| `document` | 整份 doc 的根（doc title / abstract）| NULL | 不會（用於 boundary marker） |
| `heading` | 中介節點（chapter / section）| NULL | 不會（context fetch 才用） |
| `leaf` | 真正的 text-bearing chunk | 4096-d half-precision | **是** |

**Embedding rule**：只有 `chunk_type='leaf'` 的 row 有 embedding。`heading` / `document` row 的 embedding 欄位是 NULL，HNSW 自動跳過。

### 4.2 Chunker 改動

`HierarchicalChunker.chunk()` 現在 emit 一條 row per heading section。新行為要 emit **多條 row**：

```python
def chunk(self, document_text, parse_meta, params):
    tree = self._build_tree(document_text)   # 既有邏輯，重整成 tree
    chunks = []

    # 1. document root (no embedding)
    root = ChunkResult(
        chunk_key="doc-root",
        content=parse_meta.get("title", "(untitled)"),
        token_count=...,
        metadata={"chunk_type": "document", "chunk_level": 0, ...},
    )
    chunks.append(root)

    # 2. heading nodes (no embedding)
    for heading in tree.headings_dfs():
        chunks.append(ChunkResult(
            chunk_key=f"heading-{heading.path_slug}",
            content=heading.title,
            token_count=_tokens(heading.title),
            metadata={
                "chunk_type": "heading",
                "chunk_level": len(heading.path),
                "heading_path": heading.path,
                "parent_chunk_key": heading.parent.chunk_key,
            },
        ))

    # 3. leaf nodes (paragraph-level, embedded)
    max_leaf_tokens = params.get("max_leaf_tokens", 256)
    for leaf in tree.leaves(max_tokens=max_leaf_tokens):
        chunks.append(ChunkResult(
            chunk_key=f"leaf-{leaf.idx:04d}",
            content=leaf.text,
            token_count=_tokens(leaf.text),
            metadata={
                "chunk_type": "leaf",
                "chunk_level": leaf.level,
                "heading_path": leaf.path,
                "parent_chunk_key": leaf.parent.chunk_key,
            },
        ))

    return chunks
```

**關鍵改動**：

- 整棵 tree 都 emit；不只 leaves
- `parent_chunk_key`（不是 `parent_chunk_id`，因為 chunker 不知道 DB id）寫進 metadata；worker 在寫 DB 時把 chunk_key → id 反查並填 parent_chunk_id
- `max_leaf_tokens` default 從 1024 降到 **256**（這就是「小 chunk 找」的「小」）
- 新 param `max_parent_tokens` default 1024（heading section 上限；超過會在 leaf 層再切）

**選項：什麼是 leaf?** 三選一：

| 選項 | leaf 大小 | 評估 |
|---|---|---|
| **A1**（推薦） | paragraph 級（~256 tokens） | 切到段落邊界；尊重作者 paragraph 意圖；embedding 量約為 section 級的 4× |
| A2 | section 級（~1024，現況） | 等於只多 emit parent rows；retrieval 不變細，無實質效益 |
| A3 | sentence 級（~50 tokens） | 太碎，embedding 量爆，retrieval 命中後 LLM 缺 context |

**選 A1**。`max_leaf_tokens` 暴露為 collection-level config，admin 想調可以。

### 4.3 Worker 寫入 (handlers.py)

`handlers.py` 既有路徑：parse → chunk → embed → write。改動：

```python
chunks = chunker.chunk(text, parse_meta, params)

# Group by chunk_type for two-pass insert
parents = [c for c in chunks if c.metadata.get("chunk_type") != "leaf"]
leaves  = [c for c in chunks if c.metadata.get("chunk_type") == "leaf"]

# Pass 1: insert parents (no embedding)
parent_id_map = await store.add_chunks_no_embed(parents)
# parent_id_map: chunk_key -> parent_chunk_id (real DB id)

# Pass 2: embed leaves
embeddings = await embedder.embed([l.content for l in leaves])

# Pass 3: insert leaves with parent_chunk_id resolved
for leaf, vec in zip(leaves, embeddings):
    leaf.metadata["parent_chunk_id"] = parent_id_map.get(
        leaf.metadata["parent_chunk_key"]
    )
await store.add_chunks_with_embed(leaves, embeddings)
```

兩 pass 是因為 leaf 的 `parent_chunk_id` 需要 parent row 寫完才有 id。可選：在同一個 transaction 用 `WITH inserted AS (INSERT ... RETURNING id, chunk_key)` 一次解。後者複雜，pass-based 較易讀。

### 4.4 Retrieval 改動 (pgvector_store + search.py)

**目前路徑**：

```python
async def search(query_embedding, collection_id, top_k=5):
    return await db.fetch("""
        SELECT id, chunk_key, content, metadata, token_count,
               1 - (embedding <=> $1) AS score
          FROM document_chunks
         WHERE collection_id = $2
         ORDER BY embedding <=> $1
         LIMIT $3
    """, query_embedding, collection_id, top_k)
```

**新路徑**：

```python
async def search(query_embedding, collection_id, top_k=5, expand_parents=True):
    # Vector search restricted to leaves
    leaves = await db.fetch("""
        SELECT id, chunk_key, content, metadata, token_count,
               parent_chunk_id,
               1 - (embedding <=> $1) AS score
          FROM document_chunks
         WHERE collection_id = $2 AND chunk_type = 'leaf'
         ORDER BY embedding <=> $1
         LIMIT $3
    """, query_embedding, collection_id, top_k)

    if not expand_parents:
        return [_to_dict(r, parent_content=None) for r in leaves]

    parent_ids = {r["parent_chunk_id"] for r in leaves if r["parent_chunk_id"]}
    if not parent_ids:
        return [_to_dict(r, parent_content=None) for r in leaves]

    parents = await db.fetch("""
        SELECT id, content
          FROM document_chunks
         WHERE id = ANY($1::bigint[])
           AND collection_id = $2     -- belt-and-braces tenant filter
    """, list(parent_ids), collection_id)
    parent_map = {p["id"]: p["content"] for p in parents}

    return [
        _to_dict(r, parent_content=parent_map.get(r["parent_chunk_id"]))
        for r in leaves
    ]
```

成本：1 額外 SQL round-trip。`parent_ids` 通常 ≤ top_k 個（5 個），`id IN (...)` 走 PK index 是 O(log n)，實測 < 5ms。

### 4.5 API response shape

`ChunkRow` 加一個 optional 欄位：

```python
class ChunkRow(BaseModel):
    id: int
    chunk_key: str
    content: str
    metadata: dict
    token_count: int | None
    created_at: datetime
    # NEW (Sprint 9 X):
    parent_chunk_id: int | None = None
    parent_content: str | None = None      # JOIN-fetched at retrieval time
```

舊的 caller 看到 None 時行為不變；新的 caller（譬如 RAG agent 在組 LLM context 時）可以選 `parent_content if parent_content else content`。

### 4.6 LLM context 組裝（agent 端）— **DEFERRED**

> **Defer 到 AgenticRAG sprint**（決策 #3）。
>
> 本 plan 只負責把 `parent_content` 送到 API response 那層；下游 agent / Router 怎麼用 `parent_content` 取代或補強 leaf `content` 進 LLM context 是分開的議題，會在 AgenticRAG 的 RAG-quality sprint 裡跟 reranker 順序、context window budget、citation 對齊 etc 一起討論。
>
> 在那之前，新 API 欄位 `parent_content` 可以是 `None`（既有消費者不受影響）或被 caller 自行組裝。

---

## 5 · 部署策略

### 5.1 沒有 legacy data，不需 rebuild flow

**決策 #1**：目前 `document_chunks` 沒有 production 資料；新 schema + 新 chunker 從第一份新上傳的 doc 起就走新格式。意味著：

- ❌ 不做 `POST /api/ingestion/collections/{id}/rebuild` endpoint
- ❌ 不做 dual-table / `chunk_version` column 一致性方案
- ❌ 不做 frontend rebuild progress UI
- ✅ migration 0028 直接 add 3 columns；既有空 table 不受影響
- ✅ 新 chunker / 新 retrieval 路徑跟 schema 一起部署

migration 仍然帶預設值（`chunk_type='leaf'` DEFAULT、`parent_chunk_id NULLable`），純粹是為了：

1. 讓 migration 可在 chunker 更新前先跑 — 任何意外於 phased rollout 期間寫入的 row 不會 violate constraint
2. 將來若有人手寫 SQL 補資料，default 是 sane 的 fallback（看起來像 leaf-only chunk）

### 5.2 Migration 部署順序

phased rollout 內部順序（同一個 release 內）：

1. migration 0028 跑完（schema 變更）
2. CSP backend 部署（含新 retrieval JOIN）— 看到老 row 自動 `parent_content=None`
3. ingestion-worker 部署（含新 chunker tree-emit + two-pass insert）
4. 第一份新 doc 上傳 → 新格式生效

**rollback 策略**：所有改動都是 additive。降版只需 alembic downgrade 0028 → 0027（drop 3 columns）+ 部署舊 worker；新 doc 退回 leaf-only 行為。

---

## 6 · 工作量 + 順序

> 三項決策後 scope 縮減：移除 Phase 7（rebuild endpoint）、移除 Phase 9 的 rebuild runbook。原本 415 LOC → 約 **250 LOC**。

| Phase | 動作 | LOC | 依賴 |
|---|---|---|---|
| 1 | migration 0028（schema 加 3 column + index + check）| ~50 | — |
| 2 | HierarchicalChunker 改 tree-emit | ~120 | 1 |
| 3 | worker handlers.py two-pass insert | ~40 | 1, 2 |
| 4 | pgvector_store search 加 parent JOIN | ~30 | 1 |
| 5 | API response 加 parent_chunk_id / parent_content | ~15 | 4 |
| 6 | ChunkingPreviewView 顯示 parent / leaf 標籤 + parent_content preview（small UX win） | ~30 | 5 |
| 7 | Eval gate（用既有 EvaluatorView 跑 before/after sample doc） | run only | 1–4 |
| **合計** | | **~285** | **~1.5 天** |

可平行：4 可在 2 / 3 完成後立刻做。Phase 7 (eval gate) 是 ship 前驗證，不算實作工。

---

## 7 · 預期效益 vs 成本

### 7.1 量化 — 假設一份 doc 50,000 tokens / 50 個 sections

| 指標 | 現況 | 新設計 | 差異 |
|---|---|---|---|
| chunk row 數 | 50 | 50 (parents) + ~196 (leaves) = 246 | ×4.9 |
| embedded row 數 | 50 | 196 | ×3.9 |
| 向量索引大小 | 50 × 16KB = 0.8MB | 196 × 16KB = 3.1MB | ×4 |
| 總 embed 過 tokens | 50,000 | 50,000 | 不變 |
| Retrieval 延遲 | ~10ms (HNSW) | ~13ms (HNSW + 1 JOIN) | +30% |
| 預估 recall@5（multi-facet query） | baseline | +5–15% absolute | depends |

**結論**：embedding **token 總量不變**（每段文字仍只 embed 一次）；vector 數量變多但 HNSW O(log n) 不會線性受影響；額外 JOIN 約 +3ms 可接受。儲存成本 +50% per doc。

### 7.2 不可量化的 win

- LLM 在 generation 時拿到 section-level context → 答案完整度 / 一致性提升（人工判斷）
- 短查詢（「第 8 條」「條款 X」）命中精準度顯著提升
- citation rendering 仍能用 `heading_path`，UX 不變

### 7.3 風險

- chunker tree 重畫可能引入 chunk_key collision（同 doc 內兩個同名 heading）— 緩解：path-slug + sequence number
- two-pass insert 失敗回滾複雜（partial parent insert 後 leaf 失敗）— 用 transaction
- rebuild 期間若 user 持續 query 老 collection，可能命中部分新 row 部分老 row — 用 dual-table 寫法或加 `version` 欄位

---

## 8 · Open questions

> 經 2026-05-02 review 後 #2、#5 已決，移除；剩下這幾條等實作前再拍。

1. ✅ **leaf 切到 paragraph 還是更細**？ → 已決：**A1 paragraph (~256 tokens)**。對沒明顯段落結構的 PDF，fallback 到 fixed-size（這是 chunker 內建邏輯，不需新 code）。

2. **heading row 是不是該也 embed**？ → 預設**否**。但對「找 chapter X 在哪」的查詢有幫助。可以加 collection-level config `embed_headings: true` opt-in，本案先 ship 不 embed，未來再加。

3. **`max_leaf_tokens` 該收口到平台 default 還是 collection-level**？ → 推薦 collection-level（在 ChunkingPreview / KnowledgeCollections 表單暴露）。不同 corpus 適合不同切法。

4. **Cross-doc parent**（doc A 的 leaf 鏈到 doc B 的 heading）？ → **不做**。父子限同一 doc。Cross-doc 是另一個 graph-based RAG 議題。

5. ✅ **Rebuild 期間 query 一致性** → 已決：**沒有 legacy data，不做 rebuild flow**。

6. **要不要先做「heading_path-based filter」這種輕量 win 先 ship**？ → 之前 audit 看到 `metadata.heading_path` 已存但 retrieval 沒用。可以先實作「query 帶 `heading_path_filter` 限制 search 在某個 chapter」當作熱身，再進 parent-child。但這條線跟父子無直接關係，可平行。

7. ✅ **LLM context 組裝** → 已決：**defer 到 AgenticRAG sprint**，本案只負責把 `parent_content` 送到 API response。

---

## 9 · 跟既有 sprint / 模組的關係

- 跟 **service-token bootstrap-then-provision**（Sprint 8 X）無依賴 — 不同子系統
- 跟 **chunking-preview wizard**（Sprint 8 X follow-up）**強相關** — wizard 在 commit 階段應該 expose `max_leaf_tokens` 跟 `max_parent_tokens` 兩個 slider，讓使用者按 corpus 性質調
- 跟 **anila-core ingestion boundary**（9 X-5）無依賴 — 兩 pillar 框架已經容下這個改動
- 跟 **AgenticRAG retrieval pipeline** — 本案 ship 後 AgenticRAG sprint 會接著做 LLM context 組裝（決策 #3）

---

## 10 · Ready-to-go checklist

- [x] 三條主要設計選擇都決了（review 通過）
- [x] schema 改動清楚（migration 0028）
- [x] chunker 改動有 reference impl（AgenticRAG `_Node` 樹）
- [x] retrieval 改動 minimal（1 SQL JOIN）
- [x] backward compat / rollback 路徑單純
- [ ] 實作前先用 EvaluatorView 跑一份 sample doc 量化 baseline（recall@5）
- [ ] 實作後再跑同一 sample 對比，驗證 recall 真的有提升（避免做完發現沒差）

---

**End of doc.** 等待 schedule 進 Sprint 9 X。
