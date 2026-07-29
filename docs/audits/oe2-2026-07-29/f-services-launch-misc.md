# OE-2 領域審計 F —— Service Registry / Launch / Platform Links / 雜項

- 稽核基準:`SYSTEM-MAP.md`(repo root,399 行,2026-07-29),**唯一規格權威**。
- 稽核範圍:指派的 18 個檔案(全部在 `services/csp/` 下)。測試不在範圍。
- 稽核單位:construct(單一欄位／狀態機／端點／閘門／資料表／契約型別／驗證規則)。
- 本檔為唯讀稽核產出;未修改 repo 任何檔案。

---

## 0. 結論摘要

| 分類 | 數量 |
|---|---|
| CAT-A(規格要求或專案安全紅線,保留＋改引 SYSTEM-MAP) | 17 |
| CAT-B(無規格依據但無持續成本,保留＋記債) | 19 |
| CAT-C(無規格依據＋有持續成本＋唯一理由是舊 doc,收斂候選) | 26 |

**一句話**:本領域是本次審計中「doc 07 密度」最高的一塊。SYSTEM-MAP 全文 **沒有任何一處**
描述「註冊一個外部 GUI 服務」這件事——`註冊` 五次出現全部指 **agent 或模型**(L33/L62/L135/L197/L201),
`服務` 唯一一次出現是 L374「模型服務同仁」。規格認得的網站只有固定三個
(ANILA / ANILALM / CSP,L29–36)。整套 `registered_services`(33 欄)＋ Launch Gateway
(launch token / manifest / service_launches / service_audit_callbacks / service_project_bindings)
是為一份**已被取代的 doc 07** 造的多租戶 GUI 服務平台。

三個最硬的客觀事實(不是推論):

1. **`service_launches` 與 `service_audit_callbacks` 全 repo 沒有任何讀取路徑。**
   `grep -rn "ServiceAuditCallback\|ServiceLaunch" app/` 除了 model 定義與 `app/models/__init__.py` 的
   re-export 外,零命中。前端 `apps/csp-governance-ui/src/api/services.js:29` 呼叫的
   `GET /api/services/{id}/audit-callbacks` **後端不存在**(`app/api/services.py` 只有 POST)。
   → 兩張只寫不讀的資料表。
2. **`service_launches.trace_id` 直接牴觸規格明文。** SYSTEM-MAP L211:
   「→ 一張表加幾個索引。**不需要 span 樹、parent 關係、trace id。**」
   而 `migrations/versions/r1_0006_service_registry.py:207` 為 `trace_id` 建了索引。
3. **`registry_backfill.py` 保存的是規格說可以刪掉的資料。** SYSTEM-MAP L323:
   「現有資料 | 有真實對話,**沒有知識庫,全部可刪** → 資料庫可以砍掉重來」。
   整條「保留 `platform_links` 表以求 downgrade safety ＋ 逐列 backfill ＋ id 保序 ＋
   Postgres sequence reset ＋ `/api/platform-links` 相容外皮 ＋ 前端雙模式 fallback」
   的機械,唯一目的是保存一批規格已宣告可拋棄的列。

⚠ **重要界線**:`RegisteredService` 目前**確實在跑**——它是 `/api/platform-links` 的實體後端,
ANILA 落地頁的平台連結清單(ANILALM / CSP / codeserver / gitlab / n8n)都是它的列。
所以收斂的正確形狀不是「刪掉整張表」,而是**把它砍回九欄的可見連結清單 ＋ 授權**,
把 24 個 doc 07 目標欄位與整個 Launch Gateway 拆掉。詳見 §5 收斂包。

---

## 1. Q1 檢索紀錄(全部實檢 SYSTEM-MAP 原文,無一憑記憶)

已對 SYSTEM-MAP.md 逐字 grep 的詞(zh-TW ＋同義詞＋英文):

`服務` `註冊` `連結` `入口` `啟動` `iframe` `SSO` `單一登入` `免再登入` `登入` `Studio`
`ANILALM` `專案` `心智圖` `manifest` `well-known` `JWKS` `JWT` `簽章` `稽核` `trace`
`密等` `分級` `浮水印` `部門` `授權` `指派` `endpoint` `任務` `派工` `快照` `撤回` `記憶`
`附件` `回饋` `匯出` `複製` `列印` `分享` `帳號` `治理` `後台` `key` `API` `上傳` `刪除` `平台`

零命中(規格中完全不存在的概念):
`iframe`、`SSO`、`單一登入`、`manifest`、`well-known`(僅 L151 的 `/.well-known/jwks.json`)、
**`任務`(Task)**、**`快照`(snapshot)**、`心智圖`(僅 L360 說它不是產出物)。

反覆引用的規格條文(下表以行號 + 逐字引用回填):

- **L11**「**給中科院全院用的 AI 助理平台**,後台是為了發 API key、看用量等營運需要。」
- **L13–15**「**最重要的約束:這個系統由一個人維運。** 偶爾有一位幫忙。/ 任何需要專人照顧的機制(守衛、儀式、多步驟部署)都是負債,/ 除非它換來的東西大於「一個人要維護它」的成本。」
- **L31–33** 三個網站表:ANILA(登入入口、落地頁)/ ANILALM / **CSP 後台**「治理:建知識庫、註冊 agent/模型、發 key、看用量」
- **L35**「刷一次卡,三個都能進。同一個帳號。」
- **L59–65** 角色表:一般使用者 / dev / admin / owner / 單位管理員(五個,無「服務管理員」)
- **L135–136**「3 | dev | 在 **CSP** 註冊 agent、拿 API key」「4 | admin+ | 指派哪些使用者可以用這個 agent」
- **L150–152**「**正解:短效簽章 token。** CSP 派工時簽一個 5 分鐘有效的 JWT,內含 `{user_id, department, agent_id}`,agent 用 CSP 的公鑰(`/.well-known/jwks.json`)驗簽。…**CSP 現在就有這整套基礎設施**(RS256 金鑰、JWKS 端點)。」
- **L197 / L201**「**註冊方式**:指向一個 endpoint,把它 `/v1/models` 的模型**整批帶進來**」「誰能註冊:admin + 被 admin 授權的 dev」
- **L211**「→ 一張表加幾個索引。**不需要 span 樹、parent 關係、trace id。**」
- **L227–228**「密等在**專案啟動時就標好了**,平台主要是**記錄**它。/ 實際會碰到:**無機密 / 營業秘密 / 密 / 機密**。」
- **L234–242** 五個外流面門檻表 ＋「可以做 = 密等 ≤ **營業秘密**」「要落稽核 = 密等 ≥ **營業秘密**」
- **L275**「記什麼 | 讀取受控文件/對話、上傳、刪除、改密等、匯出、列印、分享、登入登出、呼叫模型/agent、管理動作」
- **L278**「防竄改 | **要** —— 明確是為了防止 admin 權限的人偷偷做假」
- **L280**「⚠ **append-only 的稽核帳因此是必要的,不是過度設計。** 威脅模型包含特權內部人。」
- **L323**「現有資料 | 有真實對話,**沒有知識庫,全部可刪** → 資料庫可以砍掉重來」
- **L387**「以下在現行程式碼裡存在,但**不對應上述任何需求**」(§13,六項列舉)
- **L396–399** 使用者原話:「我就是想要紀錄 Agent 用量也想要可以被派工,但我不知道為什麼給你們做一做就這麼奇怪。」/「需求是兩句話,做出來的是一套分散式追蹤治理協定。」

