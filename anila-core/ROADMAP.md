# anila-core Roadmap — 引擎 / Runtime Foundation

> **子專案技術 roadmap**。`anila-core` 是整個 ANILA 的「主大腦」：LLM-as-Router（`anila-core-router` 載它的 app-factory）與每個註冊 agent（`anila-agent` 模板）全建在它上面。
> 平台/系統級前瞻見 [`../docs/ROADMAP.md`](../docs/ROADMAP.md)。本檔聚焦**引擎內部**：把 **v0.14.0 → 可被各單位安心建構的穩定 1.0 runtime foundation**。
> 依據：8 subsystem code-level 深讀 + 架構 critic（2026-06-30）。標記 `[tier/估時]`、`★`=foundation-critical。

## 健康快照
- **Runtime core（turn loop / lifecycle / approvals / guardrails / interrupt）是最成熟、最佳分解的部分**，已接近可重用 1.0 primitive（101/104 測試綠，3 個失敗只是缺 `aiosqlite` env，非邏輯）。
- 其餘子系統有真實債：**`router_server.py` 2594 行 god module**、**大量已匯出但沒接線的公開 API**（handoff/coordinator/fs-memory/GraphRAG retrieval）、**兩個 HIGH 安全/品質洞**、**測試套不能當 release gate**。

---

## 四大支柱

1. **宣告基座（Declare the foundation）**：凍結真正的公開 API（`__all__` / `_internal` / SemVer）、統一版本、讓每個匯出的 primitive 要嘛**有接線的 reference**、要嘛**誠實標 experimental**。今天公開面未宣告、且一大半是孤兒，**沒人能安心 build on 它**。
2. **撐得住自己的部署（Survive its own deployment）**：單一 air-gapped gateway（`.12`）現實要求 provider 層 retry/backoff、honored abort、gpt-oss 非收斂補救、typed terminal events——全部由**把 router god module 裡 3 個 inline httpx 收進（現乾淨但沒用的）provider 抽象**這個 keystone 重構解鎖（一個重構五處受益）。
3. **補兩個容器/品質洞**：`exec_bash` 容器化是**假承諾**（first-token allowlist + `shell=True` + 無 rlimits）；air-gapped ingestion **靜默壓低文件品質**（Docling 被擋、OCR 整篇替換、檢索 SQL 零測試）。兩者皆 HIGH、皆 deployment-defining。
4. **在真模型上驗證**：測試套綠化當 release gate、重構前先補 characterization tests、對 model-dependent 功能（抽取、reasoning-split、routing）建 eval/observability——它們今天在 gpt-oss/gemma 上**靜默 no-op**。

---

## Now — 0–2 週

### Phase 0 — 綠燈 + 凍結 seam（解鎖一切後續）
- ★ **統一版本**單一來源（`__init__.py` 寫 `0.7.0` / pyproject `0.14.0` / CHANGELOG `0.13.0` 三方不一致）`now/S`
- ★ **宣告 `__all__` + `_internal` 慣例 + deprecation policy**：凍結公開面（`create_router_app`、`create_app`、`QueryEngine/QueryConfig/RunHooks`、`ToolRegistry`、message types…）—— **一切重構的前提** `now/M`
- ★ **修綠測試套當 release gate**：stale `forwarded_headers` monkeypatch（TypeError）、pre-9-X chunker selector、G3 breach point；註冊 `live` marker `now/S`
- 檔案：`src/anila_core/__init__.py`、`pyproject.toml`、`CHANGELOG.md`、`tests/`

### Phase 1 — 兩個安全 HIGH + 便宜可靠性 + 修開發者 on-ramp（不需重構）
- ★ **`exec_bash` 容器化**[security HIGH]：first-token allowlist 在 `shell=True` 下可被 `cat x; rm -rf` 繞過（`shell.py:117,161`）→ 拒 shell metachar（allowlist 啟用時）或改 `create_subprocess_exec` + `setrlimit`（CPU/AS/FSIZE via preexec_fn）`now/M`
- ★ **PDF OCR fallback 改 per-page 非破壞性**[data-quality HIGH]：現在 `needs_ocr_fallback` 觸發時整份 `content = ocr_text`，丟掉所有 native 抽取（`ocr.py`）→ 保留好文字、影像占位、頁標記 `now/M`
- ★ **修 `anila-core register` 422**：CLI 沒送必填 `base_model_id`（`cli/register_cmd.py`）→ 以 name→id 查 `GET /api/models`；對齊 template/manifest `now/S`
- **max_tokens floor + temperature** 上每個 router outbound LLM payload（現 100% 靠上游 default）`now/S`
- **不再靜默吞 malformed tool-call JSON**（`query_engine.py:344-352` 設 `{}`）→ log + surface（這正是 gpt-oss 非收斂症狀）`now/S`
- **dispatch 邊界防禦解析**：驗 CSP 回應而非裸 index（`tools/dispatch_tool.py`）`now/S`
- 修 stale HierarchicalChunker test selector `now/S`

---

## Next — 季度

### Phase 2 — keystone provider/router 重構（需 Phase 0 凍結 + characterization tests）
- ★ **先寫 golden-master SSE + payload characterization tests**（重構前護網）`next/M`
- ★ **3 個 inline httpx → 走 `CSPPlatformProvider`**：`router_server.py:1191/1775/1914/2150` 各自重建 base_url/auth/streaming/timeout，而 `providers/cspplatform_provider.py` 已擁有這些 → 收斂後**同時縮 god module + 讓 provider 抽象成正式 production path**（支柱 2 的樞紐）`next/L`

