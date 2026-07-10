# ANILA — GPT 報告審查與 `prod-intranet-card` 分支深度檢視

> 本文件三部分，可獨立閱讀：
>
> | Part | 主題 | 結論 |
> |---|---|---|
> | **Part 1** | 審查 GPT 的《ANILA 系統變更計畫書》 | 有條件核准（需修訂 8 項） |
> | **Part 2** | Agentic 執行流程（附圖）該怎麼落地、Agent 協議與 LangChain 相容 | 管線已有八成，缺接線而非新協議 |
> | **Part 3** | `prod-intranet-card` 分支獨立深度檢視 ＋ 與 GPT-5.6 報告對答案 | **同意 No-Go**；發現 1 條 GPT 低估的 CRITICAL，推翻 4 條假警報 |
>
> **若只讀一段：讀 Part 3 §3.2 的 CRITICAL-1。** 那是目前唯一「照 runbook 部署即成立」的分類資料外洩路徑，修法只需數小時。
>
> **本報告的結論已收斂成一份可執行的路線圖：[`docs/planning/anila-development-roadmap.md`](../planning/anila-development-roadmap.md)。** 若你要決定「接下來做什麼」，讀路線圖；若你要知道「憑什麼這樣說」，讀本報告。

---

# Part 1 — 系統變更計畫書審查

| 欄位 | 內容 |
|---|---|
| 審查對象 | `docs/gpt-report/ANILA_系統變更計畫書.md`（v1.0，2026-07-10，GPT 撰寫） |
| 審查基線 | `main@f661f5e8c9d2d70d4647ef3f67eb5b9624ffc93c`（與計畫書宣稱基線一致，已核對） |
| 審查方法 | 五路平行程式碼對照（Router／CSP／anila-agent／Ingestion／Shell）＋ 本機重建三套測試環境重跑基線 ＋ compose／分支／部署宣稱逐項核對 |
| 審查日期 | 2026-07-10 |
| 審查結論 | **有條件核准**——事實底座可信、架構方向正確；核准前需修訂 8 項（見 §5） |

---

## 1. 總評

這份計畫書是「讀過碼、跑過測試」寫出來的，不是腦補。全文 40+ 條可查證的技術宣稱，經逐條對照程式碼後 **絕大多數成立**，其中三套測試基線數字（anila-core 770 passed／6 failed／7 skipped、anila-agent 199 passed／1 skipped／9 個 Windows 路徑失敗、ingestion 146 passed）在本機 **一字不差重現**。唯一對不上的數字是 strict mypy 錯誤數（宣稱 24，本機 mypy 2.2.0 測得 61，判定為工具版本相依）。

計畫主張的工程原則——契約先行、結構化 RouteDecision 取代 regex、CSP 維持治理權威、fail-closed、additive migration、shadow→canary→authority——方向都正確，且 WS7 的 outbox／lease／generation 設計對應的都是本次審查獨立驗證為真的缺陷。

主要問題不在「說錯」，而在三處計畫層缺口：**人力工期假設與本團隊現實脫節**、**downstream 分支覆蓋只列一條（實際有六條）**、**未把「立即可修的止血項」與「架構演進」分離**。另有若干敘述精度偏差（§4），不影響結論但派工前應修訂。

---

## 2. 事實查核結果（依計畫書章節）

判定符號：✅ 證實　🟡 部分成立／措辭需修　❌ 與本機驗證不符

### §2.1 主大腦兩套執行路徑

| 宣稱 | 判定 | 證據 |
|---|---|---|
| 正式 Router 靠 `DISPATCH:` 字串＋regex 解析，無 structured 決策路徑 | ✅ | `router_server.py:196-202`（`_DISPATCH_RE`）、`:298-323`；LLM payload 只有 `{model, messages, stream}`（`:2126-2130`），全檔無 `response_format`／`tool_calls` |
| QueryEngine 已有 tool loop／approval／handoff／guardrails／lifecycle／session | ✅ | `query_engine.py:148-257`（tool loop）、`:411/:467`（pause/resume）、`:379`（handoff）；guardrails 實作在 `router/tool_router.py:208-282` 這一層，非 query_engine 本檔 |
| Coordinator 有 worker orchestration | ✅ | `coordinator.py:107-206`（spawn_worker／parallel／sequential，建構於 QueryEngine 上） |
| 兩者未成為正式 Router 執行核心 | ✅ | `router_server.py` 零 `from ..engine` import；dispatch 走 HTTP→CSP（`dispatch_to_agent_response`） |
| Router session 只保存有限訊息 | ✅ | 全檔僅 `:707-709` 一處 `add_items`（只存最後一則 user text） |
| caller 自帶 system message 時內部 routing prompt 不被注入 | ✅ | `router_server.py:726-730`——`has_system` 為真時 `routing_messages = messages`，控制 prompt 完全消失，無上游過濾 |
| `anila_multi_turn` 缺可靠上限 | ✅ | `:717` `max(1, int(body.get("anila_multi_turn", 1)))`——只有下限、無上限，caller 可傳任意大值 |
| 「registry 過期、未知 Agent、提示注入時沒有統一 fail-closed PolicyGate」 | 🟡 | 「無統一 PolicyGate」成立（全 anila-core 零 `PolicyGate`）；但 **unknown agent 其實已就地 fail-closed**（`:883-911` manifest 為 None 即拒發），真正 fail-open 的是 **stale registry**（`remote_agent_manifest.py:86-90` refresh 失敗只記 error、保留舊快取續跑）。原措辭易被誤讀為「未知 Agent 會被盲目派工」 |

### §2.2 CSP 有正式資料、Router 只看 legacy 投影

| 宣稱 | 判定 | 證據 |
|---|---|---|
| `models/agent.py`＋`schemas/contracts/agents.py` 承載 manifest／approval／health／ceiling／runtime／trace-test | ✅ | `models/agent.py:55-155` 六類欄位齊備；contracts 有 `AgentManifest:150`、`ApprovalStatus:60`、`TraceTestReport:191`。唯 health 在 contracts 層無封閉型別（僅 model 字串欄位） |
| `/v1/agents` 是較小 legacy 投影 | ✅ | `api/proxy.py:429-441` 只回 8 鍵（id/name/description_for_router/endpoint_url/capabilities/input_schema/requires_encryption…）；`remote_agent_manifest.py:17-27` 與之完全對齊；完整治理欄位只在控制面 `/api/agents`（`registration.py:107-183`） |
| CSP 知道可派工性、Router 無法先過濾 | 🟡 | 核心成立（health／ceiling／trace-test 不在投影），但 **approval 已在 DB 查詢層過濾**（`proxy.py:414,421-422` 只回 approved）——並非「全部列出」。未過濾的是 health |

### §2.3 anila-agent

