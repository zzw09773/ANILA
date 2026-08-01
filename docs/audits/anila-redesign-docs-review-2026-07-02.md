> ⚠ **2026-08-01 P2.1**：agent 派工身分已改為平台現簽的 5 分鐘 JWT（JWKS 驗簽；
> 開發者不領 `csk-`／`CSP_SERVICE_TOKEN`）。下文保留當時紀錄，**勿依此做現行接入**；
> 現行上手見 `docs/guides/developer-guide.md` 與治理中心「接入驗簽 · 三級制」。

# ANILA Redesign Docs 複核報告（2026-07-02）

> 範圍：`docs/anila-redesign-docs/` 全部 11 份設計文件（00–10）對照當前程式碼逐條查核。
> 方法：6 組平行唯讀查核（每組 1–2 份文件），抽出文件中所有「對現有系統的事實宣稱」，逐條對照原始碼、migration、compose、nginx、腳本驗證；引用皆附 file:line。
> 基準：本機工作樹（分支 `prod-intranet-card`，HEAD `c4c28a2`）；分支特定宣稱另以 `git show origin/prod-intranet-card` 交叉驗證。

---

## 1. 總體結論

全 11 份文件共抽出 **344 條可驗證宣稱：293 條確認（85%）、13 條錯誤、約 36 條部分正確、2 條無法驗證（repo 外組織事實）**。

| 文件組 | 宣稱數 | 確認 | 錯誤 | 部分正確/其他 |
|---|---|---|---|---|
| 00 產品憲章 + 01 domain model | 50 | 44 | 0 | 5 部分、1 無法驗證 |
| 02 系統架構 + 10 遷移護欄 | 67 | 62 | 0 | 5 部分 |
| 03 CSP 治理 + 04 模型 gateway | 74 | 54 | **7** | 13 部分 |
| 05 Agent Registry + 06 OpenWebUI 遷移 | 64 | 57 | 2 | 4 部分、1 無法驗證 |
| 07 GUI Service + 08 分類閂鎖 | 49 | 46 | 2 | 1 部分 |
| 09 API/事件契約 | 40 | 30 | 2 | 6 部分、2 參照文件缺失 |
| **合計** | **344** | **293** | **13** | **~38** |

**品質分佈**：各文件的「Repo evidence / 現況補齊」段落整體非常準確（08 的 24 條現況宣稱 24/24 全對；02/10 零錯誤）。錯誤集中在 **03/04**（API 清單混入不存在的 route、把不存在的防護列為「保留」）與 **09**（SSE meta 形狀、錯誤 envelope 與現況不符）。

**最大的風險不是寫錯，而是基準與遺漏**：見下節。

---

## 2. 跨文件重大發現（重設計前必須先解決）

### 2.1 ⚠ 分支基準分歧：文件混用兩個不同的「現況」（最重大）

本機分支名為 `prod-intranet-card`，但 HEAD `c4c28a2` 是 **origin/main 的後裔（main 系內容）**，對 `origin/prod-intranet-card` 為 **ahead 22 / behind 92**。本機工作樹**完全沒有**卡登子系統：

- 只在 `origin/prod-intranet-card`（v1.2.0 系）存在：`card_auth.py`、`card_auth_service.py`、`cspki_ca_bundle.pem`、`auth_providers.py`、`external_identities`、`REQUIRE_CARD_LOGIN_ONLY`、員編下傳 commit `80f60d4`（`downstream_identity()` / `build_agent_headers()` / `build_model_gateway_headers()` 雙 builder）、`scripts/intranet-deploy.sh`、card 版 `0035_iso_42001_traceability.py`、card compose delta（`./secrets` JWT 掛載、`SSL_CERT_FILE`、`share/pki`、`extra_hosts`）。
- 本機工作樹：純帳密 auth、單一 `_build_downstream_headers()` 送 **DB PK**（非員編）、main 版 `0035_drop_sso...`。

