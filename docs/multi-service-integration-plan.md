# ANILA Multi-Service Integration Plan v0.1

**Status**: Draft for review
**Date**: 2026-04-25
**Author**: ANILA 平台團隊
**Companion docs**: [`ingestion-platform-design.md`](./ingestion-platform-design.md) · [`anila-core-boundary.md`](./anila-core-boundary.md)
**Source of investigation**: `/home/aia/c1147259/project` 目錄實際 grep 結果

---

## 0. Decisions Log

### v0.1 (2026-04-25) — 初版

關於組內既有服務（位於 `/home/aia/c1147259/project`）如何整合進 ANILA 平台的決議：

| 服務 | 決議 | 理由 |
|---|---|---|
| **My-OpenAI-Frontend** | **取代** — CSP 接手所有功能 | 離職同仁開發、無人維護；功能 95% 與 CSP 重疊 |
| **NotebookLM** | **暫保獨立** + 平台連結卡片 + 寫 future agent 化 plan | 現役 prod 服務，破壞風險高；未來再考慮整合成 multi-artifact agent |
| **data-quality** | **不納入** | 是 n8n workflow 的一部分，不是獨立服務 |
| **ComfyUI** | **註冊為 Model**（Phase 1）+ 留 Agent 升級路徑（Phase 2 可選）| 90% 使用情境只需要 OpenAI Images API；10% 進階 workflow 才需要 agent |
| **codeserver** | **ANILA 平台集成 dev 入口** — 不是 agent，而是讓 dev 連回 ANILA postgres 做 ingestion / 檢索 | dev 實際開發在 mlsteam，codeserver 是 unified dev workspace |
| **n8n**（新增） | **平台連結卡片** | workflow 平台，使用者跳轉使用 |
| **gitlab**（新增） | **平台連結卡片** | git server，使用者跳轉使用 |

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
| §4 ComfyUI 註冊為 model | `myCSPPlatform/README.md` 的 `model_registry` | 走既有 model 註冊機制 |
| §5 codeserver DB credential | [`ingestion-platform-design.md`](./ingestion-platform-design.md) §3.3 RLS | per-dev credentials 自動 `SET LOCAL anila.agent_id` |
| §6 NotebookLM agent 化 | [`anila-core-boundary.md`](./anila-core-boundary.md) | NotebookLM 9 種 artifact 對應 agent template fork pattern |

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

## 3. Class A：My-OpenAI-Frontend 廢除計畫

### 3.1 重疊範圍實證

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

**只有一項 CSP 缺**：audio transcription（whisper-style）endpoint。Phase 2 動工時要補上 `/v1/audio/transcriptions`。

### 3.2 廢除 Migration 步驟

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

## 4. Class C：ComfyUI 整合 — Model 優先 + Agent 升級路徑

### 4.1 為什麼是 Model 而不是 Agent

**ComfyUI 的本質**：
- 不是單一 model，是 workflow runner（把 stable diffusion / FLUX / LoRA / ControlNet 串成 graph）
- 對外 expose 的 API 不是 OpenAI compatible（`POST /prompt` + `GET /history/{id}` + WebSocket progress）

**使用者的 90/10 分布**：
- **90% 場景**：「給 prompt → 拿圖」 — 不關心 workflow 細節
- **10% 進階場景**：「用我這個 LoRA + ControlNet workflow」— 需要客製 graph

### 4.2 推薦做法：兩段式整合

#### Phase 1（推薦現在做）：ComfyUI 註冊為 Model

寫一個 **thin wrapper service `comfyui-bridge`**：
- 對外 expose **OpenAI Images API**（`POST /v1/images/generations`）
- 對內 把 prompt 餵給預設 workflow（或從 `workflow_preset` 選）
- CSP `model_registry` 加一筆 `model_type = "image"` 的 model

```python
# comfyui-bridge/main.py
from fastapi import FastAPI
import httpx

app = FastAPI()

# Pre-defined workflow presets (放 ComfyUI workflow JSON)
PRESETS = {
    "sd-default": load_workflow("workflows/sd_default.json"),
    "flux-fast": load_workflow("workflows/flux_fast.json"),
    "high-quality-zhtw": load_workflow("workflows/zhtw_quality.json"),
}

@app.post("/v1/images/generations")
async def generate(req: ImageGenRequest):
    """OpenAI Images API compatible.

    Body:
      model: "sd-default" | "flux-fast" | ...   (對應 PRESET name)
      prompt: str
      n: int = 1
      size: str = "1024x1024"
    """
    workflow = PRESETS[req.model].copy()
    inject_prompt(workflow, req.prompt, req.size)

    # Submit to ComfyUI
    async with httpx.AsyncClient() as client:
        r = await client.post("http://comfyui:8188/prompt", json={"prompt": workflow})
        prompt_id = r.json()["prompt_id"]

    # Poll for completion (或 WS subscribe)
    image_urls = await wait_for_completion(prompt_id)

    # Return OpenAI-compatible response
    return {
        "created": int(time.time()),
        "data": [{"url": u} for u in image_urls]
    }
```

