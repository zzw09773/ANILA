> ⚠ 2026-08-17 盤點：本檔為【歷史紀錄】——redesign 收斂期的設計文件,保留當時的決策與依據,不代表現況。專案權威＝`PLAN.md`(現況與執行順序),規格＝`SYSTEM-MAP.md`。

> ⚠ **2026-08-01 P2.1 契約更新**：下文若仍描述 `csk-`／`bsk-`／`X-CSP-Service-Token`／
> `CSP_SERVICE_TOKEN` 作為 agent 派工身分，該段已過時。現行＝5 分鐘派工 JWT＋JWKS 驗簽
> （開發者不領鑰匙；三級接入見 `docs/guides/developer-guide.md`）。
> 歷史段落未逐字改寫，以免破壞 redesign 文件結構。

# 06. OpenWebUI Agent Migration Plan

> Status: draft v0.1  
> Purpose: 將 ML Team 目前註冊於 OpenWebUI 的 Agent 遷移至 CSP Agent Registry，並降低開發者改動成本。  
> Important correction: OpenWebUI 不是正式入口、不是 legacy runtime、不是 Agent host 目標架構的一部分。  
> 取證基準：現況（Repo evidence）以 `origin/prod-intranet-card`（v1.2.0 系）為準；本機工作樹與其分歧時以 origin 為準。目標設計與現況相左時，一律以目標為準，現況段落僅作遷移起點對照。

> ✅ 已拍板（2026-07-02，owner）：**目標保留，以人工重註冊達成** —— 走既有 wizard／CLI（本檔 L1 路徑：`/developer/agents` 精靈或 `anila-core` CLI `register` → issue csk- → test-connection）；自動化橋接（sidecar／`wrap_pipe` bridge／`import-openwebui`）**不列入範圍、不排程**，本檔第 5 節與第 7 節相關草案僅保留作日後參考。

---

## 1. Problem Statement

目前各開發者在 ML Team 上開發的 Agent 註冊於 OpenWebUI。ANILA 新架構要求：

```text
Agent 的正式註冊、授權、審計、分類、trace 必須由 CSP 管理。
```

挑戰：

- 不希望每個開發者大改 `api.py`。
- 不希望 OpenWebUI 成為正式依賴。
- 但正式 ANILA 需要 Full Trace。
- 現有 Agent 的 endpoint / prompt / tools / RAG flow 不應全部重寫。

---

## 2. Migration Strategy

分三階段：

```text
Phase 0 Inventory
→ Phase 1 CSP Shadow Registration
→ Phase 2 Trace Adapter
→ Phase 3 OpenWebUI Decommission for ANILA
```

---

## 3. Phase 0: Inventory

建立盤點表，不改程式。

| 欄位 | 說明 |
|---|---|
| openwebui_name | OpenWebUI 上的名稱 |
| owner | 開發者 |
| department | 小組 |
| current_endpoint | 目前 endpoint |
| runtime | Pipe / Function / LangChain / custom |
| model_usage | 使用哪些模型 |
| tools | 使用哪些工具 |
| data_sources | 使用哪些資料 |
| sensitivity | 預估分類 |
| migration_level | L1 / L2 / L3 |

輸出：

```text
openwebui-agent-inventory.csv
```

---

## 4. Phase 1: CSP Shadow Registration

把每個現有 Agent 先在 CSP 建立 `AgentDefinition`，狀態為 `draft`（目標新增值）或 `pending`
（對齊 doc 05 §3 enum；現況 enum 僅 pending／approved／rejected）。

```json
{
  "name": "legacy-openwebui-risk-agent",
  "runtime_type": "openwebui_pipe_compatible",
  "endpoint_url": "https://agent-host.local",
  "description_for_router": "...",
  "approval_status": "pending",
  "audit_level": "full_trace"
}
```

此階段 Agent 不一定可正式使用，但先建立治理視圖。

---

## 5. 附錄：Future Bridge Ideas（原 Phase 2: Trace Adapter — Not v1 Scope）

> ✅ 已拍板（2026-07-02）：本節（含方案 A／B／C 的自動化橋接）**不列入 v1 範圍、不排程**（見文首），內容僅保留作日後參考。v1 主線流程為：人工盤點 → 手動 CSP 註冊（精靈 / CLI）→ 簽發 Agent Integration Key → test-connection → trace 實作（native 或 adapter）→ approval → 平台依賴退場。

### 方案 A：Zero-code / Low-code sidecar

對可用反向代理包住的 Agent：

```text
ANILA CSP
→ trace sidecar
→ existing agent endpoint
```

Sidecar 負責：

- 補上 `/.well-known/anila-agent.json`
- 驗 `X-CSP-Service-Token`
- 產生 agent.run spans
- 轉發 SSE
- 捕捉 request/response
- 盡可能包裝 tool/model spans

限制：

- 無法知道 Agent 內部所有 tool step。
- 若 Agent 本身不揭露內部事件，只能達到 L2/L2.5。

### 方案 B：Bridge wrapper

提供 wrapper，讓開發者最小改動（bridge API 與 doc 05 §9 統一命名為 `wrap_pipe`）：