影響：文件多處宣稱（員編 header、card auth route、dual builders）**對 origin 為真、對本機為假**；反之 nginx `/anila` 入口只在本機最新 commit。任何人拿本機樹驗證會誤判文件錯，拿文件驗證 origin 會誤判本機碼缺失。**重設計基準必須先明定：以 `origin/prod-intranet-card`（v1.2.0）為 source of record，並 reconcile 本機分支。**

### 2.2 443 `/anila/` 同源入口未入文件

最近兩個 commit（`c4ef8af`、`c4c28a2`；origin 對應 `a06c0cb`）把 ANILA UI 搬到 **443 `/anila/` 同源入口**（`nginx.conf:385-403`），`anila-ui` build arg `BASE_PATH=/anila/`（`docker-compose.yml:334`）。文件 02/07/10 仍寫「4443 是 runtime UI 入口」且 07 §15.4 說 `/anila/` 在 4443 —— **已過時**。文件建議的「路由收斂」其實已經開始做了。另注意 dev compose 的 `anila-ui` 沒帶 `BASE_PATH`，共用 nginx.conf 的 `/anila/` 區塊在兩個 stack 間不一致。

### 2.3 「現況」與「目標」混寫、未標記

多處把尚不存在的 API/機制寫進看似現況的清單，最容易誓導重設計：

- 03 §11 API 清單混入不存在的 `/api/services/*`、`POST /v1/traces`、`GET /api/traces/{id}`、`GET /api/audit-events`（實為 `/api/audit-logs`）。
- 04 §8 把「classification ceiling 出站前檢查」「模型 key secret envelope」列為**保留（既有）**項——兩者都不存在（模型 gateway key 是明文 env；AES-GCM envelope 只用於 csk-/bsk- 與 ingestion 憑證）。
- 09 §10 把 `anila.retrieval_started/finished` 列入「保留既有事件」——不存在；同節多個事件（`tool_call_*`、`spans`、`todos_updated`…）只有前端 handler + Router passthrough，**repo 內無 producer**（frozen contract 標為 RESERVED/LATENT）。
- 04 §9 健康狀態五態（`degraded`/`disabled` 為發明；實際 models 用 `online/connecting/offline`，agents 欄位註解 `unknown/healthy/unhealthy` 但 health loop 寫入 model 詞彙——這個雙詞彙錯置文件 §11 倒是有抓到）。

### 2.4 SSE 契約：與 frozen contract 不一致、typed terminal 缺席

`docs/platform/router-sse-contract.md`（宣告 FROZEN for v1）與 `test_router_typed_terminal.py` 只存在於 `origin/feat/stage3-typed-terminal`，不在本分支。Doc 09 沒有引用也不一致：

- `anila.meta` 實際欄位是 `{trace_id, trace[], citations, confidence, handoff_chain, follow_ups, latency_ms, classified}`（+`reasoning`、CSP 端 `usage`）；doc 09 寫的 `task_id`/`classification_level` 現況不存在，而 `trace/handoff_chain/follow_ups/latency_ms` 這些 UI 正在吃的欄位被 doc 09 悄悄拿掉。
- Stage 3 的 `anila.terminal` 終止事件、`finish_reason:"length"` → Continue 按鈕契約、`X-Anila-Session-Id` 回應 header、`X-ANILA-Trace-Id` 關聯 header 全部缺席。
- Router 對 `anila.meta` 並非原樣 passthrough（會捕捉 downstream_meta 合併再發），doc 05/09 的「原樣轉發」描述要加註例外。
- 錯誤契約：doc 09 §12 的 `{"error":{code,...}}` envelope 現況不存在——實際是 FastAPI `{"detail": ...}` + Router in-band error（`anila.trace status=error` + 友善文字 + 正常 `[DONE]`）+ resume proxy 的 `event: error`。

### 2.5 分類閂鎖：現況 enforcement 遠比文件想像薄

Doc 08 的傳播機制描述全對（24/24），但**沒有說清楚今天閂鎖真正擋什麼**：

