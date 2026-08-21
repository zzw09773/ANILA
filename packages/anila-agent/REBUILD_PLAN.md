> ⚠ **2026-08-01 P2.1**：下文重建藍圖若仍寫靜態 `csk-`／`X-CSP-Service-Token` 上手，該段已過時；認證方向已改派工 JWT＋JWKS（見本檔後續已改寫之認證條目與 `docs/guides/developer-guide.md`）。

# anila-agent 重建計畫（完整移植版）

> 版本：2026-06-14 · **狀態：P0–P5 全部完成並 live 驗證、對抗式複核後修畢**
>
> 成果：58 模組 / ~3293 LOC / 137 單元 + 1 live 測試全綠 / ruff clean / 離線安裝已驗。
> 對抗式複核 27 findings → 16 confirmed（1 high / 5 medium / 10 low）全部修畢
> （唯 #7 低風險、屬平台契約檔忠實移植不動，已記錄）。詳見 CHANGELOG.md。
> 過程兩大工程發現：自架端點拒 `strict` 工具欄位（compat 層 wire 剝除）；
> output_type(json_schema)+tools 在自架 reasoning 模型不可靠（改 prompt 行內引用 + 結構化側查詢走 json_object）。
> 基於對 `anila-agent_new/`（三參考框架）的深度分析 + git 救回的舊樣板平台契約 + 內網 live 端點實測。

---

## 0. 目標與定位

`anila-agent` 重新建構為一個 **clone-and-run 的 Agentic RAG 起手樣板**，同時兼任 **ANILA 平台官方可下載模板**。
舊架構（187 檔、~80 個手工 harness 模組）已整個清除，只保留 `anila-agent_new/`（參考）。本次為**乾淨重建**，不在舊結構上修補。

部署目標：**air-gapped / 內網**，走 OpenAI-compatible 端點。**無真實 OpenAI API、無閉源 binary、無外部 CDN**。

> **內網現況（2026-06-14）**：正式內網**只有 gpt-oss-20b + NV-embed-V2**，**gemma4 尚未到位**（dev box 上跑的 gemma4 屬試用，非交付目標）。
> 因此 **預設 LLM = gpt-oss-20b**（生成 + 結構化側查詢一顆搞定）；**gemma4 = 未來長 context 選項**，上線後才走其 reasoning 處理路徑。

---

## 1. 核心判斷：這是一次「薄」的重建

舊樣板在 openai-agents **0.3.0** 的貧瘠期，手工從 Claude Code 移植了一整套 harness。
參考的 SDK 是 **0.17.5** —— 跨了一個世代，已**原生內建**其中絕大多數。重建的價值是「設定 SDK + 保留平台契約 + 只留 SDK 缺的差異化」，而非再刻一次。

### 三參考框架角色

| 參考 | 本質 | 角色 |
|------|------|------|
| **openai-agents-python v0.17.5** | OpenAI Agents SDK，provider-agnostic | **唯一 runtime 基座**（import 進來直接用） |
| **antigravity-sdk-python** | Google Antigravity（Gemini-only + 閉源編譯 binary） | **僅 API 人體工學參考**，不能當基座、不能 air-gap |
| **claude_source_code** | Claude Code 反編譯 TS（1332 檔，不可執行） | **harness 概念參考**（memdir / slash / output-style） |

### runtime base 決策

- 基座 = **OpenAI Agents SDK v0.17.5**，pin 死。
- air-gapped vLLM 路徑（**無 OpenAI 依賴、無 LiteLLM**）：
  ```python
  client = AsyncOpenAI(base_url="http://gpt-oss-20b:8000/v1", api_key="EMPTY", http_client=<verify-aware httpx>)
  model  = OpenAIChatCompletionsModel(model="gpt-oss-20b", openai_client=client)  # keyword-only；預設 LLM
  agent  = Agent(..., model=model, model_settings=ModelSettings())               # 明確 plain settings
  ```
  > 驗證自 `openai-agents-python/src/agents/models/openai_chatcompletions.py:49`。
- LiteLLM 為 **opt-in `[litellm]` extra**，僅在需要統一 routing/cost 時用。

---

## 2. 決策定案