---

## 2. 主表:Service Registry(`models/registered_service.py`、`schemas/registered_service.py`、`migrations/r1_0006`、`r1_0008`)

| # | Construct | file:line | doc ref | Q1 證據(SYSTEM-MAP 逐字＋行號 / 無依據) | Q2 持續成本 | Q3 舊 doc 是唯一理由? | 分類 | 建議動作 |
|---|---|---|---|---|---|---|---|---|
| F-01 | `registered_services` 表本體(33 欄 GUI 服務註冊表) | `app/models/registered_service.py:50-51`;`migrations/versions/r1_0006_service_registry.py:75-161` | doc 07 §3 | **無依據**(searched: 服務/註冊/連結/入口/啟動/iframe/SSO)。`註冊` 全部指 agent/模型:L33「註冊 agent/模型」、L197「指向一個 endpoint,把它 `/v1/models` 的模型整批帶進來」。規格認得的網站只有三個固定站(L29–36) | 高:33 欄 admin 表單、前端 `PlatformLinksView.vue` 雙模式、每新增一個連結都要面對 24 個非必要欄位 | 是 | **CAT-C** | 收斂:砍回九欄可見連結表(見 §5 包 A) |
| F-02 | `slug`(字串服務識別碼,unique index;兼 launch token `aud` / path param) | `registered_service.py:58`;`r1_0006:162-164` | doc 07 §3 | **無依據**(searched: slug/識別/服務)。它存在的兩個消費端(`aud` claim、`{service_id}` path)本身都無規格依據 | 中:每列必須產生並保持唯一;`unique_slug` 工具鏈 | 是 | **CAT-C** | 隨 F-01 收斂;若保留連結表,整數 id 已足夠 |
| F-03 | `owner_department_id` / `owner_admin_user_id` | `registered_service.py:65-70` | doc 07 §3 | **無依據**(searched: 擁有/負責/部門/指派)。L136 只說「admin+ 指派哪些使用者可以用這個 agent」——是**授權**不是**所有權** | 低-中:兩個 nullable FK,建立時要選人;`api/services.py:207` 自動填 admin.id,實際無讀取者 | 是 | **CAT-C** | 收斂:刪欄 |
| F-04 | `service_admin_user_ids`(per-service 委派管理員名單,可改服務設定) | `registered_service.py:72`;消費端 `app/api/services.py:98-101, 271-272` | doc 07 §3/§11 | **無依據**。L59–65 角色表逐一列出 **五個**角色(一般使用者/dev/admin/owner/單位管理員),並明說單位管理員是「**目前不存在,要新增**」(L65)。沒有第六種「服務管理員」。L74 更明文限縮:單位管理員「❌ 指派 agent/模型權限(admin 才行)」 | **高:這是一條額外的授權層。** 一人維運要永遠推理「誰能改這個服務」有兩個答案;限制未來角色模型設計 | 是 | **CAT-C** | 收斂:刪除委派層,改設定一律 require_admin |
| F-05 | `service_type` 五值封閉 enum(project_portal / analysis_gui / artifact_tool / data_system / workflow_system) | `registered_service.py:74-76`;`schemas/registered_service.py:20-26` | doc 07 §3 | **無依據**(searched: 類型/種類/服務)。規格從未分類外部服務 | 中:422 驗證、admin 必選、前端下拉。**全 repo 無任何邏輯讀它**(僅 create/update/serialize) | 是 | **CAT-C** | 收斂:刪欄＋刪 enum |
| F-06 | `project_entry` / `project_id`(服務宣告自己是某專案的主入口) | `registered_service.py:77-80` | doc 07 §13 | **無依據**(searched: 專案)。L163 的「專案知識庫」、L105 的「專案 agent」是**知識庫/agent** 的形容詞,規格中沒有 project 實體 | 中:兩欄 ＋ 下游 `service_project_bindings` 表 ＋ 存取演算法 step 7 | 是 | **CAT-C** | 收斂:與 F-18 一起刪 |
| F-07 | `entry_url`(承接 legacy `platform_links.url`) | `registered_service.py:82`;compat property `:141-145` | doc 07 §3 | **間接依據**:L24–25「ANILA -->|同一帳號,免再登入| ANILALM / CSP」、L31 ANILA 是「登入入口、所有人的落地頁」。要有落地頁連到別的站,就要存 URL | 低:本來就存在的欄位 | 否 | **CAT-B** | 保留;更名回 `url`,改引 SYSTEM-MAP §1 |
| F-08 | `allowed_origins` ＋ launch 前的 origin 一致性檢查 | `registered_service.py:83`;守衛 `app/api/services.py:128-140` | doc 07 §3 | **無直接依據**;它是 F-24(launch token)的 fail-closed 守衛(避免把 token 附到非預期 origin) | 中:每列要維護白名單,填錯就 400 | 是(隨父構造) | **CAT-C(隨父)** | 隨 launch 一併收斂;**只要 launch token 還在,這道守衛不得弱化**(紅線:憑證交付面) |
| F-09 | `launch_mode`(new_tab / iframe)、`iframe_allowed` | `registered_service.py:84-87`;`schemas:28-31` | doc 07 §3 | **無依據**(searched: iframe/內嵌/分頁 → 零命中) | 中:admin 表單兩個選項、前端 badge(`serviceRegistry.js:32,68`)。`launch_mode` 只被抄進 `service_launches.mode`(`api/services.py:418`),**不影響任何行為**(iframe 與 new_tab 產出的 URL 完全相同);`iframe_allowed` 零讀取 | 是 | **CAT-C** | 收斂:刪兩欄 |
| F-10 | `sso_mode` 三值 enum(card_sso / oidc / launch_jwt) | `registered_service.py:89`;`schemas:33-36` | doc 07 §3 | **無依據**(searched: SSO/單一登入/免再登入)。L35「刷一次卡,三個都能進。同一個帳號。」講的是**一個帳號一次卡登**,不是 per-service SSO 模式選單。規格從未提 OIDC | 中:enum 驗證、admin 必選。**零讀取** | 是 | **CAT-C** | 收斂:刪欄＋刪 enum |
| F-11 | `supports_launch_token` | `registered_service.py:90-92` | doc 07 §3 | **無依據**。且**零讀取**:`api/services.py:343` 的 launch 端點不檢查此旗標就直接簽 token | 低-中:一個永遠沒人看的布林,誤導 admin 以為它有作用 | 是 | **CAT-C** | 收斂:刪欄 |
| F-12 | `data_ownership` / `data_ingress` / `data_egress` ＋兩個值域驗證器 | `registered_service.py:94-98`;`schemas:45-46, 83-97` | doc 07 §3/§9 | **無依據**(searched: 資料/流入/流出/歸屬)。L158–169「資料放哪」表列的是**知識庫/附件/對話/Studio 產出**,不是服務的資料流宣告 | 中:兩個 422 驗證器要維護值域;`data_ownership` **連 Create/Update schema 都沒有**,永遠是常數 `"self_managed"`,只出現在 response(`schemas:155`) | 是 | **CAT-C** | 收斂:刪三欄＋兩個驗證器 |
| F-13 | `healthcheck_url` / `trace_callback_url` | `registered_service.py:100,102` | doc 07 §3 | `healthcheck_url`:**部分依據**——L300「平台沒回應(`/health` 連續失敗)」屬告警訊號,但那是**平台自己**的 /health,不是外部服務的。`trace_callback_url`:**規格反對**——L211「**不需要 span 樹、parent 關係、trace id**」 | 低(零讀取)但 `trace_callback_url` 屬明文反對項 | 是 | **CAT-C** | 收斂:刪兩欄。若 §9 告警要監外部服務,另立最小欄位並改引 L298–305 |
| F-14 | `audit_callback_url` | `registered_service.py:101` | doc 07 §3/§10 | **無依據**;隨 F-27 稽核回呼協定 | 低(零讀取) | 是 | **CAT-C** | 隨 F-27 收斂 |
| F-15 | `service_client_id` ＋ fail-closed client↔service 綁定 | `registered_service.py:109-114`;`migrations/versions/r1_0008_service_client_binding.py:34-52`;強制點 `app/api/services.py:499-541` | doc 07 §3 gap / ADR-0008 | **無 SYSTEM-MAP 明文**,但屬**專案安全紅線**:未綁定 → 403(default-deny),綁定不符 → 403,兩者皆落稽核。其防護目標有規格背書:L278「防竄改 | **要** —— 明確是為了防止 admin 權限的人偷偷做假」、L280「威脅模型包含特權內部人」——允許任一整合金鑰持有者往任意服務灌稽核事件,正是規格要防的偽造 | 中:admin 要多做一次綁定 | 是(但受紅線保護) | **CAT-A(條件式)** | **保留、不得弱化**,前提是 F-27 存在;若 F-27 收斂掉,本欄隨父移除。改引 §8 L278/L280 |
| F-16 | `classification_ceiling` ＋存取演算法 step 6 硬天花板(admin 亦不可越過) | `registered_service.py:116`;強制點 `app/services/access_control.py:19-24`(非本領域檔案,僅為理解引用) | doc 07 §3/§12 | **間接依據**:L241「可以做 = 密等 ≤ **營業秘密**」、L234–237 複製/匯出/分享/列印門檻表——把帶密等的內容交給外部服務等同外流面。並與 OE-1 已裁定的 `classification_ceiling` KEEP 一致 | 低:單一 nullable 欄,fail-closed | 否 | **CAT-A** | 保留;改引 §8 L234–242。⚠ 值域受 **OE-3** 影響(現用五級 `ClassificationLevel`),門檻語意受 **OE-4** 影響 |
| F-17 | `required_roles` / `is_public` / `is_active` / `sort_order` | `registered_service.py:117-134` | doc 07 §3(`sort_order` 由 platform_links 承接) | **依據充分**:L61「用被指派的 agent 與模型」、L63 admin「**指派一般使用者可用的 agent/模型**」、L202「**每個人看到的模型清單不同**(依指派)」——「每個人看到的清單不同」是規格明白的模式 | 低:既有欄位 | 否 | **CAT-A** | 保留;改引 §2 L61–63 / §6 L202 |
| F-18 | `ServiceProjectBinding` 表 ＋ unique 約束 ＋ 三個端點 ＋ 兩個 schema | `registered_service.py:148-180`;`r1_0006:248-279`;`schemas:246-258`;端點 `api/services.py:595-661` | doc 07 §13 | **無依據**(searched: 專案)。規格中無 project 實體。且 `access_control.py:26-27` 自承 step 7 是「MVP no-op pass … there is no user↔project store to gate on」 | 中-高:一張表 ＋ 三個 REST 端點 ＋ 存取演算法一個永遠 pass 的步驟 | 是 | **CAT-C** | 收斂:刪表、刪端點、刪 step 7 |
| F-19 | config_source 事實來源四件組(`config_source` / `env_seed_key` / `db_editable_fields` / `last_seeded_at`) | `registered_service.py:128-131`;偵測 `app/services/registry_backfill.py:46-60`;seed `app/services/auto_seed.py:37-80, 496-510`;前端鎖定 `apps/csp-governance-ui/src/utils/serviceRegistry.js:26-30` | doc 07 §3/§15.1 | **無依據**(searched: 播種/env/事實來源/覆蓋)。這是為了解決「AUTO_REGISTER_LINKS 每次開機蓋掉 admin 編輯」而發明的機制 | **高**:每列 4 個欄位;admin 編輯行為依 `config_source` 而異;`db_editable_fields` 白名單要人維護;前端要畫「相容模式/鎖定」UI。正是 L13–15 說的「需要專人照顧的機制…是負債」 | 是 | **CAT-C** | 收斂:**只留一個事實來源**。院內只有五、六個連結,建議刪掉 `AUTO_REGISTER_LINKS` 播種、DB 為唯一來源,四欄全刪 |
| F-20 | `LaunchRequest` / `LaunchResponse` 契約 | `schemas/registered_service.py:180-196` | doc 07 §5/§6/§13 | **無依據**;`task_id` / `source_snapshot_id` 兩個欄位所指的實體(任務、快照)在規格中**完全不存在**(searched: 任務/派工/快照 → 任務、快照零命中) | 中:公開 API 契約 | 是 | **CAT-C** | 隨 §5 包 B 收斂 |
| F-21 | `AuditCallbackPayload` / `Actor` / `Resource` / `Response` ＋ `event_type` regex 驗證 | `schemas/registered_service.py:202-240` | doc 07 §10 | **無依據**(searched: 稽核)。L275 列的稽核事件全是 **CSP 自己**的動作(讀取/上傳/刪除/改密等/匯出/列印/分享/登入登出/呼叫模型 agent/管理動作),不含「外部服務回報事件」 | 中:四個契約型別 ＋ 一條 regex | 是 | **CAT-C** | 隨 F-27 收斂 |
| F-22 | `RegisteredService.url` compat property | `registered_service.py:141-145` | doc 07 §14 | **無依據**;純粹是為了讓 `PlatformLinkResponse` 序列化不變 | 低 | 是 | **CAT-B** | 收斂 F-01 時一併消失;現階段記債 |
| F-23 | `r1_0006` 冪等補欄區塊(修 alembic 鏈漂移:icon / sort_order / required_roles) | `r1_0006:45-72` | doc 10 §17.3 | **無依據**(這是工程債修補,非需求) | 低:一次性、冪等、已跑過 | 否(修的是真漂移) | **CAT-B** | 保留;若 F-01 收斂則整段隨 migration 重寫 |
| F-24 | `r1_0006` Postgres sequence reset | `r1_0006:311-317` | doc 07 §14 | **無依據**;僅為 backfill 保序 id 服務 | 低 | 是(隨 F-26) | **CAT-B** | 隨 backfill 收斂 |

