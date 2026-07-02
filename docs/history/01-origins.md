# 時代 A — 前身專案與 ANILA 的誕生（Genesis）

> 系列文件《ANILA 開發史》第 01 篇。涵蓋範圍：兩個公開前身 repo（`myCSPPlatform`、`AgenticRAG`）的完整 commit 歷史，以及它們如何匯流、在 2026-04-17 於單一 monorepo 內誕生為 ANILA。
> 本篇僅記錄 **前身歷史 + main 的創世（genesis）數個奠基 commit**；`main` 完整平台演進表由「時代 B」續接。
> 證據優先:以 git 為第一手來源(前身 repo 已 bare clone 至 scratchpad 唯讀分析),輔以各 repo README / pyproject / `anila_plan.md` / 根 `AGENTS.md`。凡屬推斷者均明確標註。

---

## 概述

ANILA 不是從零起筆,而是由兩個先行 repo 匯流而成:

- **`AgenticRAG`**(2026-04-05 起,15 commits)—— 一個面向 **Agentic RAG** 的 Python runtime 與框架雛形。它的 Python package 名稱從第二個 commit 起就叫 **`anila-core`**(pyproject `name = "anila-core"`,description「ANILA Core - Python agent runtime」)。**ANILA 這個名字實際誕生於此 repo**,而非平台 repo。它提供 runtime foundation(query engine、coordinator、tool router、compaction、memory、ingestion、pgvector 檢索)與一支 OpenWebUI 相容的 RAG proxy(`api.py`,port 24786)。
- **`myCSPPlatform`**(2026-04-10 起,10 commits)—— **CSP(Cloud Service Platform)治理平台雛形**:企業內部 AI 模型服務的統一管理面板(API Key 發放、模型註冊、OpenAI 相容代理、用量追蹤、使用者/部門/認證、告警、審計)。Vue 3 + FastAPI + PostgreSQL + Nginx。

2026-04-17 當天,作者 Hsiu-Ying,Kung(zzw09773)以 5 個 commit 把兩者匯入單一 repo、加上專有授權、接上 `anila-core-router` 與 `docker-compose` 整合層,並落下一份「ANILA 平台整合計畫」願景文件 —— ANILA 於焉成形。此後(2026-04-20 起)才進入平台功能演進(時代 B)。

---

## 里程碑敘事

### A-1 · `AgenticRAG` — Agentic RAG 引擎雛形,也是 ANILA 名稱的起點

`AgenticRAG` repo 的預設分支為 `main`(HEAD),共 15 個 commit,時間跨度 2026-04-05 → 2026-04-24(最後一筆為封存通知)。它的演進節奏是「先 RAG 管線,再長出 agentic 工具迴圈,最後長成完整 agent 框架」:

- **2026-04-05 `b7a8f72` Initial commit** → **2026-04-06 `d9025e3` 初版 ANILA Core**:第二個 commit 就把 package 命名為 `anila-core`,定位「Python agent runtime focused on Agentic RAG orchestration」。這是 **ANILA 名稱與 runtime 基座的原點**。
- **v0.2.0(`544e1b1`,2026-04-07)** 完成 Agentic RAG 後端;同日 `89a8449` 新增 **OpenWebUI 相容 RAG proxy(`api.py`)**,這支獨立 surface 日後在 monorepo 內成為「第一個可註冊 agent 模板」。
- **v0.3.0(`d462cfe`/`60f1422`,2026-04-07)** 導入 **tool-driven RAG loop**:LLM 自主決定何時搜尋、搜什麼、是否多輪檢索,並提供 `vector_search` / `keyword_search` / `read_document` 三個 RAG 工具、Layer 3 滑動窗口壓縮。README 標題此時明確寫著「**ANILA Core 是一個面向 Agentic RAG 的 Python Runtime**」。
- **2026-04-09 `00efc41`** 加入 **Hybrid Search**(語意 + 關鍵字);**2026-04-13 `4d31446`** 加入協調者/文檔撰寫者/任務分析師/研究者/審核者等多個 agent 配置 —— 框架的「多 agent 協調」雛形至此齊備。
- **2026-04-24 `d48d219`** 由作者 1147259 落下 **封存通知**:README 改寫為「此 repo 已封存,主線開發已遷移至 ANILA 平台 monorepo,升格為官方 RAG agent template」。這是前身正式交棒的墓誌銘。

