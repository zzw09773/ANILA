# anila-agent

> ANILA 官方 **air-gapped Agentic RAG 起手樣板**：以 **OpenAI Agents SDK v0.17.5** 為 runtime 基座，
> 為中科院內網（air-gapped）部署強化。clone 下來、填上 retriever 與 prompt 即可跑。
> 這是 CSP **Agent Registry** 核准上架的參考 runtime，原生內建 Full Trace。

**繁體中文** · [English](README.en.md) · 完整重建藍圖見 [REBUILD_PLAN.md](REBUILD_PLAN.md)。

## 在 redesign 版圖中的定位

- **位置**：monorepo `packages/anila-agent/`（§17.1 版圖：`services/` · `apps/` · `packages/` · `infra/`）。
- **獨立可攜**：只長一棵以 `openai-agents==0.17.5` 為根的相依樹，**刻意不依賴 `anila-core`**；整包可搬到
  MLSteam Lab 或任一內網主機獨立起服務。
- **對平台的角色**：由 `services/csp`（治理中心 CSP）的 **Agent Registry** 核准上架、由 `apps/csp-governance-ui`
  （治理中心前端）的開發者精靈註冊。原生 Full Trace 直接滿足 7 態審核的 `pending_trace_test` 關卡。
- **部署模式無耦合**：登入／部署 delta 落在各分支，樣板本身跨分支一致；不進平台 compose，以 agent
  endpoint 形式被 CSP Router 派工。

## 設計重點

- **基座 = openai-agents 0.17.5**（pin 死）。provider-agnostic，指向本地 vLLM／OpenAI-compatible 端點
  （內網即 Model Gateway，`.12`）；預設 LLM `gpt-oss-20b`。
- **air-gapped by construction**：強制 Chat Completions（vLLM 不實作 Responses API）、關閉 tracing exporter
  （不外連 platform.openai.com）、無外部 CDN、placeholder api_key、自簽 TLS 可關驗證、自架端點相容層
  （wire 層剝除 `strict` 工具欄位）。
- **reasoning 模型防護**：內網 LLM 皆為 reasoning 模型，`max_tokens` 自動夾到 ≥512；結構化輸出剝 ```json
  fence 且 fail-closed；結構化側查詢用 `response_format: json_object`（非不可靠的 json_schema）。
- **薄而非重**：SDK 已原生化大半 harness（Sessions／MCP／guardrails／retry／HITL），自製只留差異化。

## 功能總覽

| 分層 | 內容 |
|------|------|
| RAG 檢索 | Retriever Protocol + 四種後端（dummy／ANILA 原生 pgvector／通用 pgvector／CSP HTTP），env 依固定優先序自動選（`csp_http` > `anila_pgvector` > `pgvector` > `dummy`） |
| 工具政策 | **deny-all 預設** + 明列 allow 唯讀；工具能力表 + fail-closed 啟動守衛（餵進 SDK tool-guardrail） |
| 短期記憶 | SDK 原生 Session（SQLite 預設／Postgres 選用／摘要壓縮） |
| 長期記憶 | **memdir**：typed taxonomy + 索引常駐 + 混合 recall（embed 粗篩 + LLM 精選，fail-closed）+ 去敏自動抽取 + 新鮮度標記 |
| HITL | RunState 序列化暫停／恢復 |
| 多代理 | `/deep-research`（planner → 平行檢索 → writer） |
| CLI | 串流 REPL + slash 指令 + 可切換 output style |
| 擴充 | `SKILL.md` skills、事件 triggers、MCP client（config-gated） |
| 服務化 | OpenAI-compatible service wrapper（CSP 可派工，`csk-` service-token 認證） |
| 觀測 | RunHooks 稽核 + token／cost 計量 + **原生 Full Trace**（見下） |

## 快速開始

```bash
git clone <repo> && cd packages/anila-agent