| 宣稱 | 判定 | 證據 |
|---|---|---|
| pin `openai-agents==0.17.5` | ✅ | `pyproject.toml:12` |
| air-gap／fail-closed policy／CSP retrieval／per-user memory／tracing hooks | ✅ | `runtime/model.py:34-41`、`serving/auth.py:16-28`＋`policy/guardrail.py`、`retrieval/csp_http.py`、`agent_factory.py:81-83`、`tracing.py` |
| service 只取最後一則 user message、未用完整 history 與 session id | ✅ | `service_wrapper.py:411,423`；`run_once/run_streamed` 本就接受 `session=`（`runtime/run.py:25,44`）但 service 從不傳——**是缺接線，不是缺能力** |
| 「`ANILA_COLLECTION_ID` 對所有服務成為必要條件」 | 🟡 | 過強。serving 路徑缺 collection 是 **per-request 503**（`service_wrapper.py:428-435`），服務仍可啟動（lifespan `:167-173` 只 log 不擋）；CLI 路徑 fallback `DummyRetriever` 照跑。「非 RAG Agent 被 RAG starter 綁定」對 serving 路徑成立（`:441` 無條件建 `CspHttpRetriever`） |
| 無正式 manifest endpoint／StepEvent／cancel/resume／HITL | ✅ | 實際 route 僅 `/health`、`/v1/models`、`/v1/chat/completions`（`:233,238,384`）；SSE 只轉發 text delta（`:356-362`） |
| skills／MCP／triggers 分離未接入 runtime | ✅ | 三模組僅被各自測試檔 import；`agent_factory.py` 不含 |
| `runstate.py` 參照未實作的 `run_once_state()` | ✅ | `runstate.py:11`——**模組 docstring 範例**的懸空參照（全 repo 無此函式），屬文件債，非執行期會炸的程式碼 |
| PostgresSession 不完整符合 pinned SDK Session protocol | ✅ | SDK 0.17.5 的 `Session` protocol 要求公開 `session_id` 屬性＋`session_settings`；`memory/session.py:47-48` 存成私有 `_session_id`，`runtime_checkable isinstance` 會失敗。結構性原因：retriever 有 `isinstance` 硬檢查（`agent_factory.py:74`），Session 端沒有等價守衛 |

### §2.4 Shell

| 宣稱 | 判定 | 證據 |
|---|---|---|
| agentic.jsx／toolExecution.jsx／sse.js 已有元件與 typed parser | ✅ | `InterruptCard:86`、`TodoChecklist:372`、`ToolExecutionWidget:27` 等皆在；sse.js 解析 **10 種** named `anila.*` 事件（`sse.js:198-287`）——比「部分」更完整 |
| send/edit/regenerate/continue 未共用 execution reducer | 🟡 | 成立，但這些路徑 **全在 app.jsx**（`:926/:1159/:1366/:1415`），chat.jsx 是純呈現層；且計畫書 **漏了第 5 條路徑 `sendCompare`**（`app.jsx:1672`，A/B 比較欄） |
| 未接上 tool/todo/interrupt/resume/replay | ✅ | agentic/toolExecution 元件 **從未被 app.jsx／chat.jsx import**（dark code，僅測試引用）；app.jsx 只傳 `onText/onTrace/onMeta/onReasoning`，typed 事件解析後因無 callback 被丟棄。注意 `anila.trace` **是有被消費的**（既有 trace timeline），未通的是 agentic typed 類 |
| 後端已有部分 event | ✅ | Router SSE 格式（`_make_event:337-338`）與 sse.js 期待完全對齊；Router 直發 trace/meta/resumed/spans，interrupt/todos/tool_call 靠 agent 端發出再改名轉發（`:2219-2231,2355-2364`） |
| （隱含）Shell 無 resume UI | 🟡 | API 管線兩端都造好了（`sse.js:349` `streamSessionAnswer`；Router `POST /v1/sessions/{id}/answer:1303`＋owner 綁定），**只差 UI 沒接**——後端比計畫書描述的更接近完成 |

### §2.5 Ingestion（全部證實，實況比計畫書寫的更嚴重）

| 宣稱 | 判定 | 證據 |
|---|---|---|
| upload／job／enqueue 非同一可靠提交協定 | ✅ | `documents.py:287-321`：commit document → Arq enqueue → commit job row 三段分離；**upload 對 enqueue 失敗無 try/except**（對照 reprocess `:387-397` 有回滾——同鏈路兩入口韌性不對稱）；worker 可能在 job row commit 前跑完，`_update_job` 以 `arq_job_id` 比對打到 0 rows **靜默略過**（`handlers.py:585,621`） |
| `max_tries=3` 靜態、retryable 未參與決策 | ✅ | `main.py:70`；docstring `:19-20` 自承是未來 Sprint 工作；`handlers.py:925-948` catch 後一律 raise，不看 `err.retryable` |
| parent/leaf 分批交易、重試非冪等、會撞唯一鍵 | ✅ | `pgvector_store.py`：`add_parent_chunks:289-348` 與 `index_chunks:106-190` 各自獨立交易、皆 **無 ON CONFLICT**；migration `0016` 註解宣稱「worker 會先刪舊 chunk」但 `delete_document()`（`pgvector_store.py:492`）**全倉零呼叫點**——重試／reprocess 必撞 `UNIQUE(collection_id, document_id, chunk_key)` |
| 空 chunks → document indexed 但 job 未 succeeded | ✅ | `handlers.py:761-767` 設 indexed 後直接 return，job 停在 `:668` 設的 running |
| 同步 parse 阻塞 event loop、timeout/cancel 留 running | ✅ | `handlers.py:693-697` 同步呼叫 `extract_text` 無 executor；`ingest_document` 無 finally、不接 `CancelledError`；`job_timeout=300`（`main.py:71`）逾時即卡 |
| 分類欄位已 migrate 但 worker 未傳播 | ✅ | `r1_0003_five_level_classification.py:68-74,235-245` 已加欄位；`index_chunks`／`add_parent_chunks` 的 INSERT 欄位清單 **完全不含 `classification_level`**——且 search 端也不讀（**雙向死欄位**） |
| search 缺 clearance gate | ✅ | `search.py` 只有 collection access＋agent bound scope＋RLS（綁 collection_id）；SQL 無任何 classification 述詞 |
| similarity O(N²) self-join、超限留 stale edges | ✅ | `similarity_relations.py:28-49` centroid 自連接、每次 ingest 呼叫（`handlers.py:891`）；`:82-88` 超過 `similarity_max_docs` 的 return 在 DELETE（`:109-113`）之前 |
| LLM relation HTTP 包在 DB transaction 內 | ✅ | `llm_relations.py:226-274`：`conn.transaction()` 內做 httpx POST，`relation_llm_timeout_seconds=120.0` |
| 單一 `document.status` 混合排程與可搜尋語意 | ✅ | `models/ingestion.py:162`＋docstring `:122-125` |

### §2.6／§2.7 分支與部署

| 宣稱 | 判定 | 證據 |
|---|---|---|
| 基線 commit `f661f5e8…` | ✅ | `git rev-parse main` 一致 |
| `prod-intranet-card` 與 main 12/11 divergence | ✅ | `git rev-list --left-right --count` = 12 / 11 |
| compose 未注入 `ANILA_TRACE_ENDPOINT` | ✅ | `infra/compose/platform.yml:263-267` router 環境只有 `CSP_BASE_URL`／`CSP_SERVICE_TOKEN`／`MODEL`；platform.yml 與 dev.yml 全文 **零** TRACE 字樣。注意：fail-open 是 README 明載的 **設計決策**（additive tracing），計畫書要求 required profile 改 fail-closed 屬合理的設計變更，非修 bug |
| （WS7-A 隱含）Redis 耐久性不可依賴 | ✅ | `platform.yml:201` `--appendonly no` |
| CSP trace ingest 端點已存在 | ✅ | `traces.py:100-173`（`POST /v1/traces/{trace_id}/spans`，202、批次 1..256、冪等） |

### §8.1 測試基線（本機重現，Windows / Python 3.11.9 / pytest 9.1.1）