---

## 3. Launch 模組(`app/modules/launch/*`、`app/modules/__init__.py`、`models/service_launch.py`)

| # | Construct | file:line | doc ref | Q1 證據 | Q2 持續成本 | Q3 | 分類 | 建議動作 |
|---|---|---|---|---|---|---|---|---|
| F-25 | Service Launch Gateway 作為「所有正式 GUI service launch 必經閘道」 | `app/modules/launch/__init__.py:1-12` | doc 02 §1 / doc 07 / doc 10 §12 | **無依據**(searched: 啟動/入口/閘道)。`啟動` 三次命中:L227「專案啟動時就標好密等」、L390「不符就拒絕啟動」、L391「卡片 CRL 的啟動硬門檻」——後兩者正是 §13「這份圖譜沒有的東西」 | 高:一整個 module ＋ 端點 ＋ 資料表 | 是 | **CAT-C** | 收斂(見 §5 包 B) |
| F-26 | `app.modules` 邊界契約(importlinter independence + forbidden 契約 ＋ CI gate) | `app/modules/__init__.py:1-16`;`services/csp/.importlinter:1-28`;`infra/ci/lint-boundaries.sh:2` | doc 02 §1 / doc 10 §4/§14 | **無依據**(searched: 邊界/模組/契約/gate)。反向證據:L13–15「任何需要專人照顧的機制(守衛、儀式、多步驟部署)都是負債」;L390 明列「部署姿態契約…不符就拒絕啟動」屬 §13 不需要清單 | 中:一道會擋合併的 CI gate ＋ 契約檔要跟著新 module 更新 | 是 | **CAT-B**(降級理由:gate 便宜且自動,不需人工儀式;但四個 module 之一(launch)若收斂,契約檔須同步縮減) | 保留＋記債;launch 收斂時同步刪 `.importlinter` 中的 `app.modules.launch` 條目 |
| F-27 | Launch token 14 claims(`iss/aud/launch_id/service_id/user_id/employee_id/department_id/roles/task_id/trace_id/classification_level/source_snapshot_id/iat/exp`) | `app/modules/launch/token.py:29-69` | doc 07 §6 | **無依據 —— 但高度可回收。** L150–152 確實要求一個短效簽章 JWT,然而那是**給 agent 派工用**的:「CSP 派工時簽一個 5 分鐘有效的 JWT,內含 `{user_id, department, agent_id}`,agent 用 CSP 的公鑰(`/.well-known/jwks.json`)驗簽」。本 claims 集是給 **GUI 服務啟動**用,含 `launch_id`/`task_id`/`source_snapshot_id`/`trace_id` 四個規格無此概念的欄位 | 中:14 個 claim 的公開契約 | 是(以現用途論) | **CAT-C(可回收)** | **收斂但不要刪碼**:把 `build_launch_claims` / `issue_launch_token` 改造成 **P2.1** 的 agent 派工 token(L150–152 明文要求),claims 縮成 `{iss, aud, user_id, department, agent_id, iat, exp}` |
| F-28 | `LAUNCH_TOKEN_TTL_MINUTES = 10`(doc 建議 5–10,取上界) | `app/modules/launch/token.py:25-26` | doc 07 §6 | **規格給的是 5**:L150「簽一個 **5 分鐘有效**的 JWT」。現值 10 是舊 doc 的上界 | 低(一個常數) | 是 | **CAT-C** | 回收為 P2.1 時 **TTL 必須改成 5 分鐘**,對齊 L150 |
| F-29 | RS256 簽章路徑 ＋ `kid` ＋ 沿用 JWKS 信任錨 | `app/modules/launch/token.py:72-83` | doc 07 §6/§15.5 | **CAT-A(專案安全紅線:JWT 信任錨)**;且有規格正面背書 L152「**CSP 現在就有這整套基礎設施**(RS256 金鑰、JWKS 端點)」 | 低 | 否 | **CAT-A** | **保留、不得弱化**;改引 §4 L150–152 |
| F-30 | `LAUNCH_TOKEN_ISSUER = "anila-csp"` | `app/modules/launch/token.py:24` | doc 07 §6 | **無依據**(規格未指定 iss 字串) | 極低 | 是 | **CAT-B** | 保留＋記債(回收為 P2.1 時沿用) |
| F-31 | Service manifest 抓取 `GET {origin}/.well-known/anila-service.json` ＋ `GET /api/services/{id}/manifest` | `app/modules/launch/manifest.py:27-56`;端點 `api/services.py:579-589` | doc 07 §4 | **無依據**(searched: manifest / well-known → 僅 L151 的 jwks.json)。規格沒有「服務自我描述檔」的概念;最接近的 L114「比對各 agent 的**自我描述**」是 **agent** 的自述,且用於 router 派工 | 中:一個對外 HTTP 呼叫面 ＋ 端點 ＋ 5s timeout / 64KB 上限 / 錯誤型別 | 是 | **CAT-C** | 收斂:刪 manifest.py 與端點 |
| F-32 | manifest 抓取前的中央 SSRF 守衛 `validate_outbound_url` | `app/modules/launch/manifest.py:39-41`;`:15` | doc 07 §4 | **CAT-A(專案安全紅線:SSRF guard)**;規格側面背書 L347「`/router/docs` 匿名可讀、洩內部路徑 | A05 安全設定缺陷」、L337「不得有 OWASP Top 10 高風險項」 | 低 | 否 | **CAT-A** | **保留、不得弱化**;若 F-31 收斂則本守衛隨呼叫端消失(不是被弱化) |
| F-33 | manifest 尺寸/逾時/不跟隨轉址上限(5s、64KB、`follow_redirects=False`) | `manifest.py:18-19, 43, 51-52` | doc 07 §4 | **CAT-A(隨 F-32,屬 SSRF 縱深防禦:不跟隨轉址是繞過 guard 的主要手法)** | 低 | 否 | **CAT-A** | 隨 F-32 |
| F-34 | `service_launches` 表(每簽一張 token 一列) | `models/service_launch.py:39-66`;`r1_0006:167-207` | doc 07 §5/§6/§10 | **無依據**,且**全 repo 無讀取路徑**(`grep ServiceLaunch` 僅 model + `models/__init__.py` re-export)。稽核需求 L275 的清單不含「啟動 GUI 服務」 | 中-高:只寫不讀的表會無界成長,且無保留策略(規格 L276 稽核「留多久 = **半年**」對它不適用因為它不是稽核帳) | 是 | **CAT-C** | 收斂:刪表 |
| F-35 | `service_launches.trace_id` ＋ 其索引 | `models/service_launch.py:55`;`r1_0006:207` | doc 07 §5 | **規格明文反對**:L211「→ 一張表加幾個索引。**不需要 span 樹、parent 關係、trace id。**」;L393 §13 亦列 trace-test 為不需要之物 | 中:一個索引 ＋ 一組 `new_trace_id()` 產生器 | 是 | **CAT-C(最高信心)** | 收斂:刪欄刪索引 |
| F-36 | `service_launches.task_id` FK → `tasks.id` | `models/service_launch.py:52-54`;`r1_0006:182-187` | doc 07 §5 | **無依據**(searched: 任務 → **零命中**)。規格中不存在 Task 實體 | 中:跨領域 FK,綁住 tasks 表的生命週期 | 是 | **CAT-C** | 收斂:隨 F-34 |
| F-37 | `service_launches.source_snapshot_id` | `models/service_launch.py:56` | doc 07 §5 | **無依據**(searched: 快照 → **零命中**) | 低-中(另有 `_validate_source_snapshot_access` 授權分支,`api/services.py:143-161`) | 是 | **CAT-C** | 收斂:隨 F-34 |
| F-38 | `service_launches.status` / `consumed_at`(自承保留給未來一次性語意) | `models/service_launch.py:61, 64`;docstring `:6-9` | doc 07 §6/§15.5 | **無依據**;`status` 永遠是 `"issued"` 從不轉移,`consumed_at` 永遠 NULL | 低(死欄) | 是 | **CAT-B** | 隨 F-34 一併消失;現階段記債 |
| F-39 | `service_audit_callbacks` 表(append-only,外部服務回報稽核事件) | `models/service_launch.py:69-93`;`r1_0006:210-245`;寫入 `modules/launch/service.py:87-107`;端點 `api/services.py:479-573` | doc 07 §10 | **無依據**,且**無讀取路徑**(後端無 GET;前端 `apps/csp-governance-ui/src/api/services.js:29` 呼叫的 GET 端點根本不存在)。L280 的 append-only 背書是給 **CSP 自己的稽核帳**(防特權內部人),不是給「接受外部服務投稿的稽核」——後者反而擴大 L278 要防的偽造面 | 中-高:一張只寫不讀的表 ＋ 一個對外認證端點 ＋ 前端一個永遠 404 的呼叫 | 是 | **CAT-C** | 收斂:刪表 ＋ 刪 POST 端點 ＋ 刪前端呼叫。若日後真要,改走 CSP 既有 audit_logs |
| F-40 | `service_audit_callbacks.integration_key_id` FK | `models/service_launch.py:88-92` | doc 07 §10 | 隨 F-39;其存在的價值(可歸屬)由 F-15 綁定提供 | 低 | 是 | **CAT-C(隨父)** | 隨 F-39 |
| F-41 | 全域 `ON DELETE SET NULL` preserve-history 策略(grant / launch / callback 於服務刪除後存活) | `models/service_launch.py:45,50,53,76,81,90`;`models/service_access_grant.py:59-64`;`api/platform_links.py:149-183` | doc 07 §14/§15.1 | **部分依據**:L278「防竄改」、L280「append-only 的稽核帳…是必要的」支持「授權/稽核軌跡不因刪除而蒸發」。但這裡保護的是 launch/callback 兩張**本身無依據**的表 | 低 | 部分 | **CAT-B** | 授權軌跡(grants)那半保留並改引 §8 L278/L280;launch/callback 那半隨父收斂 |

