# RFC：anila-memory 統一記憶層（路線 3）

**狀態**：草案，等待簽核
**作者**：2026-05-04
**範圍**：把目前並存的兩套記憶實作（CSP 平台使用者記憶 + anila-core agent memdir）整合，採**路線 3**：anila-core 擁有語意，CSP 物理託管儲存與 HTTP 端點。約 4–5 工作天，分階段執行。

---

## 1. 為什麼要做

目前有**兩套**形狀相似但歸屬與儲存不同的記憶子系統：

| 子系統 | 儲存 | 租戶 | 使用端 | 管理 UI | Schema |
|--------|------|------|-------|--------|--------|
| **CSP 平台使用者記憶**（P1/P2/P3 已上線） | Postgres（`user_facts` + `conversation_memory_chunks`） | per-user | CSP `/v1/chat/completions` proxy hooks（任何走 CSP 的聊天都吃到） | ANILA_UI「設定 → 記憶」tab | 結構化 `key/value` + halfvec(4000) RAG |
| **anila-core agent memdir** | 檔案系統（`MEMORY.md` + `*.md` 含 YAML frontmatter） | per-agent process | 個別 agent SDK runtime | 無（admin 直接看檔案） | 非結構化 markdown + relevance scoring |

> 命名澄清：CSP = `myCSPPlatform` 後端；ANILA_UI = 主聊天前端；ANILALM 是知識庫管理 SPA（其對話也走 CSP，所以自動受惠記憶，但本身不持有記憶邏輯）。

並存代價：兩套儲存要維運、兩套萃取要演進、agent 讀不到使用者事實、稽核 / GDPR 重做兩遍。

**為什麼選路線 3 而非完全分離服務（原 B3）或全搬 anila-core（路線 2）**：
- 路線 1（維持現狀並重新定位）— 定位錯誤，CSP 不該擁有 agent state。
- 路線 2（完全搬到 anila-core）— 直選 agent / 直選 LLM 的聊天會繞過 anila-core router，**沒有記憶覆蓋**；除非強制所有聊天經過 anila-core single front door（路線 2 + 修補 A），但會把 anila-core 變成單點故障。
- **路線 3（本案）** — anila-core 擁有定義（schema、萃取規則、retention 政策、embedding 契約），CSP 物理託管（DB、API endpoint、auth）。記憶覆蓋仍在 CSP 層保證 100%，邏輯歸屬正確。

**非目標**：不取代 in-process `Session` protocol（短期單會話工作記憶，是另一個範疇）。

---

## 2. 架構

### 2.1 職責分配

```
┌─────────────────── anila-core (Python lib) ───────────────────┐
│                                                                │
│  anila_core.memory.user/                                       │
│   ├─ models.py        ─ UserFact / MemoryChunk dataclasses     │
│   ├─ extraction.py    ─ 萃取 prompt + parser                    │
│   ├─ embedding.py     ─ embedding 契約（dim、norm、模型名）       │
│   ├─ retention.py     ─ TTL / 上限政策（v1 留空）                  │
│   └─ adapter.py       ─ MemoryAdapter Protocol（CRUD 介面）       │
│                                                                │
│  anila_core.memory.agent/   (現有 memdir，後續 phase 4 整合)      │
│                                                                │
└────────────────────┬───────────────────────────────────────────┘
                     │ import as library
                     ↓
┌──────────────── CSP (myCSPPlatform 後端) ──────────────────────┐
│                                                                │
│  app.services.memory_service                                   │
│   └─ PostgresMemoryAdapter(implements adapter.MemoryAdapter)   │
│       ├─ 真正的 SQLAlchemy session、httpx embed client          │
│       └─ 沿用 anila-core 定義的 schema、prompt、契約              │
│                                                                │
│  app.api.memory          ─ /api/memory/* REST 端點              │
│  app.api.proxy           ─ /v1/chat/completions hooks          │
│  migrations/             ─ alembic 0030 / 0031                 │
│                                                                │
└────────────────────┬───────────────────────────────────────────┘
                     │ HTTP /api/memory/*
                     ↓
┌──────────────── ANILA_UI (anila-ui) ───────────────────────────┐
│  「設定 → 記憶」tab — 列表 / 刪除 / 清空                            │
└────────────────────────────────────────────────────────────────┘
```

### 2.2 邊界規則

* **anila-core 不直接 import SQLAlchemy / httpx / asyncpg**。它只定義介面與純函數（萃取 prompt、parser、retention 規則、embedding 契約）。
* **CSP 不定義業務語意**。Schema 變動、萃取行為調整都在 anila-core，CSP 跟著升 dependency 版本。
* **介面契約**：`anila_core.memory.user.adapter.MemoryAdapter` Protocol — 同步/非同步雙版本，CSP 提供 PostgresMemoryAdapter，未來測試 / SDK 可提供 InMemoryMemoryAdapter。
* **跨租戶**：v1 採 per-user only（P1 已實作）。Agent memdir 整合進來後成為 per-agent 第二租戶，cross-tenant 讀寫由 anila-core 的 policy 層仲裁。

### 2.3 ANILA_UI 不變

`/api/memory/*` REST 介面不變；前端不需動。所有結構性變動都在後端 import 邊界。

---

## 3. 分階段執行

每個 phase 結束都有可驗收產出。

### Phase 1 — 抽取 schema 與 contract 到 anila-core（1 天）
* 在 `anila-core/src/anila_core/memory/user/` 建立新 package。
* 把現有 CSP `memory_service.py` 的 dataclasses（`UserFact`, `RetrievedChunk`, `MemoryReadResult`）平移過去。
* 把 `_EXTRACT_SYSTEM_PROMPT`、`parse_extraction_response` 平移過去（純函數，無依賴）。
* 定義 `MemoryAdapter` Protocol（async CRUD 介面）。
* anila-core 加單元測試（不需要 DB / httpx）。
* CSP 暫時不變動，仍用本地實作。