依 `src/anila_core/` 模組樹佐證其「引擎」本質:`engine/`(query_engine、budget_tracker、rag_preprocessor)、`coordinator/`、`registry/agent_registry`、`router/tool_router`、`compact/`(auto/micro/session_memory/sliding_window)、`memory/`(extract/consolidation/relevance)、`ingestion/`(chunker/parsers)、`providers/`(embedding_nvidia、openai_compat)、`storage/adapters/`(pgvector_store、postgres_store)。

> **旁支(未併入 `main`,共 4 個 commit)**:`AgenticRAG` 另有兩條實驗分支 —— `codexdo`(`1edf404`,部署資產)與 `claude/agentic-rag-framework-DJ8tK`(`d1d5cc2` reshape 為專職框架、`d94d9a8` CJK tokenizer + cross-encoder reranker、`e930ec0` reranker 測試 + Phase 4 計畫)。這些改動未進入 `main`,故未隨主線匯入 ANILA;列於下表「旁支」段以求歷史完整。

### A-2 · `myCSPPlatform` — CSP 治理平台雛形

`myCSPPlatform` 的預設分支為 `claude/csp-platform-setup-1aYVc`(HEAD),共 10 個 commit,2026-04-10 → 2026-04-14。README 自述為「**Cloud Service Platform —— 企業內部 AI 模型服務的統一管理平台**」:集中式介面管理 LLM / Embedding / VLM / Agent 四類模型的存取權限、以 `sk-` 前綴發放 OpenAI 格式 API Key、OpenAI 相容代理(`/v1/chat/completions`、`/v1/embeddings`,SSE)、用量追蹤、使用者/部門/自助註冊、JWT + LDAP + OIDC 認證、告警中心、審計日誌、平台卡片。技術棧 Vue 3 + FastAPI + SQLAlchemy + PostgreSQL 16 + Nginx。

演進節奏:

- **`a2cd232`(2026-04-10)** 初始版本 → 同日 `8f9e7cc` 模型自動註冊 + Docker 部署、`af5741a` **Agent 模型類型 + MLSteam 整合**、`4b585cd` Nginx 反向代理、`c2a8f66` 生產級架構升級(healthcheck、`MODEL_*` env)。首 6 個 commit 由「Claude」署名(AI 產生的鷹架)。
- **`d16cb1a`(2026-04-11)** PostgreSQL 遷移 + Admin 重設密碼。
- **2026-04-12 起** 作者改為 Hsiu-Ying,Kung(人類接手):`c27603d` 使用者認證系統(JWT / 密碼管理 / SQLite→PostgreSQL 遷移)、`61c4987` 認證/註冊/API Key 模組 + 前後端、`15ec1ba` 核心後端 API 模組 + 前端服務層。
- **`5f38cb4`(2026-04-14)** Nginx 強制 HTTPS + 新增 SSL 憑證。

其 `backend/app/api/` 已具備 `api_keys`、`models`、`proxy`、`usage`、`users`、`departments`、`auth_providers`、`audit_logs`、`platform_links` 等端點 —— 這批治理端點日後幾乎原封成為 monorepo 的 `myCSPPlatform/` 控制/資料平面。

### A-3 · 匯流:2026-04-17,ANILA 於單一 monorepo 誕生

當天 5 個 commit(全由 Hsiu-Ying,Kung 提交)構成 ANILA 的創世序列。依 diff 校正 commit 訊息後,實際發生的事:

1. **`0eaf3d5`(root)** —— 以 submodule 指標把數個候選前身登記進新 repo(每筆僅 1 行 gitlink)。其中真正被採納的是 `myCSPPlatform` 與 `AgenticRAG` 兩者;另有一個評估標的在下一個 commit 即被移除、其程式碼從未併入,故不列入本史。
2. **`2e96978` 匯入兩前身完整程式碼** —— 一次 **210 檔、25,908 行插入**,把 `AgenticRAG/` 與 `myCSPPlatform/` 兩整棵目錄實體化進 monorepo(同時刪去上一步那個未採納指標)。這是「合併」真正落地的一刻。
3. **`ec235b3` 新增專有授權** —— repo 根加入 `LICENSE`(Proprietary)。
4. **`f1efea7`「finish phase1-3」** —— 真正的整合程式碼:新增 **`anila-core-router/`**(`main.py` + Dockerfile,OpenAI 相容的 dispatcher 入口)、**`smoke-openai/`**(整合 smoke 測試)、串起 monorepo `docker-compose.yml`,並微調 `AgenticRAG/` 與 `myCSPPlatform/`(共 30 檔、1,752 行插入)。
5. **`f902809`** —— commit 訊息宏大(「多租戶 AI 平台架構、開發者生態、SDK、agent registry 拆分、身分驗證授權、OpenAI 兼容路由器入口」),但其 diff **僅新增一份 `anila_plan.md`**。也就是說:那段架構描述是**計畫的願景**,程式碼落地留待時代 B;此 commit 的貢獻是把該藍圖寫成文件。

