# Changelog

## 1.0.0 — 2026-06-14

從零重新建構為以 **OpenAI Agents SDK v0.17.5** 為基座、為 ANILA air-gapped 內網強化的
Agentic RAG 起手樣板（取代舊的 0.3.0 手刻 harness）。

### Runtime / air-gap
- 直接 `AsyncOpenAI + OpenAIChatCompletionsModel` 指向本地 vLLM/OpenAI-compatible 端點；預設 LLM `gpt-oss-20b`。
- air-gap 三鐵律：強制 Chat Completions、關閉 tracing exporter、明確 ModelSettings。
- `ANILA_SSL_VERIFY` 接進 httpx；自架端點相容層（wire 層剝除 `strict` 工具欄位）。
- reasoning 模型防護：`max_tokens` 下限 512 + 結構化輸出剝 ```json fence + fail-closed 解析。

### 平台契約（逐字保留）
- Retriever Protocol + Document 形狀；ANILA 原生 pgvector（halfvec/SET LOCAL RLS/leaf）。
- CSP HTTP retriever；retriever 自動選擇優先序（csp > anila_pgvector > pgvector > dummy）。
- service wrapper（3 端點、model_type=agent）+ fail-closed service-token 認證（非 JWT）。

### Harness（SDK 原生為主）
- deny-all 工具政策 DSL + tool-input-guardrail + fail-closed 啟動守衛；工具能力表。
- 短期 Session（SQLite 預設 / Postgres 選用 / 摘要壓縮）；RunState HITL 持久化；觀測 hooks + cost。
- 原生 MCP client 接線（config-gated，static tool filter）。

### 差異化
- memdir 長期記憶：typed taxonomy + 索引常駐 + **混合 recall（embed 粗篩 + LLM 精選，fail-closed）** + 去敏自動抽取 + 新鮮度標記。
- CLI：串流 REPL + slash 指令 + 可切換 output style。
- 多代理 `/deep-research`（planner→平行檢索→writer）；SKILL.md skills；事件 triggers。
- 接地引用：concise-cited output style（行內【來源：id】，自架模型穩定）。

### 出貨
- 離線輪檔包（`offline/`，--no-index 安裝）；Dockerfile；120+ 單元測試 + live e2e。