```python
from anila_openwebui_bridge import wrap_pipe

class Pipe:
    async def pipe(self, body, __user__=None):
        return await wrap_pipe(
            agent_id="foo",
            body=body,
            user=__user__,
            handler=self.original_pipe,
        )
```

### 方案 C：Native Full Trace

針對正式機敏任務 Agent：

- anila-agent：內建 trace SDK。
- LangChain：callback handler。
- custom：HTTP trace callback。
- OpenWebUI Pipe：bridge + explicit step trace hooks。

---

## 6. Full Trace Acceptance

Agent 必須至少提供：

| Trace | 必須 |
|---|---|
| run start/end | yes |
| model call start/end | yes |
| tool call start/end | yes |
| retrieval chunks | yes if retrieval used |
| error | yes |
| final output | yes |
| classification level | yes |
| citations | yes if source used |

否則只能停留在 `dev/test`，不得進正式任務。

---

## 7. Developer UX

### CSP Developer Console

新增「從 OpenWebUI 遷移」wizard：

```text
1. 輸入 OpenWebUI Agent 名稱
2. 選 runtime type
3. 輸入 endpoint
4. 貼上或產生 manifest
5. 選分類上限
6. 下載 wrapper / sidecar config
7. trace test
8. 送出審核
```

### CLI

```bash
anila agent import-openwebui \
  --name old-agent \
  --endpoint https://agent.local \
  --runtime openwebui-pipe \
  --owner dept-a

anila agent trace-test --agent old-agent
```

---

## 8. Trace Test

CSP 提供測試：

```text
POST /api/agents/{id}/trace-test
```

測試項目：

- endpoint health。
- manifest valid。
- service token valid。
- `/v1/chat/completions` reachable。
- SSE valid。
- `anila.spans` received。
- required span types complete。
- classification header respected。

---

## 9. Compatibility Contract

現有 Agent 只要能提供 OpenAI-compatible chat endpoint，即可先接：

```text
POST /v1/chat/completions
```

但正式要求：

- 不直接呼叫模型 endpoint；改走 CSP。
- 不直接存取未授權資料。
- 若用 RAG，必須透過 CSP service-token search endpoint 或 agent bound collection。
- 必須回報 full trace。

---

## 10. Risks

| 風險 | 對策 |
|---|---|
| 開發者不想改 code | 提供 sidecar / wrapper / CLI |
| sidecar 無法取得 tool step | 只能 dev/test；正式任務必須 native trace |
| OpenWebUI Pipe 依賴特殊 __user__ | Bridge 提供 adapter |
| 既有 Agent 寫死模型 endpoint | 網路政策禁止直連模型；規格要求改 CSP provider |
| RAG 使用者身分 gap | service-token（csk-）search endpoint 已存在（S-Q1：agent csk- 以 owner 身分執行、硬鎖 bound collection）；缺的是 per-user 變體（`X-ANILA-User-Id` 身分＋per-user RLS scope），屬目標新增 |

---

## 11. Decommission Criteria

OpenWebUI 對 ANILA 不再必要的條件：

- 所有 active ML Team Agent 都有 CSP record。
- 需要正式使用的 Agent 都通過 Full Trace。
- Router discovery 只從 CSP `/v1/agents`。
- ANILA UI 不再讀 OpenWebUI metadata。
- CSP dashboard 可顯示 agent usage / trace / classification。
- OpenWebUI 只保留為個別開發者自用工具，不是平台依賴。

---

## 12. Repo evidence / 現況補齊

### OpenWebUI 註冊資料匯出

repo 內目前沒有 OpenWebUI 註冊資料匯出檔、connector、匯入指令或 migration script。
搜尋 `OpenWebUI`、`openwebui`、`Pipe`、`import-openwebui`、
`wrap_pipe`、`traced_pipe` 的結果顯示：

- OpenWebUI 出現在 redesign docs、UI audit 檔案，以及程式註解（`app/api/proxy.py`、`router_server.py`、anila-core `__init__.py`、`ANILA_UI` index.html）、`anila_plan.md` 與 `docs/ingestion/ingestion-platform-design.md`；皆為說明性引用，無任何功能性整合。
- `anila_openwebui_bridge` package 尚不存在。
- `anila agent import-openwebui` CLI 尚不存在。
- `POST /api/agents/{id}/trace-test` 尚不存在。

因此 Phase 0 inventory 一律採手動盤點（✅ 已拍板 2026-07-02：不新增 OpenWebUI export/import 工具，見文首）。
在 repo 沒有 OpenWebUI DB schema / API response 範例前，也不能宣稱可自動匯出既有註冊資料。

### 目前可用的 CSP shadow registration

現有 CSP 已能支援 L1 shadow registration。`myCSPPlatform/backend/app/api/agents.py`
的 `POST /api/agents/register` 需要：

```text
name
endpoint_url
description_for_router
base_model_id
```

可選：

```text
api_version
collection_id
capabilities
input_schema
```

註冊後預設 `approval_status="pending"`；admin 可用
`POST /api/agents/{agent_id}/approve` 核准。endpoint 註冊與更新都會跑 SSRF guard，
已核准 Agent 若改 endpoint 會退回 `pending`。

