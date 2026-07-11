# ANILA Redesign 主實作計畫（Slice 0–9）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 依 `docs/anila-redesign-docs/`（憲法 = `00-product-constitution.md`）將 ANILA 從 `origin/prod-intranet-card` v1.2.0 系基線重構為「任務為入口、CSP 治理為底座」的新架構，在分支 `anila-redesign` 上完成 doc 10 全部 Slice 0–9。

**Architecture:** 保留成熟骨架（card SSO / JWT / JWKS / revocation / CSRF / RLS / SSRF guard / proxy），先搬 §17.1 新佈局，再逐 slice 升級 contract：Task/Trace/Classification（P0）→ Agent/Model/Service Registry（P1）→ Studio artifact（P2）→ Shell IA + 繁中政策。MVP 拍板：Task Service / Policy Engine / Launch Gateway 都在 CSP service 內以 module boundary 隔離，不拆獨立服務。

**Tech Stack:** FastAPI + SQLAlchemy + Alembic + PostgreSQL(pgvector/halfvec, RLS) + Redis + arq；Vue 3（治理 UI）、React（anila-shell / anilalm）；docker compose；pytest / vitest / tsc。

## Global Constraints（每個 task 隱含適用）

- 憲法 `00-product-constitution.md` §5 准入合約、§6 凍結清單、§7 允許例外；違反必附 ADR。
- 安全不變量不可弱化：card SSO、RS256 JWT/JWKS、revocation、CSRF、RLS（runtime role=`csp_app`）、SSRF guard、單向分類閂鎖。
- 不動 card auth 驗章邏輯、不改 RLS migration 主策略、不把 proxy 換成另一套 gateway（doc 10 Slice 1 Do-not）。
- 改 schema 必加 Alembic migration（up+down），從現 head `0046` 線性延伸；**revision id 避免與 main 系撞號**（前例：`0035` 兩分支同 id 不同內容）→ 本分支新 migration 一律用 `r1_` 前綴（`r1_0001` 起）。
- 所有正式 model/agent call 走 CSP proxy；artifact 綁 task/source_snapshot；正式 task 有 trace_id（doc 10 §12 runtime rules）。
- Import 邊界（doc 10 §12）：shell 不 import model client；runtime 不 import UI；agents 不 import CSP DB models；studio 不直讀 CSP DB。
- 前端新增字串一律繁體中文（台灣用語），identifiers 不譯（doc 11）。
- 祕密零外洩（PUBLIC repo）：`.env`/`*.pem`(除 cspki bundle 例外)/`*.key`/`secrets/` 不入庫。
- Commit 授權：本分支自由 commit、**不 push**；不動本機 running `anila-platform-*` 容器。
- 測試基線（2026-07-02，搬遷前）：backend pytest **315 passed / 45 failed / 12 errors（全為既有）**；ANILA_UI vitest 153 全過；ANILALM tsc 乾淨；CSP frontend build 過。之後每步以「不新增紅字」為 gate，45 敗/12 錯清單見 scratchpad `baseline-backend-pytest.md`。

## 參考資料（實作 subagent 必讀）

- 設計文件：`docs/anila-redesign-docs/`（00–11）
- 契約萃取（scratchpad，遺失可重生）：`/tmp/claude-1001/-home-aia-anila-ANILA/81493b14-f838-412a-91ea-8e8a8eb104df/scratchpad/extractions/doc-XX-extract.md`
- 搬遷影響地圖：同目錄 `recon-move-impact.md`（逐檔逐行 old→new）
- CSP 結構地圖：同目錄 `recon-csp-structure.md`（god-modules、alembic 鏈、符號定位表）

---

## Slice 1A：§17.1 目錄大搬遷（5 commits）

依 `recon-move-impact.md` 執行；與 §17.1 原文的差異決策（記入 ADR-0006）：