| 套件 | 計畫書宣稱 | 本機重現 | 判定 |
|---|---|---|---|
| anila-core | 770 passed／6 failed／7 skipped；至少一個 hierarchical chunker 功能失敗，其餘 Windows/tool/staleness | **770／6／7 一字不差**；`test_hierarchical_pop_resets_deeper_headings` 確為功能失敗（AssertionError），其餘 5 個＝2 cp950 編碼＋1 grep 路徑過時＋1 event loop＋1 fake 簽名過時 | ✅ |
| anila-agent | 排除 9 個 Windows 路徑失敗後 199 passed／1 live skipped | **9 failed／199 passed／1 skipped 一字不差**（9 個全是 `test_multitenant_memory` 的 POSIX 路徑假設） | ✅ |
| ingestion-worker | 約 146 passed | **146 passed 一字不差** | ✅ |
| anila-agent strict mypy | 24 errors | **61 errors in 21 files**（mypy 2.2.0；未裝 fastapi 時 64） | ❌ 數字不符——判定為 mypy 版本相依（pyproject 只要求 `>=1.9`，撰文者可能用 1.x 量測）。方向（strict 未歸零）正確 |

---

## 3. 審查加碼發現（計畫書未提、但派工時必須知道）

1. **`delete_document()` 全倉零呼叫**——migration 0016 的 unique 約束建立在「re-index 前會先刪舊 chunk」的假設上，該前置清理從未接線。這是 §2.5 撞鍵問題的根因級證據，也代表 WS7-C 之前就值得先做一個小修（刪除或 upsert 二擇一）。
2. **job 狀態更新靜默失效**——`_update_job`／`_record_job_failure` 吞例外＋0-row 略過（`handlers.py:585-586,621-622`），`ingestion_jobs` 的狀態可信度整體偏弱，監控不能只看 job 表。
3. **classification ceiling 執法是條件式**——`enforce_agent_ceiling` 在 agent 未設 `classification_ceiling` 時整段 no-op（`ceiling.py:82-84`）。WS2 定義 `ready_for_dispatch` 時必須明確：ceiling 為 NULL 算不算 sufficient。
4. **`/v1/agents` 已是 caller-scoped**（admin 看全部 approved、一般 user 只看有 `UserAgentPermission` 者，`proxy.py:413-424`）——Router 候選過濾設計要把這層既有授權算進去，避免重複或矛盾。
5. **分類 latch 語意已存在**且散落兩處（`router_server.py:420-422` one-way latch、`agent_as_tool.py:132-157` fail-closed 翻轉）——WS3/WS7-E 的 classification latch 應整併既有實作，不要三套並存。
6. **`security/url_guard.py` 是現成的 fail-closed 樣式**（SSRF、production 拒 http），PolicyGate 可參考／整併。
7. **Shell 的 dark code 已有完整單元測試**（agentic/toolExecution/sse 測試都在）——WS6 的整合風險比「從零寫」低很多，是好消息。
8. **非串流 agent 呼叫缺 token_usage 記錄**（`proxy.py:684-685,727-728` 自註）——治理帳完整性的 pre-existing gap，建議納入 WS2 或 WS8 範圍。
9. **anilalm 與 anila-studio 不消費 `/v1/agents` 或 Router SSE**（grep 零命中；anilalm 的 SSE 是 studio job stream）——計畫書相容矩陣未提這兩個 consumer，經查證可安全排除，但矩陣應明文記載「已確認不受影響」。
10. **`ANILA_COLLECTION_ID` 議題內部稽核已標過**（`docs/audits/anila-full-audit-2026-06-02.md:765`，HIGH）——非計畫書首發，修復時可對齊該稽核單。
11. **附錄 A 的 `pgvector_store.py` 實際位於 anila-core**（`packages/anila-core/src/anila_core/storage/adapters/`，worker 經 `handlers.py:30` import）——它是共用元件，改動會同時影響 anila-core 其他消費者，變更協調範圍比表列的「Worker」更大。

---

## 4. 計畫書需修訂的偏差清單

| # | 嚴重度 | 位置 | 問題 | 建議修訂 |
|---|---|---|---|---|
| 1 | MEDIUM | §2.1 | 「未知 Agent…沒有統一 fail-closed」易誤讀——unknown agent 已就地擋，真正 fail-open 的是 stale registry | 改寫為「stale registry fail-open＋防護散落不一致＋缺統一閘」 |
| 2 | MEDIUM | §7.4 | port matrix 只列 `prod-intranet-card`；AGENTS.md §3 有六條 downstream，外網現役 prod 是 `prod-public-passwd` | 擴充矩陣涵蓋全部部署分支，或明文寫出其他分支的凍結／延後策略。**⚠ 本條經 Part 3 §3.1 修正**：實測 `prod-intranet-card` 與 `main` 只差 `.env.example` 一個檔，程式碼位元組相同。計畫書所稱「12/11 divergence」是 commit 圖差異而非內容差異，porting 負擔目前極低。正確的長期藥方不是六分支 port matrix，而是**單一 code line ＋ 簽章 deployment profile** |
| 3 | MEDIUM | §8.1／WS5 | strict mypy「24 errors」本機不可重現（61 @ mypy 2.2.0） | 改寫為「strict mypy 歸零（以 CI pin 的 mypy 版本為準）」並在 CI 鎖定 mypy 版本 |
| 4 | LOW | §2.3 | 「`ANILA_COLLECTION_ID` 對所有服務成為必要條件」過強（實為 serving 路徑 per-request 503，服務可啟動；CLI 有 fallback） | 改寫為「serving 路徑無條件綁 RAG retriever，缺 collection 時所有請求 503」 |
| 5 | LOW | §2.3 | `run_once_state()` 是 docstring 懸空參照，非執行碼 | 標明為文件債，避免誤判為 runtime bug |
| 6 | LOW | §2.4／WS6 | 訊息路徑在 app.jsx 非 chat.jsx；漏第 5 條 `sendCompare` 路徑 | 修正檔案歸屬；reducer 重構範圍納入 sendCompare |
| 7 | LOW | 附錄 A | `pgvector_store.py` 路徑寫在 `ingestion_worker/` 下 | 更正為 anila-core 路徑並註明共用影響面 |
| 8 | LOW | 全文 | 「附圖」引用 8 次但未嵌入文件，獨立閱讀者看不到目標畫面 | 將附圖存入 `docs/gpt-report/assets/` 並嵌入，或以文字完整描述目標流程 |

---

## 5. 計畫本體評估

### 5.1 同意並讚許

1. **診斷精準**：缺口清單（regex-only routing、system prompt 可覆寫、multi_turn 無上限、legacy 投影、ingestion 非原子、分類死欄位、trace 部署 no-op）全部經獨立驗證為真。
2. **原則正確**：契約先行、CSP 權威不可被 Router 取代、fail-closed、additive migration、expand→backfill→switch→contract、shadow→canary、回滾只能更保守——皆是對的。
3. **WS7 設計切中要害**：outbox／lease／generation／終態一致對應的都是真實缺陷，不是過度設計。
4. **不做事項明確**（§0.3）：無 marketplace、無自由 swarm、multi-agent 條件式預設 off——對本團隊規模尤其重要。
5. **WS1 選擇擴充既有 `api/events.py`** 而非另起爐灶——與線上已有的 10 種 `anila.*` 事件及 Shell parser 相容的機會存在，路徑正確。

### 5.2 主要疑慮（核准前應處理）

