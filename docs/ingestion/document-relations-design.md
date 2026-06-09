# Document Relations — 跨文件關聯檢索設計（Phase 1: 文件級 + regex 引用抽取）

> **Status**: design spec v2 — codex review (2026-06-09, AGREE WITH CHANGES) 已吸收，awaiting sign-off
> **Scope**: ANILA Ingestion / RAG
> **Read alongside**: [`ingestion-platform-design.md`](./ingestion-platform-design.md)、[`parent-child-rag-design.md`](./parent-child-rag-design.md)
> **附錄**：§14 列出 codex 10 點 finding 與本版如何回應。

---

## 1. 問題與動機

現行 RAG 是 **collection 內的向量檢索**：`document_chunks.embedding` 做 pgvector cosine top-k（`api/ingestion/search.py`），加上**文件內**父子（`hierarchical` / `markdown-aware` chunker 依標題階層產生 `chunk_key` + `document_chunks.parent_chunk_id`，檢索命中時 JOIN 帶出 `parent_content`）。

資料模型只有兩種關係：`Collection ──< Document ──< Chunk(chunk.parent_chunk_id 指向同一份文件的 chunk)`。

**缺口**：文件「之間」沒有任何關聯。對「一堆互相牽連的規章/作業規定/SOP」(母法 + 補充 + 修正 + 廢止 + 交叉引用)，只能靠 embedding 碰巧相似把相關條文一起撈到，**無法保證「撈到母法第 X 條時把對應補充/修正條文一併帶出」**。企業/機關的法遵與作業規範部署正是此場景，且引用關係多以明確字樣寫明。

## 2. 目標 / 非目標

### Phase 1 目標
- 建立**共用關聯底座**（一張 edge 表），檢索能沿關聯展開。
- **文件級**關聯（document ↔ document）。
- **regex 自動抽取**規章引用（條號/規章名），source=`rule`；支援**手動** source=`manual`。
- 檢索 **1-hop 展開**：向量命中後把關聯文件的代表片段一併帶回。
- 引用目標**尚未上傳**時可先存（`dst_document_id` NULL + `target_ref`），日後回填。

### 非目標（後續）
- **Phase 1.5 — chunk 級**：`dst_chunk_id` 精準到「條對條」。schema 預留，Phase 1 不抽不查 chunk 級。
- **Phase 2 — GraphRAG（B）**：LLM 抽實體/關係當**第二個 edge 生產者**（source=`llm` + confidence），複用同一 `document_relations` 表與展開邏輯。
- Phase 1 不做語意自動推斷（只抓寫明的引用）。

### A→B 共存原則
A（rule/manual edge）與 B（llm edge）都寫**同一張 `document_relations` 表**，以 `source` 區分、`confidence` 分層信任、**`source` 納入 UNIQUE key**（見 §3，讓三來源各保留各自 row + provenance，不互相覆蓋）。檢索「沿 edge 展開」只寫一次。A 先打底座、B 之後加抽取器疊上。

---

## 3. 資料模型

### 3.1 `ingestion_documents` 補強（解析目標所需）— **codex #3**
文件目前只存 `filename` + sha256，**沒有規章正式名稱**；機關/企業 PDF 常以日期字號命名（`○○規範…112年修正版_20230615.pdf`），用 filename 比對引用目標會失敗。新增：
| 欄位 | 型別 | 說明 |
|------|------|------|
| `title` | varchar(500) NULL | 解析/上傳時取得的規章顯示名稱（如「公司獎懲辦法」） |
| `normalized_title` | varchar(500) NULL, index | 正規化後的比對鍵（全半形統一、去書名號/空白、繁簡正規化） |

- `title` 來源優先序：上傳時帶入 > 解析文件內容首個標題 > filename 去副檔名（fallback）。
- 另加 **`document_aliases`**(輕量,可選)供手動補別名:`(id, collection_id, document_id FK, normalized_alias, created_by)`,給「同一規章多種稱呼」解析。Phase 1 可先只用 `normalized_title`,別名表列為需要時啟用。