---

## 4. Platform Links / Grants / Backfill

| # | Construct | file:line | doc ref | Q1 證據 | Q2 持續成本 | Q3 | 分類 | 建議動作 |
|---|---|---|---|---|---|---|---|---|
| F-42 | `platform_links` 表整張保留(標 DEPRECATED,「for downgrade safety」) | `app/models/platform_link.py:15-26`;`r1_0006:12-20` | doc 07 §14 | **規格明文反對保留動機**:L323「現有資料 | 有真實對話,**沒有知識庫,全部可刪** → 資料庫可以砍掉重來」。為一個宣告可砍掉重來的資料庫做 downgrade safety,理由已消失 | 中:一張殭屍表永遠在 schema 裡、在 `models/__init__` 裡、在每次 migration 檢查裡 | 是 | **CAT-C** | 收斂:刪表(重啟樹上 `.15` 資料本來就要砍) |
| F-43 | `registry_backfill.backfill_registered_services`(逐列複製、id 保序、時間戳保留、origin 回填、grants.service_id 回填) | `app/services/registry_backfill.py:63-129`;呼叫點 `r1_0006:310` | doc 07 §14/§15.1 | **無依據**,理由同 F-42(L323)。它保存的是規格說可拋棄的資料 | 中:129 行 DB-agnostic 遷移碼要維護 | 是 | **CAT-C** | 收斂:與 F-01/F-42 一起,改為單一乾淨 migration |
| F-44 | `env_seeded_names()` / `AUTO_REGISTER_LINKS` 環境播種偵測 | `registry_backfill.py:46-60`;`app/config.py:152`;`app/services/auto_seed.py:37-80, 496-510` | doc 07 §15.1 | **無依據**(searched: 播種/env/自動)。規格未要求以環境變數宣告平台連結 | 中:JSON 環境變數要人維護,與 DB 事實來源打架(即 F-19 存在的原因) | 是 | **CAT-C** | 收斂:與 F-19 綁在一起,擇一保留 |
| F-45 | `/api/platform-links` 相容外皮(5 個端點,實體後端已是 `registered_services`) | `app/api/platform_links.py:1-183` | doc 07 §14 | **無依據**;存在理由自承是「existing CSP admin UI and any callers keep working with zero changes」(`:4-6`)。而 CSP UI 是本專案自有的(`apps/csp-governance-ui`),不是無法改的第三方 | **高:兩套 API 蓋同一張表。** 前端 `PlatformLinksView.vue:301` 還做 runtime 探測(先打 `/api/services`,404 才退回 legacy),UI 上印「· 相容模式(platform_links)」badge。一人維運要永遠記得兩條路徑 | 是 | **CAT-C** | 收斂:選一條(建議留 `/api/platform-links` 的簡單形狀、刪 `/api/services` 的 33 欄形狀),刪掉前端 fallback 探測 |
| F-46 | `DELETE /{link_id}/purge` 硬刪端點 | `api/platform_links.py:149-183` | doc 07 §14 | **無依據**(searched: 刪除)。L275 只要求刪除**落稽核**,未要求提供硬刪 UI。已有 soft delete(`:122-146`) | 低-中:一個不可逆的破壞性端點 | 是 | **CAT-B** | 保留＋記債(有落稽核,危害受控);收斂 F-45 時順手評估是否需要兩種刪除 |
| F-47 | `ServiceAccessGrant` 表本體(per-user / per-department opt-in 授權) | `app/models/service_access_grant.py:32-83` | migration 0012 / `docs/platform/multi-service-integration-plan.md` §7.5 | **依據充分**:L61「用**被指派的** agent 與模型」、L63 admin「**指派一般使用者可用的 agent/模型**」、L202「**每個人看到的模型清單不同**(依指派)」——per-user/per-department 授權是規格模式 | 低-中 | 否 | **CAT-A** | 保留;改引 §2 L61–63 / §6 L202,並刪掉 docstring 對已失效 `multi-service-integration-plan.md` 的引用 |
| F-48 | user XOR department CHECK 約束 | `service_access_grant.py:34-39` | migration 0012 | **無明文**,但是 F-47 的完整性守衛(避免「同時命中」的曖昧)。L69 單位管理員「**綁在部門樹的某個節點**」顯示部門維度授權是規格語彙 | 低(DB 層自動) | 否 | **CAT-B** | 保留＋記債 |
| F-49 | soft revoke(`revoked_at` 而非 DELETE)＋ active 局部唯一索引 | `service_access_grant.py:73`;docstring `:8-11` | migration 0012 | **依據充分**:L275「記什麼 … **管理動作**」、L278「防竄改 | **要**」——「誰在何時授了什麼、誰撤銷」正是要留痕的管理動作 | 低 | 否 | **CAT-A** | 保留;改引 §8 L275/L278 |
| F-50 | 雙 FK 並存(`platform_link_id` legacy ＋ `service_id` new),存取演算法兩邊都比對 | `service_access_grant.py:48-64`;`app/services/access_control.py:52-72`(非本領域,僅引用) | doc 07 §14/§15.1 | **無依據**;純為 F-43 backfill 的雙寫過渡 | 中:每次授權查詢要 OR 兩欄,永久的認知負擔 | 是 | **CAT-C** | 收斂:與 F-42/F-43 一起,只留一個 FK |