1. **人力／工期假設與現實脫節**：計畫假設 1 技術負責人＋3-4 後端＋1 前端＋1 QA/SRE 跑 9 個 Sprint（約 18 週）。實際開發模式是 1 人＋AI 協作。文件雖自註「2–3 人應延長 Sprint」，但整體 phase 排程仍按多人假設。**建議**：核准範圍時同步重排——以「單一垂直切片」為單位推進，而非平行 workstream。
2. **止血項未與架構演進分離**：以下四項是「小時級～天級、不改契約」的獨立修正，不必等 WS0-WS3 的 flag／shadow 架構：
   - `anila_multi_turn` 加 server-side 上限（`router_server.py:717` 一行）；
   - caller system message 不得取代控制 prompt（`:726-730`，需保留使用者偏好注入的合法用例）；
   - upload 的 enqueue 失敗回滾（比照 reprocess `:387-397` 的既有寫法）；
   - 空 chunks 路徑補 job 終態（`handlers.py:761-767`）。
   **建議**：計畫書增設「WS-0.5 立即止血」清單，與 P0 架構項解耦。
3. **WS7 一次全包風險**：outbox＋lease＋generation＋stage＋classification 五大機制全列 P0、排 2 個 Sprint，過於樂觀。**建議**分層：P0-A（outbox＋lease＋終態一致）→ P0-B（generation 冪等，或先以 delete-before-insert／ON CONFLICT 止血）→ P0-C（classification 傳播＋clearance gate）；WS7-D 的 stage 分離可降 P1。
4. **Phase 3 與 Phase 4「可平行」**的前提是不同工程師——單人團隊時此假設不成立，排程需改串行。
5. **StepEvent 與既有事件的映射未給**：§4.5 定義了全新 StepEvent schema，但未說明與線上 10 種 `anila.*` 事件（含已被消費的 `anila.trace`／`anila.spans`）的演進關係（取代？包裝？並行過渡多久？）。Shell parser 與既有 trace UI 已依賴現行事件，缺映射表會讓 WS4/WS6 的相容策略懸空。

### 5.3 建議決策

**有條件核准**：以本計畫書為派工藍本的前提是完成 §4 的 8 項修訂，並在 Phase 0 前先落地 §5.2-2 的止血清單。事實底座經本審查驗證可信，風險登錄與回滾設計合格；最大的執行風險是規模——按實際人力重排 phase 後即可啟動。

---

## 附：審查方法與可重現性

- 程式碼對照：5 個平行唯讀 agent 各驗一個子系統，逐條回報「判定＋file:line 證據」；本報告引用的行號皆對應 `main@f661f5e8`。
- 測試重現：本機（Windows 11／Python 3.11.9）以乾淨 venv 安裝 `anila-core[dev,rag]`、`anila-agent[serving,pgvector,csp]`＋dev 工具、`anila-core[rag]`＋ingestion-worker deps，pytest 9.1.1 全套重跑；mypy 2.2.0。
- 未驗證項：ruff 基線（計畫書宣稱 anila-agent ruff pass、ingestion 有既有 lint 問題）——未重跑，不影響結論。

---
---

# Part 2 — Agentic 執行流程與 Agent 協議（補充分析）

> 本節回答計畫書引用 8 次卻未嵌入的「附圖」該怎麼落地，以及它牽涉的 agent 端協議與 API 欄位。結論：**管線已有八成，缺的是接線，不是發明新協議。**

## 2.1 附圖體驗對應到 ANILA 的哪一層

附圖中每一列（執行技能／執行工具／執行指令／讀取資料 + 已完成徽章）＝一個步驟事件。ANILA 的三層現況：

| 層 | 現況 | 證據 |
|---|---|---|
| 協議層（事件定義） | **已存在** | `api/events.py:17-178` — typed pydantic 事件：`tool_call_started`、`tool_call_finished`、`todos_updated`、`interrupt_requested` 等 |
| 傳輸層（Router 轉發） | **已存在** | `router_server.py:2219-2231` passthrough 白名單，把 agent 發的事件改名成 `anila.*` 轉給前端；`_make_event:337-338` 輸出 named SSE |
| 前端解析層 | **已存在** | `sse.js:198-287` 解析 10 種 `anila.*` 事件；`TodoChecklist`、`ToolExecutionWidget`、`InterruptCard` 元件皆備妥且有單測 |

斷掉的是頭尾兩段：

1. **Agent 端不發事件**（源頭沒水）：`service_wrapper.py:356-362` 只轉發 text delta，工具軌跡明言不外送。
2. **Shell 不消費**（水龍頭沒開）：`app.jsx` 五條訊息路徑只接 `onText/onTrace/onMeta/onReasoning`；agentic/toolExecution 元件從未被 import。

## 2.2 對 agent 端協議與 API 欄位的實際要求

**協議不需要新設計——repo 裡已有一版在線上跑。** 對 agent 開發者的實際 ask：

- **掛點現成**：openai-agents SDK 的 `on_tool_start/on_tool_end` hooks，`anila-agent/tracing.py` 已經用同一組 hooks 在發 trace span 到 CSP。同一掛點再多發一份 UI 事件即可，不動業務邏輯。
- **欄位**：`kind`（tool/skill/command/retrieval，決定圖示）、名稱、狀態、順序。現有 `ToolCallStartedPayload` 大致夠用。「已完成 8」＝前端數終態事件。防亂序／重複才需要 `sequence`/`step_id`（第一版可先用 `tool_call_id` 去重）。
- **對第三方的 ask 很小**：SSE 裡多發幾種 named event。不發也不會壞——Shell 對缺失事件容忍，只是該 agent 沒有步驟列表。可「官方 template 先做、第三方逐個跟上」。

## 2.3 建議的落地順序（與計畫書 Phase 3 的差異）

計畫書把此 UI 排在 Phase 3（第 6 個 Sprint），前面壓了 RouteDecision、PolicyGate、EventStore 全套，理由是「UI 不早於 StepEvent 真值」。**原則同意（步驟必須由真實事件驅動，不是前端動畫），但它把兩件正交的事綁死了**：RouteDecision/PolicyGate 解決「派工正確性與治理」，與「執行透明化 UI」無關。以 1 人＋AI 的現實，建議走薄的垂直切片：

- **切片 1**：anila-agent 在 RunHooks 發 tool 事件（1–2 天）→ Shell 建共用 reducer 接上 callbacks、render 既有元件（2–4 天，五條路徑含 `sendCompare` 一起收）→ E2E。全程真事件，不違反「非動畫」底線。
- **切片 2**：approval／interrupt 接線——兩端半成品都在（`InterruptCard` 元件、`interrupt_requested` 事件、Router `POST /v1/sessions/{id}/answer`），只差 UI。
- **切片 3 才是 EventStore/replay**：第一版接受「重新整理後只剩最終文字」。

兩個底線站計畫書那邊：① 附圖第一列 `discover-relevant-plugin` 那種動態 plugin 發現／安裝**不要學**（skills/MCP/triggers 尚未接 runtime；v1 做「已接線工具的執行透明化」即可）；② 事件只送 safe summary，參數與輸出在 **agent 端就 redact**。

## 2.4 動態 plugin：拆成兩件事

- **動態「安裝」**（runtime 拉進未審核能力）：**永遠不該做**。air-gap 進不了外部 registry、分類環境不允許未審核程式碼執行、北極星明文排除 marketplace。這不是保守，是平台定義。
- **動態「發現」**（在已核准集合內由模型即時挑選）：**可以做**，且 Router 已有 agent 粒度的雛形（`_build_agent_list` 把清單塞進 prompt 讓模型挑）。工具粒度就是給模型一個 `discover_relevant_skill(query)` meta-tool，檢索範圍限定在 CSP 已核准集合內，每步發事件、可稽核。