### Phase 2 — CSP 改用 anila-core lib（1 天）
* CSP `memory_service.py` 拆成兩塊：
  - `PostgresMemoryAdapter`（實作 anila-core Protocol，含 SQLAlchemy / httpx）
  - 高層流程（`build_memory_block`, `persist_turn`）改成呼叫 adapter。
* 萃取 prompt 從 anila-core import，本地刪除。
* P1 測試全綠；ANILA_UI「設定 → 記憶」操作仍可用。
* DB schema 不變動（無 migration）。

### Phase 3 — Agent memdir 統一進來（1.5 天）
* `anila-core/memory/agent/` 新增 `MemoryAdapter` 的「對 user 記憶的 read-only view」— agent 處理使用者請求時可以拉該使用者的 facts。
* `MemdirManager` 介面新增 `get_user_facts(user_id)` 方法，預設打 CSP `/api/memory/{user_id}/facts`（HTTP）。
* CSP 新增 service-token 認證讓 agent 可以代呼叫使用者記憶。
* 跨租戶讀取流程驗證：sub-agent 處理請求時能看到使用者 facts。

### Phase 4 — 跨租戶 policy 與稽核（0.5 天）
* anila-core 新增 `policy.allow_cross_tenant_read(caller_kind, caller_id, target_kind, target_id) → bool`。
* 預設 policy：agent 可讀「正在服務的使用者」的 facts，不能讀其他使用者；user 不能讀任何 agent。
* 跨租戶讀取寫稽核 log。

### Phase 5 — 維運打磨（0.5 天）
* `/api/memory/{user_id}/export` GDPR 整批匯出。
* `/api/memory/{user_id}/cascade-delete` 帳號刪除時連帶清空。
* per-tenant byte usage metric。

**總計**：~4.5 天。砍 Phase 4/5 → **3 天「MVP 重構版本」**。

---

## 4. 風險表

| 風險 | 機率 | 緩解 |
|------|------|-----|
| Phase 2 cutover 引入退步（萃取行為微妙改變） | 中 | Phase 1 的 anila-core lib 萃取規則跟 CSP 現況 1:1 對齊；Phase 2 結束跑 P1 全部測試 + 手動驗收一輪 |
| anila-core 變成 CSP build dependency，版本管理複雜 | 低 | 用 PEP 660 editable install + monorepo 版本綁定；CI 確保 anila-core 改動會觸發 CSP 測試 |
| 跨租戶讀取繞過 policy（Phase 3） | 中 | adapter 介面強制 caller context 參數，policy 檢查在 adapter 層而非業務層 |
| Phase 3 agent SDK 改動破壞既有 agent | 中 | `MemdirManager` 既有方法簽章不變，只新增方法；feature flag 控制是否啟用使用者記憶讀取 |
| Phase 5 cascade-delete 遺漏 chunks（FK orphan） | 高 | migration 0030 已設 `ON DELETE CASCADE`，pytest 加一個 user 刪除後 chunks 歸零的斷言 |

---

## 5. 不做的事

* **跨使用者記憶共享**。
* **記憶版本 / 時光回溯**。
* **自動修剪 / TTL**（v1 唯一保留控制是手動「清空全部」）。
* **embedding 維度彈性**（halfvec(4000) only，未來真要多 embedder 再做）。
* **獨立 anila-memory 服務容器**（原 B3 版本）— 路線 3 不需要。

---

## 6. 開工前需要拍板的決議

1. **anila-core lib 安裝方式**：editable install 還是 wheel publish？
   * 建議 **editable install**（monorepo 已採用，CSP requirements 直接指 `-e ../anila-core`）。

2. **Phase 3 agent 跨租戶讀取**：是否在 v1 啟用？
   * 建議 **啟用** — 你之前提的「agent 看得到使用者偏好才能個人化」是路線 3 主要動機之一。

3. **Phase 4 cross-tenant policy**：v1 預設多寬鬆？
   * 建議**只允許「處理該使用者請求中」的 agent 讀取該使用者 facts**；其他組合一律拒絕。

4. **anila-core memory namespace**：用 `anila_core.memory.user` 還是 `anila_core.memory.platform`？
   * 建議 **`user`**（路線 3 v1 只有 user tenant；agent 是消費端不是租戶）。

5. **既有 CSP `app.services.memory_service` 模組**：Phase 2 後是否保留？
   * 建議 **保留為薄殼**（只委派給 adapter），刪掉的話 import path 會破壞既有測試。

6. **命名**：保留 `anila-memory` 名稱在 RFC 標題？
   * 建議**改名 `anila-memory-layer-rfc.md`** — 這是「記憶層整合」不是「新服務」，避免誤會。

---

## 7. P1 / P2 / P3 不受影響

* P1（記憶寫入 / 讀取邏輯）— Phase 1-2 是 refactor，行為不變。
* P2（ANILA_UI 設定 → 記憶 tab）— `/api/memory/*` 介面不動，前端不變。
* P3（加密繼承 latch）— `is_encrypted` 是 chunk 欄位，跟 adapter 抽象正交。

---

## 8. 簽核檢核

開工 Phase 1 前每項要勾完：

- [ ] §6.1 editable install 方式確認
- [ ] §6.2 Phase 3 跨租戶讀取啟用確認
- [ ] §6.3 預設 policy 範圍確認
- [ ] §6.4 namespace 命名確認
- [ ] §6.5 舊模組保留方式確認
- [ ] §6.6 RFC 改名確認
- [ ] §3 分階段流程接受；切點（2 / 3 / 4）已理解
- [ ] §4 風險表接受