---

## 5. 雜項:對話 / 訊息 / ingestion 中的 doc 引用構造

> 本節只稽核這些檔案裡**有 doc 引用**的構造(依派工界定),不稽核整檔。
> ⚠ 分類等級值域屬 **OE-3**、外流門檻屬 **OE-4**,已另案裁定,下表不重複報。

| # | Construct | file:line | doc ref | Q1 證據 | Q2 持續成本 | Q3 | 分類 | 建議動作 |
|---|---|---|---|---|---|---|---|---|
| F-51 | 五級分類共通欄位之 `classification_level`(對話) | `app/models/conversation.py:46-48` | doc 08 §5 Slice 3a | **依據充分**:L227–228「密等在**專案啟動時就標好了**,平台主要是**記錄**它。/ 實際會碰到:**無機密 / 營業秘密 / 密 / 機密**。」;§8 L234–242 的五個外流面門檻必須靠每個物件帶密等才能判 | 低 | 否 | **CAT-A** | 保留;改引 §8 L227–242。⚠ 值域由 **OE-3** 對齊四級 |
| F-52 | 同上,訊息 | `app/models/message.py:29-32` | doc 08 §5 | 同 F-51(訊息是 L275「讀取受控…對話」的最小粒度) | 低 | 否 | **CAT-A** | 同 F-51 |
| F-53 | 同上,ingestion collection ／ document | `app/models/ingestion.py:91-94, 168-171` | doc 08 §5 Slice 3a | 同 F-51;另 L275「讀取**受控文件**」直接對應 document 層密等 | 低 | 否 | **CAT-A** | 同 F-51 |
| F-54 | `classification_latched_at`(四處) | `conversation.py:49`;`message.py:33`;`ingestion.py:96, 172` | doc 08 §5 | **無直接依據**(searched: 閂鎖/時間/升密)。間接:L275 稽核要記「改密等」——但那應由稽核帳承擔,此欄是去正規化副本 | 低(自動填,無人工流程) | 是 | **CAT-B** | 保留＋記債 |
| F-55 | `classification_source`(四處) | `conversation.py:50`;`message.py:34`;`ingestion.py:97, 173` | doc 08 §5 | **無依據**(searched: 來源/出處) | 低(nullable,自動填) | 是 | **CAT-B** | 保留＋記債 |
| F-56 | `classification_event_id` FK → `classification_events`(四處) | `conversation.py:51-55`;`message.py:35-39`;`ingestion.py:98-102, 174-178` | doc 08 §5/§6 | **無直接依據**;間接靠 L275「改密等」＋ L278「防竄改」。⚠ 父表 `classification_events` 不在本領域檔案集,其本身的依據由分類領域負責 | 低(nullable FK) | 部分 | **CAT-B** | 保留＋記債;請分類領域一併判 `classification_events` 表 |
| F-57 | legacy `classified` 布林保留為 compatibility read model ＋ 鏡射規則 `classified = level >= 機密` | `conversation.py:31-33, 42-45`;消費端 `app/api/conversations.py:292, 316` | doc 08 §15 Step 3 | **無依據**:規格只有**一個**密等概念(L228 四級),沒有第二個布林。且鏡射門檻 `>= 機密` 與 §8 的兩條線(L241「可以做 = 密等 ≤ 營業秘密」、L242「要落稽核 = 密等 ≥ 營業秘密」)**都不一致** | **中:雙寫狀態,永遠有漂移風險**;兩個消費端(搜尋摘要抑制、讀取落稽核)其實應直接讀 level | 是 | **CAT-C** | 收斂:改為由 `classification_level` 衍生(property 或 view),刪除持久化欄。⚠ **必須與 OE-3/OE-4 同批做**,否則門檻會二度漂移 |
| F-58 | `classification_inherited` 布林(記憶引用導致自動升密的來源標記)＋ UI 分流 banner | `conversation.py:34-41`;契約 `app/api/conversations.py:110-127` | P3 / Sprint 14 / migration 0031(Bell-LaPadula no-write-down) | **無依據**,且規格指的是**相反方向**:L188「**密等內容不納入記憶**」(密等內容根本不該進記憶)、L189「⚠ **對話中途升密 → 之前萃取的記憶要撤回** → 每則記憶要記得來源對話,才找得到」(要的是 記憶←對話 的撤回鏈,不是 對話←記憶 的升密鏈) | 低-中:一個欄位 ＋ 一條 API 契約 ＋ 一條前端 banner 分支 | 是 | **CAT-B** | 保留＋記債(成本低、方向安全);但**規格要的機制(L189 記憶撤回鏈)另在記憶領域,請確認有人做**。長期正解是 L188 的入口阻擋,而非事後升密 |
| F-59 | 單向閂鎖 / 無 `/declassify` 端點(「once latched, never downgraded」) | `app/api/conversations.py:418-434` | README Wave 2 / Sprint 8 X / Phase K | **CAT-A(依據充分,且為派工校準點)**:L227「密等在專案啟動時就標好了,平台主要是**記錄**它」＋ L275 改密等落稽核 ＋ L278「防竄改…防止 admin 權限的人偷偷做假」——提供 HTTP 降密後門正是規格要防的 | 低 | 否 | **CAT-A** | **保留、不得弱化**;改引 §8 L227/L275/L278 |
| F-60 | 已分類對話的搜尋摘要抑制(只比對標題,snippet 留 None) | `app/api/conversations.py:262-270, 292-302` | (無 doc 引用,列此因與 F-57 同一消費鏈) | **間接依據**:L73–74 單位管理員「❌ 看對話明文」、§8 外流面表。惟現行判準走 legacy `classified`(= `>= 機密`),受 **OE-4** 影響 | 低 | 否 | **CAT-A** | 保留;判準改讀 `classification_level`,門檻由 OE-4 定線 |
| F-61 | 讀取已分類對話時落稽核 `log_classified_access` | `app/api/conversations.py:316-317` | (無 doc 引用) | **依據充分**:L238「**落稽核** | 不用 | **要** | **要** | **要**」、L255「2. **每次讀取落稽核**」 | 低 | 否 | **CAT-A** | 保留;改引 §8 L238/L255。⚠ 觸發門檻由 **OE-4** 改為 `≥ 營業秘密` |
| F-62 | `DocumentRelation` 跨文件關係表(rule/manual/llm 三來源、confidence、複合 FK 同 collection 約束、三個索引、order-independent `target_ref` 解析) | `app/models/ingestion.py:310-375` | design v2 §3/§7、codex #1/#9、migration 0039 | **無依據**(searched: 關係/引用/圖/GraphRAG/心智圖)。最接近的 L360「**心智圖不是產出物**,是讓使用者快速理解知識庫節點的**瀏覽方式**」——是**瀏覽 UI**,不是文件關係圖譜。L141「**檢索是 agent 自己做的**,CSP 只提供搜尋 API」進一步縮小 CSP 的檢索職責 | 中-高:一張表、3 個索引、2 個複合 FK、1 個唯一約束,以及一條 regex 引文抽取管線 | 是 | **CAT-C** | 收斂候選。⚠ **跨領域**:ingestion/RAG 屬其他領域,本項僅登記為 doc 衍生構造,請該領域確認取捨 |
| F-63 | `IngestionEvalRun`(Chunking Evaluator:sample docs / strategies / queries / judge LLM / results) | `app/models/ingestion.py:187-223` | Sprint 4(無 doc 編號,但屬 doc 期產物) | **無依據**(searched: 切塊/評估/策略)。L176「塞不下 → 才切塊檢索」只要求切塊會發生,未要求評估器 | 中:一張表 ＋ 一條 worker 管線 | 是 | **CAT-C(跨領域)** | 登記;由 ingestion 領域裁決 |
| F-64 | `UserLlmCredential` API key 靜態加密(AES-256-GCM,PBKDF2 主金鑰;密文/nonce/tag 三欄) | `app/models/ingestion.py:226-265`;`app/api/ingestion/credentials.py:114, 216` | Sprint 4 | **CAT-A(專案安全紅線:祕密處理)**;規格側面背書 L337「不得有 OWASP Top 10 高風險項」、L345「「加密模式」宣稱加密但沒加密 | A02 加密失效」。⚠ 注意 L265「**現階段不做真的加解密**」講的是**內容**加密,不是**憑證**加密——憑證加密不在該豁免內 | 低 | 否 | **CAT-A** | **保留、不得弱化** |
| F-65 | 憑證 endpoint 的 SSRF 守衛 ＋ 更新時重驗 | `app/api/ingestion/credentials.py:38-49, 111-113, 180-186` | doc 04 §8(Slice 6a) | **CAT-A(專案安全紅線:SSRF guard)** | 低 | 否 | **CAT-A** | **保留、不得弱化** |
| F-66 | `endpoint_kind="generic"` 分域(BYO judge/外部 LLM 維持全域 http 旗標語意,不套 model-kind 閘門) | `app/api/ingestion/credentials.py:38-49` | doc 04 §8 | **依據充分**:L198「⚠ **端點可能是 `https://<domain>/v1` 也可能是 `http://<host>:<port>`。內網用 http 是常態,不是安全違規。** 現行程式碼把 http endpoint 當違規要改」——分域正是為了讓 model-kind 端點放行 http 而不整體放寬。與 P0.2 擁有者拍板一致 | 低 | 否 | **CAT-A** | 保留;改引 §6 L198(刪 doc 04 §8 引用) |
| F-67 | 憑證輪替只能 DELETE + POST(PATCH 不含 `api_key`) | `app/api/ingestion/credentials.py:62-66, 12-14` | Sprint 4 | **間接依據**:L275 稽核要記「管理動作」;強制 delete+create 讓輪替在稽核帳上是兩筆明確事件 | 低 | 否 | **CAT-B** | 保留＋記債 |
| F-68 | `AgentLlmCredential = UserLlmCredential` 過渡別名(自承 Sprint 5 要刪) | `app/models/ingestion.py:267-269` | Sprint 4/5 | **無依據**(純過渡相容) | 極低 | 是 | **CAT-B** | 保留＋記債:Sprint 5 從未到來,建議直接刪 |
| F-69 | `document_chunks` 刻意不建 ORM model(強制走 RLS-scoped adapter) | `app/models/ingestion.py:1-14` | (無 doc 引用) | **CAT-A(專案安全紅線:RLS 不可繞過)** | 低 | 否 | **CAT-A** | **保留、不得弱化** |

