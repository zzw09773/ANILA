> ⚠ 2026-08-17 盤點：本檔為【歷史紀錄】——稽核報告,保留當時的判定與依據,不代表現況。現行狀態與執行順序見 `PLAN.md`。

> ⚠ **2026-08-01 P2.1**：agent 派工身分已改為平台現簽的 5 分鐘 JWT（JWKS 驗簽；
> 開發者不領 `csk-`／`CSP_SERVICE_TOKEN`）。下文保留當時紀錄，**勿依此做現行接入**；
> 現行上手見 `docs/guides/developer-guide.md` 與治理中心「接入驗簽 · 三級制」。

# OE-2 領域 B:AGENTS REGISTRY 對 SYSTEM-MAP 規格符合性稽核

- 稽核基準:`SYSTEM-MAP.md`(repo 根,399 行,2026-07-29)——**唯一規格權威**。所有 Q1 證據皆為對該檔的實際 grep,行號逐條記錄。
- 稽核範圍(exclusive):`services/csp/` 下 9 個檔(見各節)。跟隨 import 只為理解構件,不裁決別的領域擁有的檔。
- 已在別處裁決、**本報告不重複當作新發現**:OE-1(七態機 + trace-test 硬閘 + full_trace approval blocker → 收斂為 registered/approved/disabled;`classification_ceiling` 確認 KEEP)、OE-3(五級 → 規格四級)、OE-4(外流門檻兩條線)。凡屬 OE-1 覆蓋者一律標記 `CAT-C (OE-1)`,列出僅為滿足「每個 doc-cited 構件都要出現在表中」的驗收條件。

## 0. 結論摘要

| 類別 | 數量 | 說明 |
|---|---|---|
| CAT-A | 24 | 規格有據 或 專案安全紅線 → 保留、把 doc 引用改標 SYSTEM-MAP |
| CAT-B | 25 | 規格無據但無持續成本(多為 r1_0004 加了卻**從未被寫入**的死欄位)→ 保留、記負債 |
| CAT-C | 33(其中 **20 條屬 OE-1 已裁決**,新增候選 **13 條**) | 規格無據 + 有持續成本 + 唯一理由是舊 doc → 收斂候選 |

新增(非 OE-1)的收斂候選,依影響排序:
1. **Manifest 契約整叢**(A11–A14、A10、B15、C6、C14、E1)——SYSTEM-MAP 全篇無 manifest 概念。
2. **`runtime_config`**(B26)——零規格依據的 admin runtime 旋鈕 + 30 秒輪詢契約。
3. **`shadow` 註冊模式 → draft**(C7)。
4. **`bound_collection_id` 基數 = 1**(B6)——與 L140「一個 agent 可以綁**多個**知識庫」直接抵觸(守衛本身是 CAT-A,要改的是形狀)。
5. **`RuntimeType.OPENWEBUI_PIPE_COMPATIBLE` 預留值**(A2)。

---

## 1. `app/schemas/contracts/agents.py`(21 處 doc 引用)