**何時做**——用客觀觸發條件而非時間表：當單一 agent 的工具/技能數多到 prompt 放不下（經驗值 20–30 個以上）才有價值；只有 5 個工具時硬加 discover 是浪費一步延遲。前置條件三項：① 切片 1 的事件透明化先通；② skills/MCP 真正接進 runtime（現況 `skills/loader.py` 只有測試在 import，manifest 宣告未接線能力是計畫書明列禁項）；③ P0-01 的 prompt 注入防護先修（技能描述會進 prompt，檢索式發現放大注入面）。對應計畫書 P2 時段，順序合理。

> 學它的 UX 模式（discover 作為可稽核的一步出現在 timeline），不學它的供應鏈模式（runtime 裝新外掛）。

## 2.5 若第三方用 LangChain 開發 agent：統一「線協議」，不統一「框架」

今天的 codebase 已經給了答案：CSP 派工對 agent 的全部要求是「OpenAI 相容 HTTP endpoint + `X-CSP-Service-Token` 驗證」（`services/csp/app/services/proxy/headers.py:138-182`）。anila-agent 只是官方參考實作，**不是必要條件**。計畫書 §12.1「不需要更換框架，加一層 adapter」的立場正確，也是唯一可行的立場。

**必須統一（wire contract）：**
1. OpenAI 相容 request/response（LangChain 包一層 FastAPI 即有）
2. named SSE 事件集——不統一的話，Shell 的單一 parser 不可能講 N 種方言，conformance 也無從測起
3. 治理欄位——trace 關聯 id、classification、safe summary 的**遮罩規則寫死在協議層**，不讓各框架自由發揮（分類環境的治理需求，非技術潔癖）
4. 模型與檢索呼叫回頭走 CSP（記帳 + SSRF 治理；內網拓撲本來就強制）

**不必統一：** 內部框架、規劃方式、記憶體實作、程式語言。

**LangChain 怎麼接**：`astream_events` 的 hooks（`on_tool_start`/`on_tool_end`/`on_chain_start`…）與 ANILA 事件集幾乎一對一，adapter 約一頁程式碼；檢索則寫一個 LangChain Tool 包 CSP search API。**建議在計畫書 WS1 交付物加一項：官方 LangChain adapter 範例**——把「不用換框架」的承諾做實，這是說服外部 agent 團隊的最低成本方式。

**採用策略用漸進降級**：不發事件的 agent 照樣能跑（Bronze，只是沒有步驟列表），發了才升 Silver 拿完整 timeline。第三方可先上線再補協議。

**視野補充**：業界已有「agent 對前端發 typed 事件」的標準化嘗試（AG-UI 的事件分類與 ANILA 現有 `anila.*` 幾乎同構；A2A 對應 manifest/agent card 那層）。內網不必引入依賴，但設計 StepEvent schema 時把形狀對齊它們，未來若生態收斂，映射成本近乎零。

---
---

# Part 3 — `prod-intranet-card` 分支深度檢視 ＋ 與 GPT-5.6 報告對答案

| 欄位 | 內容 |
|---|---|
| 審查對象 | 分支 `origin/prod-intranet-card@2d1bf08a`（＝ GPT-5.6 報告宣稱基線，已核對一致） |
| 對照報告 | `docs/gpt-report/ANILA_prod-intranet-card_完整專案分析與決策建議.md`（GPT-5.6） |
| 審查方法 | 10 路平行領域檢視（先獨立找缺陷、再讀對照報告給判定，避免錨定）＋ **對抗式驗證**（對每條 CRITICAL/HIGH 派人「預設它是錯的」盡力推翻）＋ 本機重建四套測試環境實跑 |
| 高危裁決 | 13 條 CRITICAL/HIGH → **9 存活**（1 CRITICAL、5 HIGH、3 降 MEDIUM）、**4 推翻** |
| 結論 | **同意 GPT-5.6 的 No-Go 判定**，且發現一條它嚴重低估的 CRITICAL。修正其 9 處誇大／誤判，補入 15 項它漏掉的發現 |

## 3.1 先講最重要的一件事：分支模型的認知已經過時

**實測：`origin/prod-intranet-card` 與 `main` 只差一個非文件檔——`.env.example`。所有程式碼位元組相同。**

```
git diff --name-only main origin/prod-intranet-card -- . ':(exclude)docs/**' ':(exclude)*.md'
→ .env.example
```

delta 僅四個旗標：`ANILA_ALLOW_DEV_SECRET` 1→0、`ANILA_ALLOW_HTTP_ENDPOINT` 1→0、`ENABLE_CARD_LOGIN` false→true、`REQUIRE_CARD_LOGIN_ONLY` false→true。`.env.example` 自己就寫著「程式碼已收斂為單一版本，分支之間只有此環境設定檔不同」。

三個推論：

1. **`AGENTS.md` §3 與 `CLAUDE.md` 的「card/SSO 永久 fork 熱區」描述已過時**——`card_auth.py`、`auth_service.py`、`password.py`、`_common.py` 在兩支上皆位元組相同。目前 cherry-pick 衝突風險近乎零。**建議更新這兩份專案文件**，否則後續 agent 會依過時前提做保守決策。
2. **變更計畫書 §2.6 的「12/11 divergence，不能假設 fast-forward」字面正確但嚴重度被高估**——那是 commit 圖差異（同語意不同 SHA），不是內容差異。我在 Part 1 §4 第 2 項據此提的 port matrix 疑慮，實際 porting 負擔目前極低。
3. **GPT-5.6 §14.3「分支不是安全邊界」完全正確，且它開的藥方（單一 code line ＋ 簽章 deployment profile ＋ posture assertion）比變更計畫書的「六分支 port matrix」更對。** 正式部署身分目前由**可變環境設定**決定，不是由不可變 release artifact 決定。

補充一個好消息：compose 的 fallback 是安全的（`ANILA_ALLOW_DEV_SECRET:-0`、`ANILA_ALLOW_HTTP_ENDPOINT:-0`、`ENABLE_CARD_LOGIN:-true`、`REQUIRE_CARD_LOGIN_ONLY:-true`），且 `assert_no_dev_defaults()` / `assert_intranet_lockdown_consistency()` 確實在 startup 執行並會拒絕啟動——prod posture 的守衛是真的（這也是 CSP 卡片測試在缺 secrets 時會 fail 的原因）。

## 3.2 CRITICAL-1（本審查獨立發現，GPT 嚴重低估）

### Code Server 工作區暴露整庫分類資料的 superuser dump

GPT §13.1-3 只說「`secrets/jwt-private.pem` 未遮，取得 Code Server 密碼即可簽 owner JWT」。**實際暴露面遠大於此，且危害更高的是 DB dump 而非 JWT 私鑰。**

證據鏈（我逐行親驗，並經對抗驗證者盡力推翻失敗 → **三重確認**）：