---

## 6. 檔案覆蓋確認(「沒有可稽核構造」必須明說)

| 檔案 | 狀態 |
|---|---|
| `app/models/registered_service.py` | 已稽核(F-01–F-06, F-07–F-19, F-22) |
| `app/schemas/registered_service.py` | 已稽核(F-05, F-09, F-10, F-12, F-20, F-21) |
| `app/services/registry_backfill.py` | 已稽核(F-43, F-44) |
| `app/modules/launch/__init__.py` | 已稽核(F-25);其餘內容為 re-export，無獨立構造 |
| `app/modules/launch/manifest.py` | 已稽核(F-31, F-32, F-33) |
| `app/modules/launch/token.py` | 已稽核(F-27, F-28, F-29, F-30) |
| `app/modules/__init__.py` | 已稽核(F-26)。**全檔只有 docstring**,無程式碼;唯一構造即模組邊界契約 |
| `app/models/service_launch.py` | 已稽核(F-34–F-41) |
| `app/models/service_access_grant.py` | 已稽核(F-47, F-48, F-49, F-50) |
| `app/models/platform_link.py` | 已稽核(F-42);另 `is_public`/`required_roles` 兩欄的依據同 F-17(CAT-A),但實體已遷至 `registered_services`,此處為殭屍副本 |
| `app/api/platform_links.py` | 已稽核(F-45, F-46) |
| `app/models/ingestion.py` | 已稽核(F-53–F-56, F-62, F-63, F-64, F-68, F-69)。⚠ 依派工只取 doc 引用構造;`IngestionCollection`/`IngestionDocument`/`IngestionJob` 的核心欄位屬 ingestion 領域,未在此判定 |
| `app/api/ingestion/credentials.py` | 已稽核(F-65, F-66, F-67) |
| `app/models/conversation.py` | 已稽核(F-51, F-54–F-58)。**`ConversationShare`(`:69-83`)無 doc 引用,依派工不納入**——但見 §7 鄰接觀察 |
| `app/api/conversations.py` | 已稽核(F-58 契約、F-59, F-60, F-61)。其餘 CRUD/rating/edit 端點無 doc 引用,不在本領域 |
| `app/models/message.py` | 已稽核(F-52, F-54–F-56)。**除五級分類欄位外,全檔無其他 doc 引用構造** |
| `migrations/versions/r1_0006_service_registry.py` | 已稽核(F-01, F-18, F-23, F-24, F-34, F-35, F-39, F-50) |
| `migrations/versions/r1_0008_service_client_binding.py` | 已稽核(F-15)。**全檔只做一件事**(加 `service_client_id` 欄＋索引),無其他構造 |