| # | 構件 | file:line | doc ref | Q1 證據(SYSTEM-MAP 逐字 + 行號) | Q2 持續成本 | 類別 | 建議動作 |
|---|---|---|---|---|---|---|---|
| A1 | `RuntimeType` 封閉 5 值 enum | agents.py:44-52 | doc 05 §3 | **無依據**(searched: `runtime`、`langchain`、`anila-agent`、`Open WebUI`、`pipe`)。唯一相關文字 L134「在 **MLSteam** 用 anila-agent / langchain 建 agent」——講的是**在 MLSteam 怎麼建**,沒要求 CSP 記錄 runtime 種類 | 低:全庫**無任何分支讀 runtime_type**(只有註冊寫入 + UI 顯示 `DeveloperAgentsView.vue:156,247`)。封閉字彙略微限制未來擴充 | CAT-B | keep + mark-debt(可降級為自由字串標籤) |
| A2 | `OPENWEBUI_PIPE_COMPATIBLE` 預留值 | agents.py:49-50 | doc 05 §9 / doc 06 | **無依據**(searched: `Open WebUI`、`pipe`、`bridge`)。L197「像 Open WebUI」只描述**模型註冊方式**(整批帶入 `/v1/models`),與 agent runtime 無關 | 有:註解自陳「v1 不交付 bridge」,是**永遠不會被選的字彙成員**,卻進了註冊 UI 的下拉選單,讓 dev 每次註冊都要面對一個假選項 | **CAT-C** | converge(移除該 enum 成員;UI 選項同步移除) |
| A3 | `DEFAULT_RUNTIME_TYPE` | agents.py:57 | doc 05 §3 | 同 A1,**無依據** | 無(常數) | CAT-B | keep + mark-debt(隨 A1 處置) |
| A4 | `ApprovalStatus` 七值 | agents.py:60-69 | doc 05 §3 | L387「以下在現行程式碼裡存在,但**不對應上述任何需求**」;L136 的流程只有「4 admin+ 指派哪些使用者可以用這個 agent」,無審批關卡 | 有 | CAT-C (OE-1) | 依 OE-1 收斂為 3 值 |
| A5 | `LEGACY_APPROVAL_BACKFILL` | agents.py:73-77 | doc 05 §3 | 同 A4 | 有(遷移映射需維護) | CAT-C (OE-1) | 隨 OE-1 |
| A6 | `DOWNGRADE_APPROVAL_MAP` | agents.py:81-89 | doc 05 §3 | 同 A4 | 低(僅 r1_0004 downgrade 用) | CAT-C (OE-1) | 隨 OE-1 |
| A7 | `REGISTER_DEFAULT_APPROVAL` | agents.py:92 | doc 05 §3 | 同 A4 | 有 | CAT-C (OE-1) | 隨 OE-1(改為 `registered`) |
| A8 | `TRACE_TEST_ELIGIBLE_STATES` / `STATE_AFTER_TRACE_PASS` | agents.py:96-104 | doc 05 §3 | L393「agent 必須回傳 6 種 span 且單一根節點才准核准(trace-test)」列在 §13 **不對應任何需求** | 有 | CAT-C (OE-1) | 隨 OE-1 刪除 |
| A9 | `AuditLevel`(單一成員 `full_trace`)+ `DEFAULT_AUDIT_LEVEL` | agents.py:107-113 | doc 05 §2 | **無依據**(searched: `稽核等級`、`full trace`、`審計`)。§8「稽核」L275 只規定**記什麼**,未規定 agent 分稽核等級 | 無:全庫只有 default 與序列化,**零讀取者**(grep `audit_level` 僅 3 處,全在 model/response) | CAT-B | keep + mark-debt;OE-1 一旦拿掉 full_trace blocker,此欄唯一語意即消失,建議併入同一支 drop migration |
| A10 | `TraceCallbackMode` | agents.py:116-121 | doc 05 §4 | **無依據**(searched: `callback`、`SSE`、`回報通道`);反證 L211「**不需要 span 樹、parent 關係、trace id**」 | 有(manifest 契約成員) | **CAT-C** | converge(隨 manifest 叢一起移除) |
| A11 | `ManifestCapabilities` | agents.py:127-132 | doc 05 §4 | **無依據**(searched: `manifest`→ 0 命中、`well-known`→ 僅 L151 的 `jwks.json`、`capabilities`、`能力`) | 有(fail-closed 契約) | **CAT-C** | converge |
| A12 | `ManifestTrace`(`required`/`protocol` Literal/`callback_mode`) | agents.py:135-140 | doc 05 §4 | **無依據**;且 L211「不需要 span 樹、parent 關係、trace id」直接否定 trace 協定必要性 | 有:`protocol` 是 `Literal["anila-full-trace-v1"]`,agent 端協定版本一動即 422 | **CAT-C** | converge |
| A13 | `ManifestClassification`(ceiling + default,兩者必填) | agents.py:143-147 | doc 05 §4 | 分級本身有據(L227「密等在**專案啟動時就標好了**,平台主要是**記錄**它」),但**由 agent 自報 manifest 決定 ceiling** 無依據;且此處驗過的值**從未寫進被強制執行的欄位**(見 §9 觀察 D4) | 有:五級 enum(OE-3 將改四級)+ 必填 | **CAT-C** | converge(分級改由 CSP 側欄位設定,見 B20/D3) |
| A14 | `AgentManifest` + `extra="forbid"` fail-closed | agents.py:150-171 | doc 05 §4 | **無依據**(searched: `manifest`、`宣告`、`清單`、`自我描述`)。L114「比對各 agent 的**自我描述**」對應的是 `description_for_router`(CAT-A,見 C3),不是 manifest | 有:註冊/更新的 422 閘門,agent 端多帶一個欄位就被拒;`api_version: Literal["v1"]` 把版本焊死 | **CAT-C** | converge(降為選填自由 JSON,或整組移除) |
| A15 | `TraceTestItemStatus` | agents.py:174-179 | doc 06 §8 | L393(§13 不對應任何需求)逐字列出 trace-test | 有 | CAT-C (OE-1) | 隨 OE-1 |
| A16 | `TraceTestItem` | agents.py:182-188 | doc 06 §8 | 同 A15 | 有 | CAT-C (OE-1) | 隨 OE-1 |
| A17 | `TraceTestReport` | agents.py:191-201 | doc 06 §6/§8 | 同 A15;另 L211「不需要 span 樹、parent 關係、trace id」 | 有(API 回應契約 + UI 消費 `DeveloperAgentsView.vue:892-937`) | CAT-C (OE-1) | 隨 OE-1(UI 同步) |

---

## 2. `app/models/agent.py`