**目錄血緣(有據可查)**:

| monorepo 目錄(genesis 當時) | 來源 | 角色 |
|---|---|---|
| `myCSPPlatform/` | `myCSPPlatform` 前身(全量,`2e96978`) | CSP 控制/資料平面(API Key、模型註冊、代理、治理) |
| `AgenticRAG/`(內含 `src/anila_core/`) | `AgenticRAG` 前身(全量,`2e96978`) | RAG 引擎 + runtime foundation package;`api.py` 為首個可註冊 agent 模板 |
| `anila-core-router/` | 新建(`f1efea7`) | 用 `anila-core` SDK 打造、配置成 dispatcher 的 OpenAI 相容 Router 入口(非 from scratch) |
| `smoke-openai/` | 新建(`f1efea7`) | OpenAI 相容整合 smoke 測試 |
| `docker-compose.yml` / `anila_plan.md` | 新建 | monorepo 編排 + 整合計畫願景 |

`anila_plan.md` 的關鍵認知也印證了血緣:「`AgenticRAG` 的 Python package `anila-core` 是平台 SDK foundation,ANILA Core Router 是用該 SDK 打造、配置成 dispatcher 的新 service」;並定調 **CSP = data plane**(所有 LLM/agent 流量經 CSP proxy、Router/agent 不持有 upstream key)、**registry 拆兩張表**(`model_registry` 管原始模型、新增 `agents` 表管 endpoint-based agent)。這正是日後 ANILA 的骨架。

> **雜湊穩定性註記**:現行 `main` 的 genesis 雜湊是 2026-04-27 一次 `git-filter-repo` 歷史重寫**之後**的值。早期文件(如 `AgenticRAG` 封存 README)所引用的遷移 commit `c4bf85a` / `9d5b052` / `59f05f6` 為重寫前雜湊,對應現行 `main` 上同名的 `7978c5c` / `1e85c7f` / `c45aca4`。交叉比對舊文件雜湊時須留意此落差。

---

## 完整 commit 對照表

### 前身一:`AgenticRAG`(HEAD = `main`,15 commits)

| hash | 日期 | 摘要 |
|---|---|---|
| `b7a8f72` | 2026-04-05 | Initial commit |
| `d9025e3` | 2026-04-06 | 初版 ANILA Core 實作(pyproject `name = anila-core`,Python agent runtime) |
| `49153f7` | 2026-04-06 | 合併 `main` 分支 |
| `ba20804` | 2026-04-06 | 擴充 README + 新增繁中章節 |
| `b797360` | 2026-04-06 | 修 `tool_result` 訊息序列化 + 加 e2e smoke test |
| `544e1b1` | 2026-04-07 | 完成 Agentic RAG 後端 v0.2.0 |
| `89a8449` | 2026-04-07 | 新增 OpenWebUI 相容 RAG proxy(`api.py`)+ 更新 README |
| `ca1bcca` | 2026-04-07 | system prompt 指南擴充為三層 |
| `d462cfe` | 2026-04-07 | tool-driven RAG loop + RAG tools + compact L3 |
| `60f1422` | 2026-04-07 | README 更新為 v0.3.0 AgenticRAG 功能 |
| `17aea33` | 2026-04-09 | `index_documents.py` 加文件管理(列出/刪除已索引) |
| `00efc41` | 2026-04-09 | 新增 Hybrid Search(語意 + 關鍵字)+ 更新顯示格式 |
| `1652161` | 2026-04-10 | remediation:history parsing / scope isolation / error SSE / MemoryFileStore |
| `4d31446` | 2026-04-13 | 新增協調者/文檔撰寫者/任務分析師/研究者/審核者等 agent 配置 |
| `d48d219` | 2026-04-24 | docs:封存通知 —— 已遷移至 ANILA monorepo（升格為官方 RAG template） |