### 3.2 `document_relations`(新表)
| 欄位 | 型別 | 說明 |
|------|------|------|
| `id` | PK int | |
| `collection_id` | int NOT NULL | RLS scope + 複合 FK 一員 |
| `src_document_id` | int NOT NULL | 引用方 |
| `dst_document_id` | int NULL | 被引方;未解析為 NULL |
| `dst_chunk_id` | int NULL | **Phase 1.5 預留**;Phase 1 恆 NULL |
| `target_ref` | varchar(500) NOT NULL | 原始引用字串(normalized 形式;display 形式存 evidence) |
| `relation_type` | varchar(20) NOT NULL | `based_on`/`amends`/`supersedes`/`cites`/`supplements`/`relates` |
| `confidence` | float NOT NULL default 1.0 | rule/manual=1.0;llm=模型信心 |
| `source` | varchar(10) NOT NULL | `rule`/`manual`/`llm` |
| `extractor_run_id` | varchar(40) NULL | rule 抽取批次 id(對帳/清理用,見 §6)|
| `evidence` | text NULL | 命中原句(display 形式),長度上限見 §10 |
| `created_at` | timestamptz | |
| `created_by_user_id` | int NULL | manual 由誰建 |

**完整性約束（DB 層強制,不靠應用層）— codex #1**
- 先給 `ingestion_documents` 加 `UNIQUE(collection_id, id)`(id 本就唯一,此為複合 FK 目標)。
- `document_relations` 複合 FK:
  - `(collection_id, src_document_id)` → `ingestion_documents(collection_id, id)` ON DELETE CASCADE
  - `(collection_id, dst_document_id)` → `ingestion_documents(collection_id, id)` ON DELETE SET NULL
  - (MATCH SIMPLE:`dst_document_id` 為 NULL 時 FK 不觸發 → 容許未解析;一旦填值即強制與 src **同 collection**。)
  - Phase 1.5 的 `dst_chunk_id` 同理用 `(collection_id, dst_chunk_id)` 複合 FK 對 `document_chunks`。
- **UNIQUE key 含 source — codex #9**:`UNIQUE(collection_id, src_document_id, target_ref, relation_type, source)` → rule/manual/llm 對同一目標各保留一 row(各自 confidence/provenance),不被覆蓋。

**設計取捨**:`dst_document_id` 可 NULL + `target_ref` 保留(引用不在語料庫先存後回填);方向 `src --relation_type--> dst`。

---

## 4. Migration(新 alembic 版本)