### Phase 3 — runtime 韌性 + 可觀測 contract（需 Phase 2，事件流經單一 provider path）
- ★ **turn loop honor `abort_signal`**：`AgentContext.abort()` 已存在但 loop 從不檢查（`query_engine.py:148-256`）→ 加 abort 檢查 + FastAPI `is_disconnected`（client 斷線/stop 鈕才能中止 in-flight run）`next/M`
- ★ **max_turns 非收斂補救**：偵測到但從不補救（`query_engine.py:233-235`）→ 加 forced final-answer turn（`tool_choice='none'` 要模型收尾）+ 不同 user-facing signal `next/M`
- ★ **`_api_call` bounded retry-with-backoff** + split connect/read/total timeouts（provider 層）：現零 retry，一次 transient 錯誤丟掉整輪 `next/M`
- **typed terminal-reason 進 SSE/event contract**（completed｜max_turns｜aborted｜budget｜length｜error）`next/M`
- **合併 reasoning 正規化 + leaked-thought sanitizer 成單一 `providers/reasoning` module**（現 4 份分歧實作：router offline CJK-density、router streaming 重寫、openai_compat 丟棄、extraction `<think>` regex）`next/M`
- **structured-output helper**（`json_object` never-raise + pydantic 驗證）`next/M`
- `OpenAICompatProvider` 真的解 `reasoning_content` 並對齊 docstring（現 docstring 說有、code 沒做）`next/S`

### Phase 4 — 基座可信度 + 資料品質（可與 Phase 3 並行）
- ★ **wire-or-quarantine 孤兒 primitives**：handoff（`query_engine.py:406` raise `RunHandoff` 但無人 catch）、coordinator（沒接線且宣稱未實作的 SendMessage/TaskStop）、fs-memory backend、relation/parent_content retrieval consumer → 決定「接進 running path」或「移出公開 API 標 experimental」`next/M`
- ★ **擴展點文件 + 每個 extension point 一個 wired reference**：tool / retriever / chunker / provider / memory-adapter（讓單位真能 build on）`next/M`
- **真 `[docling]` extra + air-gap offline-weights runbook**：Docling 是唯一有 table-structure 還原 / xlsx・pptx / layout-aware 的 path，被 air-gap（下載權重）擋住——這是 ingestion 品質天花板的核心 `next/M`
- **pgvector parent/child + relation-expansion SQL 單元測試**（現完全沒測：similarity_search / per_document RANK·PARTITION / keyword_search）`next/M`
- **air-gap SDK 自身安裝 runbook + wheelhouse**（`pip download` → `--no-index --find-links`）`next/M`
- **extraction-yield observability**（extracted/empty/parse-fail 計數）+ 對真 gpt-oss/gemma 的 golden-output 回歸 fixtures `next/M`
- multi-hop dispatch 補 context forwarding（`context_messages`/`handoff_meta` 經 Router `_dispatch_safe`）+ visited-agent cycle detection + RemoteAgentRegistry per-key cache evict `next/S–M`
- 啟動 config 驗證 + 非互動 token-based CLI auth `next/M`

---

## Later — 策略（凍結 seam 後安全進行）

- **完成 `router_server.py` 解構**成 `sse_stream` / `dispatch_directive`（`_DISPATCH_RE`/`_parse_dispatch` 文字協定）/ `recompose` / `reasoning_sanitizer` / `routes` `later/L`
- **結構化 routing contract**（typed tool-call）並存現有 DISPATCH 文字協定 `later/L`
- **`EmbeddingProvider` Protocol + 真 nv-embed adapter** `later/M`
- **原生 table / xlsx / pptx fidelity**（markdown header separator、merged cells；DOCX 表格現以 ` | ` 拼接無表頭分隔）`later/L`
- HierarchicalChunker parent 帶 section summary / first-N-tokens（honour `max_parent_tokens`）`later/M`
- **GraphRAG retrieval consumer**（multi-hop）—— 與平台 `docs/ROADMAP.md` 的 GraphRAG 深化呼應 `later/L`
- Coordinator `SendMessage`/`TaskStop` 實作（或刪除宣稱）+ 共享 registry Protocol `later/L`
- 短期 `SqliteSession` history bound + 保留/壓縮故事（接 `compact/`）`later/M`
- **openai-agents SDK positioning 對齊**：providers reader 發現 **SDK 其實不在 `anila-core` 的 pyproject**（`anila-agent` 模板才用 0.17.5）—— 釐清「built on openai-agents」定位：包成 provider 或修文件 `later/L`

---

## 排序依賴（critic）
`Phase 0（綠燈+凍結）` → `Phase 1（安全/便宜可靠性/on-ramp，免重構）` → `Phase 2（keystone provider 重構，需 0 + characterization tests）` → `Phase 3（韌性，需 2）` ∥ `Phase 4（可信度/品質，可與 3 並行）` → `Phase 5/Later（解構 + 進階）`。

## 與平台 roadmap 的交叉項
- **關聯/parent_content retrieval consumer**（本檔 Phase 4 wire-or-quarantine）＝ 平台 `R-WIRE-1`（同一 lever，引擎側落地）。
- **ingestion 品質 / Docling / OCR**（本檔 Phase 1+4）＝ 平台 Later「ingestion/parsing 品質」天花板的**實作位置**。
- **`anila-core register` 422 修復**（本檔 Phase 1）＝ 平台「開發者生態」品質債。
- **reasoning sanitizer 合併 + max_turns 補救**＝ 平台 ANILA chat「tool-trace / 信任」體驗的後端基礎。

*依據：8-subsystem 深讀 + 架構 critic（api/router 子系統因 reader 觸及 structured-output 上限失敗，改由 critic top-refactors + providers/orchestration/runtime 三 reader 與直接讀碼補齊，行號已核）。本檔為前瞻 TODO；anila-core 已完成項併入根 `../CHANGELOG.md`。*