| # | 構件 | file:line | doc ref | Q1 證據 | Q2 持續成本 | 類別 | 建議動作 |
|---|---|---|---|---|---|---|---|
| B1 | `UserAgentPermission` 表 | agent.py:10-18 | —(前 doc) | **有據**:L136「| 4 | admin+ | **指派哪些使用者可以用這個 agent** |」;L61「用被指派的 agent 與模型」 | 有(規格要求) | **CAT-A** | keep + re-cite L136 |
| B2 | `ApiKeyAgentPermission` 表 | agent.py:21-29 | — | **有據**:L125「MLSteam agent ──API key──▶ CSP」+ L349「agent/模型權限、跨使用者資料存取 | A01 存取控制失效」 | 有 | **CAT-A**(存取控制紅線) | keep + re-cite L125/L349 |
| B3 | `owner_department_id` | agent.py:38-41 | doc 05 §3 | **無依據**(searched: `部門` 全 11 處;L210 要求**用量列**帶部門,L216「報表要能回答「部門用了哪些 agent」」——由 usage 列推導,非 agent 擁有者欄位) | 無:全庫**零寫入零讀取**(grep 命中全在 model_registry / registered_service) | CAT-B | keep + mark-debt(併入 drop migration) |
| B4 | `base_model_id`(FK,註冊時必填) | agent.py:42-45 | —(前 doc) | **有據**:L210「要記的欄位:**時間、使用者、部門、模型、agent、token 數**」——`usage_service.py:573-609`「Group token usage by `agents.base_model_id`」正是 agent 呼叫的**模型維度**來源;L216 報表需求 | 有(規格要求) | **CAT-A** | keep + re-cite L210/L216 |
| B5 | `bound_collection_id` 作為**檢索範圍守衛** | agent.py:46-51;`api/ingestion/search.py:107` | S-Q1 | **紅線**:L163「專案知識庫 …… 誰看得到 = **綁定的 agent + 有權限的人**」+ L349 A01。agent 的 `csk-` 若可搜任意 collection = 跨庫資料存取 | 有(規格要求) | **CAT-A**(存取控制紅線) | keep + re-cite L163/L349 |
| B6 | 同欄位的**基數 = 1**(單一 collection) | agent.py:46-51;registration.py:93,307 | S-Q1 | **與規格抵觸**:L140「- 一個 agent 可以綁**多個**知識庫」;L142「⚠ collection ID 設定在 **MLSteam 那邊的 `.env`**,不是在 CSP 綁定」 | 有:限制未來設計(規格要求多庫),且 CSP 側綁定與 L142 的責任劃分不一致 | **CAT-C** | converge = **放寬為多對多**(保留守衛、改形狀),不是刪除 |
| B7 | `manifest_url` | agent.py:53-55 | doc 05 §3 | **無依據**(`manifest` 0 命中) | 無:**零寫入**(唯一 grep 命中是 `modules/launch/manifest.py` 的同名函式,與本表無關) | CAT-B | keep + mark-debt |
| B8 | `healthcheck_url` | agent.py:56 | doc 05 §3 | **無依據**;L300「平台沒回應(`/health` 連續失敗)」講的是平台自身告警,不是 agent 可自訂探點 | 無:**零寫入**(grep 命中皆為 `registered_service`) | CAT-B | keep + mark-debt |
| B9 | `agent_version` | agent.py:58-60 | doc 05 §3/§4/§13 | **無依據**(searched: `version`、`版本`、`semver`) | 無:**零寫入**(連 manifest.version 都沒回填) | CAT-B | keep + mark-debt |
| B10 | `runtime_type` 欄位 | agent.py:61-68 | doc 05 §3 | 同 A1,**無依據** | 低(見 A1) | CAT-B | keep + mark-debt |
| B11 | `supported_task_types` | agent.py:70-71 | doc 05 §3 | **無依據**(searched: `task type`、`任務種類`) | 無:註冊/更新端點**都不接受**此欄,零寫入 | CAT-B | keep + mark-debt |
| B12 | `output_schema` | agent.py:73 | doc 05 §3 | **無依據**(searched: `schema`→0 命中) | 無:零寫入(`input_schema` 有接受,`output_schema` 沒有——不對稱) | CAT-B | keep + mark-debt |
| B13 | `allowed_tool_ids` | agent.py:74 | doc 05 §3 | **無依據**(searched: `tool`、`工具`;L215 的 tool 是比喻) | 無:零寫入 | CAT-B | keep + mark-debt |
| B14 | `input_schema` / `capabilities`(鬆散 JSON) | agent.py:72,75 | 前 doc | **無依據**,但為註冊 UI 既有欄位 | 無 | CAT-B | keep + mark-debt |
| B15 | `manifest_json` | agent.py:76-78 | doc 05 §4/§12 | **無依據**(見 A14) | 有:序列化進 `AgentResponse`,且是 trace-test `manifest_valid` 項的輸入 | **CAT-C** | converge(隨 manifest 叢) |
| B16 | `trace_callback_mode` | agent.py:79-80 | doc 05 §4 | **無依據**;L211 反證 | 無:**零寫入**(連 manifest 存檔時都沒回填) | CAT-B | keep + mark-debt |
| B17 | `health_status` | agent.py:81-82 | 前 doc | **有據**:L303「MLSteam 上某個 agent 派工失敗」為必做告警訊號之一;L290「監控告警 …… **必做**」 | 有(規格要求) | **CAT-A** | keep + re-cite L303/L290(字彙問題見 G1/D2) |
| B18 | `approval_status`(七值)欄位 | agent.py:83-89 | doc 05 §3 | 見 A4 | 有 | CAT-C (OE-1) | 隨 OE-1 |
| B19 | `audit_level` | agent.py:90-93 | doc 05 §2 | 見 A9,**無依據** | 無(零讀取) | CAT-B | keep + mark-debt;建議併入 OE-1 的 drop migration |
| B20 | `classification_ceiling` | agent.py:94-96 | doc 05 §3/§11 | **有據**(且 OE-1 已確認 KEEP):§8 L241-242「可以做 = 密等 ≤ 營業秘密 / 要落稽核 = 密等 ≥ 營業秘密」;L267「collection 與 agent 上有一個**列管標記**」;執行點 `services/proxy/ceiling.py:165 enforce_agent_ceiling` | 有(規格要求) | **CAT-A** | keep + re-cite §8;**但目前無任何寫入路徑,守衛實質失效**(見 D3) |
| B21 | `trace_test_passed_at` / `trace_test_report` | agent.py:97-100 | doc 05 §6 | L393 逐字列於 §13 不對應任何需求 | 有 | CAT-C (OE-1) | 隨 OE-1 |
| B22 | `requires_encryption`(列管旗標) | agent.py:101-103 | 前 doc / doc 08 §3 | **有據**:L267「collection 與 agent 上有一個**列管標記**,存取它的人與動作全部記錄」 | 有(規格要求) | **CAT-A** | keep + re-cite L267,**但必須改名/改字**:L268「不可以寫「已加密」,要寫「列管」或「受控存取」」;L347 把「加密模式」列為 OWASP A02(見 D5) |
| B23 | `default_classification_level` | agent.py:104-110 | doc 08 §3/§4/§15 | **有據**:L227「密等在**專案啟動時就標好了**,平台主要是**記錄**它」;消費點 `api/proxy.py:77`、`api/classification_inventory.py:98` | 有(規格要求) | **CAT-A** | keep + re-cite L227;等級字彙隨 OE-3 改四級;缺寫入路徑見 D3 |
| B24 | `approved_by` / `approved_at` | agent.py:111-114 | 前 doc | **無據**(searched: `核准者`、`誰核准`);L275「管理動作」要求的是**稽核帳**,不是資料列欄位 | 無 | CAT-B | keep + mark-debt(reject 也在寫這兩欄,語意混淆,見 F4) |
| B25 | `bootstrap_token_hash` / `_expires_at` / `_consumed_at` / `_issued_by` | agent.py:117-129 | Sprint 8 X | **有據 + 紅線**:L135「| 3 | dev | 在 **CSP** 註冊 agent、**拿 API key** |」;憑證處理屬專案安全紅線(單次 `bsk-` → `csk-`,`_consumed_at` 的 atomic CAS 是防重放的關鍵) | 有(規格要求) | **CAT-A** | keep + re-cite L135 |
| B26 | `runtime_config`(tool_permissions / workspace / guardrails,agent 每 30 秒輪詢) | agent.py:131-155 | Sprint 13 PR A3(**無 doc 引用**) | **無依據**(searched: `工具權限`、`護欄`、`guardrail`、`沙箱`、`輪詢`、`30 秒`——全 0 命中) | 有:整組端點面(`app/api/agents/runtime_config.py` + `/me/runtime-config` [HISTORICAL: removed])+ agent 端輪詢契約 + admin 要維護的 JSON 形狀;L13「這個系統由**一個人**維運」 | **CAT-C** | converge 候選;⚠ 端點模組不在本次 FILE SET,裁決前需併看 |
| B27 | `api_version` 欄位 | agent.py:57 | 前 doc | **無依據**(searched: `api_version`、`版本`) | 無(固定 "v1") | CAT-B | keep + mark-debt |