| 環節 | 證據 |
|---|---|
| codeserver 掛 repo root、**read-write**，遮蔽清單只有兩條 | `infra/compose/platform.yml:506-508`（`${CODESERVER_WORKSPACE:-../..}:/home/coder/workspace`，無 `:ro`；只遮 `.env` 與 `infra/nginx/certs/server.key`） |
| 備份預設落在被掛載的工作區內 | `infra/deployment/scripts/anila-ops.sh:42`（`BACKUP_DIR="${ANILA_BACKUP_DIR:-$REPO_ROOT/backups}"`） |
| 備份內容 = **superuser 全庫 dump，繞過 RLS** | `anila-ops.sh:239`（`pg_dump -U csp -d csp` → `db-csp.sql.gz`）；另 `:244` 複製 `.env` 為 `env.bak`、`:248` 複製 `secrets/*.pem` |
| 第二份未遮蔽的 `.env` 全文副本 | `infra/deployment/intranet/intranet-deploy.sh:131`（regen 分支會在 repo 根留下 `.env.bak.<timestamp>`） |
| **官方 runbook 要求每日 cron 產生備份** | `docs/runbooks/intranet-zero-to-prod-guide.md:174-177`（每日 02:30 `anila-ops.sh backup`，週日 `--full`） |
| codeserver 預設隨 stack 啟動 | `platform.yml` 無 `profiles:`；`platform.yml:9-10` 載明 2026-06-11 已解凍 |
| 唯一認證 = 共享靜態密碼，**SSO 是未做的 TODO** | `platform.yml:489`（「SSO 接好後改 nginx auth_request 驗 CSP JWT」）；`infra/nginx/anila.conf:311-314, 697-699`（443 與 4443 兩個 server block 皆無 `auth_request`，註解自承「不帶 SSO」） |
| 設計者自己的威脅模型就把「持 codeserver 密碼者」列為須圍堵對象 | `platform.yml:502-503` 註解 |

**失敗情境**：照官方 runbook 部署的內網正式機，每晚 02:30 把整個五級分類知識庫的 superuser dump 寫進一個以共享密碼保護、無 SSO、read-write 掛載的瀏覽器 IDE 工作區。任何取得 `CODESERVER_PASSWORD` 者可直接下載 `backups/*/db-csp.sql.gz`（**完全繞過 RLS**）、讀 `env.bak` 取得 postgres superuser 密碼＋`SECRET_KEY`＋`CSP_SERVICE_TOKEN`；即使 `jwt-private.pem` 原檔在 intranet 路徑為 `root:600` 不可讀，`secrets/` 目錄由 UID 1001（＝codeserver）擁有且可寫，攻擊者可**替換整組 JWT keypair**，CSP 下次重啟即載入 → 簽發任意 owner JWT。

**這不是誤配置，是照著 runbook 走的預設狀態。** `.gitignore` 有 `/backups/`，但**容器掛載不看 gitignore**。

修法（Gate 0，數小時）：① `CODESERVER_WORKSPACE` 指向非機密子目錄，或整個把 codeserver 移出正式 profile；② `ANILA_BACKUP_DIR` 移出 repo root；③ nginx `/codeserver` 還原 `return 404;`（做法就寫在旁邊註解裡）。

## 3.3 對抗式驗證殺掉的假警報——本審查方法論的核心價值

對每條 CRITICAL/HIGH 都派一個「預設它是錯的、盡力推翻」的驗證者。**13 條中推翻 4 條、降級 3 條**：

| 原始發現 | 裁決 | 推翻／降級理由 |
|---|---|---|
| 文件不繼承 collection 分類 → 下游看到無機密來源（HIGH） | **REFUTED → LOW** | 執行期 ceiling 的 task 等級是從 **collection** 導出（`tasks/service.py:100-124`），不讀逐檔標籤 → 無 fail-open |
| `document_ids` 過濾在全域 top-k 之後 → 指定文件錯誤回空（HIGH） | **REFUTED → LOW** | 互動搜尋根本不帶 `document_ids`，失敗情境不可達（bug 屬實，影響面被高估） |
| `CARD_DEV_SKIP_NONCE_BINDING` 誤開 → 未認證 owner 登入（HIGH） | **REFUTED → MEDIUM** | **投遞路徑不存在**：該旗標在 compose 與 `.env.example` 中皆不存在，且 csp 用顯式 `environment:` 區塊（無 `env_file:`）→ 在 `.env` 設它傳不進容器。**（我獨立複驗）** 殘留議題：缺 startup hard gate（姊妹旗標都有） |
| `create_all` fallback → 新表無 RLS → 跨 collection 讀分類資料（HIGH） | **REFUTED → MEDIUM** | `create_all(checkfirst=True)` 是預設值 → 既有帶 RLS 的分類表被**跳過**、不會被脫除。**（我獨立複驗簽名）** 殘留議題：半-migrate 狀態下 `/health` 仍回 200 |
| Memory 分類洗白（CRITICAL） | 降級 **HIGH** | facts 是 per-user，不構成跨使用者外洩；破壞的是跨密級／模型 clearance 隔離 |
| Task 永不收斂終態（HIGH） | 降級 **MEDIUM** | 治理帳完整性問題，未繞過 auth 或 classification ceiling |
| Studio status/download 不一致、anilalm artifact 殘留（HIGH ×2） | 降級 **MEDIUM** | 前者多數情境可復原；後者殘留的是 metadata/titles 而非機敏內文 |

同樣地，我自查 CSP 全套測試時一度懷疑三條「疑似正式阻斷」，**全部經查證為環境問題而非缺陷**：

- `test_expired_token_rejected` **單獨跑會過** → 全套失敗是測試污染，不是「過期 token 未被拒」
- `test_single_head_in_r1_namespace` 失敗原因是 **cp950 讀不了 `alembic.ini`**，不是 migration 多 head
- `test_swagger_ui_is_404_by_default` 是 `assert 401 == 404` → `/docs` 有擋（401），只是沒隱藏

**若不做這步，這份報告會多出七條假警報。**

## 3.4 存活的 HIGH（經對抗驗證，各附 file:line）

1. **營業秘密對話可繞過分享封鎖** — 五級定序 `無機密(0) < 營業秘密(1) < 機密(2) < 極機密(3) < 絕對機密(4)`（`schemas/contracts/classification.py:39-43`，「排序不可變」是契約）；鏡射規則 `classified = level >= 機密`（`modules/policy/service.py:225`）使營業秘密的 `classified=False`；`create_share` 只擋 `conv.classified`（`conversation_service.py:384-388`）；`GET /public_share/{token}` 程式註解明寫 "No auth required"，`classified=False` 即回傳**全部訊息與標題**（`public_share.py:58-67`）。`ENABLE_PUBLIC_SHARE` 預設 `True`（`config.py:18`），**compose 從未關閉**——即使程式註解自己寫著「air-gapped / card-only 部署應完全關閉此介面」。可達性經驗證：agent policy 可把對話 latch 到營業秘密（`proxy.py:67-82, 575`）。**修法是 compose 一行。**
2. **Memory 分類洗白 ＋ stored prompt injection** — `UserFact` 無分類欄位（`user_memory.py:41-90`，對照 `:129` 的 `ConversationMemoryChunk` **有**）；`persist_turn` 對每 turn 無條件抽 fact（`memory_service.py:523-586`）；`_format_block` 無條件把所有 facts 注入 system prompt（`:294-355`）；`enforce_model_ceiling` 只讀 conversation/task level、對注入的 fact 分類全盲。chunk 通道有 `encryption_inherited → _latch_inherited_classification` 的 no-read-down latch，**facts 通道完全沒有**。延伸（GPT 未提）：`_extract_facts`／`_embed` 直呼固定模型（`memory_service.py:200, 433`），**完全不經 `enforce_model_ceiling`** → 絕對機密對話內容每個 turn 都被送去 gemma。
3. **Governance UI 建立服務必 422** — UI 送 `url`（`PlatformLinksView.vue:380-398`），backend 必填 `entry_url`（`schemas/registered_service.py:49-65`）；`registryMode` 在 prod 必為 true（`router.py:53` 無條件掛載，無 feature flag）。
4. **多數專案入口卡片 launch 恆 400** — auto-seed 的 6 張卡片中 5 張是 relative URL（`platform.yml:135-139`，字面 YAML block、`.env` 無法覆寫），`auto_seed.py:56,78` 逐字存不做正規化，`services.py:110-113` `_url_origin` 對非 http(s) scheme 直接 400，且 `:379` 的 URL 驗證在 `:383` 權限檢查**之前** → 未授權者收到的是 400 而非 403。`main.py:131-132` 的 auto_seed 非 dev-only，prod-intranet-card 也會 seed。
5. **Air-gap bundle 缺 image closure** — 匯出腳本只 build/save 7 個 image（`build-and-export-for-intranet.sh:54, 80-88`），`flux2-dev-agent` 只有 `build:`、無 `image:`、無 `profiles:`，而 `intranet-deploy.sh:258` 跑 `docker compose up -d --no-build` → **全新內網主機必然部署失敗**。根因：commit `4674e70` 新增服務時未同步更新匯出清單。