| 決策 | 內容 |
|---|---|
| D1 | 實際路徑 `models/inference/*`（§17.1 原文過時） |
| D2 | `models/inference/src` → `infra/models/src`（隨 compose 同進，`./src` 預設不變） |
| D3 | 權重 `models/model/` 不動；compose 預設 `${ANILA_HF_DIR:-../model}` → `../../models/model` |
| D4 | `myCSPPlatform/scripts/init_db.py` 併入 `services/csp/scripts/`，刪 csp.Dockerfile 多餘 COPY |
| D5 | certs → `infra/nginx/certs/`（追蹤的 .gitignore 隨移；live 憑證手動搬）；`myCSPPlatform/README*.md` → `services/csp/`；scripts 孤兒 → `infra/deployment/scripts/` |
| D6 | `myCSPPlatform/start.sh` 引用不存在檔案（已死）→ git rm |
| ALM | ANILALM → `apps/anilalm`（過渡；併入 anila-shell 是 Slice 9 的產品決策，非檔案搬移） |

- [ ] **1A-1 純搬移 commit**：全部 `git mv` ＋ `.gitignore` 路徑規則同步（`!services/csp/app/services/cspki_ca_bundle.pem`、`packages/anila-agent/templete/*`、標頭註解）＋ `git rm myCSPPlatform/start.sh`。無任何內容改寫。
- [ ] **1A-2 compose 重寫 commit**：`recon-move-impact.md` §1.1 全表；新增 root shim `compose.yaml`（`name: anila-platform` + `include: [infra/compose/platform.yml]`）與 `compose.dev.yaml`；`name:` 欄位保留。
- [ ] **1A-3 Dockerfile 重寫 commit**：§1.2（csp.Dockerfile ×5 行、ingestion-worker ×2、router ×3、D4 刪行）。
- [ ] **1A-4 scripts 重寫 commit**：§1.4 全表（先修 REPO_ROOT 深度，再修 compose -f / certs / CSPKI bundle / 跨腳本 exec）＋ `apps/anilalm/scripts/gen-studio-types.sh:22`。
- [ ] **1A-5 本機環境重建（不入庫）**：依 §2.3 重建三個 venv 與三個 node_modules；live certs `mv` 到 `infra/nginx/certs/`。
- [ ] **1A-6 驗證 gate**：`docker compose config`（root shim）、`docker compose -f infra/compose/platform.yml --env-file .env config`、`docker compose -f infra/models/docker-compose.yml config` 三者通過；`services/csp` pytest 不低於基線（315 passed）；`services/ingestion-worker` pytest 不低於基線；三前端 build/test 過；`bash apps/anilalm/scripts/gen-studio-types.sh` 過；`infra/deployment/scripts/deploy-prod.sh` preflight 乾跑不因路徑炸。
- [ ] **1A-7 docs 沿路修正 commit**：README/AGENTS.md/CLAUDE.md/.env.example/runbook 路徑掃修（§1.6）＋ ADR-0006（搬遷決策 D1–D6 + ALM 過渡）。

## Slice 1B：CSP 骨架整理（module boundary + naming + contract schemas）

Keep 清單照 doc 10 §3；只 refactor module layout / naming / contract schemas / tests organization。

- [ ] god-module 拆分（依 `recon-csp-structure.md` §1.1）：`api/agents.py`(1384) → registration / approval / credentials / runtime_config / functions 子模組；`api/auth.py`(837) → password / oidc / card / revocation 子模組；`services/proxy_service.py`(893) → headers / sse / usage / guard 子模組。行為不變（pure move + import 重導），pytest 全綠 gate。
- [ ] 建 `app/modules/` 邊界骨架：`tasks/`、`policy/`、`launch/` 三個空 package + import-linter 契約（禁止跨模組 import 內部實作）→ CI gate `lint-boundaries` 起步。
- [ ] contract schema 集中：新增 `app/schemas/contracts/`（Pydantic：TaskCreate/TraceSpanIn/PolicyDecisionOut/ClassificationLevel enum…，doc 09 為準）。

## Slice 2：Task Service + Source Snapshot（P0）

契約：doc-01/02/03/09 extracts。表：`tasks`、`task_runs`、`source_snapshots`、`citations`、`policy_decisions`、`trace_spans`（migration `r1_0001`…）。