| # | 決策 | 選定 | 理由 |
|---|------|------|------|
| 範圍 | **完整移植版** | 全差異化一次做齊（skills loader、/deep-research、cost 貨幣層、output styles、triggers、Postgres Session） | user 指定 |
| 多代理拓撲 | **單一 cited agent 預設 + `/deep-research` opt-in** | 預設最省 token 最好 reason；多代理 pipeline 當可選 slash command 示範 | 實測友善 |
| 工具政策 | **deny-all 預設 + 明列 allow read-only** | 反轉 Antigravity 開放預設；中科院內網/air-gap 未匹配呼叫一律拒絕 | 強化姿態 |
| 選擇器模型 | **hybrid：NV-embed 粗篩 + gpt-oss-20b 精選**（見 §3） | 內網 live 實測定案 | 實測 |
| Provider 預設 | direct `AsyncOpenAI` + `OpenAIChatCompletionsModel`（baked） | air-gap 答案明確，無重依賴 | — |
| **預設 LLM** | **gpt-oss-20b**（生成 + 結構化側查詢）；gemma4 為未來長 context 選項 | 內網現只有 gpt-oss + nv-embed，gemma 未到；且 gpt-oss 結構化輸出最可靠 | 內網現況 |
| 短期記憶 | `SQLiteSession` 預設 + Postgres Session 可選（baked） | clone-and-run 零依賴 | — |

---

## 3. 實測：memdir recall 選擇器策略

內網 live 端點（gemma4 / gpt-oss-20b / NV-embed-V2）+ 25 條真實記憶 + 12 條查詢實跑：

| 方法 | recall@5 | MRR | precision | 延遲/q |
|------|---------:|----:|-----------|-------:|
| gpt-oss-20b 選擇器 | **1.000** | **1.000** | 優 | 0.36s |
| NV-embed-V2 embedding | 0.958 | 0.917 | 差（雜訊） | **0.11s** |
| gemma4 選擇器 | 0.500 | 0.500 | 一半回空 | 4.95s |

**定案：兩階段 hybrid recall（fail-closed）**

1. 粗篩：NV-embed → 25 條取 top-8（recall@5=0.96，右答案幾乎必在）
2. 精選：gpt-oss-20b 從候選挑 ≤5（小清單 precision 滿分，殺雜訊）
3. 退場鏈：LLM 掛 → 退 embedding 粗篩；embed 掛 → 退 frontmatter 關鍵字掃描
4. **不用 gemma4 當選擇器**（最慢又漏一半）—— 況且內網現無 gemma

**預設一顆 gpt-oss-20b 全包**：生成 + 選擇器精選 + guardrail 評審 + CitedAnswer 校驗，全走 gpt-oss-20b（0.36s/q、JSON 可靠）。

**重要：gpt-oss 與 gemma4 都是 reasoning 模型**（皆回 `reasoning` 欄位）。差別在推理冗長度/預算，非「是不是 reasoning」：
- gpt-oss-20b：推理簡短（~57 完成 token），content 穩定有值，max_tokens=64 都 `finish=stop` ✅
- gemma4：推理冗長（~235 token），`content=None` 直到推理完，小 max_tokens → `finish=length` 截斷成空 ❌