---

## 3. `app/api/agents/registration.py`

| # | 構件 | file:line | doc ref | Q1 證據 | Q2 持續成本 | 類別 | 建議動作 |
|---|---|---|---|---|---|---|---|
| C1 | `_enforce_endpoint_url`(註冊時 SSRF 守衛) | registration.py:38-54,257 | doc 04 §8 | **紅線**(專案不可協商);規格側 L198「端點可能是 `https://<domain>/v1` 也可能是 `http://<host>:<port>`。**內網用 http 是常態,不是安全違規**」——已由 P0.2 的 `endpoint_kind="agent"` 分域滿足 | 有(必要) | **CAT-A** | keep + re-cite L198(旗標分域)+ 紅線 |
| C2 | `GET /template/download`(anila-agent 範本 zip) | registration.py:219-240 | 前 doc | **弱依據**:L134「在 **MLSteam** 用 **anila-agent** / langchain 建 agent」——規格承認 anila-agent 是建置途徑,但未要求 CSP 提供下載 | 無(dev 自助,無強制流程);僅需範本目錄存在於映像檔 | CAT-B | keep + mark-debt |
| C3 | `POST /register`(含 `description_for_router` 必填、dev/admin 閘門) | registration.py:243-322;`_common.py:37-44` | — | **有據**:L135「3 | dev | 在 **CSP** 註冊 agent、拿 API key」;L201「誰能註冊:admin + 被 admin 授權的 dev」;L114「比對各 agent 的**自我描述**」→ `description_for_router` | 有(規格要求) | **CAT-A** | keep + re-cite L135/L201/L114 |
| C4 | `base_model_id` **必填**(Field(...)) | registration.py:90,259-276 | — | **有據**:L210 用量欄位含「模型」;`usage_service.py:588-609` 以 `Agent.base_model_id` 分組出「agent 用了哪個模型」 | 有(規格要求) | **CAT-A** | keep + re-cite L210 |
| C5 | `collection_id` 選填 + `_require_collection_access` | registration.py:93-95,281-283 | S-Q1 | **有據**:L163「綁定的 agent + **有權限的人**」;L349 A01 | 有(必要) | **CAT-A** | keep + re-cite L163/L349(基數問題見 B6) |
| C6 | `manifest` 請求欄位(註冊時 fail-closed 422) | registration.py:100-101,285-292 | doc 05 §4 | **無依據**(見 A14) | 有:註冊路徑上的 422 閘門 | **CAT-C** | converge |
| C7 | `shadow: bool` → `approval_status=draft` | registration.py:102-104,295-298 | doc 06 Phase 1 | **無依據**(searched: `盤點`、`shadow`、`草稿`、`draft`——全 0 命中) | 有:多一種註冊模式與一個狀態;`draft` 不在 OE-1 的目標三態內 | **CAT-C** | converge(移除 `shadow` 參數,隨 OE-1 一併清 `draft`) |
| C8 | `AgentResponse` 的 doc-derived 欄位(`runtime_type`/`agent_version`/`audit_level`/`classification_ceiling`/`default_classification_level`/`manifest_json`/`trace_test_passed_at`) | registration.py:122-130,175-183 | doc 05 §3/§4/§6 | 各自依附 B9/B10/B19/B20/B23/B15/B21 之判定 | 低(唯讀鏡射) | CAT-B | keep + mark-debt;隨各欄位處置同步收斂(UI 只消費 `runtime_type`、`trace_test_report`) |
| C9 | `_AGENT_HEALTH_MAP`(舊三值→ agent 字彙) | registration.py:139-152 | 前 doc | **無依據**(健康字彙分級規格未定義);健康檢查本身有據(L303) | 有:與 `health_checker.py` 的五態字彙**各自為政**(見 D2) | CAT-B | keep + mark-debt(建議統一由 `normalize_health_status` 出口) |
| C10 | `GET ""` / `GET /{id}` 可見性(admin-tier 全看、其餘限 owner) | registration.py:325-352 | 前 doc | **有據**:L349「agent/模型權限、跨使用者資料存取 | A01 存取控制失效」 | 有(必要) | **CAT-A** | keep + re-cite L349 |
| C11 | `PUT /{agent_id}` + `AgentUpdateRequest` 刻意省略清單 | registration.py:189-204,355-452 | 前 doc | **無明文**(searched: `編輯 agent`、`更新`);屬營運必要 | 低 | CAT-B | keep;⚠ 省略清單造成 `classification_ceiling`/`default_classification_level` **無任何設定入口**(見 D3) |
| C12 | 端點變更 → **強制退回重新核可**(防「核可後改內網」) | registration.py:389-397,425-438 | 前 doc(H4) | **紅線**(SSRF/存取控制繞道);規格側 L349 A01、L348 A05 | 有(必要) | **CAT-A** | keep + re-cite L349;退回的**目標狀態**隨 OE-1 改為 `registered` |
| C13 | 同上區塊中清空 `trace_test_passed_at` / `trace_test_report` | registration.py:436-437 | doc 05 §6 | 見 A8/B21 | 有 | CAT-C (OE-1) | 隨 OE-1 刪除 |
| C14 | `manifest` 請求欄位(更新時 fail-closed 422) | registration.py:202-203,380-387 | doc 05 §4 | 同 C6 | 有 | **CAT-C** | converge |
| C15 | `DELETE /{agent_id}` 限 admin | registration.py:455-473 | 前 doc | **有據(間接)**:L63 admin 才可管 agent 權限;L275 稽核記「管理動作」;L349 A01 | 有(必要) | **CAT-A** | keep + re-cite L63/L349 |
| C16 | 全端點的 `log_audit_event`(register/update/delete) | registration.py:317-321,442-451,468-472 | 前 doc | **有據**:L275「記什麼 …… 呼叫模型/agent、**管理動作**」;L278「防竄改 **要**」;L280 append-only 稽核帳是必要的 | 有(規格要求) | **CAT-A** | keep + re-cite L275/L278/L280 |