- [ ] Alembic：六表 + 索引 + FK（trace_spans 先建表供 Slice 4 落資料）。
- [ ] `app/modules/tasks/`：TaskService（create/get/list/transition）+ `/api/tasks` CRUD + `task_runs` 狀態機。
- [ ] SourceSnapshot 三規則（產出必指 snapshot 或宣告無來源；Citation 只指 snapshot chunk；snapshot 分類=來源最高分類）。
- [ ] `/v1/chat/completions` 接受 `task_id`（header `X-ANILA-Task-Id` 或 body metadata）；無 task_id 舊流量相容並標 `legacy_runtime_call`（usage/audit 標記）。
- [ ] PolicyDecision 落表（9 action enum + allow/deny/require_approval），proxy 呼叫前寫入。
- [ ] ANILA UI 建 Task 的最小流（chat 起手即建 task，帶 task_id 呼叫）。
- Done gate（doc 10 §4）：UI 建 Task ✓、`/v1/chat/completions` 可帶 task_id ✓、舊流量相容標記 ✓。

## Slice 3：五級分類升級（P0）

契約：doc-08 extract（backfill=floor、變體 A、Inventory Before Cutover）。

- [ ] Alembic：`classification_level`（五級中文 enum：無機密<營業秘密<機密<極機密<絕對機密）+ `classification_event` + `declassification_request` + supervisor approval 欄位；backfill `classified=false→無機密、true→機密`（floor、不可逆不破壞）。
- [ ] Policy engine：`effective_level = max()`、單向閂鎖、ceiling 檢查（model/agent/service）。
- [ ] 降級流程（變體 A）：申請人≠核准人；核准權與平台角色脫鉤（authority 指派表）；無權責者 fail-closed pending；紙本核定代錄需公文文號。
- [ ] UI level badge + Cutover 前 Classification Inventory 報表。
- Done gate（doc 10 §5）：舊 boolean latch 不破 ✓、level badge ✓、降級申請+批核可跑通 ✓、ceiling 檢查 ✓。

## Slice 4：Full Trace 基礎（P0）

契約：doc-05/09 extracts；SSE SSOT=`docs/platform/router-sse-contract.md`（FROZEN，改事件先改契約檔）。

- [ ] `trace_spans` 落地寫入路徑 + `POST /v1/traces/{trace_id}/spans`（收取端點，caller=service token/agent credential）。
- [ ] `anila_trace_sdk`（packages/anila-core 內或獨立小包）：span builder + batch 上報。
- [ ] anila-core tracing hooks 接 Router dispatch；CSP proxy/model call 產 spans。
- [ ] SSE `anila.spans` 事件開始有 producer（映射既有 `anila.meta`/`anila.trace` 不中斷 UI）。
- [ ] Trace Explorer：ANILA UI SpanTree 接真資料 + `GET /api/traces/{trace_id}`。
- [ ] anila-agent 模板產 full trace sample。
- Done gate（doc 10 §6）：Router/proxy/model call 至少產 spans ✓、agent sample full trace ✓、UI SpanTree ✓、無 trace agent 不能 approved ✓。

## Slice 5：Agent Registry 遷移（P1）

契約：doc-05/06 extracts。不做：OpenWebUI 自動匯入/wizard/sidecar（已拍板）。

- [ ] agents 表擴充：`runtime_type`（5 值）+ `audit_level` + `classification_ceiling` + `approval_status` 七值 enum（migration + backfill 現有三值）。
- [ ] manifest schema + 驗證；`POST /api/agents/{id}/trace-test`（8 測項）；approval blocked until trace-test passed。
- [ ] 人工盤點 inventory 模板（CSV 10 欄位）+ shadow registration flow + wizard/CLI 強化。
- [ ] LangChain / custom HTTP trace adapter sample。
- Done gate（doc 10 §7）：至少 1 anila-agent native trace ✓、至少 1 非 anila-agent runtime adapter trace ✓、Router 只從 CSP `/v1/agents` discovery ✓。

## Slice 6：Model Gateway 強化（P1）

契約：doc-04 extract。硬規則：production model endpoint = HTTPS + API Key，`ANILA_ALLOW_HTTP_ENDPOINT` 不適用於正式模型 → 旗標分域（新 `ANILA_ALLOW_HTTP_AGENT_ENDPOINT`，model 路徑拒收 http）。