1. `ALTER TABLE ingestion_documents ADD COLUMN title / normalized_title`(+ index on normalized_title)+ `UNIQUE(collection_id, id)`。
2. (可選)`create_table("document_aliases", ...)` + RLS。
3. `create_table("document_relations", ...)` + 上述複合 FK + UNIQUE(含 source)+ 索引 `(collection_id, src_document_id)`、`(collection_id, dst_document_id)`、`(collection_id, target_ref)`。
4. **RLS — 完全照 `0037_ingestion_images_rls.py` pattern(codex #2)**:
   ```sql
   ALTER TABLE document_relations ENABLE ROW LEVEL SECURITY;
   ALTER TABLE document_relations FORCE ROW LEVEL SECURITY;   -- owner 也擋
   CREATE POLICY document_relations_isolation ON document_relations
     USING (collection_id = NULLIF(current_setting('anila.collection_id', true), '')::int)
     WITH CHECK (collection_id = NULLIF(current_setting('anila.collection_id', true), '')::int);
   ```
   (`NULLIF(..., true)`:GUC 未設回 NULL → 0 rows 而非報錯;`document_aliases` 同樣套用。)
5. `downgrade()` drop tables/policies/indexes + revert ingestion_documents 欄位。
6. guarded(inspector 檢查)以相容測試 `metadata.create_all`。

---

## 5. Ingest 階段:regex 引用抽取(獨立 `citation_extractor` 模組)— codex #5/#6/#8

**位置**：獨立模組 `anila_core.ingestion.citation_extractor`（**不內嵌在 chunker**），由 ingestion-worker 在文件解析出純文字後呼叫。可單測、Phase 2 LLM 抽取器並存同一介面。

**中文數字正規化**：先把條號數字轉成 int —— 支援 `〇/零/一…十/百/兩/廿/卅`、阿拉伯數字、`第十條之一`(附屬條)、`第三條至第八條`(範圍 → 展開或記成 range)、`項/款/目`多層；繁簡轉換(opencc s2t,如環境有)後比對。

**抽取流程(先抽 span 再分類 — codex #6)**：
1. 用條號/規章名 pattern 找出所有 citation **span**（規章名 + 條/項/款）。
2. 對每個 span 看其上下文動詞分類 relation_type：`依/依據/依照/按/根據/訂定` → `based_on`；`修正` → `amends`；`廢止/停止適用` → `supersedes`；`準用/參照/參見/詳見` → `cites`；`補充/增訂` → `supplements`；無動詞 → `relates`。
3. **一句可產生多 edge**(`依據…修正…`)；歧義（`修正…第X條` 同時是依據與目標）以優先權規則 + test case 明確記錄預期。
4. 每 span 產 (relation_type, target_ref=normalized 規章名[+條號], evidence=display 原句)。

**ReDoS 防護(codex #8)**:所有 regex **module 級 `re.compile`**;掃描前限制全文長度(或 sliding window 分段掃,單段上限);`evidence` 截斷(如 ≤500 字);加異常 CJK 輸入的 fuzz test。

**抽取產物** → 進 §6 解析 + upsert(source=`rule`、confidence=1.0、`extractor_run_id`=本次批次)。

## 6. 目標解析 + 對帳(order-independent)— codex #3/#4

每份文件 ingest(或 re-extract)完成抽取後,**單一 transaction** 內：

1. **對帳(codex #4)**:先 `DELETE FROM document_relations WHERE collection_id=? AND src_document_id=? AND source='rule'`(清掉本文件上一輪 rule edge);**`source='manual'` 一律不動**。再 INSERT 本輪結果。→ 重抽乾淨、不殘留、不爆量;manual 隔離。
2. **出向解析**：本文件 `target_ref` 的規章名 → 在**同 collection** 比對 `normalized_title`(+ aliases);命中回填 `dst_document_id`,否則 NULL。
3. **入向回填**：掃同 collection 內 `dst_document_id IS NULL` 的既有 rule/manual edge,其 `target_ref` 規章名比到剛上傳這份 → 回填 `dst_document_id`。
4. **多筆命中**：記為 ambiguous（不靜默選一）—— Phase 1 留 dst NULL + 在 evidence/log 標記候選,交由手動或 alias 釐清。

- **觸發**:正常 ingest(含內容變更 sha 不同時重跑);另給 `POST /api/ingestion/collections/{id}/relations:reresolve`(owner/admin)整 collection 重掃 + 重抽。
- **併發(codex #4)**:同 collection 多文件並行 ingest 時,步驟 1-3 的 transaction + UNIQUE(含 source)讓 upsert 冪等;入向回填用 `WHERE dst IS NULL` 條件式 update,避免互蓋。
- 比對策略 Phase 1:normalized exact / contains;模糊比對(編輯距離)列後續。

---

## 7. 檢索展開(`api/ingestion/search.py` + pgvector store)— codex #7

**新增 store 能力(codex #7)**:`pgvector_store.similarity_search_per_document(collection_id, doc_ids, query_embedding, k=1)` —— SQL `WHERE document_id = ANY(:doc_ids)` 配 `RANK() OVER (PARTITION BY document_id ORDER BY embedding <=> query)` 取每份文件 top-k,**一次批次查詢**。不可用「post-filter 主 top-k」(會 N 次全掃 + 目標 chunk 不在 top-k 而漏失)。

**展開流程**：
1. 向量 `similarity_search` 取主 top-k → 收集命中 `document_id` 集合 `D`。
2. 查 `document_relations`:`src ∈ D` ∪ `dst ∈ D`,限本 collection、`relation_type ∈ 允許集`、`confidence ≥ 門檻`、**1-hop**。
3. 對關聯到的文件集合,呼叫 `similarity_search_per_document(..., k=1)` 批次取代表片段(Phase 1.5 改用 dst_chunk_id 精準條文)。
4. Response 加 `related`,標 `relation_type`/`target_ref`/`source`/來源文件,供 RAG agent 放進 context。**related 為加分附加,不取代主 top-k**。

**Request/Response schema(明列 — codex #10)**：
- `SearchRequest` 加(皆有預設、向後相容):`expand_relations: bool=False`、`relation_types: list[str]|None=None`、`max_related: int=5`、`min_relation_confidence: float=0.0`。
- `SearchResponse` 加 `related: list[RelatedHit]`(`RelatedHit`: document_id/title、chunk content+score、relation_type、target_ref、source、direction)。
- **Phase 1 只支援 request-level 開關**;collection 級預設欄位留 Phase 1.5(本版不加 `IngestionCollection` 欄位)。

---

## 8. API（`api/ingestion/relations.py`,新 router）

| 方法 | 路徑 | 權限 | 說明 |
|------|------|------|------|
| GET | `/api/ingestion/collections/{id}/relations` | collection 存取者 | 列(含未解析 target_ref / ambiguous 標記) |
| POST | `/api/ingestion/collections/{id}/relations` | owner/admin | 手動加(source=manual) |
| DELETE | `/api/ingestion/relations/{rel_id}` | owner/admin | 刪(僅 manual;rule 由 re-extract 管) |
| POST | `/api/ingestion/collections/{id}/relations:reresolve` | owner/admin | 重掃 + 重抽 |

ACL 沿用 `_require_collection_access`;mutation 走 CSRF(cookie)/Bearer(SDK)+ 寫 audit_log(`document_relation.create/delete/reresolve`)。

## 9. 前端(Phase 1 最小)
`CollectionDetailView` 加「關聯」分頁:表列(src→type→dst/target_ref、未解析標紅、ambiguous 標記、source、evidence)+ 手動新增/刪除 + 「重新解析」;唯讀者只看。

## 10. 安全 / RLS
- RLS 照 §4(ENABLE+FORCE+NULLIF),跨租戶不可見/改,table owner 也擋。
- 同 collection 完整性由**複合 FK**(§3)DB 層保證,非僅 RLS/應用層。
- **ReDoS**:regex 預編譯 + 限掃描長度/sliding window + evidence 截斷 + fuzz test(codex #8)。抽取純 pattern match,無 eval/外呼。
- 手動 CRUD owner/admin + CSRF/Bearer + audit。

## 11. 後續階段
- **Phase 1.5(chunk 級)**:抽取連條號解析到 `dst_chunk_id`(複合 FK 對 document_chunks),展開帶精準條文;collection 級展開預設欄位。
- **Phase 2(GraphRAG / B)**:新增 `source=llm` 抽取器(LLM 抽實體/關係,寫同表,帶 confidence,UNIQUE 含 source 故與 rule/manual 共存);選擇性加實體節點表 + 多跳 graph 檢索 + 環路控制。檢索層「沿 edge 展開」複用,放開 hop 數 + 信心加權;若需「合併視圖」可加 canonical-edge view(over rows by collection/src/target/type)。
- **Phase 2.5(embedding 強化 / C)**:已實作 **(b) 主題相似邊**(`source='similarity'`、`relation_type='relates'`、文件級 centroid 餘弦的 per-doc top-K 最近鄰)。**Backlog(feature-work,尚未做)**:
  - **(a) embedding 模糊名稱解析**:rule 邊的 `target_ref` 名稱對不上 title 時,用語意相似補配(優先度低 —— LLM 已直接挑 doc_id 繞過名稱配對)。
  - **(c) 大語料庫 LLM 候選預篩**:collection 文件數 > `relation_llm_max_candidates`(預設 200)時,先用 embedding 取 top-N 最相關 sibling 餵 LLM,解掉現在「超過上限就跳過 LLM」的硬限。

## 12. 測試計畫
- **citation_extractor**:各句型(依據/修正/廢止/準用/補充)→ relation_type+target_ref+evidence;中文數字(`〇/兩/廿/百`)、`第十條之一`、`第三條至第八條`、項/款/目、繁簡;歧義句多 edge;**ReDoS fuzz**(大量重複空白/符號、超長)。
- **解析/對帳**:出向命中/未命中;入向回填(補充先到、母法後到);re-extract 刪舊 rule 留 manual;ambiguous 多命中;併發 ingest 冪等。
- **檢索展開**:`similarity_search_per_document` 正確取每文件 top-1;出向/入向關聯帶出;`max_related`/`relation_types`/confidence 門檻;related 不擠掉主 top-k;無關聯時 related 空。
- **完整性/RLS**:複合 FK 擋跨 collection 的 src/dst;RLS 跨 collection 不可見/改;GUC 未設回 0 rows。
- **API/ACL**:owner/admin 才能 CRUD;唯讀者 403;audit 寫入。

## 13. 待決 / 風險
1. **title 來源**:解析抽 title 的可靠度(PDF 首標題 vs 字號命名)→ 允許上傳帶入 + 別名表兜底。
2. **解析率**:normalized_title exact/contains 命中率;命名不一致 → 別名/模糊比對(後續)。
3. **覆蓋率**:Phase 1 只抓寫明引用;隱性關聯靠 Phase 2。
4. **效能**:展開 = 命中後 +1 關聯查詢 + 1 次批次 per-document 向量查;`max_related` 控成本。

---

## 14. Codex review 回應對照(2026-06-09, AGREE WITH CHANGES)

| # | Codex finding | 本版回應 |
|---|---------------|----------|
| 1 HIGH | 同 collection 完整性只靠 RLS/應用層 | §3.2 改**複合 FK `(collection_id, document_id)`** + `ingestion_documents` 加 `UNIQUE(collection_id,id)` |
| 2 HIGH | RLS 寫法不符現有 pattern | §4 改 `ENABLE+FORCE ROW LEVEL SECURITY` + `NULLIF(current_setting('anila.collection_id',true),'')::int`(照 0037) |
| 3 HIGH | 解析靠 filename,無正式 title | §3.1 加 `title`/`normalized_title`(+別名表)+ 解析時抽 title |
| 4 HIGH | search 用 post-filter top-k 會漏 | §7 新增 `similarity_search_per_document`(`WHERE document_id=ANY` + `RANK() OVER PARTITION`) |
| 5 MED | regex 覆蓋不足 | §5 獨立 `citation_extractor` + 中文數字正規化 + 之一/範圍/項款目/繁簡 + fixtures |
| 6 MED | 動詞歧義 | §5 先抽 span 再分類、一句多 edge、優先權 + test |
| 7 MED(本表 #4 重點 HIGH) | per-document 向量查詢能力缺 | §7（同上） |
| 8 MED | ReDoS | §5/§10 預編譯 + 限長/sliding window + evidence 截斷 + fuzz |
| 9 MED | UNIQUE 無 source,llm/rule 會合併 | §3.2 **UNIQUE 含 `source`**(三來源各 row);canonical view 留 Phase 2 |
| 10 LOW | request/response schema 未明列 | §7 明列 SearchRequest/Response 變更 + Phase 1 僅 request-level |
| 4(對帳) MED | re-extract 不刪舊 rule edge | §6 同 transaction 先刪 src 的 rule edge 再插、manual 隔離、`extractor_run_id` |