**CSP 端註冊**：
```yaml
# AUTO_REGISTER_MODELS 加一筆
- name: "comfyui-sd-default"
  display_name: "ComfyUI - SD Default"
  model_type: "image"
  endpoint_url: "http://comfyui-bridge:8200"
  api_version: "v1"
```

**使用者體驗**：
```bash
# 任何 OpenAI SDK 都能用
curl -X POST http://csp:8000/v1/images/generations \
  -H "Authorization: Bearer sk-xxx" \
  -d '{"model": "comfyui-sd-default", "prompt": "繁中海報設計"}'
```

**工程量**：1 個 Python file ~200 行 + 3-5 個預設 workflow JSON。**1-2 天可交付**。

#### Phase 2（看需求才做）：ComfyUI Agent Template

當有人需要「客製 workflow」時，再做：
- AgenticRAG template 風格 — 一份 `ComfyUIAgent` template，dev fork 後改 workflow registry
- 註冊為 Agent（model_type = `agent`）
- 走 anila-router 分派
- 適合「我要用我自己的 LoRA」、「我要做 ControlNet 多步流程」這類進階場景

### 4.3 兩者並行不衝突

```
   一般使用者 ──→ POST /v1/images/generations (model="comfyui-sd-default")
                         ↓ CSP proxy
                  comfyui-bridge wrapper
                         ↓ predefined workflow
                       ComfyUI

   進階 dev   ──→ POST /v1/chat/completions (model="my-comfy-agent")
                         ↓ Router 分派
                   ComfyUI Agent (fork from template)
                         ↓ 客製 workflow
                       ComfyUI
```

兩條路打同一個 ComfyUI instance，不會打架。

### 4.4 決策原則（給未來新增類似服務時參考）

| 服務特徵 | 註冊為... |
|---|---|
| 無狀態、單次請求、輸入/輸出固定 | **Model**（走 `/v1/*` proxy）|
| 有狀態、多輪 tool calling、需要 LLM 自主決策 | **Agent**（走 Router 分派）|
| 兩者都要 | **同一服務註冊兩次**（不同 model name）|

---

## 5. codeserver — ANILA 平台集成 Dev 入口

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

CSP 後端提供 endpoint `POST /api/dev-credentials/db`：

```python
@router.post("/api/dev-credentials/db")
async def issue_db_credential(
    agent_id: int,
    caller: User = Depends(get_current_user),
):
    # Authorization
    agent = await get_agent(agent_id)
    if agent.owner_user_id != caller.id and not caller.is_admin:
        raise HTTPException(403)

    # 建立短效 PG role
    role_name = f"dev_{agent.name}_{secrets.token_hex(4)}"
    password = secrets.token_urlsafe(32)
    await pg_admin.execute(f"""
        CREATE ROLE {role_name} LOGIN PASSWORD '{password}'
            VALID UNTIL '{datetime.utcnow() + timedelta(hours=24)}';
        GRANT pg_read_all_data TO {role_name};
        GRANT pg_write_all_data TO {role_name};
        ALTER ROLE {role_name} SET anila.agent_id = '{agent_id}';   -- 關鍵
    """)

    # 寫進 audit log
    await audit_log("issued_dev_db_credential", actor=caller.id,
                    resource_type="agent", resource_id=agent_id,
                    detail={"role": role_name, "ttl_hours": 24})

    return {
        "host": settings.PG_HOST,
        "port": settings.PG_PORT,
        "database": settings.PG_DB,
        "user": role_name,
        "password": password,           # 只回傳這一次
        "valid_until": expiry.isoformat(),
        "scope": {"agent_id": agent_id},
    }
```

**關鍵設計**：`ALTER ROLE ... SET anila.agent_id = N` — 這個 PG role 一登入就自動 set session var，**RLS policy 自動套用**，dev 寫的任何 query 都不可能看到別 agent 的 data。完全跟 ingestion-platform-design §3.3 layer 2 對齊。