- [ ] model_registry → ModelEndpoint 欄位擴充（`protocol`/`api_key_secret_ref`/`classification_ceiling`/`owner_department_id`/`supports_*`；`allowed_task_types` 需拍板→先不做，記 ADR）。
- [ ] per-model secret ref（enc::v1:: envelope 延伸；MVP 保留 env fallback；UI 只顯示狀態）。
- [ ] health 收斂五態（`unknown/healthy/degraded/unhealthy/disabled`；映射 online→healthy 等）+ `GET /api/models/{id}/health` + `POST /api/models/{id}/test`。
- [ ] 出向前 policy check：`task.classification_level <= model.classification_ceiling` 否則 deny + PolicyDecision。
- Done gate（doc 10 §8）：模型全 HTTPS+Key ✓、Agent/Router 不持模型 key ✓、usage/trace 歸戶 ✓。

## Slice 7：Project Entry / Service Registry（P1）

契約：doc-07 extract（補派中）。從 `platform_links`/`service_access_grants` 種子擴充。

- [ ] 表：`registered_services`（含 `service_admin_user_ids`、`launch_mode`、`config_source`、classification ceiling）、`service_launches`、`service_audit_callbacks`、`service_project_bindings`；platform_links 資料遷移。
- [ ] Launch Gateway（CSP 內 module）：launch token 簽發/驗證、iframe allow policy、SSO mode、audit callback 收取端點。
- [ ] 治理 UI：服務上架/審核/授權（延續 PlatformLinksView/ServiceAccessView）。
- Done gate（doc 10 §9）：他組 Admin 可上架 ✓、iframe 可啟動 ✓、launch token 可驗 ✓、audit callback 可寫 ✓、grants default deny ✓。

## Slice 8：Studio Artifact Contract（P2）

契約：doc-02/09 extracts。最大 blocker：五類 job 狀態在 process memory。

- [ ] 表：`artifacts`、`artifact_versions`、`artifact_jobs`、`export_records`（persisted job store，crash/restart 不丟）。
- [ ] 五類 pipeline 共用 job model；artifact 綁 task/source_snapshot；classification 繼承。
- Done gate（doc 10 §10）：五類產出都能回 trace ✓、restart 不丟 job metadata ✓。

## Slice 9：ANILA Shell IA + 繁中政策（P3）

契約：doc-00 §2、doc-11 extract。

- [ ] anila-shell 導覽四入口：任務中心/我的知識庫/產出中心/專案入口；admin 另見治理中心。
- [ ] Project Entry 服務卡片改讀 registry；Task result 可轉 artifact 或 launch service。
- [ ] 不再以 ANILALM/Studio/CSP 為平行品牌露出（knowledge/artifacts 入口整合方案 = 導流或 iframe 皆可，記 ADR）。
- [ ] doc 11 繁中政策：字串集中化 `src/strings/`、CSP 治理 UI 中文化（最大改造面 77% 英文）、CI 簡體字/大陸用語 lint。
- Done gate（doc 10 §11）：四入口 ✓、卡片從 registry 讀 ✓、無平行品牌 ✓。

## 全量驗證（收尾）

- [ ] 完整測試矩陣（doc 10 §15）：unit/contract/integration/security/migration 各項。
- [ ] airgap-invariant-reviewer 安全紅線審查全 diff。
- [ ] migration up-down 測試（乾淨 DB `alembic upgrade head` + 逐級 downgrade）。
- [ ] Codex 深度稽核 prompt 移交（`docs/anila-redesign-docs/codex-deep-audit-prompt.md`）。

## 執行紀律

1. 每 task 一 commit（conventional commits）；混 concern 必拆。
2. 每 slice 結束：跑該範圍測試 + 不低於基線 gate + 向 user 回報 slice 摘要（供 Codex 複核）。
3. 實作 subagent prompt 用英文；只有對 user 回覆用繁中。
4. 不 push；不動 running 容器；不跑 START-HERE/intranet-deploy 腳本。