make install                 # 建 .venv 並 editable 安裝（含 dev）
cp .env.example .env         # 填入 ANILA_BASE_URL / ANILA_MODEL
make test                    # 198 個單元測試（不連網）
make lint                    # ruff（anila_agent + tests）
make run                     # 啟動互動 CLI
```

`.env` 最小設定（內網現況：gpt-oss-20b）：

```ini
ANILA_BASE_URL=http://gpt-oss-20b:8000/v1
ANILA_MODEL=gpt-oss-20b
ANILA_API_KEY=EMPTY
ANILA_SSL_VERIFY=1     # 自簽內網憑證設 0
```

未設任何 retriever 時走內建 `DummyRetriever`（記憶體內、零基建），可立即對談驗證連通。

啟用差異化功能（opt-in）：

```ini
ANILA_MEMORY=1                 # 長期記憶 memdir（需 embed 端點）
ANILA_EMBED_BASE_URL=http://nv-embed-proxy:8000/v1
ANILA_CITED=1                  # 行內來源引用
ANILA_OUTPUT_STYLE=zh-tw-formal
```

CLI 指令：`/help`、`/memory [查詢]`、`/style`、`/clear`、`/deep-research <問題>`，以及
`configs/commands/*.md` 定義的 `/<檔名>`（範例：`/summarize`）。

## 服務化（CSP 派工）

```bash
make install            # 已含 [serving]
make serve              # python app.py（先 load .env，再起 :8200）
# 或明確指定：
uvicorn anila_agent.serving.service_wrapper:app --host 0.0.0.0 --port 8200
```

service wrapper 對外開 3 個端點：`GET /health`、`GET /v1/models`（manifest，標
`model_type=agent`）、`POST /v1/chat/completions`（主入口，含 streaming）。這個 `host:port`
就是註冊給 CSP 的 agent endpoint。

認證走 doc-08 的 `csk-` 單金鑰：CSP Router 以 `X-CSP-Service-Token`（`csk-`）派工，**驗過才信**
`X-ANILA-User-Id/-Email/-Groups`（Router 不轉發使用者 JWT，驗 `Authorization: Bearer <jwt>` 是錯的）。
未設 `CSP_SERVICE_TOKEN` 時 fail-closed 拒絕（本地測試設 `ANILA_ALLOW_NO_SERVICE_TOKEN=1`）。

## Full Trace（doc-05 §6 / doc-06 §6，L3 approval blocker）

本樣板原生內建 `anila_agent/tracing.py`，示範 CSP 要求的完整 span 集，**衍生 agent 照抄即可**。
CSP dispatch 帶 `X-ANILA-Trace-Id` 時自動啟用：把 `agent.run/step/model_call/tool_call/retrieval/`
`output/error` spans 批次（≤256/批）callback `POST {CSP}/v1/traces/{trace_id}/spans`（CSP 回 202），
憑證重用 agent 自己的 `csk-`（與 RAG 出向同一把）。**無 trace header 或無 endpoint → 完全停用、
零外送、零行為變化**；ship 失敗一律 drop-and-log，絕不讓 agent 掛掉。

三個接線元件，換自己的工具／retriever 一樣沿用即可，無需改核心：

- **`TracingRunHooks`**：把 `AuditHooks` 包起來，openai-agents 的 `on_agent_*` / `on_llm_*` / `on_tool_*`
  事件自動轉成 step／model_call／tool_call span；新增 `@function_tool` 不必額外加碼。
- **`TracingRetriever`**：包住任一 retriever，`search()` 前後自動送 `agent.retrieval` span
  （含 `collection_ids` / `chunk_ids` / `document_ids` / `top_k`）。
- **`TraceEmitter`**：緩衝 + 批次發送器；`async with emitter.span(...)` 可自訂子區段，自動巢狀在當前
  span 下（併發下以 `contextvars` 分艙）。

env：`ANILA_TRACE_ENDPOINT`（預設 = `CSP_BASE_URL`）、`ANILA_TRACE_ENABLED`（預設 1）、
`ANILA_CLASSIFICATION_LEVEL`（五級分類等級，隨 run／output span 帶出，滿足 doc-06 §8 trace-test 的
分類等級必備項）；`X-ANILA-Task-Id` 亦隨 run span 帶出以歸因到任務中心的 Task。

> **非 anila-agent runtime**（LangChain／custom HTTP）要接上同一條管線，見
> [`examples/trace-adapters/`](../../examples/trace-adapters/README.md) 的 copy-paste `AnilaTraceAdapter`。

## 註冊上架（CSP Agent Registry）

樣板跑得動只是第一步；agent 要進正式任務，須通過 Agent Registry 的 **7 態審核**
（`draft` → `pending_connection_test` → `pending_trace_test` → `pending_security_review` → `approved`，
另有 `rejected` / `disabled`）。兩條註冊路徑：

- **精靈**：治理中心 `apps/csp-governance-ui` 的 `/developer/agents` 兩步精靈 —— 填 endpoint／runtime
  type／分類上限 → 簽發 `csk-` → test-connection → trace-test。
- **CLI**：`anila-core register`（讀 `anila.yaml` → `POST /api/agents/register`），支援
  `--runtime-type` / `--classification-ceiling` / `--version` / `--draft`（影子註冊）旗標。

## Docker / MLSteam 環境映像

搬進 air-gap 內網的另一條路：build 一顆「環境」image（套件 + JupyterLab，無源碼），上傳 MLSteam 由其建
Lab；源碼從 workspace clone。`make docker-build`／`make docker-save`（存 tar）／`make docker-run`
（本機起 JupyterLab，:8888）。完整流程見 [DOCKER.md](DOCKER.md)。

## air-gapped 離線安裝

`openai-agents` 會拉整棵相依樹。真 air-gap 用離線輪檔包（`pip download` → `--no-index` 安裝）：見
[offline/README.md](offline/README.md)。

## 開發狀態

P0–P5 全部完成並對本地 gpt-oss-20b / NV-embed-V2 端到端驗證（含 memdir 混合 recall、deny-all 政策、
多輪 session、deep-research、service wrapper、Full Trace）。`make test` 收 **198** 個單元測試
（另有 1 個 `live` 標記測試需真實端點，共 199）。詳見 [REBUILD_PLAN.md](REBUILD_PLAN.md)。

## License

Apache-2.0
