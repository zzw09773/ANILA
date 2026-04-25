# ANILA Multi-Service Integration Plan v0.1

**Status**: Draft for review
**Date**: 2026-04-25
**Author**: ANILA 平台團隊
**Companion docs**: [`ingestion-platform-design.md`](./ingestion-platform-design.md) · [`anila-core-boundary.md`](./anila-core-boundary.md)
**Source of investigation**: `/home/aia/c1147259/project` 目錄實際 grep 結果

---

## 0. Decisions Log

### v0.3 (2026-04-25) — ISO 42001 合規 + codeserver nginx 同源 path

兩個關鍵 follow-up：

| # | 議題 | 立場 |
|---|---|---|
| 8 | **GitLab 是 ISO 42001 合規必備基礎設施，不是「nice to have」** | 內網部署嚴格遵守 ISO/IEC 42001（AI Management System 標準）：所有 agent 源碼**必須**有版本控制存放庫。GitLab 從「Platform Link 卡片之一」升格為**平台核心 service**。詳見 §7.4 |
| 9 | **codeserver 對外 = 同源 nginx path `/codeserver`** | 不獨立 cert，走 `https://<anila-host>/codeserver/*` 反向代理。詳見 §5.0.1 更新版（需要特殊 WebSocket 設定）|

對 sprint 排程的影響：
- Phase 1 task list（§8.1）多一項：**確認 GitLab 部署狀態 + 註冊到 platform_links**
- 若組裡 GitLab 還沒部署，**Phase 1 不能等 GitLab 就緒**才上線（會 block Phase 1 ~~~ 需要 GitLab 自己一個 sprint）→ 解法：Phase 1 註冊 GitLab card 但 URL 寫 `pending_deployment`，平台 UI 顯示「即將上線」，不 block 其他服務

### v0.2 (2026-04-25) — 7 個議題 review 後的修訂

第二輪討論（含 ComfyUI namespace 釐清、NotebookLM 部門級 grant、My-OpenAI-Frontend 「停用備份」而非廢除等）後的修訂：

| # | 議題 | v0.1 立場 | v0.2 修訂 |
|---|---|---|---|
| 1 | Memory file 的定位 | 不明確 | **`memory/` module 留 anila-core；`MemoryFileStore` KEEP（dev mode）；Phase 3+ 補 `PostgresMemoryStore`（prod mode）**。詳見 §1.4 與 [`anila-core-boundary.md`](./anila-core-boundary.md) §2.3 |
| 2 | ComfyUI 整合方式 | 註冊為 Model | **改為註冊為 Agent**。理由：ANILA 主介面是 chat 對話，Router 必須在 manifest 看到「會畫圖的 agent」才能自動分派；Model 介面只能透過 OpenAI SDK 顯式呼叫 model name 觸發。詳見 §4 |
| 3 | NotebookLM 權限模式 | 平台連結卡片（無 access control）| **OIDC SSO + 模式 A 嚴格白名單**。`required_roles=NULL` + `service_access_grants` table，**支援 user-level 與 department-level grant 兩種**（部門 grant 一次蓋整批）。詳見 §6 與 §7.5 |
| 4 | codeserver 部署位置 | 平台連結（外部跳轉）| **納入 ANILA monorepo `docker-compose.yml`** — 組裡沒獨立部署，要新增 service。詳見 §5 |
| 5 | n8n / gitlab / mlsteam URL | 假設值 | **實際 URL 已查證**（從 `My-OpenAI-Frontend/webui/src/pages/index.tsx` 取得）：mlsteam=`https://aiops.ai.ncsist.org.tw:4443/`、n8n=`/n8n`（nginx 內部 path）。GitLab URL 待你補。詳見 §7 |
| 6 | My-OpenAI-Frontend 處置 | 2-3 週廢除工程 + data migration | **改為「停用備份」**：現在沒人在用，不做 migration。docker compose down + 保留 image + README 加 archive notice。詳見 §3 |
| 7 | dev DB credential TTL | 24 小時 | **改為 30 天** + revoke API + 30d 自動 reminder + audit log。詳見 §10.2 |

並加入：
- **§1.4 Memory architecture 章節**（澄清 platform memory ≠ Ingestion 的關係）
- **§4 ComfyUI namespace 釐清**（ANILA 的「model」vs ComfyUI workflow 內部的「model」是兩個不相干 namespace）
- **§4.5 ComfyUI workflow preset 4 層擴展機制**（Layer 0 內建預設 / Layer 1 admin 加 preset / Layer 2 LLM 自動選 / Layer 3 dev fork agent template）
- **§7.5 Service Access Control**（`required_roles` + `service_access_grants` 雙機制 + effective access 演算法）

### v0.1 (2026-04-25) — 初版

關於組內既有服務（位於 `/home/aia/c1147259/project`）如何整合進 ANILA 平台的初版決議：

| 服務 | 決議 | 理由 |
|---|---|---|
| **My-OpenAI-Frontend** | ~~取代~~ → **停用備份** (v0.2 修訂) | 離職同仁開發、無人維護；現在沒 active 使用者 |
| **NotebookLM** | **暫保獨立** + 平台連結卡片 + 寫 future agent 化 plan | 現役 prod 服務，破壞風險高 |
| **data-quality** | **不納入** | 是 n8n workflow 的一部分 |
| **ComfyUI** | ~~註冊為 Model~~ → **註冊為 Agent** (v0.2 修訂) | Router 須能在自然對話分派 |
| **codeserver** | ~~Platform Link 卡片~~ → **納入 monorepo** (v0.2 修訂) | 組裡沒獨立部署，要新增 service |
| **n8n** | **平台連結卡片** | workflow 平台 |
| **gitlab** | **平台連結卡片** | git server |

---

## 1. 動機

### 1.1 從 LLM Gateway 升格為「多服務管理平台」

CSP 目前定位是 control + data plane，但組內現實是**還有一堆獨立服務各自跑**（NotebookLM、ComfyUI、n8n、gitlab、codeserver）。使用者每個服務都要單獨記網址、單獨登入、各自管 API key。

CSP 已有的 `AUTO_REGISTER_LINKS` 機制（範例見 `myCSPPlatform/README.md`）是「平台連結卡片」雛形。本 plan 把它擴充成完整的多服務管理平台，讓 ANILA dashboard 變成**組內所有 AI / dev 服務的統一入口**。

### 1.2 兩條主線並行

```
主線 A：取代 My-OpenAI-Frontend     ← 解決功能重疊
主線 B：把外部服務串進 dashboard   ← 解決使用者多次登入痛點
```

兩條主線**獨立進行**：A 完成不會 block B，反之亦然。

### 1.3 與其他 design doc 的關係

| 本 doc | 相關 doc | 關聯點 |
|---|---|---|
| §4 ComfyUI 註冊為 Agent | `AgenticRAG/anila-agent.yaml` template | 走 agent 註冊流程 |
| §5 codeserver DB credential | [`ingestion-platform-design.md`](./ingestion-platform-design.md) §3.3 RLS | per-dev credentials 自動 `SET LOCAL anila.agent_id` |
| §6 NotebookLM agent 化 | [`anila-core-boundary.md`](./anila-core-boundary.md) | NotebookLM 9 種 artifact 對應 agent template fork pattern |
| §1.4 Memory architecture | [`anila-core-boundary.md`](./anila-core-boundary.md) §2.3 | platform memory module 留 anila-core；MemoryFileStore 為 dev-mode impl |

### 1.4 Memory ≠ Ingestion（澄清）

設計討論中常被誤會的一個點：「ANILA 平台需要 memory 嗎？跟新做的 Ingestion Platform 有衝突嗎？」

**不衝突。兩者解的問題完全不同**：