**沒有任何可稽核構造的檔案:無。** 18 個檔案全部至少貢獻一個構造。

---

## 7. 鄰接觀察(不是本領域的裁決,但發現時應留痕)

1. **`ConversationShare.token`(`app/models/conversation.py:74`)** —— `secrets.token_urlsafe(32)` 的匿名連結分享。
   SYSTEM-MAP L260 明文:「**分享給指定的人/單位**,不是匿名連結。氣隙內網裡大家都有帳號,
   **不需要未登入可讀的入口**」。這是**分享領域**的 CAT-C 級發現(連同 `app/api/public_share.py`),
   在此登記,請分享領域確認。
2. **`classification_ceiling` 目前吃五級 `ClassificationLevel`**
   (`app/schemas/contracts/classification.py:40-46`,前端 `apps/csp-governance-ui/src/utils/serviceRegistry.js:17-23`
   甚至硬編了「極機密/絕對機密」)。OE-3 對齊四級時,**前端這份常數陣列也要一起改**,否則 UI 會送出後端不收的值。
3. **P2.1 已知項(不重報)**:agent 身分靠純文字標頭 ＋ 全艦隊共用靜態 token。
   本領域的 `verify_service_token`(`app/api/services.py:493`)走的是同一套 Service Client Token。
   **可回收提醒**:F-27/F-29 的 RS256 簽章 ＋ JWKS 驗簽路徑,正是 L150–152 要的那套基礎設施,
   P2.1 不必從零寫。