→ `content=None`-until-reasoning-done 是 **reasoning-model 類的通病，非 gemma 專屬**。故 `model.py` 的 reasoning 處理是**預設路徑 P0 責任**（不是 gemma 條件式）：
1. **max_tokens 下限 ≥512**（gpt-oss 推理短→不會多花，但擋換更囉嗦模型時靜默變空）
2. 解析結構化輸出前**剝 ```json fence**（gemma 會包 fence，gpt-oss 不會，防禦性處理）
3. 容忍/讀取 `reasoning` 欄位（確認 SDK OpenAIChatCompletionsModel 對 `reasoning`/`reasoning_content` 的映射）
4. parse 失敗一律 **fail-closed**
gemma4 上線（長 context cited 生成）時只需把 max_tokens 下限調更高 + 沿用同一套處理，無架構變更。

---

## 4. 目錄結構

```
anila-agent/
├── pyproject.toml              # openai-agents==0.17.5 pinned; extras [serving][pgvector][csp][litellm-opt-in]
├── README.md / README.en.md    # zh-TW 主 / en 副
├── .env.example                # 完整 env 契約 + fallback 註記
├── Dockerfile / Makefile / LICENSE / CHANGELOG.md
├── offline/                    # 離線輪檔包：pip download → --no-index 安裝腳本
├── configs/
│   ├── model.yaml              # base_url/api_key_env/model + settings allowlist
│   ├── agent.yaml              # instructions ref / max_turns / output_type / persona
│   ├── tools.yaml              # 工具清單 + 能力表 name→{read_only|write|admin} + policy ref
│   ├── memory.yaml             # sessions backend + memdir on/off + caps
│   └── policy.yaml             # deny-all base + allow read-only rules
├── anila_agent/
│   ├── __init__.py / main.py / config.py
│   ├── runtime/
│   │   ├── model.py            # build_model(): AsyncOpenAI(verify-aware)+OpenAIChatCompletionsModel; 預設 gpt-oss-20b; chat_completions; tracing off; reasoning-model 處理(max_tokens≥512 下限+剝 fence+fail-closed parse) — 預設路徑必做
│   │   ├── agent_factory.py    # build_agent(): 組裝 + retriever 優先序 + fail-closed read-only 守衛
│   │   └── run.py              # Runner.run/run_streamed 包裝 + RunState persist/resume (schema 1.10)
│   ├── retrieval/  ← 平台契約逐字保留
│   │   ├── base.py             # Retriever Protocol (runtime_checkable)
│   │   ├── schemas.py          # Document{id,text,score?,metadata}
│   │   ├── anila_pgvector.py   # halfvec text-cast / SET LOCAL RLS / chunk_type='leaf' / embedding_dim auto
│   │   ├── csp_http.py         # POST /api/ingestion/collections/{id}/search
│   │   ├── pgvector.py         # generic langchain_postgres（可選）
│   │   ├── dummy.py            # 零基建 fallback
│   │   └── from_env.py         # 優先序 + partial-config raise 歸因保留
│   ├── tools/
│   │   ├── rag_tools.py        # @function_tool search_documents(k clamp 1..20)/read_document; set/get_retriever
│   │   └── context.py          # AnilaRunContext(user/collection/token) 注入，對模型隱藏
│   ├── policy/  ← Antigravity 人體工學，純 Python
│   │   ├── dsl.py              # allow/deny/ask_user 優先序桶 + 能力表查詢，fail-closed
│   │   └── guardrail.py        # 單一 SDK tool-input-guardrail 入口
│   ├── memory/  ← memdir 差異化（SDK 無）
│   │   ├── memdir.py           # MEMORY.md 索引(200行/25KB cap) + per-topic *.md + 路徑驗證
│   │   ├── taxonomy.py         # user/feedback/project/reference + Why/How body schema
│   │   ├── recall.py           # hybrid：embed 粗篩 → gpt-oss 精選，fail-closed 退場鏈
│   │   ├── extract.py          # Stop-hook forked 自動存 + 互斥 + 去敏（剝 token/X-ANILA-User-*）
│   │   └── freshness.py        # mtime 相對齡標記 + verify-before-assert 漂移提醒
│   ├── prompts/
│   │   ├── system.md / agent.md / tool_policy.md   # zh-TW persona
│   │   └── builder.py          # system + MEMORY.md 索引 + output style 組裝
│   ├── cli/  ← Claude Code UX（SDK 無）
│   │   ├── app.py              # REPL over run_streamed
│   │   ├── slash_commands.py   # frontmatter+body → agent input macro（含 /deep-research）
│   │   ├── output_styles.py    # .md persona（zh-TW formal / concise-cited）
│   │   └── renderer.py         # 串流 text/thoughts/tool-call 分類渲染
│   ├── serving/  ← 平台契約逐字保留
│   │   ├── service_wrapper.py  # FastAPI: GET /health, GET /v1/models(model_type=agent), POST /v1/chat/completions
│   │   └── auth.py             # P2.1: verify dispatch JWT via JWKS (fail-closed); identity in claims
│   ├── orchestration/  ← 完整移植：多代理
│   │   └── deep_research.py    # planner→parallel-retrieve→writer→verifier（/deep-research 觸發）
│   ├── observability/
│   │   ├── hooks.py            # RunHooks/AgentHooks：檢索稽核 + per-user token 計量
│   │   └── cost.py             # token→currency rollup（Gemma 價格 unknown 時標註 caveat）
│   ├── skills/
│   │   ├── loader.py           # SKILL.md frontmatter-always/body-on-demand + 不信任 shell 信任邊界
│   │   └── examples/*.md
│   └── triggers/               # 完整移植：事件觸發（Antigravity triggers 對應）
│       └── runner.py
└── tests/
    ├── test_retriever_protocol.py   # Protocol 一致性 + Document 形狀
    ├── test_anila_pgvector.py       # SQL 形狀（halfvec/SET LOCAL/leaf）mock
    ├── test_csp_http_retriever.py   # 精確 request/response 形狀
    ├── test_service_auth.py         # fail-closed token verify + header trust
    ├── test_precedence.py           # 優先序 + csp>pgvector tie-break + partial-config raise 歸因
    ├── test_model_airgap.py         # 強制 chat_completions + tracing disabled + keyword ctor
    ├── test_policy_buckets.py       # deny-all + read-only allow + 能力表
    ├── test_runstate_resume.py      # HITL 暫停/恢復
    └── test_memdir.py               # caps / hybrid recall / 去敏 / freshness
```

---

## 5. 原生 vs 自製 決策

| 關切 | 決策 | 依據 |
|------|------|------|
| 短期對話記憶 | **用原生** Session（SQLite 預設；平台另提供 Postgres Session） | 4-method protocol；不用 OpenAI-managed（會打 OpenAI） |
| context 壓縮 | **自製** summarizing Session wrapper | 原生 compaction 硬綁 OpenAI Responses，Gemma 不通 |
| 工具審批 / HITL | **用原生** `needs_approval` + RunState 可序列化暫停/恢復 | 比手刻強：跨 HTTP request 恢復 |
| 工具政策人體工學 | **混合**：純 Python DSL 餵進單一原生 tool-guardrail | 借 Antigravity 優先序桶 + Claude Tool(specifier) 文法 |
| 長期跨 session 記憶 | **自製 memdir** | SDK 無；旗艦差異化 |
| model 重試/backoff | **用原生** `retry_policies` | drop 舊 providers/retry.py |
| guardrails（PII/注入/scope/未接地） | **用原生** input/output/tool guardrails | decorator + tripwire；判定走 gpt-oss |
| MCP client | **用原生**（stdio/sse/http + tool filtering） | 對應 agent 綁定 collection 最小權限（任務內回呼複用派工 JWT） |
| tracing | **混合**：原生 spans/RunHooks，**必須 disable 預設 exporter** | 預設 exporter 會 POST platform.openai.com → air-gap 洩漏/401 |
| cost 貨幣 | **自製** 薄價格表層 | SDK Usage 只有 token；Gemma 價格 unknown 時標註 |
| slash command / output style | **自製** CLI 層 | SDK 無此概念 |
| skills | **自製** 薄 SKILL.md loader | 原生 skills 綁 sandbox runtime |
| FileSearchTool / Sandbox / ShellTool | **跳過** | OpenAI-hosted / sandbox-coupled，air-gap 不能用 |

---

## 6. 平台相容契約（不可漂移，逐字保留）

> 漂移會對真實平台資料**靜默回傳空 / 錯誤 scope，且不報錯**。每項都配形狀斷言測試。

1. **Retriever Protocol** 不變：`async search(query,k=5)->list[Document]` + `async fetch(doc_id)->Document|None` + `name`/`metadata`。`set_retriever` isinstance-check。
2. **Document 形狀** 不變：`{id:str, text:str, score:float|None, metadata:dict}`。
3. **ANILA 原生 pgvector SQL** 逐字：依 id 讀 `embedding_dim`、truncate/pad 查詢向量、txn 內 `SET LOCAL anila.collection_id=<正整數>`（RLS）、`1-(embedding<=>$1::halfvec)` cosine、`WHERE chunk_type='leaf'`、halfvec 以文字傳 + 明確 `::halfvec` cast、預設 embed `nvidia/NV-embed-V2`。
4. **CSP HTTP 契約**：`POST {ANILA_CSP_BASE_URL}/api/ingestion/collections/{id}/search`（origin 非 /v1 proxy base）、Bearer、`{query,top_k,min_score}`、回 `results[]{chunk_id,content,score,chunk_key,document_id,filename,metadata}`。
5. **retriever 自動選擇優先序**：explicit arg > csp_http > anila_pgvector > generic pgvector > dummy；**csp_http 勝過 anila_pgvector**（base-url gate 區分）；CSP 漏設 base-url → csp 回 None → anila_pgvector RAISE（保留此**錯誤歸因**）。
6. **service-wrapper**：三端點形狀逐字；`/v1/models` 回 `model_type:'agent'`（CSP 註冊標記）；port 8200。
7. **認證方向（P2.1）**：驗 `Authorization: Bearer <派工 JWT>`（RS256／JWKS／`iss=anila-csp`／`aud=anila-agent`，fail-closed）；身分取自 claims（`user_id`／`department`／`agent_id`），**不**再以靜態 `csk-`／明文 `X-ANILA-User-*` 當信任根。開發者不領長效 agent 祕密。
8. **env 契約**：`ANILA_*`、`PGVECTOR_*`、`ANILA_CSP_*`、`CSP_BASE_URL`、`ANILA_CA_FILE`（勿設 `SSL_CERT_FILE`）、`ANILA_SSL_VERIFY`（1 預設；off 同時翻 litellm.ssl_verify）。**不再**要求 `CSP_SERVICE_TOKEN` 作為上手憑證。
9. **RAG 工具契約**：`search_documents` k clamp 1..20；`read_document` 回 None 是契約（chunk 已帶全文）非 bug。
10. **model.yaml settings allowlist**：只套已知 key，未知 key 靜默丟棄不報錯。

---

## 7. air-gap 三鐵律 + 安全姿態

**air-gap 鐵律**（`build_model` 必做，配測試 pin 死）：
- 強制 `chat_completions`（SDK 預設 Responses API + `gpt-5.4-mini`，vLLM 不實作）
- `set_tracing_disabled(True)`（預設 exporter POST 到 platform.openai.com）
- 傳明確 `ModelSettings()`（GPT-5 reasoning 欄位會洩漏到 Gemma）
- **`ANILA_SSL_VERIFY` 接進 build_model 的 AsyncOpenAI/httpx client**（自簽內網 vLLM，chat 路徑否則直接失敗 — 審查抓到的 P0 blocker）

**安全姿態（縱深防禦）**：
- **deny-all 政策預設** + 明列 allow read-only retrieval
- **工具能力表唯一真相來源**：SDK `@function_tool` **沒有 is_read_only** → 在 `configs/tools.yaml` 建 `name→{read_only|write|admin}`，由 policy/dsl.py + agent_factory 守衛消費（沒這張表，read-only-by-default 做不出來 — 審查頭號修正）
- **agent_factory fail-closed 啟動守衛**：偵測到 write/admin 工具但無對應 policy → 建構時 raise
- **memdir 自動存檔前去敏**：剝 `Authorization` Bearer／舊 `X-CSP-Service-Token`／`X-ANILA-User-*`／`csk-`/`sk-` 等 token 形狀，祕密不落地
- **memdir 路徑驗證**：拒相對/root/UNC/null-byte/裸-~ 路徑；排除 repo-committed 的 autoMemoryDirectory override

---

## 8. 分階段建置（完整移植版）

| 階段 | 目標 | 主要交付 | 依賴 |
|------|------|----------|------|
| **P0 骨架 + provider 脊柱** | clone-and-run 對本地 gpt-oss-20b 用 DummyRetriever 答得出來 | pyproject(pin)、runtime/model.py（預設 gpt-oss-20b + air-gap 鐵律 + SSL verify + reasoning-model 處理 max_tokens≥512/剝fence/fail-closed parse）、config.py、retrieval base/schemas/dummy、最小 agent_factory、cli/app.py、test_model_airgap（含 reasoning content=None 防護）| — |
| **P1 平台契約層** | 真正 drop-in 平台模板 | anila_pgvector / csp_http / pgvector / from_env（優先序 + raise 歸因）、rag_tools(k clamp)+context、serving(service_wrapper + auth fail-closed)、能力表、契約測試全套 | P0 |
| **P2 原生 harness 接線** | 靠 SDK 原生取代舊 harness | 自製 Session(SQLite + Postgres opt + 非OpenAI compaction)、policy dsl+guardrail(deny-all)、input/output/tool guardrails(走 gpt-oss)、RunState resume、observability hooks+cost、原生 MCP + tool filtering | P1 |
| **P3 差異化：memdir + UX** | 移植 SDK 缺的旗艦功能 | memdir 全模組（hybrid recall + 去敏 + freshness）、prompts/builder、cli slash/output-style/renderer、CitedAnswer + 接地 guardrail + chunk freshness 標記 | P2 |
| **P4 完整移植：多代理 + skills + triggers** | 補齊「完整版」差異化 | orchestration/deep_research（/deep-research）、skills/loader（不信任 shell 邊界）+ 範例、triggers/runner | P3 |
| **P5 強化 + 出貨** | 端到端驗證 + 離線出貨 | offline/ 離線輪檔包（pip download → --no-index 全相依閉包零網路）、Dockerfile/Makefile、e2e（CSP env → service_wrapper dispatch with 派工 JWT → cited answer；read+邏輯驗證，不 docker exec、觸發由 user 從 frontend 點）、README zh-TW/en、安全 pass | P4 |

---

## 9. 風險

- **RunState schema 1.10 是外部邊界**：持久化 HITL approval 綁此版本；pin SDK + 記錄 schema 版本；SDK 升級可能破壞已暫停的 approval（提供 drain/expire 政策）。
- **0.3.0→0.17.5 巨跳**：~230 exports，舊 import 全部不解析；一切對 0.17.5 真實介面重推。
- **reasoning-model 結構化輸出（預設路徑就會碰）**：gpt-oss 與 gemma4 都是 reasoning 模型，`content=None` until reasoning done。gpt-oss 推理短（57 tok）實務上安全，但這是模型而非端點保證；model.py 須 max_tokens≥512 下限 + 剝 ```json fence + fail-closed parse，否則換更囉嗦的 reasoning 模型會靜默回空。gemma4 推理 ~235 tok，上線時下限調更高。
- **平台契約漂移靜默且災難**：halfvec cast / leaf filter / SET LOCAL / 優先序 / CSP base 任一錯 → 空或錯 scope 不報錯；靠形狀斷言測試擋。
- **認證方向回歸（P2.1 已翻轉）**：agent **必須**驗平台派工 JWT（JWKS）；勿把靜態 `csk-` 上手路徑寫回文件或預設設定。RAG／trace 任務內回呼複用同一張派工 JWT。
- **離線安裝閉包**：openai-agents 拉 openai/httpx/pydantic 整棵樹；需 vendored wheelhouse 驗證零網路安裝（非只驗「沒拉 litellm」）。
- **input guardrail 只跑第一輪**：HITL resume 不重跑；per-retrieval 安全須用 tool-input guardrail，勿依賴 input guardrail 做後續輪 scope 檢查。
- **驗證紀律**：不 docker cp/exec 進 running 容器、不 CLI 模擬 job 觸發；service-wrapper dispatch 走 read+邏輯驗證 + user 從 frontend 觸發。

---

## 10. 測試矩陣（對應契約）

| 測試 | 釘住的契約 |
|------|------------|
| test_model_airgap | chat_completions 強制 / tracing disabled / keyword ctor / plain ModelSettings / reasoning content=None 防護(max_tokens 下限 + 剝 fence + fail-closed) |
| test_retriever_protocol | Protocol 一致性 + Document 形狀 |
| test_anila_pgvector | halfvec cast / SET LOCAL / chunk_type='leaf' / embedding_dim |
| test_csp_http_retriever | 精確 request/response 欄位名 |
| test_service_auth | fail-closed token verify + X-ANILA-User-* trust-after-verify + 不驗 JWT |
| test_precedence | 優先序 + csp>pgvector tie-break + CSP 漏設→pgvector raise 歸因 |
| test_policy_buckets | deny-all + read-only allow + 能力表查詢 |
| test_runstate_resume | HITL 暫停/恢復（schema 1.10） |
| test_memdir | caps / hybrid recall / 去敏 / freshness 數學 |
```
```