降為 MEDIUM 但仍應修的：Task 終態不可達（`BLOCKED_BY_POLICY` 生產端完全不可達 → policy 阻擋在 `task.status` 上不可見）、Studio status/download 契約不一致、anilalm artifact 跨使用者殘留（`OutputsPage.tsx:116-124` 跨 collection 攤平顯示，下一位卡片使用者**不需 devtools、直接在 UI 就看到**前一位的產出標題）、`create_all` fallback、`CARD_DEV_SKIP_NONCE_BINDING` 缺 startup gate。

## 3.5 GPT-5.6 誇大或誤判之處（9 處）

| # | GPT 位置 | 問題 | 修正 |
|---|---|---|---|
| 1 | §6.2.2、§6.2.5 | owner 密碼 break-glass、token 同時回 JSON body 被列為部署級「正式阻斷」 | 兩者皆為 code 內明示的刻意設計（含拍板日期註解），屬 hardening／縱深議題（LOW–MEDIUM），非阻斷 |
| 2 | §6.2.6 | 隱含 `CARD_DEV_SKIP_NONCE_BINDING` 可經 `.env` 誤開 | 該旗標不在 compose 的 csp `environment:` 區塊 → `.env` 傳不進容器。殘留問題是「缺 startup hard gate」而非「可被誤開」 |
| 3 | §13.1-2 | `create_all` fallback 隱含可造成分類資料跨 collection 外洩 | `checkfirst=True` 使既有 RLS 表被跳過。真正問題是半-migrate 狀態下 `/health` 仍回 200 |
| 4 | §5.3 | 「token 殘留」 | prod-intranet-card 下 `login()` **unreachable**（`App.tsx:5-6` 未 mount LoginPage），token 從不寫入 localStorage；access token 是 httpOnly cookie。真正殘留的是 artifact **metadata/titles**，非機敏內文 |
| 5 | §7.1 C6 | 「UserFact 沒有來源 provenance」 | UserFact **有** `source_conversation_id`／`source_message_id`（`user_memory.py:65-72`）。缺的是**分類** provenance |
| 6 | §7.1 C4 | 「ceiling 對 invalid 回 UNCLASSIFIED」 | 對 target 端非法 ceiling 字串實為 **fail-closed**（拋 ValueError、不 dispatch） |
| 7 | §十二 | FLUX alias 相撞為 live risk | `GROUP_INTRANET`（`model-serve.sh:27`）**不含** flux → 內網 prod 現行部署無此風險。（但 GPU 相撞另有實據：`gemma4` 的 `device_ids:["3"]` 是**寫死**、不吃 env，`gpt-oss-120b` 才吃 → 出廠 default 就撞） |
| 8 | §13.1-6 | 「Deploy 多為 warning，最後仍印部署完成」 | 對 `deploy-prod.sh` 不成立：`check_branch` 於非 prod 分支**直接 fatal**（`:88-92`） |
| 9 | §十一 | 「classification 可由 caller 自報」易被讀成提權 | 自報 launch level 仍受 service `classification_ceiling` 上限約束（`access_control.py:97-99`） |

另有一處 GPT **低估**：§6.2.6 以「看似真實」形容 `cht/` 測材——實則為**確定真實**（鏈到正式 CSPKI Root G1、真實 NCSIST OID 與 `repository.ncsist.org.tw` CRL/OCSP URL、在職人員姓名／員編／email／卡序）。**這是 PUBLIC repo。** 依 `CLAUDE.md`「祕密零外洩」要求，應以 synthetic CA／假員編重建測材並評估 git history 清理。

（已確認 repo 內**無**明文私鑰或 API key 外洩：唯一 tracked 的 `.pem` 是 `cspki_ca_bundle.pem` 公開 CA bundle，屬刻意且正確。）

## 3.6 GPT-5.6 漏掉的重要發現（15 項）

- **CSP 測試跑 SQLite in-memory**（`tests/conftest.py:17`），正式是 PostgreSQL + RLS → **測試架構上不可能驗證 RLS**。RLS 政策只存在 4 個 migration 的 raw SQL（`0014/0019/0037/0039`），ORM metadata 零 RLS 認知。配合「無 CI」→ 分類隔離骨幹的 release 保證趨近於零。
- **CSP 容器以 root 執行**（`infra/docker/csp.Dockerfile` Stage2 無 `USER`）→ 放大 §13.1-1 superuser-in-runtime 的爆炸半徑。
- **Memory 寫入側 ceiling 繞過**：`_extract_facts`／`_embed` 直呼固定模型，不經 `enforce_model_ceiling`。
- **chunk latch 低估密級**：`_latch_inherited_classification` 硬編「機密」（`proxy.py:125`）→ 絕對機密來源只把消費對話 latch 到機密。
- **Trace span ingest 不驗 trace ownership**（`traces.py:100-173`）；`TraceSpan.classification_level` 欄位存在但**從不設定** → 所有持久化 span 一律標「無機密」。
- **`BLOCKED_BY_POLICY` 終態生產端不可達**：ceiling deny 時只把 TaskRun 收成 failed，Task 停在 running → policy 阻擋在 `task.status` 上完全不可見。
- **非串流 agent dispatch 完全不寫 `token_usage`**（`proxy.py:684-685` 註解自承）→ 這正是 GPT 稱讚的「usage 與 Task attribution」的實質破口。
- **生圖計量失真**：影像 job 只以回傳 markdown 連結的數十 token 估算，GPU-分鐘級成本在帳本上隱形。
- **`FluxClient` 對熱切換回來的 endpoint 不跑 `validate_outbound_url`**（SSRF guard 繞過）。
- **`similarity_search_per_document` 已寫在程式庫裡卻沒接線**（正確修法就在 `pgvector_store.py:239`，docstring 甚至明寫 post-filter 是 failure mode）。
- **`services/csp/Dockerfile` 是死檔**（compose 用 `infra/docker/csp.Dockerfile`；死檔註解還寫著舊品牌 `myCSPPlatform/backend`）。
- **CSP 在 33 個檔案 import `anila_core`，但 `requirements.txt` 沒宣告此依賴**（靠 Dockerfile 顯式安裝）。
- **anilalm 在 cookie session 下無法續期**：`auth.ts:56-58` 在 localStorage refreshToken 為 null 時直接短路、從不打 `/api/auth/refresh`。
- **`is_active` 未在卡登入路徑檢查**，且停用不 bump `token_version`、不寫 `TokenRevocation` → 只同步 revocation 事件的 anila-studio 無從得知帳號已停用。
- **`get_share_by_token` 每次未認證 GET 都寫 DB 並 commit**（`view_count += 1`）→ 未認證寫入放大。

## 3.7 兩份 GPT 報告的架構分歧，與我的裁決

這是本次審查最重要的判斷，因為兩份報告開了**方向相反**的藥方：