| 面向 | Ingestion Platform | Platform Memory |
|---|---|---|
| 解的問題 | 把使用者上傳的「文件」變成可檢索的 chunks | 記住跨 session 的「對話 facts / user preferences / past decisions」 |
| 寫入時機 | Dev 主動上傳 documents（一次性 batch）| 每個 chat turn 自動 background extraction（trailing-run coalescing）|
| 資料量級 | 大 — 整本 PDF 切幾百個 chunk | 小 — 每個 user 累積幾百個 markdown memory file |
| 儲存 | pgvector 共用 cluster + agent_id 隔離 | 目前 file system (`MemoryFileStore`)；Phase 3+ 新增 PG impl |
| Schema | `document_chunks` table | `MEMORY.md` 索引 + 個別 `.md` files（4-type taxonomy: user_preference / project_convention / debugging_lesson / api_pattern）|
| 對應 anila-core | 從 anila-core **搬出**到 AgenticRAG template（見 boundary doc）| **留在 anila-core**（pure runtime 必備）|
| 程式入口 | `anila_core.ingestion.*`（搬走後）→ `agentic_rag.ingestion.*` | `anila_core.memory.*`（memdir / extract_memories / relevance_selector / consolidation）|

**生命週期對比**：

```
Ingestion 寫入：dev 一次性上傳 → IngestionService.ingest() → 寫 pgvector
Memory  寫入：每個 chat turn 結束 → background ExtractMemories worker
              → 從 conversation 萃取 facts → 寫 MemoryStore

Ingestion 讀取：agent 對話時 vector_search 工具 → top-k chunks
Memory  讀取：agent turn 開始時 RelevanceSelector → 選相關 memories 注入 context
```

**未來進階方向（v0.3 才考慮，本 plan 不做）**：類似 Ingestion Platform 的 design pattern，做一個 **central memory service**，讓 100 agent 共享 user-level preferences（例如「使用者 A 偏好簡短回覆」這個 fact 被所有 agent 共用）。屆時 `MemoryStore` Protocol 加一個 `CentralMemoryClient` impl 走 CSP `/api/memory/*`。但這要等 platform memory 真有跨 agent 共用需求時再啟動。

**本 plan 立場**：memory 不是 multi-service integration 的範疇，只在這節做澄清避免誤會。memory module 本身的演進（`PostgresMemoryStore` 補上、未來 central memory service）走獨立 design doc。

---

## 2. 服務分類

### 2.1 四種整合 Class

```
Class A: 「取代」                   — CSP 直接接手功能
  └─ My-OpenAI-Frontend

Class B: 「平台連結卡片」（純跳轉） — 使用者點卡片開新分頁
  ├─ NotebookLM
  ├─ codeserver
  ├─ n8n
  └─ gitlab

Class C: 「註冊為 Model」           — 走 model_registry，使用者透過 OpenAI SDK 呼叫
  └─ ComfyUI

Class D: 「不納入」
  └─ data-quality（屬於 n8n workflow）
```

### 2.2 每個服務的 Port / 對接資訊

| 服務 | 內部 URL | Auth 模式 | 說明 |
|---|---|---|---|
| NotebookLM | `http://localhost:3100` (FE) / `:8100` (BE) | 自帶 JWT | 9 種 artifact 生成 |
| codeserver | `http://localhost:8443`（推測，需確認）| 自帶（VS Code session）| Dev workspace |
| n8n | `http://n8n:5678` | OAuth2 capable | Workflow 平台 |
| gitlab | `http://gitlab:8080`（內網推測）| OAuth2 capable | Git server |
| ComfyUI | `http://localhost:8188` | 無 auth | 圖片生成 |

---

## 3. Class A：My-OpenAI-Frontend 處置（v0.2: 從廢除改為停用備份）

### 3.0 v0.2 修訂

> v0.1 規劃 2-3 週的「廢除工程」（含 model_registry / api_keys / token_usage / users 一次性 migration）。
>
> **v0.2 修訂為「停用備份」**：經確認**現在沒有 active 使用者打 My-OpenAI-Frontend 的 `/v1/*`**，data migration 沒必要做。改為直接停用 + 保留 image 作為災難備份。

### 3.0.1 v0.2 處置步驟（簡化版）