---

## 4. `app/api/agents/health.py`

| # | 構件 | file:line | doc ref | Q1 證據 | Q2 持續成本 | 類別 | 建議動作 |
|---|---|---|---|---|---|---|---|
| D1 | `POST /{id}/health-check`(admin 手動探測) | health.py:60-140 | 前 doc | **有據**:L303「MLSteam 上某個 agent 派工失敗」為必做告警;L289「系統要能自己喊救命」 | 有(規格要求) | **CAT-A** | keep + re-cite L303 |
| D2 | 呼叫時 SSRF 重驗(3 處:health-check / test-connection / trace-test) | health.py:81-94,170-174,400-404 | doc 04 §8 | **紅線**(TOCTOU / DNS rebinding);規格 L198 允許 http 由旗標分域 | 有(必要) | **CAT-A** | keep + re-cite 紅線;⚠ 背景迴圈的同型守衛有缺陷,見 D-obs-1 |
| D3 | `POST /{id}/test-connection` + `TestConnectionResponse` | health.py:143-221 | 前 doc(S-Q3) | **無依據**(searched: `連線測試`、`測試`;L301「資料庫連不上」屬平台告警) | 無:純自助診斷按鈕,不是必經流程(**OE-1 拿掉 `pending_connection_test` 後仍可獨立存在**) | CAT-B | keep + mark-debt(對一人維運確有價值) |
| D4 | `POST /{id}/trace-test` 端點 | health.py:371-489 | doc 05 §6 / doc 06 §8 | L393 逐字:「agent 必須回傳 6 種 span 且單一根節點才准核准(trace-test)」列於 §13「**不對應上述任何需求**」(L387) | 有 | CAT-C (OE-1) | 隨 OE-1 移除 |
| D5 | `_TRACE_TEST_REQUIRED_SPAN_TYPES`(6 型別) | health.py:50-57 | doc 06 §6 | 同 D4(L393 的「6 種 span」正是此常數) | 有 | CAT-C (OE-1) | 隨 OE-1 |
| D6 | `_poll_trace_spans` 有界輪詢 | health.py:224-241 | doc 05 §6 | 同 D4;L211「**不需要 span 樹、parent 關係、trace id**」 | 有 | CAT-C (OE-1) | 隨 OE-1 |
| D7 | `_evaluate_trace_test` 逐項檢核表 | health.py:244-368 | doc 06 §8 | 同 D4 | 有 | CAT-C (OE-1) | 隨 OE-1 |
| D8 | `parentage_single_root` 判定 | health.py:307-317 | doc 06 §8 | L393「…且**單一根節點**才准核准」;L211「不需要 span 樹、parent 關係」 | 有 | CAT-C (OE-1) | 隨 OE-1 |
| D9 | `full_trace_13_complete`(引用 `REQUIRED_AGENT_SPAN_TYPES` 13 型別) | health.py:320-328;`contracts/traces.py:27-41` | doc 05 §6 | L211 逐字否定 span 樹;§13 L393 | 有 | CAT-C (OE-1) | 隨 OE-1(traces 契約屬他域,僅標記依賴) |
| D10 | trace-test 派發帶 `X-ANILA-Classification-Level` 等標頭 | health.py:427-432 | doc 05 §6 | 分級傳遞本身有據(L227),但**此處只服務 trace-test**;真正派工路徑在 `api/proxy.py`(他域) | 有 | CAT-C (OE-1) | 隨 OE-1(不影響 proxy 的分級傳遞) |
| D11 | health/test-connection/trace-test 的 `log_audit_event` | health.py:86-93,202-207,481-488 | 前 doc | **有據**:L275「呼叫模型/agent、管理動作」 | 有(規格要求) | **CAT-A** | keep + re-cite L275 |