- 後端 enforcement 只有三件事：share 建立 403、public share 事後遮蔽、classified 讀取寫 audit log（**是記錄不是授權**）。
- 「匯出管制」是前端裝飾：`exportConversation` 是 client-side 包 `GET /api/conversations/{id}`，後端無任何匯出閘門。
- 搜尋仍洩漏 classified 對話**標題**（只抑制 snippet）。
- 記憶檢索不過濾加密 chunk（自由召回再閂鎖目前對話）；`requires_encryption` 從不阻擋路由——classified 對話可打任何模型/agent。
- collection/document/chunk（ingestion 層）**零分類欄位**；RLS 只在 ingestion 表、與分類無關；conversations 表無 RLS。
- 無人可解密——包括 admin（`declassify` 端點已因後門疑慮刻意移除；docstring「irreversible by non-admin」誤導）。文件的「Admin 申請降級＋主管批核」是全新機制，且 `departments`/`users` 無任何主管欄位；doc 08 §7 vs §12 還有「admin 申請、admin group 批核」的自我核准迴圈未解。
- ANILALM 是盲點：它有送 `X-ANILA-Conversation-Id`（會觸發閂鎖）但**零 classified UI**（無浮水印/禁用動作）。

### 2.6 TLS/CSPKI 完全沒進 doc 04

Doc 04 的範圍就是「院內不同主機、HTTPS + API Key」，但整個信任鏈機制隱形：`ANILA_MODEL_CA_FILE`/`SSL_CERT_FILE`（CSPKI bundle、「取代非疊加」的 footgun）、FQDN vs IP 要求、`ANILA_ALLOW_HTTP_ENDPOINT`/`ANILA_ALLOW_PRIVATE_ENDPOINT` 放寬旗標（內網 MLSteam agent 就是靠 http + 旗標在跑）、trusted hosts 會跳過所有 host 檢查。文件的「HTTPS required」讀起來比部署現實嚴格。

### 2.7 OpenWebUI：前提在 repo 外，且文件自相矛盾

Repo 內 OpenWebUI **零足跡**（無 compose 服務、無依賴、無資料；只有註解與規劃文件）。「ML Team agent 目前註冊在 OpenWebUI」是 repo 外組織事實，無法從碼驗證。且 doc 00 §7/ADR-0003 把「OpenWebUI→CSP 遷移工具」列為 v1 例外目標，doc 01 人工複核註記卻說 OpenWebUI 匯出匯入「與本專案開發毫無意義」，doc 01 §8 又保留 `openwebui_pipe_compatible` runtime_type——**in-scope 與否必須先拍板**。

### 2.8 已存在的資產被當 greenfield（重設計應「沿用」而非「重蓋」）

- **Usage attribution ~70% 已有**：`request_type`、`caller_agent_id`/`caller_client_id` + partial index、per-agent/by-base-model/by-client rollup 端點。缺的是 task_id、`estimated` 標記（估算 usage 目前與上游回報不可區分）、非串流 agent 轉發不記 usage（碼內註解自認 gap）。
- **csk- 單金鑰雙用途（S-Q1）已上線**：一把 csk- 兼入向 dispatch 驗證＋出向 bound-collection RAG 搜尋（`search.py:69-110` 硬綁 `bound_collection_id`）。Doc 05 §12 要「移除 JWT-only search」——該狀態不存在；Doc 06 §10 把 service-token search 當未做——已做，真正缺的是「以使用者身分＋per-user RLS」的變體。
- **bsk- bootstrap 生命週期**（15 分 TTL、endpoint 綁定、原子 CAS 防重放、rotate 24h grace、revoke）與 legacy 共用 `CSP_SERVICE_TOKEN` 退場追蹤（`is_legacy`、`/api/usage/legacy-token-stats`）——doc 03 憑證表只畫了穩態 csk-。
- **Auth/revocation 契約**：JWT cookie + `token_version` + `token_revocations` + JWKS + Redis pub/sub + studio 冷啟 replay——doc 09 完全沒有 auth 契約段。
- **卡登既有實作**：origin 上已有完整 CMS/PKCS#7 驗章＋CSPKI 鏈＋nonce（16/16 測試綠）。Doc 07 §8 的 Shared Card SSO 應定位為「沿用擴充」而非「另行 port」。
- **X-ANILA-User-Groups 是死管線**：builder 有參數但**沒有任何呼叫端傳入**，兩份文件都把它列為現行契約。
- `X-ANILA-Task-Id`/`X-ANILA-Classification-Level` header 現況完全不存在（下傳只有 service token + User-Id/Email；入向只讀 `X-ANILA-Conversation-Id`/`X-ANILA-Trace-Id`）。