```
Step 1  ──  Audit
   grep 整個內網確認沒有 client 還在打 My-OpenAI-Frontend `/v1/*`
   工具：grep -rn "my-openai-frontend\|MY_OPENAI" /home /opt
   若找到，先請該 client owner 改打 CSP

Step 2  ──  Sunset
   docker compose down (My-OpenAI-Frontend 那組 service)
   不刪 image，保留作備份
   nginx 把 /v1/* 路由改回 410 Gone（防誤打）
   保留 admin UI（如果還有人想看歷史 usage 數據）

Step 3  ──  Archive notice
   在 My-OpenAI-Frontend repo README 加上：
     ⚠️ DEPRECATED — Use myCSPPlatform instead.
     This service is kept as a backup; new traffic should not be sent here.
```

**完成標準**：
- `docker ps | grep my-openai-frontend` 無 running container
- `curl <my-openai-frontend>/v1/...` 回 410 Gone
- README 有 archive notice
- ANILA dashboard 上不顯示 My-OpenAI-Frontend 卡片（或顯示但標 `[ARCHIVED]`）

**不做的事（跟 v0.1 對比）**：
- ❌ 不做 model_registry data migration
- ❌ 不做 api_keys / token_usage migration
- ❌ 不開發 audio transcription endpoint（v0.1 為了「無縫 cutover」要補的）— 改為**有人實際需要時再補進 CSP**

### 3.1 為什麼還是要記錄這段（功能重疊實證）

雖然不做廢除，但功能重疊事實要留檔，避免日後有人不知情又啟用 My-OpenAI-Frontend。

### 3.1.1 重疊範圍實證

從 `/home/aia/c1147259/project/My-OpenAI-Frontend/README.md` 與 `myCSPPlatform/README.md` 對比：

| 功能 | My-OpenAI-Frontend | myCSPPlatform | 結論 |
|---|---|---|---|
| OpenAI 相容 `/v1/*` | ✅ chat / embeddings / audio | ✅ chat / embeddings | 取代 |
| OAuth2 + JWT | ✅ | ✅（含 LDAP / OIDC）| CSP 更全面 |
| API Key management | ✅ | ✅ | 取代 |
| Usage tracking | ✅ basic | ✅ + dashboard 圖表 + CSV export | CSP 更全面 |
| Web dashboard | ✅ Next.js | ✅ Vue 3 | 取代 |
| Multi-model | ✅ load balance | ✅ model_registry + 健康檢查 + 自動註冊 | CSP 更全面 |
| Audio transcription endpoint | ✅ | ❌ | **CSP 缺，需補** |

**唯一 CSP 缺的功能**：audio transcription（whisper-style）endpoint。
- v0.1 計畫：cutover 前先補上
- **v0.2 修訂**：改為「有人實際需要時再補」（沒人用、沒急迫性）

### 3.2 ~~廢除 Migration 步驟（v0.2 已棄用）~~

> 以下保留作為**未來若改變決策**（決定真的廢除 + migrate）的執行 reference。

#### v0.1 原計畫

```
┌─ Step 1: Audit ──────────────────────────────────────────────┐
│ grep 整個內網找出所有打 My-OpenAI-Frontend `/v1/*` 的 client：│
│   - NotebookLM backend `.env` 的 LLM_API_BASE_URL            │
│   - data-quality n8n workflow 內的 LLM call URL              │
│   - 其他散落的 SDK caller / curl script                       │
│                                                              │
│ 工具：`grep -rn "my-openai-frontend\|MY_OPENAI" /home /opt`  │
└──────────────────────────────────────────────────────────────┘

┌─ Step 2: Data Migration（一次性，down-time ~ 30 min）────────┐
│ a. Models       → CSP `model_registry` table                 │
│ b. API Keys     → CSP `api_keys` table（保留 hash, 不重發）  │
│ c. Usage data   → CSP `token_usage` table（保留歷史）        │
│ d. Users        → CSP `users` table（合併重複帳號）          │
│                                                              │
│ Migration script: `scripts/migrate-from-openai-frontend.py`  │
│ Rollback: 保留 My-OpenAI-Frontend DB dump 7 天                │
└──────────────────────────────────────────────────────────────┘

┌─ Step 3: Add audio transcription to CSP ─────────────────────┐
│ 在 myCSPPlatform/backend/app/api/proxy.py 新增：             │
│   POST /v1/audio/transcriptions                              │
│ 介接到既有 vision / audio model endpoint                     │
└──────────────────────────────────────────────────────────────┘

┌─ Step 4: Client cutover ─────────────────────────────────────┐
│ 一鍵切換：                                                    │
│   - NotebookLM `.env` LLM_API_BASE_URL → CSP                 │
│   - data-quality n8n workflow 改 URL                          │
│   - 其他 client 同步                                          │
│                                                              │
│ 觀察 1 週確認無流量回打 My-OpenAI-Frontend                    │
└──────────────────────────────────────────────────────────────┘

┌─ Step 5: Sunset ─────────────────────────────────────────────┐
│ My-OpenAI-Frontend `/v1/*` 回 410 Gone（保留 admin UI 90 天  │
│ 讓使用者匯出資料）                                            │
│ 90 天後 docker compose down + 移除 image                      │
└──────────────────────────────────────────────────────────────┘
```

### 3.3 Migration script skeleton

```python
# scripts/migrate-from-openai-frontend.py
import asyncpg
import asyncio

async def migrate():
    src = await asyncpg.connect(MY_OPENAI_DB_URL)
    dst = await asyncpg.connect(CSP_DB_URL)

    # 1. Models
    src_models = await src.fetch("SELECT * FROM models WHERE active = true")
    for m in src_models:
        await dst.execute(
            "INSERT INTO model_registry (name, display_name, model_type, "
            "endpoint_url, api_version, ...) VALUES (...) "
            "ON CONFLICT (name) DO UPDATE SET ...",
            m['name'], m['display_name'], _map_type(m['type']), ...
        )

    # 2. API Keys（保留 key_hash，不重發明文）
    src_keys = await src.fetch("SELECT * FROM api_keys WHERE revoked = false")
    for k in src_keys:
        # 對應使用者：以 username 比對 CSP users 表
        user = await dst.fetchrow(
            "SELECT id FROM users WHERE username = $1",
            k['owner_username']
        )
        if not user:
            print(f"WARN: user {k['owner_username']} 不存在於 CSP，跳過 key {k['name']}")
            continue
        await dst.execute(
            "INSERT INTO api_keys (key_hash, user_id, name, allowed_models, ...) "
            "VALUES ($1, $2, $3, $4, ...)",
            k['key_hash'], user['id'], k['name'], k['allowed_models']
        )

    # 3. Usage history
    # ... bulk copy 同 schema 對應
```

### 3.4 風險與 Mitigation

| 風險 | Mitigation |
|---|---|
| Client 漏改，仍打舊 URL | Step 4 觀察 1 週流量、Step 5 改 410 Gone 暴露漏網之魚 |
| API Key hash 演算法不同 | 預先驗證兩邊都用 bcrypt（從 source 確認）；不同則統一改一邊 |
| Username 衝突 | Migration 跑前先 dry-run 列出衝突，由 admin 手動 merge |
| Audio transcription 補上的時間 | 排在 Step 2 之後、Step 4 之前；無此 endpoint 不能 cutover |

---

## 4. Class C：ComfyUI 整合 — 註冊為 Agent（v0.2 修訂）

### 4.0 v0.1 → v0.2 修訂

> v0.1 推薦「Model 優先 + Agent 升級」，理由是「90% 使用情境只需要 OpenAI Images API」。
>
> **v0.2 修訂為單一路徑「Agent」**。理由：ANILA 主介面是 chat 對話，必須有「會講 chat 介面的 agent」才會被 Router 自動分派；Model 介面只能透過 OpenAI SDK 顯式呼叫 model name 觸發，**一般使用者在 ANILA chat 永遠用不到**。

### 4.1 為什麼必須是 Agent（場景對比）

#### 場景 A：ComfyUI 是 Model（v0.1 路線，已被 v0.2 否決）

```
使用者 chat 框輸入「畫一張海報」
    ↓
Router 看 message + agent manifest：
   「manifest 裡沒有 image agent，只能回答『我不會畫圖』」
    ↓
使用者體驗 ❌ — 一般 user 在 ANILA chat 永遠摸不到 ComfyUI

只有寫 SDK script 的人能用：
   openai.images.generate(model="comfyui-x", prompt=...)
```

#### 場景 B：ComfyUI 是 Agent（v0.2 採用）

```
使用者 chat 框輸入「畫一張海報」
    ↓
Router 看 message + agent manifest：
   「manifest 裡有 comfyui-image agent，描述包含『圖片生成』
    → 分派！」
    ↓
ComfyUI Agent 收到 chat request：
   1. 從 messages 抽出 prompt
   2. 跑 ComfyUI workflow
   3. SSE stream：先「生成中...」 → markdown image embed
    ↓
使用者在 chat 介面看到圖 ✅
```

### 4.2 Namespace 釐清（重要：兩個「model」不是同一個）

**設計討論裡常被混淆的點**：

| 名詞 | 意義 | 例子 | 在哪管 |
|---|---|---|---|
| **ANILA model** | `model_registry` 註冊的 LLM / Embedding / VLM | `google/gemma4`、`nvidia/NV-embed-V2`、`meta/llama-4-maverick` | CSP 的 `model_registry` table |
| **ComfyUI 內部的 model** | Diffusion checkpoint file（圖片生成的「畫筆」）| `flux1-dev.safetensors`、`sd_xl_base_1.0.safetensors` | ComfyUI 機器的本機 `models/checkpoints/` |

**兩者完全不相干**。本節討論的 ComfyUI workflow preset **只跟 ComfyUI 自己的 model 有關**，跟 ANILA 的 LLM `model_registry` 無關。

一份 ComfyUI workflow JSON 簡化長這樣：

```json
{
  "1": {
    "class_type": "CheckpointLoaderSimple",
    "inputs": { "ckpt_name": "flux1-dev.safetensors" }
  },
  "2": {
    "class_type": "CLIPTextEncode",
    "inputs": { "text": "{{USER_PROMPT}}", "clip": ["1", 1] }
  },
  "3": {
    "class_type": "KSampler",
    "inputs": {
      "model": ["1", 0], "positive": ["2", 0],
      "steps": 20, "cfg": 7.5, "sampler_name": "euler",
      "scheduler": "normal", "seed": 42
    }
  },
  "4": { "class_type": "VAEDecode", "inputs": { "samples": ["3", 0], "vae": ["1", 2] } },
  "5": { "class_type": "SaveImage", "inputs": { "images": ["4", 0] } }
}
```

「workflow preset / 食譜」就是**這整份 JSON**。它指定的全部是 ComfyUI 自己世界的東西（哪個 .safetensors / 幾步 / 什麼 sampler / 要不要套 LoRA）。

### 4.3 工程上要做什麼：`comfyui-agent` service

寫一個 **約 250 行 Python 的 service**（fork AgenticRAG template 起步）：

```
   對外（給 ANILA Router 用）
      POST /v1/chat/completions   ← OpenAI 格式
                                      ↓
                comfyui-agent 內部邏輯
                                      ↓
            從 messages 抽 user prompt
            選 workflow preset（預設或從文字判斷風格）
                                      ↓
   對內（呼叫 ComfyUI 真的 API）
      POST http://comfyui:8188/prompt   ← ComfyUI 自己的格式
                                      ↓
                拿到 image url
                                      ↓
            SSE stream 回傳給 ANILA Router
              「生成中...」
              ![](https://anila.tw/images/abc.png)
```

**`anila-agent.yaml` 註冊**：

```yaml
name: comfyui-image
endpoint_url: http://comfyui-agent:8210
api_version: v1
description_for_router: |
  圖片生成 agent — 海報、概念圖、繪畫、視覺素材。
  支援風格：FLUX 高品質繁中字型（預設）。
  輸入：自然語言描述要的圖。輸出：markdown embedded image。
base_model: flux1-dev   # 註：這是 description 用，不是真實 ANILA model_registry 名字
capabilities:
  streaming: true
  image_generation: true
  languages: [zh-TW, en]
approval_status: approved
```

### 4.4 一個預設食譜，使用者完全不用設

Phase 1 出貨時 `comfyui-agent/workflows/` 只放**1 個 preset**：

```
comfyui-agent/
├── main.py
├── workflows/
│   └── default.json    ← 唯一的預設食譜（FLUX 高品質繁中字型）
└── Dockerfile
```

使用者在 chat 說「畫海報」就直接用 `default.json` 出圖。**完全不用知道有「workflow preset」這個概念**。

### 4.5 workflow preset 的 4 層擴展機制（未來）

當需要更多風格時，4 層擴展：

| Layer | 怎麼加 | 誰可以加 | 何時做 |
|---|---|---|---|
| **0** | 內建 1 個預設食譜 | 平台團隊 | **Phase 1 出貨**（本次）|
| **1** | 多個 preset 並列（日系 / 寫實 / 卡通 / ...）| **admin** 在 CSP UI 上傳 workflow JSON | Phase 2+（看需求）|
| **2** | Agent 從使用者文字自動選 preset（LLM 判斷風格）| 由 LLM 判斷 | Phase 2+ |
| **3** | Dev 自帶整套 workflow → 註冊成自己的 agent | **developer** fork comfyui-agent template | Phase 3+ |

**為什麼 Layer 1 上傳 workflow 限 admin**（更正 v0.1 措辭）：

不是因為「會洩漏 ANILA LLM model」（兩個 namespace 不交集），實際理由：

1. ComfyUI workflow 會 reference **本機存在的 .safetensors file** — admin 才知道 GPU 機器上有哪些 checkpoint，user 上傳 workflow 引用不存在的 model 會跑爆
2. 控制 **GPU 資源使用** — 某些 workflow 設成 1024×1024 + 100 steps + batch=4 會吃光顯存
3. **平台一致性** — 讓使用者從乾淨選單挑 preset，不接觸 raw workflow JSON

### 4.6 兩種介面是否並存？

**v0.2 立場**：Phase 1 **只做 Agent 介面**（OpenAI chat completions）。

理由：
- ANILA 主流量是 chat 對話，Agent 是必須的
- OpenAI Images API (`/v1/images/generations`) 在組內沒人在寫（grep 沒 caller）
- 加 SDK direct 介面 = 多寫一個 endpoint + 多一份維護成本，沒收益就不做

**未來若有 SDK 直接呼叫需求**：在 `comfyui-agent` 加 `POST /v1/images/generations` endpoint，內部共用同一份 workflow runner。一個服務兩 endpoint 不衝突。

---

## 5. codeserver — ANILA 平台集成 Dev 入口（v0.2: 納入 monorepo deploy）

### 5.0 v0.1 → v0.2 修訂

> v0.1 把 codeserver 視為「外部已部署服務」，只在 `AUTO_REGISTER_LINKS` 加導向卡片。
>
> **v0.2 修訂**：經確認**組裡沒有獨立 codeserver 部署**，要由 ANILA 平台**自己提供**。所以 codeserver 不只是「導向卡片」，要**納入 ANILA monorepo 的 `docker-compose.yml`** 作為平台 service 之一。

### 5.0.1 部署整合（v0.3: 同源 nginx path）

#### Docker Compose service

ANILA 根 `docker-compose.yml` 加新 service（v0.3: **不再 expose port 8443**，改透過內網訪問）：

```yaml
services:
  # ... 既有 csp / router / anila-ui / csp-db ...

  codeserver:
    image: lscr.io/linuxserver/code-server:latest
    container_name: anila-codeserver
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=Asia/Taipei
      - PASSWORD_FILE=/run/secrets/codeserver_password
      - SUDO_PASSWORD_FILE=/run/secrets/codeserver_sudo_password
      - PROXY_DOMAIN=${ANILA_HOST}             # v0.3: 設成 ANILA host
      - DEFAULT_WORKSPACE=/workspace
    volumes:
      - codeserver_config:/config
      - codeserver_workspace:/workspace
      - /var/run/docker.sock:/var/run/docker.sock:ro
    # v0.3: 不對外 expose port，由 nginx 內網訪問
    expose:
      - "8443"
    networks:
      - anila_internal
    secrets:
      - codeserver_password
      - codeserver_sudo_password
    restart: unless-stopped

volumes:
  codeserver_config:
  codeserver_workspace:

secrets:
  codeserver_password:
    file: ./secrets/codeserver_password.txt
  codeserver_sudo_password:
    file: ./secrets/codeserver_sudo_password.txt
```

#### Nginx 同源 reverse proxy 設定

走 `https://<anila-host>/codeserver/*` 反向代理到 `http://codeserver:8443`。
**WebSocket 是必須的**（code-server 的 terminal、file watcher、live preview 全部依賴 WS）：

```nginx
# anila/nginx/anila.conf

server {
    listen 443 ssl;
    server_name anila.internal;
    # ... ANILA UI / CSP / Router 既有設定 ...

    # ───── codeserver 同源 path ─────
    location /codeserver/ {
        # 1. 路徑改寫：strip /codeserver prefix
        proxy_pass http://codeserver:8443/;

        # 2. WebSocket upgrade 必須（terminal / file watcher / live reload）
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        # 3. 標準 reverse proxy headers
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Prefix /codeserver;

        # 4. code-server 偶有大檔上傳（git push、artifact）
        client_max_body_size 200M;

        # 5. 長連線：codeserver 內部 WS 不要被 nginx idle timeout 砍
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;

        # 6. v0.3 Phase 1: 仍用 codeserver 自己的 password (PASSWORD_FILE)
        # v0.3 Phase 3 SSO: 改走 nginx auth_request 模組驗 CSP JWT cookie
        # auth_request /_csp_validate;          # ← Phase 3 才打開
    }

    # ───── 其他既有 location（CSP / UI / Router）保持不變 ─────
}
```

#### 為什麼選同源 path（vs 獨立 hostname）

| 方案 | 同源 path（採用）| 獨立 hostname |
|---|---|---|
| SSL cert | 與 ANILA 共用 | 要另一張 cert |
| CORS | 同源無問題 | 要設 CORS / iframe sandbox |
| Cookie | 自動帶 | 要 SameSite/Cookie domain 配置 |
| Phase 3 SSO 整合 | 直接 `auth_request` | 要做 cross-domain JWT 傳遞 |
| 部署複雜度 | 低 | 中 |
| 缺點 | URL 帶 `/codeserver` 前綴；code-server 要設 `--proxy-domain` 或 `PROXY_DOMAIN` env | — |

#### Phase 1 → Phase 3 SSO 演進

**Phase 1**：codeserver 自己的 PASSWORD_FILE 認證
- `PASSWORD_FILE=/run/secrets/codeserver_password`
- 使用者第一次點卡片要輸入 codeserver password
- admin only（required_roles=['admin']，§7.5）

**Phase 3**：nginx `auth_request` + CSP JWT
- 移除 `PASSWORD_FILE`
- nginx 設 `auth_request /_csp_validate`
- `/_csp_validate` 內部跳到 CSP 驗 JWT cookie，回 200 → forward；回 401 → 跳登入
- code-server 直接 trust upstream auth header（從 X-Forwarded-User 撈 username）

### 5.1 角色定位

```
            Dev 真正的開發環境在哪裡？
                        │
        ┌───────────────┴───────────────┐
        ▼                               ▼
  mlsteam（GPU host）              codeserver
  「跑 model / 跑 agent」          「browser-based VS Code」
        │                               │
        │                               │
        └───────┬───────────────────────┘
                ▼
        都需要連到 ANILA postgres
        （pgvector，做 ingestion / 檢索）
```

codeserver 在 ANILA 體系的角色 **不是 agent，也不是 model**：

> codeserver 是 **dev workspace 入口**。Dev 透過 codeserver 寫 agent code，但實際 agent 跑在 mlsteam GPU 機器。codeserver 與 mlsteam 都需要連回 ANILA 的 pgvector 做 ingestion 與檢索（這就是之前 ingestion-platform-design 在解決的問題）。

### 5.2 dev 連 ANILA postgres 的安全機制

**問題**：mlsteam 上的 dev 寫 agent，需要 `DATABASE_URL` 連 ANILA postgres 寫 chunks。但是：
- 不能給原始 superuser credentials（會洩漏）
- 不能給 read-write 整個 cluster（會踩到別 agent 的 data）
- 必須跟 [`ingestion-platform-design.md`](./ingestion-platform-design.md) §3.3 的 RLS 機制整合

**解法**：CSP 提供 **per-developer scoped DB credentials**（短效）

```
┌─────────────────────────────────────────────────────────────┐
│  CSP Dev Console                                            │
│                                                             │
│  Agent: my-legal-rag                                        │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ 🔑 Database Credentials                              │   │
│  │                                                      │   │
│  │   Host:     pgvector.anila.internal                  │   │
│  │   Port:     5432                                     │   │
│  │   Database: anila_pgvector                           │   │
│  │   User:     dev_my-legal-rag_eyJk...                 │   │
│  │   Password: (one-time copy)                          │   │
│  │                                                      │   │
│  │   Valid for: 24 hours                                │   │
│  │   Scope:     agent_id = 42 (my-legal-rag)            │   │
│  │                                                      │   │
│  │   [Copy connection string]  [Revoke]                 │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

### 5.3 實作機制

CSP 後端提供 endpoint `POST /api/dev-credentials/db`（v0.2: TTL 改 30d + revoke + reminder）：

```python
DEV_CRED_TTL = timedelta(days=30)   # v0.2: 從 24h 改為 30d

@router.post("/api/dev-credentials/db")
async def issue_db_credential(
    agent_id: int,
    caller: User = Depends(get_current_user),
):
    # Authorization
    agent = await get_agent(agent_id)
    if agent.owner_user_id != caller.id and not caller.is_admin:
        raise HTTPException(403)

    # 撤銷該 agent 之前的 active credential（同一 agent 同時只能有一把）
    await revoke_active_credentials_for_agent(agent_id)

    # 建立 30 天 PG role
    role_name = f"dev_{agent.name}_{secrets.token_hex(4)}"
    password = secrets.token_urlsafe(32)
    expiry = datetime.utcnow() + DEV_CRED_TTL
    await pg_admin.execute(f"""
        CREATE ROLE {role_name} LOGIN PASSWORD '{password}'
            VALID UNTIL '{expiry.isoformat()}';
        GRANT pg_read_all_data TO {role_name};
        GRANT pg_write_all_data TO {role_name};
        ALTER ROLE {role_name} SET anila.agent_id = '{agent_id}';   -- 關鍵：自動觸發 RLS
    """)

    # 記錄到 dev_db_credentials 表（追蹤 active credentials 用）
    cred_id = await db.insert(DevDbCredential(
        agent_id=agent_id,
        issued_to=caller.id,
        pg_role_name=role_name,
        issued_at=datetime.utcnow(),
        expires_at=expiry,
        revoked_at=None,
    ))

    # 寫進 audit log
    await audit_log("issued_dev_db_credential", actor=caller.id,
                    resource_type="agent", resource_id=agent_id,
                    detail={"cred_id": cred_id, "ttl_days": 30})

    return {
        "credential_id": cred_id,
        "host": settings.PG_HOST,
        "port": settings.PG_PORT,
        "database": settings.PG_DB,
        "user": role_name,
        "password": password,           # 只回傳這一次
        "valid_until": expiry.isoformat(),
        "scope": {"agent_id": agent_id},
    }


@router.post("/api/dev-credentials/{cred_id}/revoke")
async def revoke_db_credential(
    cred_id: int,
    caller: User = Depends(get_current_user),
):
    """Dev / admin 隨時可撤銷自己的 credential。"""
    cred = await get_credential(cred_id)
    if cred.issued_to != caller.id and not caller.is_admin:
        raise HTTPException(403)

    await pg_admin.execute(f"DROP ROLE IF EXISTS {cred.pg_role_name};")
    await db.update(DevDbCredential, cred_id, revoked_at=datetime.utcnow())
    await audit_log("revoked_dev_db_credential", actor=caller.id,
                    resource_type="dev_credential", resource_id=cred_id)
    return {"status": "revoked"}
```

**Schema**：

```sql
CREATE TABLE dev_db_credentials (
    id              BIGSERIAL PRIMARY KEY,
    agent_id        BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    issued_to       BIGINT NOT NULL REFERENCES users(id),
    pg_role_name    TEXT NOT NULL UNIQUE,
    issued_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    revoked_at      TIMESTAMPTZ,
    last_used_at    TIMESTAMPTZ
);

CREATE INDEX idx_dev_creds_active ON dev_db_credentials(agent_id, expires_at)
    WHERE revoked_at IS NULL;
```

**自動 reminder（背景任務）**：

```python
# 每天跑一次 — 檢查即將過期的 credential
async def remind_expiring_credentials():
    soon = datetime.utcnow() + timedelta(days=3)
    creds = await db.fetch(
        "SELECT * FROM dev_db_credentials "
        "WHERE revoked_at IS NULL AND expires_at <= :soon AND expires_at > now()",
        soon=soon
    )
    for cred in creds:
        await send_notification(
            user_id=cred.issued_to,
            type="dev_credential_expiring",
            message=f"Agent '{cred.agent_name}' 的 DB credential 將於 {cred.expires_at} 過期，"
                    f"請至 ANILA Dev Console 重新 issue。"
        )
```

**關鍵設計**：`ALTER ROLE ... SET anila.agent_id = N` — 這個 PG role 一登入就自動 set session var，**RLS policy 自動套用**，dev 寫的任何 query 都不可能看到別 agent 的 data。完全跟 [`ingestion-platform-design.md`](./ingestion-platform-design.md) §3.3 layer 2 對齊。

**TTL 30d 的理由（v0.2 修訂）**：
- 24h 太短：dev 每天要 issue 麻煩，倒逼大家把 credential 寫死在 config
- 30d 適中：對應一個 sprint 週期，過期前有 reminder
- 90d 太長：洩漏風險明顯放大
- **配套**：revoke API 隨時可撤、自動 reminder（過期前 3 天）、audit 每次 issue 與 revoke

### 5.4 codeserver 的使用流程

codeserver 由 ANILA 平台部署（見 §5.0.1），dev 流程：

1. ANILA dashboard 點「Code Server」卡片（admin role 才看得到，§7.5）
2. 開新分頁進入 codeserver workspace（`https://anila.internal/codeserver`）
3. 用 git clone 自己 agent 的 repo（gitlab）
4. 點 ANILA dashboard 的「Get DB Credentials」拿 connection string（30d TTL）
5. 貼進自己 agent 的 `.env`
6. 寫 code、跑 test、push 回 gitlab
7. agent build 完 deploy 到 mlsteam（不是 codeserver — codeserver 不跑 production agent）

**codeserver 在 ANILA 的位置**：
- **Phase 1**：admin-only platform service（`required_roles=['admin']`），其他人看不到
- **Phase 3 SSO**：可以擴大開放給 developer role
- **長期**：作為 ANILA 平台與 mlsteam 之間的「地面站」— dev 在 codeserver 開發、測試（連回 ANILA postgres），確認 OK 後 deploy 到 mlsteam GPU 機器跑

### 5.5 為什麼 mlsteam 不算 ANILA 子服務

mlsteam 是 organization-wide GPU 平台，**不歸 ANILA 管**。ANILA 跟 mlsteam 的關係是：
- mlsteam 提供算力給 dev / agent 跑
- agent 完成後在 ANILA 註冊 endpoint（用 mlsteam 上的 IP）
- 但 mlsteam 本身不該被 ANILA dashboard 接管

導向卡片可以放 mlsteam 連結（已有），但**不需要做 SSO**。

---

## 6. NotebookLM 整合（Phase 1 SSO + grant，Phase 4 agent 化）

### 6.0 v0.2 修訂：兩個階段

> v0.1 只規劃了 Phase 4 的 agent 化計畫。v0.2 補上 **Phase 1 / Phase 3 的 access integration**：

| 階段 | 整合內容 |
|---|---|
| **Phase 1** | 平台連結卡片 + `service_access_grants` 嚴格白名單（模式 A） |
| **Phase 3** | OIDC SSO（NotebookLM auth 認 CSP token） |
| **Phase 4** | Agent 化（單 agent + 9 tools） |

### 6.0.1 Phase 1：嚴格白名單 grant（模式 A）

NotebookLM 在 `platform_links` table 註冊：

```sql
INSERT INTO platform_links (name, url, icon, description, required_roles)
VALUES (
  'NotebookLM',
  'http://notebooklm.internal:3100',
  'book-open',
  'AI 學習內容生成（podcast / slides / mind map / quiz / report）',
  NULL   -- ★ 純白名單模式：required_roles=NULL → 任何 role 預設都看不到
);
```

**Effective access**（演算法見 §7.5）：
- `required_roles=NULL` → 沒有 role-based 自動授權
- 使用者必須有 `service_access_grants` 紀錄才看得到卡片
- admin 在 CSP UI 一個個（或一個部門一次）grant

**Admin UI**：
```
Platform Link: NotebookLM
  Required Roles: (none — 純 grant 白名單模式)

  🏢 Department Grants
     ✅ 工程部           granted 2026-04-25 by admin (12 users)
     ✅ 產品部           granted 2026-04-26 by admin (8 users)
     [+ Grant to department]

  👤 Individual User Grants (例外加開)
     ✅ alice@example.com   granted 2026-04-25 by admin
     [+ Grant to user]

  📊 Effective Access: 21 users
```

### 6.0.2 Phase 3：OIDC SSO（auth 共用）

Phase 1 階段使用者點卡片進 NotebookLM **仍要單獨登入**（NotebookLM 自己的 JWT）。Phase 3 補上 SSO：

```
User 已登入 ANILA
    ↓ CSP 已發 JWT cookie
User 點 NotebookLM 卡片
    ↓ CSP 簽 OIDC ID token 帶在 redirect URL
NotebookLM 收到 callback
    ↓ 用 CSP 的 JWKS 驗 token（不是用本地 password）
NotebookLM 直接進 workspace（無需再登入）
```

**NotebookLM 改動**：
- `auth.py` 加 OIDC client middleware（FastAPI `authlib`）
- 收到 `Authorization: Bearer <csp_jwt>` → 用 CSP JWKS 驗 → 從 token claim 撈 `user_id` / `username`
- 本地 password 認證 deprecated（保留 superuser fallback for emergency）

### 6.1 為什麼 agent 化現在不做

NotebookLM 是 prod 服務、有自己 user base、有 9 種 artifact 生成，貿然 agent 化的破壞風險高。先放著、寫 plan，等 ANILA platform 成熟後再啟動。

### 6.2 兩種 agent 化路線

#### 路線 A：9 種 artifact 各自當一個 agent

```
notebooklm-podcast-generator        agent
notebooklm-slides-generator         agent
notebooklm-mindmap-generator        agent
notebooklm-flashcards-generator     agent
notebooklm-quiz-generator           agent
notebooklm-infographic-generator    agent
notebooklm-datatable-generator      agent
notebooklm-report-generator         agent
notebooklm-video-script-generator   agent
```

| 優點 | 缺點 |
|---|---|
| Router 可以精準分派（使用者問「幫我做投影片」→ slides agent）| 9 個 agent 要維護 9 個 endpoint |
| 每個 artifact 可以獨立改 prompt / 換 model | 共用邏輯（檔案上傳、向量搜索）要拆出 |
| 可以細粒度開放權限（dept A 只能用 podcast）| Agent 數量爆炸（100 agent 計畫變 100+9 個）|

#### 路線 B：合一個 multi-tool agent

```
notebooklm-studio          agent
  ├─ tool: generate_podcast
  ├─ tool: generate_slides
  ├─ tool: generate_mindmap
  └─ ... (9 個 tool)
```

| 優點 | 缺點 |
|---|---|
| 單一 agent endpoint，簡單 | Router 無法針對 artifact 類型精準分派 |
| 共用邏輯不用拆 | 使用者要在對話中明確說「請幫我用 podcast 工具」|
| Agent 數量不爆炸 | tool_calling 失敗時整個 agent 不可用 |

#### 推薦：**路線 B**（單 agent + 9 tools）

理由：
- NotebookLM 9 個 artifact 共用底層邏輯（同一份文件 → 不同呈現），拆 9 個 agent 等於重複實作
- Router 精準分派的價值在 RAG 類「不同知識庫」，不在「同知識庫不同呈現方式」
- LLM tool_calling 的能力已經足夠在 multi-tool agent 內做選擇

### 6.3 Agent 化的前置條件

NotebookLM 啟動 agent 化前，必須完成：
1. **CSP 取代 My-OpenAI-Frontend** — NotebookLM 內部要打 CSP `/v1/*` 而非 OpenAI Frontend
2. **Ingestion Platform 上線** — NotebookLM 的 chroma 改用 ANILA pgvector（或保留 chroma 但加同步）
3. **SSO** — NotebookLM auth 改認 CSP token

未滿足這 3 個條件，agent 化會打結。

### 6.4 Agent 化執行步驟（未來）

```
Step 1: NotebookLM backend 加 OpenAI-compatible endpoint
        POST /v1/chat/completions
          ↓ 內部 dispatch 到 9 個 artifact generator
          ↓ 用 tool_calling pattern
          ↓ SSE stream

Step 2: 註冊到 CSP 為 Agent
        anila-agent.yaml:
          name: notebooklm-studio
          endpoint_url: http://notebooklm-backend:8100
          base_model: gemma4
          requires_encryption: false
          capabilities:
            tool_calling: true
            tools: [podcast, slides, mindmap, ...]

Step 3: ANILA UI 的 router 自動發現
        使用者透過 anila-router 呼叫 notebooklm-studio
        Router 把 LLM 主對話轉給它
```

### 6.5 與 AgenticRAG template 的差異

| 面向 | AgenticRAG template | NotebookLM (agent 化後) |
|---|---|---|
| 知識來源 | pgvector（共用 ANILA Ingestion Platform） | Chroma（自有，內部生成 artifact 用）|
| 對話類型 | 多輪檢索 + 來源標注 | 單次「給文件 → 生 artifact」|
| Output | 文字 + citations | PPTX / PDF / mind map JSON 等 |
| Fork 對象 | 一般部門 RAG agent | 不適合 fork（特殊用途）|

NotebookLM 不會變成 template，它是**一個特殊的 agent 實例**（One of a kind）。

---

## 7. Platform Links — 完整服務清單（v0.2 更新）

### 7.1 v0.2 完整註冊內容

URL 已從 `My-OpenAI-Frontend/webui/src/pages/index.tsx` 查證實際值：

```yaml
# myCSPPlatform/.env 的 AUTO_REGISTER_LINKS（v0.2 final）

- name: "NotebookLM"
  url: "http://notebooklm.internal:3100"
  icon: "book-open"
  description: "AI 學習內容生成（podcast / slides / mind map / quiz / report）"
  required_roles: null   # 純白名單，admin 個別 grant（§6.0.1）

- name: "Code Server"
  url: "http://codeserver.internal:8443"   # ← ANILA monorepo 自己部署（§5.0.1）
  icon: "code"
  description: "Browser VS Code — agent 開發整合入口"
  required_roles: ["admin"]   # 僅 admin

- name: "n8n 工作流程"
  url: "/n8n"   # nginx 同源 reverse proxy 路徑（已有，從 my-openai-frontend 確認）
  icon: "workflow"
  description: "自動化工作流程平台 — 排程任務、跨服務串接"
  required_roles: ["admin", "developer"]

- name: "GitLab"
  url: "http://TODO-gitlab-url"   # ⚠️ Phase 1 卡片仍註冊但 URL=pending_deployment
                                  # 理由：GitLab 是 ISO 42001 合規必備（§7.4）
                                  # 過去 my-openai-frontend 註解掉是因當時還沒引入 ISO，
                                  # 現在內網部署嚴格遵守 42001 → 必須提供存放庫
  icon: "git-branch"
  description: "Git server — agent 程式碼與 issue tracking（ISO 42001 合規必備）"
  required_roles: ["admin", "developer"]

- name: "MLSteam"
  url: "https://aiops.ai.ncsist.org.tw:4443/"   # ← 已從 my-openai-frontend 查得
  icon: "cpu"
  description: "MLOps 平台 — agent 訓練與部署"
  required_roles: ["admin", "developer"]

# ComfyUI Studio (raw UI) 是否要做卡片是個 design choice：
#   - 一般使用者透過 chat agent 用（§4），不需要直接進 ComfyUI UI
#   - 但 admin / developer 可能需要「進去看 ComfyUI 自己有什麼 model file」
#   - 本 v0.2 暫不加 ComfyUI Studio 卡片，等真有需求時再開
# - name: "ComfyUI Studio"
#   url: "http://comfyui.internal:8188"
#   icon: "image"
#   description: "ComfyUI 直接介面（進階；一般使用者請在 ANILA chat 直接畫圖）"
#   required_roles: ["admin", "developer"]
```

### 7.2 SSO 整合（Phase 3 才做）

n8n 與 gitlab 都支援 OAuth2：
- n8n: `N8N_AUTH_TYPE=oauth2` + IdP discovery
- GitLab: 內建 OmniAuth

CSP 啟用 OIDC Provider mode 後（見 §6.0.2），這兩個服務改認 CSP 為 IdP，使用者點導向卡片就直接登入。

**Phase 1 不做 SSO** — 純跳轉，使用者重新登入但破壞風險最低。

### 7.3 你要補的資訊（v0.3 更新）

| Item | 為什麼缺 | 怎麼補 |
|---|---|---|
| GitLab 內網 URL | 過去未部署（合規前）| **v0.3**: 確認 GitLab 部署計畫；§7.4 已指出這是 ISO 42001 合規必備、Phase 1 即使尚未部署也要佔卡片位置標 `pending_deployment` |
| ~~codeserver 暴露方式~~ | ✅ **v0.3 確認**：同源 nginx path `/codeserver`（§5.0.1 已 update） | done |
| `comfyui.internal` 的實際 host | grep 沒到，可能組裡用其他名字 | 你提供 |

### 7.4 ISO 42001 合規與 GitLab 的角色（v0.3 新增）

#### 為什麼 GitLab 從「nice to have」升格為「必備基礎設施」

ISO/IEC 42001:2023（AI Management System）對「AI 系統開發」要求：

| 條款 | 要求 | 對 ANILA 的意義 |
|---|---|---|
| 8.2 (Resources for AI systems) | AI 系統開發資源（含 source code）必須**有管理、有追蹤** | agent 源碼不能散落在 dev 個人機器，必須中央存放庫 |
| 8.4 (AI system requirements analysis) | 變更必須可追溯、有 audit trail | 每次 agent code 變更要有 commit + reviewer |
| 9.1 (Performance evaluation) | AI 系統效能評估要可驗證 | Eval result 要能 trace 回到 source commit hash |
| A.6.2.5 (Verification & validation) | 部署前必須有 V&V 流程 | PR review + CI 必過 |
| A.6.2.6 (Deployment) | 部署 artifacts 必須可追溯回 source | docker image tag → git commit → reviewer |

**換句話說**：沒有版本控制中央存放庫，整個平台**無法通過 ISO 42001 驗證**。GitLab 不是「為了方便」，是「為了能上線」。

#### 對 ANILA 平台設計的牽連（不只是 GitLab 一張卡片）

ISO 42001 影響面遠超出 GitLab 本身。**這次 design doc 範圍內先處理 GitLab**，但下面這些值得後續另開 design doc：

| 影響面向 | 現況 | 42001 要求 | 需要動的地方 |
|---|---|---|---|
| Agent registration | `agents` table 有 `endpoint_url` / `description` | 要記 `source_commit_sha` / `last_reviewer` / `v&v_status` | CSP `agents` schema 加欄位 |
| Model registry | `model_registry` 有 `endpoint_url` | 要記 `model_card_url` / `training_dataset_ref` / `weights_sha256` | CSP `model_registry` schema 加欄位 |
| Audit log | 已有 `audit_logs` table | ✅ 已符合「變更追蹤」要求 | 確認 retention 期 ≥ 12 個月 |
| Deployment artifacts | docker compose 直接拉 image | image tag 要能 trace 回 git commit | CI 流程 — image tag = `git rev-parse HEAD` |
| Eval results | （尚未實作）| 要記 `eval_run_id` ↔ `source_commit_sha` 對應 | Ingestion Platform §6 evaluator 要加這個欄位 |

#### Phase 1 GitLab 整合的具體步驟

```
1. 確認 GitLab 部署狀態：
   ☐ 組裡是否已 deploy GitLab instance？
   ☐ 若已 deploy → 拿 URL 填 §7.1
   ☐ 若未 deploy → 開另一個 sprint task: 「Deploy GitLab to internal network」

2. AUTO_REGISTER_LINKS 註冊 GitLab card（即使 URL=pending_deployment）：
   - card 顯示「⏳ Deployment pending」標籤
   - 點擊提示「平台正在部署，預計 X 月上線」
   - 不 block 其他服務上線

3. ISO 42001 audit checklist（給 admin 用）：
   ☐ 所有 agent repo 已 push 到 GitLab
   ☐ 所有 commit 都有 reviewer
   ☐ docker image tag 已關聯 git commit hash
   ☐ audit_logs retention ≥ 12 個月
```

#### Future design doc：ANILA × ISO 42001 完整合規

ISO 42001 對 ANILA 的影響不只 GitLab。建議下一份 design doc：

> **`docs/iso-42001-compliance.md`** — 完整盤點 ISO 42001 對 ANILA 各 component 的要求、現況 gap、補強計畫
>
> 範圍：agent registration metadata / model card / eval traceability /
> deployment SBOM / audit retention / V&V 流程 / dataset lineage

**本 plan 不展開**，只在這節記錄 surface area。下一輪 design 衝刺再做。


### 7.5 Service Access Control（v0.2 新增）

#### 7.5.1 Schema

```sql
-- platform_links 加 required_roles 欄位
ALTER TABLE platform_links
    ADD COLUMN required_roles TEXT[] DEFAULT NULL;
-- NULL = 看 service_access_grants 才決定（純白名單）
-- ['admin'] = admin role 自動有，其他人需要 grant
-- ['admin','developer'] = 兩個 role 都自動有，其他需 grant

-- 新表：grant 記錄（支援 user-level 與 department-level）
CREATE TABLE service_access_grants (
    id                BIGSERIAL PRIMARY KEY,
    user_id           BIGINT REFERENCES users(id) ON DELETE CASCADE,
    department_id     BIGINT REFERENCES departments(id) ON DELETE CASCADE,
    platform_link_id  BIGINT NOT NULL REFERENCES platform_links(id) ON DELETE CASCADE,
    granted_by        BIGINT REFERENCES users(id),
    granted_at        TIMESTAMPTZ DEFAULT now(),
    revoked_at        TIMESTAMPTZ,

    -- 強制 exactly-one：每筆 grant 要嘛 user 要嘛 dept，不能兩個都填
    CHECK ((user_id IS NOT NULL) != (department_id IS NOT NULL)),

    -- 同一 user / dept 對同一 link 只有一筆 active grant
    UNIQUE (user_id, platform_link_id),
    UNIQUE (department_id, platform_link_id)
);

CREATE INDEX idx_grants_user ON service_access_grants(user_id) WHERE user_id IS NOT NULL;
CREATE INDEX idx_grants_dept ON service_access_grants(department_id) WHERE department_id IS NOT NULL;
```

**為什麼選兩個 nullable FK + CHECK 強制 exactly-one**：
- 比 polymorphic 安全：FK constraint 可以正常 cascade（部門刪掉時 grant 自動清）
- 比兩張表簡單：同一個 SQL `JOIN` 就能查，UI 只需要一個 list 介面
- CHECK 強制 exactly-one 防止資料髒掉

#### 7.5.2 Effective Access 演算法

```python
def can_access(user, link) -> bool:
    """Decide if user has access to a platform link."""

    # 1. Role-based 自動通過（required_roles）
    if link.required_roles and user.role in link.required_roles:
        return True

    # 2. Admin 永遠通過（superuser bypass）
    if user.role == "admin":
        return True

    # 3. 個別 user grant（active 且未 revoke）
    if has_active_grant(user_id=user.id, link_id=link.id):
        return True

    # 4. 部門 grant（user.department_id 從 user 表抓）
    if user.department_id and has_active_grant(
        department_id=user.department_id,
        link_id=link.id
    ):
        return True

    return False
```

**Cascade 行為**（admin 撤部門 grant 時的安全提醒）：
- 部門 grant 撤銷 → 該部門 user **只靠部門 grant 的會失去 access**
- 該部門 user **有額外個別 grant 的不受影響**（兩種 grant 是 OR 關係）
- UI 撤銷部門 grant 時要警告：「將影響 12 位使用者，其中 2 位有個別 grant 不受影響」

#### 7.5.3 Admin UI

```
Platform Link: NotebookLM
  Required Roles: (none — 純 grant 白名單模式)
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  🏢 Department Grants (一次蓋整批)
     ✅ 工程部           granted 2026-04-25 by admin (12 users)
     ✅ 產品部           granted 2026-04-26 by admin (8 users)
     [+ Grant to department]

  👤 Individual User Grants (例外加開)
     ✅ alice@example.com   granted 2026-04-25 by admin
     ✅ bob@example.com     granted 2026-04-26 by admin
     [+ Grant to user]

  📊 Effective Access Summary
     Total users with access: 22 (20 via dept + 2 individual)
     [View user list]
```

#### 7.5.4 Audit log

每次 `grant_*` / `revoke_*` 寫進既有 `audit_logs` 表（CSP 已有此基礎建設）：

```python
await audit_log(
    action="grant_platform_link_access",
    actor=admin.id,
    resource_type="platform_link",
    resource_id=link.id,
    detail={
        "grantee_type": "department",
        "grantee_id": dept.id,
        "grantee_name": dept.name,
    }
)
```

---

## 8. Phase 1 立即可做（v0.2 更新）

### 8.1 工作項目（v0.2 final）

| # | 項目 | 工程量 |
|---|---|---|
| 1 | Alembic migration `0013`：`platform_links.required_roles` + `service_access_grants` table（§7.5）| 1-2 小時 |
| 2 | CSP backend 實作 `can_access()` + grant CRUD endpoints（§7.5）| 0.5 天 |
| 3 | 寫 `AUTO_REGISTER_LINKS` 5 筆（NotebookLM / codeserver / n8n / gitlab / mlsteam，URL 見 §7.1） | 30 分鐘 |
| 4 | CSP UI Dashboard 顯示卡片時依 `can_access()` 過濾 + Service Access 管理 UI（§7.5.3）| 1-1.5 天 |
| 5 | ANILA monorepo `docker-compose.yml` 新增 `codeserver` service（§5.0.1）| 30 分鐘 |
| 6 | 寫 `comfyui-agent` service（fork AgenticRAG template，250 行 Python + 1 個預設 workflow JSON）| 1 天 |
| 7 | ANILA monorepo `docker-compose.yml` 新增 `comfyui-agent` service | 30 分鐘 |
| 8 | CSP `AUTO_REGISTER_AGENTS` 註冊 `comfyui-image` agent（§4.3）| 15 分鐘 |
| 9 | E2E 測試 1：使用者在 ANILA chat 說「畫海報」→ Router 分派到 comfyui-image → 看到圖 | 2 小時 |
| 10 | E2E 測試 2：admin grant 工程部 access NotebookLM → 部門使用者 dashboard 看到卡片 → 點開（Phase 1 仍重新登入）| 2 小時 |
| 11 | My-OpenAI-Frontend 停用：`docker compose down` + nginx `/v1/*` 改 410 + README archive notice（§3.0.1）| 30 分鐘 |
| 12 | **(v0.3)** Nginx 設定 `/codeserver` 同源 reverse proxy（含 WebSocket upgrade）（§5.0.1）| 1 小時 |
| 13 | **(v0.3)** 確認 GitLab 部署狀態 + 註冊 platform_link card（即使 URL=pending）（§7.4）| 30 分鐘 + GitLab deploy 另計 |

**總工程量**：3-5 天（從 v0.2 的 3-4 天微幅上升）。GitLab 自身部署若未完成，可能再 +1-2 週由 SRE 處理（不 block 其他項目）。

### 8.2 Phase 1 Deliverable

- 使用者登入 ANILA dashboard 看到「自己有權限」的服務卡片（依 `service_access_grants` 過濾）
- ANILA chat 使用者說「畫海報」可以拿到圖（透過 comfyui-image agent）
- admin 可以一次 grant 整個部門 access 某個服務
- codeserver 跟著 ANILA stack 一起部署（admin only）
- My-OpenAI-Frontend 已停用，不再有流量

### 8.3 Phase 1 不會做

- 不做 SSO（各服務維持原 auth；使用者點 NotebookLM 卡片仍要登入）→ Phase 3
- 不做 My-OpenAI-Frontend data migration（沒 active 使用者，不需要）
- 不動 NotebookLM 內部結構 → Phase 4 才動
- 不做 ComfyUI workflow preset Layer 1+（admin 上傳新 preset 的 UI）→ Phase 2 看需求
- 不做 dev DB credential endpoint（§5.3）→ Phase 3

---

## 9. Phase 2：My-OpenAI-Frontend 廢除（2-3 週）

詳見 §3。要點：
- 補 CSP 的 audio transcription endpoint
- 一次性 migration script 跑完
- Client cutover + 觀察期
- 90 天後完全 sunset

---

## 10. Phase 3：SSO + codeserver Dev Workspace（2-3 週）

### 10.1 SSO

- CSP 啟用 OIDC Provider mode
- NotebookLM / n8n / gitlab / codeserver 改認 CSP 為 IdP

### 10.2 codeserver Dev Workspace 整合

- 新增 CSP endpoint `POST /api/dev-credentials/db`（§5.3）
- ANILA UI Dev Console 加「Get DB Credentials」按鈕
- 短效 PG role 機制（24h TTL）+ 自動 SET LOCAL anila.agent_id

---

## 11. Phase 4：NotebookLM Agent 化（時間未定）

詳見 §6。要點：
- 必須先完成 Phase 2（取代 My-OpenAI-Frontend）+ Ingestion Platform 上線 + Phase 3 SSO
- 採路線 B：單 agent + 9 tools
- 新加 OpenAI-compatible endpoint 到 NotebookLM backend
- 註冊為 CSP agent

---

## 12. 風險與 Mitigation

| 風險 | Phase | Mitigation |
|---|---|---|
| Phase 2 cutover 漏改 client | 2 | grep audit + Step 5 410 Gone 暴露漏網之魚 |
| ComfyUI bridge 預設 workflow 不滿足使用者 | 1 | 提供 3-5 種預設、之後加 `workflow_url` 欄位讓 dev 自帶 |
| codeserver dev credential 洩漏 | 3 | 24h TTL + audit log + revoke API |
| NotebookLM agent 化破壞 prod 流量 | 4 | 雙軌跑 1 個月（舊 UI + 新 agent endpoint），確認後再 sunset 舊 UI |
| n8n / gitlab SSO 失敗 | 3 | 各服務本地登入 fallback 保留 |
| ComfyUI bridge timeout（圖片生成慢） | 1 | wrapper 內部 polling 10 分鐘 ceiling，超時回 504 |

---

## 13. Open Questions

1. **codeserver 部署位置** — 是放在 ANILA monorepo 內 `docker-compose.yml`，還是組裡已經有獨立部署？確認 URL 後填 §7.1
2. **mlsteam 連線資訊** — 確認內部 hostname（目前 §7.1 寫 `mlsteam.internal` 是猜的）
3. **NotebookLM Chroma 處理** — Phase 4 啟動時，NotebookLM 自有 chroma 要：(a) 完全廢棄改用 ANILA pgvector；(b) 並存（chroma 給 artifact 生成、pgvector 給跨 agent 共用檢索）；(c) 一次性 migrate 進 pgvector。
4. **My-OpenAI-Frontend 廢除時間軸** — 跟 Ingestion Platform Sprint 1 並行，還是 Sprint 4 之後再做？
5. **ComfyUI workflow preset 選誰負責** — `comfyui-bridge` 內建 3-5 種 workflow，由誰提供（內部設計師 / 沿用 ComfyUI community workflow）？
6. **dev credentials 的 PG role TTL** — 24h 太長還是太短？要不要做「續期」endpoint？

---

## 14. 整體時程整合表

```
Week 1     ─── Phase 1 (導向卡片 + ComfyUI wrapper)
Week 2-3   ─── Ingestion Platform Sprint 1 (含 anila-core 瘦身)
Week 4-5   ─── Phase 2 (My-OpenAI-Frontend 廢除) +
                Ingestion Platform Sprint 2 (UI + worker)
Week 6-7   ─── Ingestion Platform Sprint 3 (Evaluator)
Week 8-9   ─── Phase 3 (SSO + codeserver dev workspace)
Week 10+   ─── 觀察期 + Phase 4 NotebookLM agent 化決策
```

**Critical path**：Phase 1 → Ingestion Platform → Phase 2 → Phase 3 → Phase 4。

各階段獨立 deploy、獨立 rollback。

---

**Last updated**: 2026-04-25 · **Companion docs**: [`ingestion-platform-design.md`](./ingestion-platform-design.md) · [`anila-core-boundary.md`](./anila-core-boundary.md)