---

## 5. `app/api/agents/_common.py`

| # | 構件 | file:line | doc ref | Q1 證據 | Q2 持續成本 | 類別 | 建議動作 |
|---|---|---|---|---|---|---|---|
| E1 | `validate_agent_manifest`(422 fail-closed) | _common.py:17-34 | doc 05 §4 | **無依據**(見 A14) | 有 | **CAT-C** | converge(隨 manifest 叢) |
| E2 | `_require_developer_or_admin`(含 owner tier) | _common.py:37-44 | 前 doc | **有據**:L201「誰能註冊:admin + 被 admin 授權的 dev」;L64「owner | 系統維運者 | **全部**」 | 有(規格要求) | **CAT-A** | keep + re-cite L201/L64 |
| E3 | `_client_ip` / `_resolve_agent` | _common.py:47-63 | — | 純管線(取 IP、404 化);**非可稽核構件** | — | —(不列類別) | 無動作 |

> 本檔**無「完全無可稽核內容」的情形**;E3 兩個 helper 明確登記為管線程式碼。

---

## 6. `app/api/agents/approval.py`

| # | 構件 | file:line | doc ref | Q1 證據 | Q2 持續成本 | 類別 | 建議動作 |
|---|---|---|---|---|---|---|---|
| F1 | `POST /{id}/approve` 端點本身(限 admin) | approval.py:23-34,66-75 | 前 doc | **規格無審批關卡**(L133-138 八步流程只有「3 註冊拿 key → 4 admin 指派使用者 → 5 使用者選它」);惟 **OE-1 已裁定保留 `approved` 態**,故轉態動作留存 | 有(admin 每個 agent 走一次) | **CAT-A**(依 OE-1 裁決保留) | keep;文件改標 OE-1 裁決,不再引 doc 05 |
| F2 | 兩道 409 阻擋(① `trace_test_passed_at` 為空 ② 狀態須為 `pending_security_review`) | approval.py:36-64 | doc 05 §6 | L393(§13 不對應任何需求) | 有 | CAT-C (OE-1) | 隨 OE-1 全數移除 |
| F3 | 核准失敗也寫稽核(status="failure") | approval.py:46-51,58-63 | 前 doc | **有據**:L275「管理動作」;L278「防竄改 **要** —— 明確是為了防止 admin 權限的人偷偷做假」 | 有(規格要求) | **CAT-A** | keep + re-cite L278 |
| F4 | `POST /{id}/reject`(無狀態守衛,覆寫 `approved_by/at`) | approval.py:78-97 | 前 doc | **無明文**;OE-1 目標三態中對應 `disabled` | 低 | CAT-B | keep + mark-debt;OE-1 收斂時順手處理:無條件轉態、且不應污染 `approved_by/approved_at` 語意 |

---

## 7. `app/services/health_checker.py`