### 2.9 內網部署工具鏈缺席（doc 10）

`origin/prod-intranet-card` 上的 `scripts/intranet-deploy.sh`（card 一次性 bootstrap）、`docs/runbooks/intranet-deployment-runbook.md`、air-gap 交付鏈（`build-and-export-for-intranet.sh`、`download-intranet-models.sh`、`model-serve.sh`、`pack/unpack-chunks.sh` 等）都不在 doc 10 的可搬移清單。對 air-gapped 產品，離線交付工具是真實 deliverable 類別。card compose delta（TLS/JWT/PKI 拓撲）也未列入「card 優先於 main」的差異清單。

---

## 3. 13 條明確錯誤（INCORRECT，建議直接修文件）

| # | 文件 | 錯誤 | 實際 |
|---|---|---|---|
| 1 | 03 §11 | `/api/services/*` | 實為 `/api/platform-links` + `/api/service-access-grants` |
| 2 | 03 §11 | `POST /v1/traces` | 不存在，repo 無任何 trace 收取端點 |
| 3 | 03 §11 | `GET /api/audit-events` | 實為 `GET /api/audit-logs`（`api/audit_logs.py:11`） |
| 4 | 03 §11 | `GET /api/traces/{trace_id}` | 不存在 |
| 5 | 04 §8 | 「classification ceiling 出站前檢查」列為保留 | 不存在；現況是事後/環繞式 metadata 閂鎖，從不阻擋出站呼叫 |
| 6 | 04 §9 | `GET /api/models/{id}/health`、`POST /{id}/test` | 實為 `POST /api/models/{model_id}/health-check`（`api/models.py:448`）；models 無 GET health、無 /test |
| 7 | 04 §9 | 健康五態 `unknown/healthy/degraded/unhealthy/disabled` | models 實際 `online/connecting/offline`；`degraded`/`disabled` 不存在 |
| 8 | 05 §12 | 移除「Agent 直查 CSP 的 JWT-only search」 | 該端點已支援 csk-（`search.py:69-110`，硬綁 bound collection），JWT-only 狀態不存在 |
| 9 | 06 §10 | 「新增 service-token search endpoint」列為風險對策 | 已存在；缺的是 per-user RLS 變體 |
| 10 | 07 §15.3 | `can_access_platform_link()` | 實名 `can_access_link()`（`access_control.py:63`） |
| 11 | 07 §15.4 | 4443 入口含 `/anila/` | `/anila/` 已搬到 443（`nginx.conf:385-403`）；4443 的 UI 在 root `location /` |
| 12 | 09 §10 | `anila.meta` 例含 `task_id`/`classification_level` 且缺實際欄位 | 實際 `{trace_id, trace, citations, confidence, handoff_chain, follow_ups, latency_ms, classified}`＋`reasoning`/`usage` |
| 13 | 09 §12 | 「所有 API 使用 `{"error":{code,message,details,trace_id}}`」 | 現況為 FastAPI `{"detail"}` + SSE in-band error + resume proxy `event: error`；該 envelope 無任何實作 |

