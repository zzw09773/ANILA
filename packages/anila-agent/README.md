# anila-agent

> Agentic RAG 起手樣板：以 **OpenAI Agents SDK v0.17.5** 為 runtime 基座，
> 為 ANILA 中科院內網 **air-gapped** 部署強化。clone 下來填上 retriever 與 prompt 即可跑。

**繁體中文** · [English](README.en.md) · 完整重建藍圖見 [REBUILD_PLAN.md](REBUILD_PLAN.md)。

> 📌 **此檔屬 `prod` 分支(中科院內網部署版)**。anila-agent 本身是 sub-agent 模板,跟 prod 部署模式無耦合。內容與 main 一致。

## 設計重點

- **基座 = openai-agents 0.17.5**（pin 死）。provider-agnostic，指向本地 vLLM／OpenAI-compatible 端點；預設 LLM `gpt-oss-20b`。
- **air-gapped by construction**：強制 Chat Completions（vLLM 不實作 Responses API）、關閉 tracing exporter（不外連 platform.openai.com）、無外部 CDN、placeholder api_key、自簽 TLS 可關驗證、自架端點相容層（wire 層剝除 `strict` 工具欄位）。
- **reasoning 模型防護**：內網 LLM 皆為 reasoning 模型，`max_tokens` 自動夾到 ≥512；結構化輸出剝 ```json fence 且 fail-closed。結構化側查詢用 `response_format:json_object`（非不可靠的 json_schema）。
- **薄而非重**：SDK 已原生化大半 harness（Sessions/MCP/guardrails/retry/HITL），自製只留差異化。

## 功能總覽

| 分層 | 內容 |
|------|------|
| RAG 檢索 | Retriever Protocol + 四種後端（dummy / ANILA 原生 pgvector / 通用 pgvector / CSP HTTP），env 自動選擇優先序 |
| 工具政策 | **deny-all 預設** + 明列 allow 唯讀；工具能力表 + fail-closed 啟動守衛（餵進 SDK tool-guardrail） |
| 短期記憶 | SDK 原生 Session（SQLite 預設 / Postgres 選用 / 摘要壓縮） |
| 長期記憶 | **memdir**：typed taxonomy + 索引常駐 + 混合 recall（embed 粗篩 + LLM 精選，fail-closed）+ 去敏自動抽取 + 新鮮度標記 |
| HITL | RunState 序列化暫停/恢復（schema 1.10） |
| 多代理 | `/deep-research`（planner→平行檢索→writer） |
| CLI | 串流 REPL + slash 指令 + 可切換 output style |
| 擴充 | SKILL.md skills、事件 triggers、MCP client（config-gated） |
| 服務化 | OpenAI-compatible service wrapper（CSP 可派工，service-token 認證） |
| 觀測 | RunHooks 稽核 + token/cost 計量（自架價格未知時標 caveat） |

## 快速開始

```bash
git clone <repo> && cd anila-agent

make install                 # 建 .venv 並 editable 安裝（含 dev）
cp .env.example .env         # 填入 ANILA_BASE_URL / ANILA_MODEL
make test                    # 單元測試（不連網）
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

CLI 指令：`/help`、`/memory [查詢]`、`/style`、`/clear`、`/deep-research <問題>`、`/<configs/commands 的指令>`。

## 服務化（CSP 派工）

```bash
make install            # 已含 [serving]
make serve              # python app.py（先 load .env，再起 :8200）
# 或明確指定：
uvicorn anila_agent.serving.service_wrapper:app --host 0.0.0.0 --port 8200
```

CSP Router 以 `X-CSP-Service-Token`（csk-）派工；驗過才信 `X-ANILA-User-*`。未設
`CSP_SERVICE_TOKEN` 時 fail-closed 拒絕（本地測試設 `ANILA_ALLOW_NO_SERVICE_TOKEN=1`）。

## Full Trace（doc-05 §6，正式 L3 approval blocker）

本範本內建 `anila_agent/tracing.py`，示範 CSP 要求的完整 span 集，**衍生 agent 照抄即可**。
CSP dispatch 帶 `X-ANILA-Trace-Id` 時自動啟用：把 `agent.run/step/model_call/tool_call/`
`retrieval/output/error` spans 批次（≤256/批）callback `POST {CSP}/v1/traces/{trace_id}/spans`，
憑證重用 agent 自己的 csk-（與 RAG 出向同一把）。**無 trace header 或無 endpoint → 完全停用、
零行為變化**；ship 失敗一律 drop-and-log，絕不讓 agent 掛掉。

自訂工具要保留 Full Trace，只需沿用既有接線，無需改工具本身：

- **模型/工具 span**：`service_wrapper` 已把 `AuditHooks` 包進 `TracingRunHooks`，
  openai-agents 的 `on_llm_*` / `on_tool_*` 事件會自動轉成 span——新增 `@function_tool`
  不必額外加碼，工具呼叫自動被追蹤。
- **檢索 span**：檢索走 `TracingRetriever`（包住 retriever），`search()` 前後自動送
  `agent.retrieval` span（含 `collection_ids`/`chunk_ids`/`top_k`）；換你自己的 retriever
  一樣包一層即可。
- **自訂子區段**：工具內若有想單獨追蹤的步驟，注入 emitter 後用
  `async with emitter.span("agent.tool_call", "my_step", attributes={...}) as sp: ...`，
  它會自動巢狀在當前 span 下（`sp.attributes[...]` 可補收尾屬性）。

env：`ANILA_TRACE_ENDPOINT`（預設 = `CSP_BASE_URL`）、`ANILA_TRACE_ENABLED`（預設 1）、
`ANILA_CLASSIFICATION_LEVEL`（隨 run/output span 帶出，滿足 doc-06 §6 分類等級必備項）。

## Docker / MLSteam 環境映像

搬進 air-gap 內網的另一條路：build 一顆「環境」image（套件 + JupyterLab，無源碼），上傳 MLSteam 由其建 Lab；源碼從 workspace clone。`make docker-build` / `make docker-save`（存 tar）/ `make docker-run`（本機起 JupyterLab，:8888）。完整流程見 [DOCKER.md](DOCKER.md)。

## air-gapped 離線安裝

`openai-agents` 會拉整棵相依樹。真 air-gap 用離線輪檔包：見 [offline/README.md](offline/README.md)。

## 開發狀態

P0–P5 全部完成並對本地 gpt-oss-20b / NV-embed-V2 端到端驗證（含 memdir 混合 recall、
deny-all 政策、多輪 session、deep-research、service wrapper）。詳見 [REBUILD_PLAN.md](REBUILD_PLAN.md)。

## License

Apache-2.0