現有 credential flow：

```text
POST /api/agents/{id}/credentials/issue-static
POST /api/agents/{id}/issue-bootstrap
POST /api/agents/{id}/bootstrap
POST /api/agents/{id}/credentials/{credential_id}/rotate
DELETE /api/agents/{id}/credentials/{credential_id}
```

`docs/runbooks/legacy-agent-bootstrap.md` 已把整合分為：

- Tier 0：owner/admin 直接 issue static `csk-`，貼到 agent `.env`。
- Tier 1：agent 以 `bsk-` bootstrap 換 `csk-`。
- Tier 2：使用 anila-core middleware / state file 管理 token。

目前 developer guide 也以 `/developer/agents` wizard 為主要路徑：註冊 endpoint、
issue `csk-`、貼入 agent env、test connection、等 admin approve。

### 現有 agent endpoint shape

`anila-agent/anila_agent/serving/service_wrapper.py` 是可直接參考的 HTTP shape：

```text
GET  /health
GET  /v1/models
POST /v1/chat/completions
```

認證：

```text
X-CSP-Service-Token: <agent csk->
X-ANILA-User-Id
X-ANILA-User-Email
X-ANILA-User-Groups
```

服務端會先驗 `X-CSP-Service-Token`，驗過後才信任 `X-ANILA-User-*`。
RAG 走 CSP HTTP search；不直連 DB。

CSP 的 `POST /api/agents/{id}/test-connection` 會對：

```text
{agent.endpoint_url}/v1/chat/completions
```

發送空 messages 並附上該 Agent 的 csk-。HTTP 401 代表 token 不被接受；其他可連通回應
代表 token wiring 基本正確。這是目前 repo 已有的測試能力；不是 Full Trace test。

### OpenAI-compatible bridge 成本

若既有 ML Team Agent 已有 OpenAI-compatible HTTP endpoint，最小改動是：

1. 把 endpoint 註冊到 CSP `/api/agents/register`。
2. 讓 endpoint 驗 `X-CSP-Service-Token`。
3. 保留 `POST /v1/chat/completions` request/response shape。
4. Agent 內部若要呼叫模型，改用 CSP `/v1/chat/completions` 或既有 CSP provider
   設定，不在 Agent 內寫死模型 endpoint / gateway key。

若 OpenWebUI 只是作為「client」使用，也可把 OpenWebUI 的 OpenAI base URL 指向
CSP data plane：

```text
base_url = https://<csp-host>/v1
api_key  = sk-...
```

CSP 已有 OpenAI-compatible：

```text
GET  /v1/models
POST /v1/chat/completions
POST /v1/embeddings
```

但這只是讓 OpenWebUI 作為 client 呼叫 CSP，不等於把 OpenWebUI Pipe 自動轉成 CSP
registered Agent。

### OpenWebUI Pipe wrapper 缺口

本檔第 5 節的 `anila_openwebui_bridge.wrap_pipe(...)` 目前是目標設計，不是 repo
現況。若既有 Pipe 只有 OpenWebUI 內部 `pipe(self, body, __user__)` 介面，且沒有
HTTP endpoint，現有 repo 不能直接註冊它。需要新增：

- sidecar 或 bridge package，將 Pipe 包成 `/v1/chat/completions`。
- `X-CSP-Service-Token` 驗證。
- `__user__` adapter，將 `X-ANILA-User-*` 轉成 OpenWebUI Pipe 期待的 user shape。
- trace hooks，至少輸出 run/model/tool/retrieval spans。
- CLI 或 wizard，用於產生 wrapper config。

因此目前不能提供實際 `api.py` 最小 diff；repo 缺少一份真實 OpenWebUI Pipe source
與橋接 package。可落地的最小範例應先以 `anila-agent` service wrapper 或任一
OpenAI-compatible Agent endpoint 作為基準。

### CLI 現況

現有 CLI 位於 `anila-core/src/anila_core/cli/`，可用能力是：

- `register_cmd.py`：讀 `anila.yaml`，登入 CSP，呼叫 `POST /api/agents/register`。
- `bootstrap_cmd.py`：以 `bsk-` 呼叫 `POST /api/agents/{id}/bootstrap`，把 `csk-`
  寫入 `service_token.json`，檔案權限設為 `0600`。
- `status_cmd.py`：查 `/api/agents` 或 `/api/agents/{id}`。

現有 CLI 不是本檔第 7 節草案中的 `anila agent import-openwebui` /
`anila agent trace-test`；後兩者仍需新增。

### ML Team 現有 Agent endpoint 形態

repo 內沒有 ML Team OpenWebUI agent inventory、endpoint 清單或匯出資料。
只能從現有 platform contract 推論可接受形態：

- 最低 L1：HTTP OpenAI-compatible `/v1/chat/completions`。
- 正式 L3：需補 Full Trace spans 與 callback/SSE。
- 只有 OpenWebUI Pipe function、沒有 HTTP endpoint 的 Agent，需要 sidecar/wrapper。

這項是 migration 前的外部資料缺口，不能由 repo 內資料補齊。