另有值得修正的部分正確項（節錄）：05 §13 `verify_presented_token` → 實名 `verify_service_token`；05 §13 `/v1/agents` wire 欄位是 `id`（值為 agent name）非 `agent_id`；01 §14 五類 Studio job 無數值 `progress` 欄位（只有 `state`+`step` 字串）；00 §9 Alert 無 `source` 欄（實為 `source_type`+`source_id`）；00 §9 漏 ANILALM 的 `/login` route（本機樹仍有本地登入頁）；09 §13 「/v1 不變」忽略已出貨的 `/v2/embeddings` 與 per-model `api_version=v2` 路由。

---

## 4. 文件間/文件內矛盾（重設計前要拍板）

1. **OpenWebUI in-scope 矛盾**（00 ADR-0003/§7 vs 01 人工註記 vs 01 §8 runtime_type）——見 §2.7。
2. **Service Admin 角色未定義**：03 §6、07 §3/§11 引入 service_admin 概念，但 03 §14 自己結論的角色 enum（owner/admin/developer/user/system）沒有它；07 §12 保留的 admin bypass（第 2 步）會讓任何平台 admin 繞過 per-service 邊界——未調和。
3. **降級審批自我核准迴圈**：08 §7（僅 Admin 可申請＋主管批核）vs §12（無主管時升級至 owner/admin group）。
4. **health enum 自相矛盾**：04 §2 目標 schema 三態 vs §9 目標狀態五態——目標 schema 裝不下目標狀態。
5. **兩份文件的「現行 header 契約」快照不一致**：03 §4 允許模型呼叫帶 `X-ANILA-Trace-Id`，04 §3 只寫 User-Id；實況兩者皆不送 trace header 給模型。
6. **Pipe bridge API 兩案並存**：05 §9 `wrap_pipe` vs 06 §5 `traced_pipe`，未調和（皆為未實作）。
7. **06 §4 shadow-registration 範例值超出 05 §3 自己定義的 enum**（`pending_migration`、`full_trace_required`）。
8. **Router 職責歸屬**：02 §10 把 primary-model refresh/fail-closed/service-token 三層解析寫進 `create_router_app()`；實際在 `anila-core-router/main.py` 部署包裝層（10 §17.1 寫對了）。
9. **443 route 清單三處不一致**：02 含 `/codeserver`、10 §17.1 漏 `/codeserver`、兩者皆漏 `/anila/`。
10. 09 §10「保留既有」清單與同文件 §15.5 的現況證據自相矛盾（retrieval_* 等）。

---

## 5. 給重設計的建議動作（優先序）

1. **先定基準（阻斷項）**：明定 redesign 以 `origin/prod-intranet-card`（v1.2.0）為現況 source of record；reconcile 本機同名分支（本機 22 個 commit 含 `/anila` nginx 入口需保留）。文件開頭加一段「取證基準」聲明。
2. **修 13 條 INCORRECT**（§3 表），並在 03 §11、04 §8/§9、09 §10 等清單加「現有／目標」標記，消除混寫。
3. **補四大遺漏章節**：(a) doc 09 補 auth/revocation 契約、SSE live-vs-latent 分層、terminal/error/correlation-header 契約（以 frozen `router-sse-contract.md` 為底）；(b) doc 04 補 TLS/CSPKI 信任鏈與放寬旗標現實；(c) doc 08 補「現況 enforcement 面」小節（share-only、前端裝飾匯出、標題洩漏、RAG 層零分類、無人可降級）；(d) doc 10 補內網部署/air-gap 交付工具鏈與 card compose delta。
4. **拍板 4 個矛盾**：OpenWebUI 遷移 in-scope？Service Admin 角色是否進 enum＋admin bypass 邊界？降級審批鏈（避免自我核准）？health 狀態詞彙（順帶收斂現有 models/agents 雙詞彙）。
5. **把「沿用」寫明**：usage attribution、csk- 雙用途、bsk- lifecycle、卡登驗章、audit_logs——重設計文件應標明這些是擴充基礎，避免重蓋。

---

*查核方式聲明：全程唯讀（未動分支、未碰運行中容器）；分支特定事實以 `git show` 對 origin 驗證；曾抽測 3 個後端測試檔（54 passed）確認文件引用之測試存在且可跑。*