| # | 構件 | file:line | doc ref | Q1 證據 | Q2 持續成本 | 類別 | 建議動作 |
|---|---|---|---|---|---|---|---|
| G1 | 五態健康字彙(`FIVE_STATE_HEALTH`、`_LEGACY_HEALTH_MAP`、`normalize_health_status`) | health_checker.py:23-55 | doc 04 §9 / doc 01 §32 | **無依據**(searched: `健康`→0、`degraded`、`降級`、`狀態字彙`);告警本身有據(L300/L303),但規格只要求「連續失敗 → 告警」,二態足矣 | 有(輕):兩套字彙 + 兩張映射表要維護(另見 C9);且 `normalize_health_status` **在 agent 讀取路徑上從未被呼叫** | CAT-B | keep + mark-debt;建議收斂為單一字彙、由 `normalize_health_status` 統一出口 |
| G2 | `probe_model_health_detailed`(探測 /health、/v1/models、/) | health_checker.py:58-94 | doc 04 §9 | **有據**:L300「平台沒回應(`/health` 連續失敗)」;L303 agent 派工失敗 | 有(規格要求) | **CAT-A** | keep + re-cite L300/L303 |
| G3 | `_agent_health_check_loop` + `upsert_alert`/`resolve_alert_by_fingerprint` | health_checker.py:155-198 | doc 04 §9 | **有據**:L290「監控告警 …… **必做**」;L303「MLSteam 上某個 agent 派工失敗」;L296 告警要分級 | 有(規格要求) | **CAT-A** | keep + re-cite L290/L303 |
| G4 | 探測前 SSRF 重驗 | health_checker.py:72-76 | doc 04 §8 | **紅線** | 有(必要) | **CAT-A** | keep;⚠ **帶缺陷**,見 D-obs-1 |
| G5 | 只探 `approval_status == "approved"` 的 agent | health_checker.py:161-165 | 前 doc | 依附 approval 態;OE-1 後 `approved` 仍存在 → 字面值不受影響 | 低 | CAT-B | keep + mark-debt(OE-1 收斂時確認字面值) |

---

## 8. `migrations/versions/r1_0004_agent_registry_upgrade.py`

| # | 構件 | file:line | doc ref | Q1 證據 | Q2 持續成本 | 類別 | 建議動作 |
|---|---|---|---|---|---|---|---|
| H1 | `approval_status` VARCHAR(20)→(30) + `pending` → `pending_connection_test` backfill + server_default | r1_0004:61-131 | doc 05 §3 | 見 A4(L387/L393) | 有 | CAT-C (OE-1) | **不要回退 r1_0004**;由 OE-1 產生的**新前向 migration** 收斂值域與 server_default |
| H2 | 新增 14 欄(見 B3、B7–B13、B15、B16、B19–B21) | r1_0004:71-116 | doc 05 §3/§4/§6 | 各欄依 §2 判定;其中 9 欄**從未被寫入** | 混合 | 逐欄見 §2 | 建議在 OE-1 的收斂 migration 裡一併 drop:`owner_department_id`、`agent_version`、`supported_task_types`、`output_schema`、`allowed_tool_ids`、`manifest_url`、`healthcheck_url`、`trace_callback_mode`、`audit_level`(9 欄,零讀寫,零風險) |
| H3 | `downgrade()`(七值→三值收斂 + drop 全欄) | r1_0004:134-170 | doc 05 §3 | 依附 A4/A6 | 無(實務上不會執行;本樹 alembic 已驗 0→r1_0008) | CAT-B | keep + mark-debt(維持鏈條完整) |

---

## 9. 相鄰觀察(不是 OE-1/3/4 的重複回報,是本域發現的實作缺陷/落差)

**D-obs-1 —— `health_checker` 背景迴圈用錯 `endpoint_kind`,會對合法 http agent 誤報離線(可操作缺陷)**
`_agent_health_check_loop`(health_checker.py:167)→ `check_model_health` → `probe_model_health_detailed` → `validate_outbound_url(base_url)`(health_checker.py:73)**未傳 `endpoint_kind`**,取預設 `generic`(`url_guard.py:310` 簽章)。`generic` 只認 `ANILA_ALLOW_HTTP_ENDPOINT`,不認 `ANILA_ALLOW_HTTP_AGENT_ENDPOINT`。
後果:內網 MLSteam / aiops 的**純 http NodePort agent**(專案 CLAUDE.md §2 明列的標準情境)若只開了 agent 旗標,背景迴圈會判 `unhealthy` 並 `upsert_alert(severity="high", title="Agent X 離線")`——健康的 agent 被持續誤告警。手動端點 `health.py:82,172,402` 都有正確傳 `endpoint_kind="agent"`,只有背景迴圈漏了。這正好打在規格 L198(內網 http 是常態)與 §9 告警可信度上。修法:`probe_model_health_detailed(endpoint_url, endpoint_kind=...)` 開參數,agent 迴圈傳 `"agent"`。

**D-obs-2 —— 健康字彙雙軌,`degraded` 會漏出到 UI**
背景迴圈把五態(含 `degraded`)寫進 `agents.health_status`,但讀取端 `registration.py:145-152` 的 `_AGENT_HEALTH_MAP` **沒有 `degraded` 鍵**,`.get(raw, raw)` 直接原樣吐出;`normalize_health_status` 在 agent 讀取路徑上完全沒被呼叫。前台會拿到未正規化的值。

**D-obs-3 —— `classification_ceiling` / `default_classification_level` 全庫沒有任何寫入路徑**
grep `agents.classification_ceiling =` / `default_classification_level =` 於 `app/` 與 `migrations/` 均無 app 端寫入(只有 r1_0003 的一次性 backfill)。註冊與更新端點都只序列化、不接受這兩個欄位(`AgentUpdateRequest` 刻意省略)。結果:OE-1 確認 KEEP 的 `enforce_agent_ceiling`(`services/proxy/ceiling.py:165`)因 ceiling 恆為 NULL 而**永遠 no-op**。若 OE-1 的 KEEP 要有意義,必須補一個 admin 設定入口(建議掛在 `PUT /api/agents/{id}`,admin-only)。