### 5.4 codeserver 的角色

codeserver 不需要特別整合到這個機制 — 它就是個一般的 browser VS Code，dev 在裡面：
1. 用 git clone agent 的 repo
2. 點 ANILA dashboard 的「Get DB Credentials」拿 connection string
3. 貼進自己 agent 的 `.env`
4. 寫 code、跑 test

**codeserver 在 ANILA 的位置**：純粹是 platform link 卡片（Class B），點下去開新分頁進入 codeserver workspace。

### 5.5 為什麼 mlsteam 不算 ANILA 子服務

mlsteam 是 organization-wide GPU 平台，**不歸 ANILA 管**。ANILA 跟 mlsteam 的關係是：
- mlsteam 提供算力給 dev / agent 跑
- agent 完成後在 ANILA 註冊 endpoint（用 mlsteam 上的 IP）
- 但 mlsteam 本身不該被 ANILA dashboard 接管

導向卡片可以放 mlsteam 連結（已有），但**不需要做 SSO**。

---

## 6. NotebookLM 未來 Agent 化計畫（Phase 4）

### 6.1 為什麼現在不做

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

## 7. n8n 與 GitLab — Platform Link 卡片

### 7.1 註冊內容

```yaml
# myCSPPlatform/.env 的 AUTO_REGISTER_LINKS

- name: "n8n 工作流程"
  url: "http://n8n.internal:5678"
  icon: "workflow"
  description: "自動化工作流程平台 — 排程任務、跨服務串接"

- name: "GitLab"
  url: "http://gitlab.internal:8080"
  icon: "git-branch"
  description: "Git server — agent 程式碼與 issue tracking"

- name: "NotebookLM"
  url: "http://notebooklm.internal:3100"
  icon: "book-open"
  description: "AI 學習內容生成（podcast / slides / mind map / quiz / report）"

- name: "Code Server"
  url: "http://codeserver.internal:8443"
  icon: "code"
  description: "Browser VS Code — agent 開發整合入口"

- name: "ComfyUI Studio"
  url: "http://comfyui.internal:8188"
  icon: "image"
  description: "圖片生成 workflow editor（進階）— 一般使用者請改用 model 介面"

- name: "MLSteam"
  url: "http://mlsteam.internal"
  icon: "cpu"
  description: "MLOps 平台 — agent 訓練與部署"
```

### 7.2 SSO 整合（Phase 3 才做）

n8n 與 gitlab 都支援 OAuth2：
- n8n: `N8N_AUTH_TYPE=oauth2` + IdP discovery
- GitLab: 內建 OmniAuth

CSP 啟用 OIDC Provider mode 後，這兩個服務改認 CSP 為 IdP，使用者點導向卡片就直接登入。

**Phase 1 不做 SSO** — 純跳轉就好，雖然要重新登入，但破壞風險最低。

---

## 8. Phase 1 立即可做（純導向卡片 + ComfyUI model wrapper）

### 8.1 工作項目

| # | 項目 | 工程量 |
|---|---|---|
| 1 | 寫 6 筆 `AUTO_REGISTER_LINKS`（NotebookLM / codeserver / n8n / gitlab / ComfyUI / mlsteam） | 30 分鐘 |
| 2 | CSP UI Dashboard 顯示「ANILA legacy 服務區」分類（visual grouping） | 1 小時 |
| 3 | 寫 `comfyui-bridge` wrapper service（200 行 Python + 3 個預設 workflow JSON） | 1 天 |
| 4 | Docker Compose 加入 `comfyui-bridge` service | 30 分鐘 |
| 5 | CSP `model_registry` 註冊 `comfyui-sd-default` 等 3 個 model | 30 分鐘 |
| 6 | E2E 測試：透過 OpenAI SDK 呼叫 CSP 生成圖片 | 2 小時 |

**總工程量**：1.5-2 天。

### 8.2 Phase 1 Deliverable

- 使用者登入 ANILA dashboard 看到所有服務卡片
- 任何 OpenAI SDK 都能透過 CSP 呼叫 ComfyUI 生圖
- `comfyui-bridge` 可獨立 deploy、不依賴其他改動

### 8.3 Phase 1 不會做

- 不做 SSO（各服務維持原 auth）
- 不動 My-OpenAI-Frontend（Phase 2 才動）
- 不動 NotebookLM（Phase 4 才動）

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