**旁支(未併入 `main`,不隨主線匯入 ANILA;列此以求完整)**

| hash | 日期 | 分支 | 摘要 |
|---|---|---|---|
| `1edf404` | 2026-04-14 | `codexdo` | 新增部署資產與設定檔 |
| `d1d5cc2` | 2026-04-22 | `claude/agentic-rag-framework-DJ8tK` | reshape 為專職 AgenticRAG framework |
| `d94d9a8` | 2026-04-22 | `claude/agentic-rag-framework-DJ8tK` | 新增 CJK-aware tokenizer + cross-encoder reranker |
| `e930ec0` | 2026-04-22 | `claude/agentic-rag-framework-DJ8tK` | reranker 測試支援 VllmScoreRerankerProvider + Phase 4 計畫 |

### 前身二:`myCSPPlatform`(HEAD = `claude/csp-platform-setup-1aYVc`,10 commits)

| hash | 日期 | 摘要 |
|---|---|---|
| `a2cd232` | 2026-04-10 | CSP Platform 初始版本 —— AI 模型服務管理平台 |
| `8f9e7cc` | 2026-04-10 | 模型自動註冊機制 + Docker 統一部署範例 |
| `af5741a` | 2026-04-10 | Agent 模型類型支援 + MLSteam 整合 |
| `4b585cd` | 2026-04-10 | Nginx 反向代理 + `.env` 統一配置管理 |
| `c2a8f66` | 2026-04-10 | 生產級架構升級(Nginx 強化、healthcheck、`MODEL_*` env) |
| `d16cb1a` | 2026-04-11 | PostgreSQL 遷移 + Admin 重設密碼 + README 重寫 |
| `c27603d` | 2026-04-12 | 使用者認證系統(JWT、密碼管理、SQLite→PostgreSQL 遷移) |
| `61c4987` | 2026-04-12 | 認證/註冊/API Key 管理模組 + 前端視圖 + 後端服務 |
| `15ec1ba` | 2026-04-13 | 核心後端 API 模組 + 前端服務層 |
| `5f38cb4` | 2026-04-14 | Nginx 強制 HTTPS + 新增 SSL 憑證與私鑰 |

### ANILA `main` 創世(5 commits,cutoff 於 `f902809` 含)

| hash | 日期 | 摘要 |
|---|---|---|
| `0eaf3d5` | 2026-04-17 | **root** —— 統整前身專案為單一 repo(登記 submodule 指標) |
| `2e96978` | 2026-04-17 | 匯入兩前身完整程式碼(`AgenticRAG/` + `myCSPPlatform/`,210 檔) |
| `ec235b3` | 2026-04-17 | 新增專有授權 `LICENSE` |
| `f1efea7` | 2026-04-17 | finish phase1-3:新增 `anila-core-router/` + `smoke-openai/` + `docker-compose` 整合 |
| `f902809` | 2026-04-17 | 新增 ANILA 平台整合計畫 `anila_plan.md`(多租戶 / Router / Registry 願景) |

> **時代交界宣告**:時代 A 收束於 **`f902809`(含)**。時代 B 從 **`7ef38f6`(2026-04-20,`feat: Add agent management …`)** 續接,`main` 完整演進表由時代 B 負責(genesis 5 筆容許輕微重疊)。

---

## 本時代結束時的系統樣貌(2026-04-17)

一個 monorepo,四塊拼圖各就各位、尚未真正咬合:

- `myCSPPlatform/` —— 可運作的 CSP 治理面板(API Key、模型註冊、代理、認證、用量),但還不知道 agent 的存在。
- `AgenticRAG/` —— 內含 `anila_core` runtime 基座 + tool-driven RAG 引擎 + `api.py`(OpenWebUI 相容 surface)。
- `anila-core-router/` —— 剛出生的 OpenAI 相容 dispatcher 骨架,尚未接上主 LLM 分派邏輯。
- `smoke-openai/` + `docker-compose.yml` —— 整合驗證與編排的最初鷹架;`anila_plan.md` 則寫下了「LLM-as-Router + Agent Registry + 開發者生態」的多租戶願景。

換言之:**程式碼已合併,平台尚未整合**。把 CSP 的治理、AgenticRAG 的引擎、Router 的分派真正縫成一個多租戶 AI 平台 —— 那是時代 B 的故事。ANILA 的名字與靈魂已定,骨架待長。