**D-obs-4 —— manifest 驗過的分級是死資料**
`AgentManifest.classification.{ceiling,default}` 經 fail-closed 驗證後只存進 `manifest_json`,**從未回填**到被強制執行的 `classification_ceiling` / `default_classification_level`。等於兩套真相來源,而權威那套是空的。這是把 manifest 叢列為 CAT-C 的補強理由。

**D-obs-5 —— 「加密模式」字樣直接踩規格明文禁區**
`credentials.py:38-61`(⚠ 不在本次 FILE SET,僅標記)寫的稽核字串是「啟用/停用 agent「X」加密模式」,而規格 L268「**UI 上的字必須誠實** —— 不可以寫「已加密」,要寫「列管」或「受控存取」」,L347 更把「「加密模式」宣稱加密但沒加密」列為 OWASP A02。欄位 `requires_encryption`(B22)本身是 CAT-A(L267 列管標記),但**名稱與文案必須改**。

**D-obs-6 —— `reject` 覆寫 `approved_by/approved_at`**
`approval.py:88-90` 用「核准者」欄位記錄「拒絕者」,且無狀態守衛(可把 `approved` 直接打成 `rejected` 而不清 trace 落章)。OE-1 收斂為 `disabled` 時一併處理。

---

## 10. 測試:本域測試鎖死了哪些「規格無據」的構件

檔案:`services/csp/tests/test_agent_registry_upgrade.py`(451 行,全檔皆為 doc 05/06 導出構件的行為鎖)。**若下列 CAT-C 收斂,本檔幾乎需整檔重寫。**

| 測試 | line | 鎖住的構件 | 收斂後 |
|---|---|---|---|
| `_valid_manifest()` fixture | 53-73 | manifest 全欄位形狀(A11–A14)、`trace.protocol`、`classification.{ceiling,default}` | 刪除或改為自由 JSON |
| `test_valid_manifest_parses` | 161-165 | `AgentManifest` + `RuntimeType.LANGCHAIN` + `trace.protocol` | 刪 |
| `test_unknown_field_rejected` | 167-171 | `extra="forbid"` fail-closed(A14) | 刪 |
| `test_non_five_level_classification_rejected` | 173-177 | 分級 enum 經 manifest 把關(A13);另與 **OE-3** 的四級收斂相關(「絕密」仍非法,但「極機密/絕對機密」將變非法) | 刪或移至分級域 |
| `test_wrong_trace_protocol_rejected` | 179-183 | `ManifestTrace.protocol` Literal(A12) | 刪 |
| `test_register_with_invalid_manifest_returns_422` | 185-203 | 註冊路徑上的 manifest 422 閘門(C6/E1) | 刪 |
| `test_register_with_valid_manifest_stores_json` | 205-224 | `manifest_json` 落地(B15)+ `runtime_type` 回應(B10) | 刪/縮 |
| `test_shadow_register_creates_draft` | 231-247 | `shadow` → `draft`(C7) | 刪 |
| `test_default_register_is_pending_connection_test` | 249-266 | `REGISTER_DEFAULT_APPROVAL`(A7,OE-1)+ `DEFAULT_RUNTIME_TYPE`(A3) | 改為 `registered` |
| `TestApprovalStateMachine`(4 測) | 272-327 | 七態機 + trace 落章 blocker(A4/F2,OE-1) | 全改寫為三態 |
| `TestTraceTest`(5 測) | 333-451 | trace-test 端點、必備 span 型別、單一根、轉態、409/403(D4–D8,OE-1) | 全刪;其中 `test_forbidden_for_non_owner`(440)與 `test_requires_credential_returns_409`(427)的**授權/憑證前置語意有保留價值**,建議移植到仍存在的端點(如 test-connection) |
| `_CORE_SPAN_TYPES` | 39-46 | 重複 `_TRACE_TEST_REQUIRED_SPAN_TYPES`(D5) | 刪 |

另註:`_env_allowances` fixture(76-86)只設 `ANILA_ALLOW_HTTP_ENDPOINT`(legacy 名),因此**測不到 D-obs-1 的分域缺陷**——新旗標 `ANILA_ALLOW_HTTP_AGENT_ENDPOINT` 在本檔零覆蓋。

---

## 11. 建議的收斂順序(給 PLAN 排程用)

1. 併入 OE-1 的收斂 migration:drop 9 個零讀寫欄位(H2)、清 `draft`/`shadow`(C7)、清 trace 落章欄(B21)。
2. manifest 叢一次收斂(A10–A14、B15、C6、C14、E1)+ 對應測試刪除。
3. 補 `classification_ceiling` / `default_classification_level` 的 admin 設定入口(D-obs-3),讓 OE-1 KEEP 的守衛真正生效。
4. 修 D-obs-1(告警誤報)與 D-obs-2(字彙漏出)——與 §9 告警可信度直接相關,成本極低。
5. `requires_encryption` 改名/改文案為「列管」(D-obs-5,規格 L268 明文)。
6. `bound_collection_id` 放寬為多庫(B6,規格 L140),保留守衛。
7. `runtime_config`(B26)需與其端點模組合併裁決,不在本域單獨決定。