4. **前端有一條 runtime 能力探測**(`apps/csp-governance-ui/src/views/PlatformLinksView.vue:301`:
   「優先走 registered_services;7a 尚未上線時退回 legacy platform_links」)。
   後端兩套 API 都在,這個 fallback 永遠不會觸發,但它讓 UI 多一條路徑與一個「相容模式」badge。
   收斂 F-45 時務必同步清掉。
5. **`api/services.js:29` 呼叫的 `GET /api/services/{id}/audit-callbacks` 後端不存在**——
   前端註解自承「端點可能尚未實作 → 呼叫端需處理 404」。這條死呼叫是 F-39 「只寫不讀」的直接佐證。

---

## 8. 收斂包建議(給指揮官排 PLAN 用;每包可獨立驗收)

**包 A —— Service Registry 瘦身(F-01–F-06, F-09–F-14, F-18, F-19, F-22)**
把 `registered_services` 從 33 欄砍回 **9 欄**(`id/name/url/icon/description/sort_order/is_active/is_public/required_roles`)
＋ 保留 `classification_ceiling`(F-16, CAT-A)。刪 `service_type`/`sso_mode`/`launch_mode`/`iframe_allowed`/
`supports_launch_token`/`data_*`/`project_*`/`owner_*`/`service_admin_user_ids`/config_source 四件組。
刪 `service_project_bindings` 表與三個端點。
規格依據:L11(後台只為發 key、看用量)、L13–15(一人維運)、L31–36(只有三個站)。

**包 B —— Launch Gateway 拆除,token 機械回收給 P2.1(F-08, F-20, F-25, F-27, F-28, F-31, F-34–F-40)**
刪 `service_launches`、`service_audit_callbacks`、manifest 抓取、`/launch` 與 `/audit-callbacks` 端點。
**但 `token.py` 的 RS256 簽章 ＋ claims builder 保留改造成 P2.1 的 agent 派工 token**
(L150–152:5 分鐘 TTL、claims = `{user_id, department, agent_id}`、agent 以 `/.well-known/jwks.json` 驗簽)。
F-15 的 fail-closed 綁定隨 audit-callback 端點一起移除(不是弱化,是父構造消失)。
規格依據:L211(不需要 trace id)、L275(稽核清單不含 GUI launch)、L150–152(真正要的 token 是給 agent 的)。

**包 C —— 相容層拆除(F-42, F-43, F-44, F-45, F-46, F-50)**
刪 `platform_links` 殭屍表、刪 backfill、刪雙 FK、二選一保留單一 API 表面、刪前端 fallback 探測。
規格依據:L323(資料庫可以砍掉重來)——downgrade safety 的前提已不存在。

**包 D —— legacy `classified` 布林衍生化(F-57)**
**必須與 OE-3(四級對齊)、OE-4(兩條門檻線)同批執行**,否則門檻會二度漂移。

**不動的紅線(任何包都不得碰)**:F-15(在其存活期間)、F-16、F-17、F-29、F-32、F-33、F-47、F-49、
F-51–F-53、F-59、F-60、F-61、F-64、F-65、F-66、F-69。