| | 變更計畫書 | prod-intranet-card 報告 |
|---|---|---|
| anila-core | **強化**：把 RouterRuntime、contracts、PolicyGate 都放進去 | **拆解**：已是 God Package，應切成 contracts／security／runtime／agent-sdk／agent-host／rag-agent |
| 首要工作 | 建 agentic 平台的控制契約 | 止血正式環境的安全洞 |

**決定性事實（我實測）：**

- `services/csp` 在 **33 個檔案** import `anila_core`（其中 `anila_core.security` ×13：SSRF guard ×8、credential crypto、endpoint kinds）
- `anila_core.security` 只有 **656 行、2 個模組**（`credential_crypto.py` + `url_guard.py`），唯一第三方依賴是 `cryptography`
- `anila-agent` 對 `anila_core` 的真實 import 是 **0**（唯一命中是一句「刻意不依賴」的 docstring）
- `RuntimeConfigPoller` 的消費者只有它自己＋自己的測試 → GPT「無 production consumer」**成立**
- `build_app()` 建空 `ToolRegistry` → GPT 引為缺陷，但 docstring 明載**這是刻意設計**（host 自帶 registry）→ 此條為 GPT **誤讀**

**裁決：兩者各對一半，正確順序是「先抽、後建」。**

- GPT-5.6 的拆解方向對，但應**排序**：`anila-security`（656 行、單一第三方依賴、CSP 有 13 處在用）是天字第一號候選——天級工作量、近零風險，且立刻把治理權威（CSP）與 God Package 解耦。
- 變更計畫書的 WS1（canonical contracts）**目標對、放置位置錯**。把 `router/decision.py`、`policy.py`、`agents/contracts.py` 放進 `anila_core`，等於讓 CSP 為了 import 契約而繼承 `fastapi`＋`asyncpg`＋`pgvector`＋`aiosqlite`＋`sse-starlette` 全套。契約與 security 必須是**獨立的薄套件**。
- RouterRuntime 本身留在 anila-core 沒問題（只有 router 消費）。

## 3.8 兩份報告的定位差異，與建議的執行序

- **變更計畫書回答的是「怎麼把 agentic 未來蓋好」**；
- **prod-intranet-card 報告回答的是「現在跑的東西安不安全」，答案是 No-Go。**

**第二個問題必須先於第一個。** 具體執行序：

### Gate 0（數天，全部是設定或一行級修改，不需任何架構工作）

1. 移除／限縮 codeserver 工作區；`ANILA_BACKUP_DIR` 移出 repo root；`/codeserver` 還原 404（**CRITICAL-1**）
2. compose 設 `ENABLE_PUBLIC_SHARE: "false"`（**HIGH-1**，一行）
3. `create_share` 的 gate 由 legacy boolean 改為 `classification_level > 無機密`（**HIGH-1** 的根因修法）
4. air-gap 匯出清單補 `flux2-dev-agent`（**HIGH-5**）
5. Registry：UI 送 `entry_url`、auto-seed 改 absolute URL（**HIGH-3/4**）
6. alembic 失敗改 fail-stop（移除 `create_all` fallback）；`/health` 反映 migration 狀態
7. `CARD_DEV_SKIP_NONCE_BINDING` 加 production startup hard gate
8. `cht/` 測材以 synthetic CA ／假員編重建

### Gate 0.5（同期，這是最高槓桿的投資）

建最小 CI——即使只跑「backend pytest ＋ 三個前端 build」也能終結「38 個 CSP 測試失敗但沒人知道哪些是真的」。**並為 RLS 加一個真 PostgreSQL 的整合測試**（現況 SQLite 測試在架構上就測不到 RLS）。

### 然後才是 Part 1 的變更計畫書路線圖

並依 §3.7 調整：先抽 `anila-security` ＋ `anila-contracts`，再建 RouterRuntime。

## 3.9 測試與品質基線（本機實跑，四套環境）

| 範圍 | GPT 宣稱 | 本機實測 | 判定 |
|---|---|---|---|
| anila-core pytest | 770 passed / 6 failed / 7 skipped | **完全一致** | 精準 |
| anila-core ruff | 22 errors | **22 errors** | 精準 |
| anila-core strict mypy | 99 errors / 26 files | **98 / 26**（mypy 2.2.0） | 差 1，等同 |
| anila-agent ruff | passed | **All checks passed** | 精準 |
| anila-agent strict mypy | 66 errors / 26 files | **61 / 21** | 工具版本相依（變更計畫書說 24，三個數字都不同）。不變量：非零 |
| ingestion-worker | 146 passed | **146 passed** | 精準 |
| CSP card+cookie+docs 子集 | 12 failed | **12 failed** | 精準 |
| **CSP 全套（兩份報告都沒跑）** | — | **38 failed / 681 passed / 2 errors** | 經分類後**幾乎全是環境問題**：10 個因缺 `ADMIN_PASSWORD`/`SECRET_KEY` 觸發 startup guard（guard 正常運作）、3 個 SQLite-vs-asyncpg mock 不符、3 個 fixture drift、1 個 cp950、1 個測試污染 |
| version triple | 0.14.0 / 0.7.0 / 0.1.0 | **完全一致** | 精準 |
| Router build 可重現性 | 不用 uv.lock | `pip install "."`；`packages/anila-core/uv.lock` 存在但未消費 | 確認 build 不可重現 |
| 規模（services 552／packages 319／apps 230／infra 92／291 測試檔／無 CI／14 服務／CSP 54 tables＋54 migrations） | 宣稱 | **全部一致**（route decorators 223 vs 宣稱 222） | 精準 |
| scraps/ | ~9,000 行 | **51 檔、19,858 行** | GPT **低估**逾一倍 |
| 前端測試腳本 | anilalm／governance 無 | **確認無**（governance 是 agent 核准與分類管理台） | 精準 |

**結論：GPT-5.6 的每一個可量測數字我都重現了。這份報告是跑過測試寫的，不是腦補。**

真正的問題不是「38 個測試失敗」，而是——**沒有 CI，所以沒人知道這 38 個裡哪些是真的**；而 CSP 測試跑在 SQLite 上，**架構上就不可能驗證 RLS**（分類隔離的骨幹）。這兩件事疊加，等於正式分支的 release 保證趨近於零。這正是 GPT §13.1-7 的論點，方向完全正確。

---

## 附：本次審查的方法與可重現性

- **10 路平行領域檢視**：每個 agent 先獨立審查程式碼找缺陷，**之後才**讀 GPT 報告給判定（避免錨定），最後回報「GPT 漏掉的」與「GPT 誇大的」。
- **對抗式驗證**：對每條 CRITICAL/HIGH 派一個預設「它是錯的」的驗證者盡力推翻。13 條中推翻 4、降級 3。存活的 1 條 CRITICAL 經**三重確認**（原始發現者 → 對抗驗證者 → 我逐行親驗）。兩條被推翻的安全發現（compose env allowlist、`create_all(checkfirst=True)`）我亦獨立複驗其反證。
- **測試重現**：Windows 11 / Python 3.11.9 / pytest 9.1.1 / ruff 0.15.21 / mypy 2.2.0，四套乾淨 venv（anila-core、anila-agent、ingestion-worker、CSP）。CSP 需 `PYTHONUTF8=1` 才能安裝（requirements 含中文註解，cp950 讀不了）。
- **未驗證項**：ingestion ruff 基線、Studio/pptx-renderer 測試、真 PostgreSQL 下的 RLS 行為（無 PG 環境——這正是問題本身）。
