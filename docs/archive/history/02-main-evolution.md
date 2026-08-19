> ⚠ 2026-08-17 盤點：本檔為【歷史紀錄】(專案起源史),不代表現況。現行狀態與執行順序見 `PLAN.md`。

> ⚠ **2026-08-01 P2.1**：agent 派工身分已改為平台現簽的 5 分鐘 JWT（JWKS 驗簽；
> 開發者不領 `csk-`／`CSP_SERVICE_TOKEN`）。下文保留當時紀錄，**勿依此做現行接入**；
> 現行上手見 `docs/guides/developer-guide.md` 與治理中心「接入驗簽 · 三級制」。

# Era B — main 主線的演進：從創始到 v1.1.0

## 概述

本篇記錄 ANILA `main` 主線從第一個 commit（`0eaf3d5`，2026-04-17）到 tag `v1.1.0`（`f3d3550`，2026-06-22）的完整演進，共 **657 個 commit**。範圍取自 `git log --reverse v1.1.0`，即所有可從 `v1.1.0` 追溯的祖先。

這段時間 ANILA 從「三個來源專案的統整」長成一套**多服務內網知識／生產力平台**：先確立 CSP 控制平面 + Router + Runtime UI 的骨架，接著平台化（多服務同源 compose）、抽出 Ingestion Platform、建立 Studio × FLUX 生成管線並抽成獨立 `anila-studio` 服務、大幅擴充 `anila-agent` framework，最後收斂成**七分支 SSOT 模型**、發布 **v1.0.0**（並改採 GPL v3.0）、補齊 **Open WebUI gap features**，再以 **v1.1.0 air-gap patch（6 項內網回報修正）** 收尾。

需注意：`v1.1.0` tag 實際落在 `prod-intranet-card` 分支的尖端，因此本範圍尾段（約 2026-06-12 起）除了 `main` 的共用工程外，也夾帶了卡登／SSO fork 的專屬 delta。創始（genesis）commit 由 Era A 敘事，但其 commit 一律列入本篇對照表。

> 依據：git 為第一手來源；`AGENTS.md` §2–3（服務地圖／分支模型）、tag message、`docs/audits/` 為輔證。無法從證據確認處以「依 commit 訊息推斷」標註。

---

## 里程碑敘事

### B-1 平台化：從三專案統整到多服務同源 compose

創始三連（`0eaf3d5`→`f902809`，2026-04-17）把 myCSPPlatform、AgenticRAG 與一個後續移除的評估專案統整進單一 monorepo，並鋪出多租戶 AI 平台架構、開發者生態、SDK、agent registry、身分驗證與 OpenAI 相容 Router 入口。隨後（04-20～04-23）補上 agent 註冊／審核／授權、Runtime UI（登入頁 + SSE）、對話／訊息／附件／handoff／通知模型，形成可運作的骨架。

真正的「平台化」發生在 2026-04-25：先落地服務存取控制（`required_roles`、`service_access_grants`、`dev_db_credentials`，`4836670`／`7921602`），再以同源子路徑把 code-server、GitLab、n8n 掛進 nginx（`a31dd86`／`d62bbbb`），並補上 admin 的 Service Access 管理頁。到 `4ebecdd`（2026-05-01）README 已改寫為「9-service topology」，標誌平台從單體服務走向多服務同源部署。權威服務地圖見 `AGENTS.md` §2。

### B-2 Ingestion Platform 與 anila-core 瘦身（骨幹工程量最大之一）

2026-04-25 起，Sprint 1 先把 RAG／ingestion 從 `anila-core` 剝離（`e634ad1`～`391f595`），讓 core 回歸「共用 runtime library」定位；同時另建 Ingestion Platform：schema + 多種 chunker + RLS-scoped store + ingestion-worker + Redis + Knowledge Collections UI + Chunking Evaluator（`ce6b747`～`ef1c018`）。後續 Sprint 4/5 把 collection 升為一等公民、embedding 走 CSP `/v1/embeddings` proxy、加入 LLM-as-judge，並清掉多輪安全審查發現（`984ae4f`～`143f89e`）。這條線是日後 RAG 檢索、跨文件關聯與 Studio 取材的資料底座。

### B-3 Studio × FLUX 生成管線，以及抽出獨立服務 anila-studio

2026-05-16 FLUX 影像生成上線（`flux2-dev` 推論服務 + `flux2-dev-agent` OpenAI-shape shim，`bf9983e`～`ab69419`）。接著 05-18～05-21 是一段密集的 Studio 品質工程：pptx-renderer 版面／主題（`THEMES` bundle、warm_journal／academic_paper／executive_brief／startup_pitch）、6 輪品質修補、以及 **FLUX 語意對齊 Stage 1-4**（rewriter + cover hero + striping／VLM 雙閘門 + 依內容自動推斷 deck 風格 + 插圖全用途路由）。2026-05-23 把 Studio **抽成獨立 `anila-studio` 服務**：JWT 由 HS256 切到 **RS256 + JWKS**、Redis pub/sub 跨服務撤銷、`studio.py` 分 9 個子 commit 改走 `csp_client` HTTP，並新增 report／mindmap／infographic／datatable 四種 artifact（`861d75d`～`92faba3`）。

### B-4 anila-agent framework 大擴充（P0/P1/P2 三波）

2026-05-06 以 subtree 納入 `anila-agent` 官方模板（基座為 OpenAI Agents SDK）。2026-05-26～05-27 進行一次大規模 framework 擴充，分 **P0（hooks／tool metadata／context／guardrails／tracing／policy DSL／AgentTool）**、**P1（connection strategy／budget／concurrency／compaction／MCP／streaming／HITL／ToolSearch／triggers／cost tracker）**、**P2（as_tool／tool guardrails／SessionMemory／file-index／task／slash commands／skill loader）** 三波，每個子項採「feat → merge → sync(main→prod/dev)」節奏推進，是本時代 commit 密度最高的一段。

### B-5 七分支模型成形

分支模型是演進出來的，不是一開始就有。早期（2026-05-18）是 `main` + `prod` + `feature-no-sso` 的**雙線並維**（見 `624f590` 的 sync backlog）。2026-05-25 一次把 main 的 204 個 commit 同步進 prod（`6fcbdce`），隔日 **2026-05-26 進行分支重構**：sync-backlog 改寫對齊「5 branch SSOT」（`46ed491`）、`deploy-prod.sh` 支援 3 條 prod branch（`70759f1`），tag `pre-branch-restructure-2026-05-26` 標記此切點。最終收斂為 `AGENTS.md` §3 記載的**七分支模型**：`main`（SSOT）+ `dev-public` / `prod-public-passwd` / `dev-military` / `prod-military-passwd` / `prod-intranet-card` / `trial-military`。核心規則：通用變更先進 `main` 再 port，downstream 之間不互 merge，`prod-intranet-card` 是唯一 card／SSO fork、不得被 `main` wholesale 覆蓋。

### B-6 v1.0.0 發布與 GPL relicense

v1.0.0 是一段跨數日的「發布收斂」而非單一 commit：`c67145e`（2026-06-12）把平台版本號 bump 到 1.0.0，`761ccdc`（2026-06-13）把專案由創始時的 Proprietary License（`ec235b3`）**改採 GNU GPL v3.0**，而 **tag `v1.0.0` 實際落在 `22c9936`（2026-06-14）**——內網發布用的「TLS full-chain 抽取」修正（tag message：intranet release / fullchain TLS extraction）。同批也含卡登**真實 PKCS#7/CMS 驗章**（`ed13e5c`，關閉 auth-bypass CRITICAL）、新端點資安硬化、以及 migration 0040 對無-SSO 分支啟動的守衛（`c6491f3`）。

### B-7 README／文件工程

文件是這段歷史的持續主線，非一次性事件：從 04-24「upgrade all platform READMEs to level C」、05-21 九子專案雙語 README（中文主 + `README.en.md`）、到 05-26 為各 prod 子專案加分支 banner。v1.0.0 前後再度密集：`5697898`（branch-tailored README for prod-intranet-card）、`2156998`（全子專案 README zh+en 全面重寫）、`59369f9`（對照現行源碼校正），以及 `1c1a18a`（README 稽核 + 截圖藝廊）。目的是讓 PUBLIC repo 的每個子專案 README 與實際程式碼一致、且中英對等。

### B-8 Open WebUI gap features

`86ea21c`（2026-06-12）「Open WebUI gap features + extensible agent-function framework」把對標 Open WebUI 的功能缺口一次補入，並帶進可擴充的 agent-function 框架。此提交對應 `docs/audits/openwebui-gap-analysis-2026-06-11.md` 的 220 項功能對抗盤點（確缺 39 項，分 5 級）。這是把平台從「能用」推向「與主流開源方案功能對齊」的關鍵一步。

### B-9 v1.1.0 air-gap patch（6 項內網修正）

tag `v1.1.0`（`f3d3550`，2026-06-22）收束一批**內網實地回報**的修正。自 v1.0.0 起累積：JWT keypair 硬化、`model-ca` 預設改用 CSPKI bundle（`6eadd83`）、robust SSE 串流修復（`1f5dd99`）、加入專案級 `AGENTS.md`／`CLAUDE.md`（`c2af31e`）。tag message 明列本次 **6 項**：刪 collection 500、usage CSV BOM、zip 檔名、失敗檔重嵌、`.json/.html/.doc` 支援、免壓縮多檔上傳；且經 Codex 審查修掉 5 項 findings。此為本時代（Era B）的終點。

---

## 工程分期一覽（作為下方對照表的閱讀索引）

| 期間 | 主題 |
|---|---|
| 2026-04-17～04-23 | 三專案統整、平台骨架、Runtime UI |
| 2026-04-24～04-25 | AgenticRAG 整合、多服務同源平台化（codeserver/GitLab/n8n/nginx） |
| 2026-04-25 | anila-core 瘦身 + Ingestion Platform Sprint 1 |
| 2026-04-27 | Ingestion Sprint 4/5、安全硬化、SSO 基礎、移除評估專案、NotebookLM→ANILA LM 更名 |
| 2026-04-28～04-30 | ANILA Functions v1 spike（建置後整批撤回） |
| 2026-05-01～05-02 | service-token bootstrap、chunking preview、parent-child RAG、anila-core Sprint 9-12 |
| 2026-05-04～05-06 | 統一 user-memory 層、AgenticRAG 純模板化、anila-agent subtree 納入 |
| 2026-05-11～05-15 | models stack 拆分（anila-models）、OpenAI-compat /v1/models、卡登前身 |
| 2026-05-16 | 內網 hardening、governance 文件、FLUX 影像生成上線 |
| 2026-05-18～05-21 | FLUX→prod、Studio×FLUX pptx、Studio 品質 6 輪、FLUX 語意對齊 Stage1-4 |
| 2026-05-23 | session TTL、anila-studio 抽出（RS256/JWKS/revocation）、4 種新 artifact |
| 2026-05-25～05-26 | main→prod 大同步、deploy-prod.sh、分支重構、templete SDK 快照 |
| 2026-05-26～05-27 | anila-agent framework P0/P1/P2 三波擴充 |
| 2026-06-02 | UIUX/a11y、資安硬化、Studio god-module 拆分、csk- 憑證 |
| 2026-06-03 | backlog #112–#117、agent onboarding csk- 一把金鑰精靈 |
| 2026-06-08 | csk- 文件對齊、studio auth 韌性、hardening 併回 main（含 late-merge 的 anila-agent P2） |
| 2026-06-09～06-10 | 跨文件關聯 RAG（rule/LLM/similarity）+ 圖 |
| 2026-06-12 | Open WebUI gap features、內網部署整備、卡登真實驗章、v1.0.0 版本號 |
| 2026-06-13 | branch-tailored README、GPL v3.0 relicense |
| 2026-06-14～06-15 | intranet-deploy.sh、TLS fullchain（v1.0.0 tag）、JWT keypair、anila-agent lean rebuild、guide MLSteam + system-prompt 產生器 |
| 2026-06-17～06-22 | README 稽核+截圖、model-ca CSPKI、robust SSE、AGENTS/CLAUDE、v1.1.0（6 內網修正） |

---

## 完整 commit 對照表（657）

> 順序即 `git log --reverse v1.1.0` 的輸出（主要依提交日期；merge 會使少數 commit 位置與日期不完全一致，例如 anila-agent P2-5/10/11 三筆日期為 2026-05-27，但因併入 main 較晚而排在 06-08 附近）。摘要欄忠實保留 commit 主旨；僅為遵守歷史撰寫規範，將 3 筆提及已移除評估專案名稱的主旨改寫（`0eaf3d5`、`c2c9204`、`f220dda`）。

| Hash | 日期 | 摘要（commit 主旨） |
|---|---|---|
| 0eaf3d5 | 2026-04-17 | 統整三個來源專案（myCSPPlatform、AgenticRAG 及一個後續移除的評估專案） |
| 2e96978 | 2026-04-17 | 納入三個專案完整程式碼 |
| ec235b3 | 2026-04-17 | Add Proprietary License |
| f1efea7 | 2026-04-17 | finish phase1-3 |
| f902809 | 2026-04-17 | 整合 ANILA 平台計畫，新增多租戶 AI 平台架構與開發者生態，強化 SDK，拆分 agent registry，新增身份驗證與授權機制，並建立 OpenAI 兼容的路由器入口。 |
| 7ef38f6 | 2026-04-20 | feat: Add agent management functionality including registration, approval, and permission assignment |
| e9f1f6a | 2026-04-20 | feat: Add runtime UI components, login page, and SSE handling |
| 280b896 | 2026-04-20 | feat: 新增 ANILA Runtime UI 服務及相關 Docker 配置 |
| abfff13 | 2026-04-20 | Add frontend completion plan for ANILA UI and myCSPPlatform, detailing current status, tasks, and test plan |
| 066b0b6 | 2026-04-20 | feat: Add conversation, message, attachment, handoff, and notification models with associated services |
| c122b7a | 2026-04-21 | feat: 地端模式移除用量限制，改為管理員逐模型設定加密 |
| 1b17513 | 2026-04-21 | wip(anila-ui): restore ESM scaffold from template; pending chat/app/main |
| 19e2a6d | 2026-04-21 | feat: add main chat UI and routing with authentication |
| 944d134 | 2026-04-22 | Refactor code structure for improved readability and maintainability |
| 508e10a | 2026-04-22 | Add regression tests for classified meta handling and drop unused quota policies |
| 0bae8bf | 2026-04-22 | Merge pull request #2 from zzw09773/claude/restore-ui-from-template-VWf99 |
| 913c7cb | 2026-04-22 | docs(developer): add inline guide and output-format reference on Agent Console |
| b1f6726 | 2026-04-22 | Merge pull request #3 from zzw09773/claude/developer-guide-docs-Ih9mU |
| c043a23 | 2026-04-22 | Add migrations for new features: is_router_primary and message rating |
| e632c7a | 2026-04-23 | Remove draft blueprint HTML document for ANILA platform |
| bf437d9 | 2026-04-23 | feat: enhance chat functionality with reasoning and SSE support |
| 710dfba | 2026-04-23 | feat: auth 重構 + Agent 強化 + UI 大改 |
| 7978c5c | 2026-04-24 | feat(agentic-rag): integrate AgenticRAG framework as ANILA template |
| 1e85c7f | 2026-04-24 | feat(agentic-rag): add CSP service-token middleware with anila-core preference |
| c45aca4 | 2026-04-24 | docs(agentic-rag): add CSP integration guide and agent registration manifest |
| d94bff8 | 2026-04-24 | docs: upgrade all platform READMEs to level C |
| 7074a3c | 2026-04-24 | fix(docs): correct mermaid syntax in 3 READMEs |
| 6b59a14 | 2026-04-24 | feat(docs): add initial draft for ANILA Ingestion Platform design document |
| 9ca4fbd | 2026-04-25 | docs: ingestion platform design v0.2 + anila-core boundary spec |
| bc30944 | 2026-04-25 | docs: multi-service integration plan + cross-link the three design docs |
| 2cd71a8 | 2026-04-25 | docs: multi-service plan v0.2 — 7 issues review + ComfyUI namespace fix |
| a0b833b | 2026-04-25 | docs: multi-service plan v0.3 — ISO 42001 + codeserver same-origin |
| 4215228 | 2026-04-25 | docs: multi-service plan v0.4 — GitLab in monorepo + ComfyUI deferred |
| 4836670 | 2026-04-25 | docs: add service access control features — required_roles, service_access_grants, and dev_db_credentials |
| 7921602 | 2026-04-25 | feat(csp): service access control endpoints + role gate |
| 7868f3f | 2026-04-25 | feat(csp): add platform_links.is_public + grandfather existing links |
| 45d07fd | 2026-04-25 | docs: multi-service plan v0.5 — sync §7.5 + §5.3 to actual implementation |
| 27b01b1 | 2026-04-25 | fix(csp): admin bypass before role gate (v0.5.1) |
| 0bfcbd2 | 2026-04-25 | feat(csp): seed 5 platform links + idempotent upsert (Phase 1 Step 3) |
| a31dd86 | 2026-04-25 | feat(infra): codeserver + GitLab + nginx same-origin paths (Phase 1 Step 5-8) |
| e8304ba | 2026-04-25 | fix(infra): GitLab subpath routing — Host header + healthcheck URL |
| 195c63f | 2026-04-25 | docs: multi-service plan v0.5.3 — Phase 1 Step 5-8 落地 + GitLab subpath trap |
| 68d8fe4 | 2026-04-25 | fix(nginx): mirror /codeserver/ + /gitlab/ in port 443 server block |
| 72a4f31 | 2026-04-25 | fix(infra): drop port from codeserver PROXY_DOMAIN |
| bd65bce | 2026-04-25 | refactor(infra): codeserver subpath → dedicated port :8443 |
| 73f765d | 2026-04-25 | fix(infra): codeserver pivot — codercom/code-server + My-OpenAI-Frontend nginx |
| 34202aa | 2026-04-25 | fix(infra): bind-mount ANILA repo into codeserver + bump GitLab 16.10.10 |
| 2aba275 | 2026-04-25 | feat(ui): admin Service Access management page (Phase 1 Step 4) |
| e6091cc | 2026-04-25 | feat(csp-ui): admin Service Access management — Vue version (Phase 1 Step 4) |
| d62bbbb | 2026-04-25 | feat(infra): deploy n8n at /n8n subpath (Phase 1 follow-up) |
| b6171e1 | 2026-04-25 | test: phase1 E2E sanity script + mark Step 9/10 done |
| 59a3ce8 | 2026-04-25 | feat(infra): /static + /uploads share dir for n8n workflow assets |
| 5499817 | 2026-04-25 | fix(nginx): variable proxy_pass + Docker DNS resolver — auto-recover stale IPs |
| 70a7c83 | 2026-04-25 | docs: multi-service plan v0.6 — Phase 1 done + NotebookLM agentification cancelled |
| e634ad1 | 2026-04-25 | refactor(anila-core): Sprint 1 Day 1-3 — remove RAG tool factories from core |
| 998fd2a | 2026-04-25 | refactor(anila-core): Sprint 1 Day 4-6 — delete ingestion + RAG endpoints + slim app_factory |
| d8b4e25 | 2026-04-25 | refactor(anila-core): Sprint 1 Day 7-9 — drop pg adapters + embedding_nvidia + RAG config |
| 391f595 | 2026-04-25 | docs(anila-core): Sprint 1 Day 10 — README + CHANGELOG + G3 gate (Sprint 1 wrap) |
| ce6b747 | 2026-04-25 | feat(ingestion): Phase 2 Sprint 1 Chunks A+B+C — schema + chunkers + RLS-scoped store |
| bea408f | 2026-04-25 | fix(ingestion): RLS actually fires — split runtime/migration roles + dim 1536 |
| 240bd3d | 2026-04-25 | chore(ingestion): docstrings reflect 1536-d truncation, not 4096 |
| b10e026 | 2026-04-25 | feat(ingestion): Chunk D — CSP /api/ingestion/collections CRUD (sync API) |
| b33818b | 2026-04-25 | feat(ingestion): Chunk E — ingestion-worker + Redis + halfvec(4000) |
| c35e586 | 2026-04-25 | feat(ingestion): Chunk F — AgenticRAG retrieval through central SDK; G3 gate ✅ |
| a509fa1 | 2026-04-25 | test(ingestion): Chunk G — Sprint 1 G1/G2/G3 gates as formal pytest |
| 81eae46 | 2026-04-25 | docs: Sprint 1 wrap — anila-core CHANGELOG v0.6.0 + design doc v0.3 |
| 2899c02 | 2026-04-25 | feat(ingestion): Chunk H — Knowledge Collections UI + 中量版 Inspector |
| 6541516 | 2026-04-25 | feat(ingestion): Chunk I — 6 chunkers + AgenticRAG parser stack reuse |
| bc134f4 | 2026-04-25 | feat(ingestion): Chunk J — job progress SSE + status update plumbing |
| a0a7429 | 2026-04-25 | feat(ingestion): Chunk K — multi-file zip upload |
| 34858ce | 2026-04-25 | feat(ingestion): Chunk L — agent_llm_credentials (AES-256-GCM + CRUD) |
| 80e50fd | 2026-04-25 | feat(ingestion): Chunk M — re-implement RAG tool factories over central SDK |
| ef1c018 | 2026-04-25 | feat(ingestion): Chunk N — Chunking Evaluator (backend + 4-step wizard UI) |
| 984ae4f | 2026-04-27 | refactor(ingestion): Sprint 4 Chunks O+P — collection-as-first-class |
| c407946 | 2026-04-27 | refactor(ingestion): Sprint 4 Chunks Q+R+S — API/worker/AgenticRAG agent-less |
| 08adfa2 | 2026-04-27 | refactor(ingestion): Sprint 4 Chunk T — frontend agent picker removed |
| 7ff3669 | 2026-04-27 | docs: Sprint 4 wrap — anila-core CHANGELOG v0.7.0 + design doc v0.4 |
| 1bdef78 | 2026-04-27 | feat(ingestion): Sprint 4 Chunk V — embedding usage tracking |
| 4ca7e92 | 2026-04-27 | feat(ingestion): Sprint 5 Chunk W — embedding via CSP /v1/embeddings proxy |
| f44c441 | 2026-04-27 | feat(ingestion): Sprint 5 Chunk X — LLM-as-judge in Chunking Evaluator |
| f9d3282 | 2026-04-27 | feat(evaluator): inline LLM credential CRUD in wizard step 4 |
| 2324ca3 | 2026-04-27 | fix(security): close H1/H2/H3 from Sprint 5 X review |
| d4bf169 | 2026-04-27 | fix(security): close M1 + M4 from Sprint 5 X review |
| eb7f009 | 2026-04-27 | fix(security): close M2/M3 + L1/L3 from Sprint 5 X review (L2 doc-only) |
| dd40f25 | 2026-04-27 | feat(gitignore): add ANILALM to the ignore list |
| c2c9204 | 2026-04-27 | chore: untrack 已移除的評估專案目錄，移交 agent 開發團隊 |
| f220dda | 2026-04-27 | docs: 記錄評估專案移交、以 git filter-repo 重寫歷史 |
| 143f89e | 2026-04-27 | fix(security): close all Sprint 5 X review findings + remove LDAP |
| d2c74ca | 2026-04-27 | chore: rename NotebookLM → ANILA LM in remaining docs/configs |
| e29316e | 2026-04-27 | feat(sso): Sprint 6 X — security tail + SSO foundations (local login retained) |
| 0b22f54 | 2026-04-27 | docs: Sprint 7 X plan — design-only sprint for SSO follow-ups |
| 0b8509e | 2026-04-27 | fix(security): remove dead API Key UI from anila-ui SPA |
| dd6aae4 | 2026-04-27 | docs(readme): reflect Sprint 5 X / 6 X / 7 X security work |
| 021ab61 | 2026-04-27 | chore: update environment configuration and model endpoints for on-prem deployment |
| b47059d | 2026-04-27 | Merge pull request #4 from zzw09773/ingestion-design |
| e15cecb | 2026-04-28 | feat(security): split url_guard into HTTP + private-IP opt-ins |
| cd534cc | 2026-04-28 | docs(spec): add ANILA Functions v1 design spec |
| 224de85 | 2026-04-28 | docs(spec): apply Codex review fixes to ANILA Functions v1 spec |
| f51a085 | 2026-04-28 | docs(spec): apply Codex round-2 review fixes |
| 1fe8363 | 2026-04-28 | docs(spec): apply Codex round-3 review fixes |
| 030dc4a | 2026-04-29 | docs(spec): apply Codex round-4 review fixes |
| fe6ac8d | 2026-04-29 | docs(spec): apply Codex round-5 review fixes |
| dfbd0e7 | 2026-04-29 | docs(spec): apply Codex round-6 review fixes |
| 924dcd9 | 2026-04-29 | docs(spec): apply Codex round-7 review fixes |
| 9aaf9cc | 2026-04-29 | docs(spec): apply Codex round-8 review fixes |
| 52633b6 | 2026-04-29 | docs(plan): add ANILA Functions v1 implementation plan |
| b473048 | 2026-04-29 | feat(sprint-7x): Studio jobs + ingestion search + conversation collection/origin |
| 65c7f0e | 2026-04-29 | chore(infra): docker-compose + nginx + cert + gitignore updates |
| 5290907 | 2026-04-29 | feat(anilalm): research-style knowledge base SPA prototype |
| c4bb061 | 2026-04-29 | chore: pin cryptography for ANILA Functions AES-GCM |
| 94b1b88 | 2026-04-29 | feat(db): add action_functions schema (functions + versions + valves + runs + reports) |
| 5aedb81 | 2026-04-29 | feat(models): add ActionFunction sqlalchemy models |
| fc5e5f7 | 2026-04-29 | feat(functions): add AES-256-GCM valves crypto helper |
| 605338d | 2026-04-29 | feat(schemas): add ActionFunction pydantic schemas |
| b251862 | 2026-04-29 | feat(functions): function CRUD with advisory-lock save |
| 8a6ad98 | 2026-04-29 | feat(functions): worker-api client stub (real impl in Sprint 2) |
| f0fe4ec | 2026-04-29 | feat(functions): ownership authz (chat_message vs test_console split) |
| 7c55dbf | 2026-04-29 | feat(functions): audit redaction (defense-in-depth) |
| f818b5d | 2026-04-29 | feat(api): action_function endpoints — CRUD/valves/market/run/runs/enabled-actions |
| 028ecaf | 2026-04-29 | feat(functions): 360-day audit retention purge |
| e9174fd | 2026-04-29 | feat(worker): project skeleton + README + requirements.txt |
| ef3802c | 2026-04-29 | feat(worker): JSON-line wire protocol (JobSpec + event envelope) |
| 7dc67e1 | 2026-04-29 | feat(worker): subprocess runtime wrapper (exec + extract dispatch) |
| 6c30bc1 | 2026-04-29 | feat(worker): static AST extractor + dynamic fallback for schema |
| 582c7f3 | 2026-04-29 | feat(worker): ambient cap clear helper for subprocess preexec_fn |
| 97df55b | 2026-04-29 | feat(worker): sandbox daemon — Unix socket accept + spawn loop |
| 57c0591 | 2026-04-29 | feat(worker): worker-api FastAPI gate (CSP-facing, Unix socket relay) |
| 2cfc6ed | 2026-04-29 | feat(worker): Dockerfiles + sandbox-entrypoint.sh + egress proxy |
| 15541cf | 2026-04-29 | feat(infra): docker-compose for ANILA Functions v1 worker stack |
| 5ce0579 | 2026-04-29 | feat(worker): sandbox isolation + egress + Sprint 2.5 smoke tests |
| 373b9e5 | 2026-04-29 | feat(anila-ui): runtime modules for ANILA Functions v1 |
| 32e2efb | 2026-04-29 | feat(anila-ui): admin Functions page (list + editor + Test Console + Audit) |
| cab2eef | 2026-04-29 | feat(anila-ui): add Function action buttons to assistant message toolbar |
| e208bff | 2026-04-29 | feat(anila-ui): Playwright E2E scaffold for Functions v1 |
| 2543b54 | 2026-04-29 | fix(worker): reuse base image's nobody:nogroup for user subprocess |
| e7247ec | 2026-04-29 | fix(worker): sandbox entrypoint chmod order + simpler squid config |
| d40c206 | 2026-04-29 | fix(worker): module path + chmod re-run + squid 6 log path |
| 4878f37 | 2026-04-29 | feat(worker): Sprint 2.5 prototype gate — 6/6 PASS validation |
| e3f3484 | 2026-04-29 | feat(anila-ui): wire ANILA Functions v1 enabled-actions through ChatRuntime |
| d55687f | 2026-04-29 | fix(worker): wire CSP secret + supplementary groups + socket chgrp |
| 8b5aca9 | 2026-04-29 | feat(anila-ui): /admin/functions route + Sidebar menu entry for dev/admin |
| b1d6797 | 2026-04-29 | fix(functions): bug bash post-dogfood (B1-B5) |
| 06f47b2 | 2026-04-29 | fix(functions): strip invisible chars from slug input + diagnostic msg |
| fa73baa | 2026-04-29 | fix(functions): extract self.actions from __init__ + visibility refetch |
| d303381 | 2026-04-29 | fix(functions): send msg.dbId not msg.id when triggering /run |
| 3de449a | 2026-04-29 | fix(functions): SSE-friendly nginx block + structured run logs |
| 44568cf | 2026-04-29 | fix(nginx): strip /api/functions/ trailing slash to prevent SPA catch-all |
| dc864bf | 2026-04-30 | chore(infra): revert ANILA Functions v1 deploy artefacts |
| dcee7d9 | 2026-04-30 | fix(infra): nginx waits for router + anila-ui before starting |
| 4f66d31 | 2026-04-30 | feat: Implement image persistence and retrieval for enhanced Studio functionality |
| 8f9761d | 2026-04-30 | revert: remove ANILA Functions v1 stack entirely |
| d367519 | 2026-04-30 | feat(cli): add terminal UI components including badges, buttons, boxes, modals, and more |
| 4c20ea6 | 2026-05-01 | feat(csp): per-agent service-token bootstrap + caller attribution |
| 5a820ca | 2026-05-01 | feat(anila-core,router): RotatingServiceTokenMiddleware + bootstrap CLI |
| 9c8a6b6 | 2026-05-01 | feat(agentic-rag,csp-frontend): bootstrap deploy story + admin UI |
| 4eaad46 | 2026-05-01 | docs+fix: runbooks + READMEs + classified-latch reload-escape hotfix |
| 5c09f91 | 2026-05-01 | feat(csp): caller_agent_id wiring + cutover dashboard widgets |
| 4ebecdd | 2026-05-01 | docs: rewrite root + ANILA UI READMEs for the 9-service topology |
| c4d9ff4 | 2026-05-01 | docs(anila-core): two-pillar boundary correction (no code change) |
| b223a05 | 2026-05-01 | feat(ingestion): chunking-preview wizard — compare strategies before commit |
| 717ae6f | 2026-05-01 | fix(ingestion): make chunking-preview the default create flow |
| 1c8f041 | 2026-05-01 | feat(ingestion): visual diff strip on chunking-preview |
| e642359 | 2026-05-01 | fix(ingestion): CJK-aware token estimation + overlap surfacing |
| d290012 | 2026-05-02 | docs: parent-child RAG retrieval design (review-passed) |
| d06d11d | 2026-05-02 | feat(ingestion): parent-child RAG retrieval (Sprint 9 X) |
| 0f2728c | 2026-05-02 | docs(runtime_logic): rewrite README for two-codebase layout + AgenticRAG roadmap |
| 5c38eaa | 2026-05-02 | docs: deep-dive on openai-agents-python for AgenticRAG enhancement |
| 4e830c0 | 2026-05-02 | docs(deep-dive): split openai-agents analysis per consumer (anila-core vs AgenticRAG) |
| e372ef1 | 2026-05-02 | refactor(agenticrag): decouple from anila-core (Phase 0) + bootstrap dev UX (Phase 0.5) |
| 462d9bd | 2026-05-02 | feat(anila-agent-framework): Phase 1 architecture + Sprint 1 stage A |
| 18c826e | 2026-05-02 | feat(agenticrag): vendor v0.1 agent framework in-tree (8 sprints) + retire anila-agent-framework |
| 0147da2 | 2026-05-02 | feat(csp): developer guide page + sidebar link + DeveloperAgentsView guide refresh |
| ecb3b2d | 2026-05-02 | feat(anila-core): Sprint 9 — web 對話 protocol (v0.8.0) |
| cc2d63f | 2026-05-02 | docs(csp): translate developer guide page to traditional chinese |
| 9bac32f | 2026-05-02 | feat(anila-core): Sprint 10 — multi-agent control flow (v0.9.0) |
| 08f0df7 | 2026-05-02 | feat(anila-core): Sprint 11 — governance & observability (v0.10.0) |
| 7b388d0 | 2026-05-03 | feat(anila-core): Sprint 12 — workspace + sandboxed tools + guardrails (v0.11.0) |
| 0aca000 | 2026-05-03 | Add runtime configuration support for agents |
| 9c614cd | 2026-05-03 | Merge pull request #6 from zzw09773/ANILALM |
| c83bc13 | 2026-05-04 | feat(memory): unified user-tenant memory layer (route 3) + cross-tenant access |
| 79be2c9 | 2026-05-04 | feat(anila-core): v0.13.0 — agent runtime plug-in for cross-tenant user memory |
| 7917650 | 2026-05-04 | feat(AgenticRAG): vendored user-memory client (route-3 cross-tenant, decoupled) |
| e50b5ef | 2026-05-04 | refactor(AgenticRAG): decouple from ANILA — pluggable UserContextProvider + dev docs |
| 2360517 | 2026-05-05 | Merge pull request #7 from zzw09773/ANILALM |
| 9279278 | 2026-05-05 | refactor(anila-core): own parser stack + VisionProvider — AgenticRAG becomes pure template |
| 590a527 | 2026-05-06 | Initial commit |
| 5c18856 | 2026-05-06 | feat: scaffold anila-agent agentic-rag template |
| 6caa5a8 | 2026-05-06 | feat(chunking-preview): enhance strategy preview messaging for embedding requirements |
| 64b9f48 | 2026-05-06 | feat: persist conversation classification state and improve error handling in classifyRetryQueue |
| 24f3e7c | 2026-05-06 | feat(models): activate endpoint + symmetric row button |
| a2785a0 | 2026-05-06 | chore(certs): gitignore cert files; each host signs its own pair |
| f42aeef | 2026-05-06 | feat(rbac): add owner tier above admin |
| 2639103 | 2026-05-06 | fix(rbac): add 'owner' to UserRole literal |
| 5495385 | 2026-05-06 | Add 'anila-agent/' from commit '5c188564ea15383370ef0ddc20fa270252745496' |
| 36861b9 | 2026-05-06 | chore: replace AgenticRAG template with anila-agent subtree |
| 1be1c4b | 2026-05-06 | docs(readme): document anila-agent subtree workflow |
| 75149f6 | 2026-05-11 | feat(retrieval): pgvector retrievers + 0.2.0 release |
| 09f0b1c | 2026-05-11 | Merge commit '75149f6402e57044b0858fc32f1a37e107bdb2d5' |
| 293cf36 | 2026-05-11 | chore(template): clear project-specific defaults for fresh clones (0.2.1) |
| 9ab4a2c | 2026-05-11 | Merge commit '293cf3600e9173b555f5e71f1a79b07037ce260d' |
| 148e3f2 | 2026-05-11 | Refactor code structure for improved readability and maintainability |
| e31245d | 2026-05-11 | feat(readme): 更新 anila-agent subtree 及 owner-tier RBAC 修補，重寫開發者指南 |
| 1177689 | 2026-05-11 | feat(models): decouple inference stack to anila-models compose + SSRF guard parity + is_internal flag |
| 6b9794c | 2026-05-11 | feat(security): DB-driven trusted_hosts allow-list + typed 400 confirm modal + JSONB-on-SQLite 解凍 |
| bc30e8a | 2026-05-11 | fix(docker-compose): 修正 AUTO_REGISTER_MODELS 的 endpoint_url 格式，避免 404 錯誤 |
| 2f108bc | 2026-05-12 | feat(api): add OpenAI-compat GET /v1/models for external client discovery |
| e0235b1 | 2026-05-12 | Refactor docker-compose.yml for clarity and organization |
| 973b959 | 2026-05-13 | feat(docker-compose): 更新 gemma4 服務配置，增加 MTP 支援與記憶體配置調整 |
| a762a1c | 2026-05-15 | Add integration tests for card login endpoints and lockdown behavior |
| f01288e | 2026-05-15 | Refactor card login functionality and update related tests |
| 93bd3e8 | 2026-05-16 | feat(security): 內網部署前 hardening + 部署 runbook |
| d41993d | 2026-05-16 | chore(deps): bump 5 packages to clear all known CVEs |
| f490aba | 2026-05-16 | feat(security): 清完 review 兩個 LOW (audit fail-soft + nginx Host allowlist) |
| 7ccc618 | 2026-05-16 | feat(security): clear Tier-1 residual CVE + recon surface |
| f5ada48 | 2026-05-16 | feat(security): add startup check for CARD_INITIAL_OWNERS placeholder |
| 567bcc4 | 2026-05-16 | Add SSO migration plan and intranet deployment runbook |
| 02076f2 | 2026-05-16 | Add governance documents for AI risk management and third-party AI components |
| cbdee49 | 2026-05-16 | Refactor authentication flow and remove SSO dependencies |
| bf9983e | 2026-05-16 | feat(flux2-dev-agent): schemas for OpenAI chat completion shape |
| 5ffe584 | 2026-05-16 | feat(flux2-dev-agent): image_store for PNG persistence + URL generation |
| 63d6f68 | 2026-05-16 | feat(flux2-dev-agent): async HTTP client for flux2-dev backend |
| 943e24f | 2026-05-16 | feat(flux2-dev-agent): gemma4-backed prompt translator with safe fallback |
| 56ff038 | 2026-05-16 | fix(flux2-dev-agent): broaden translator fallback to all exceptions |
| 870e5f8 | 2026-05-16 | feat(flux2-dev-agent): chat handler that returns markdown image |
| 357266f | 2026-05-16 | feat(flux2-dev-agent): FastAPI entrypoint with health/models/chat endpoints |
| e5860f0 | 2026-05-16 | fix(flux2-dev-agent): tighten main entrypoint hardening |
| cae6ff5 | 2026-05-16 | build(flux2-dev-agent): Dockerfile for the shim container |
| ef686c0 | 2026-05-16 | feat(flux2-dev): FastAPI inference server with injectable pipeline |
| 396c7d5 | 2026-05-16 | chore(flux2-dev-agent): silence pylance warnings on main.py |
| def7e0a | 2026-05-16 | chore(flux2-dev-agent): add pyright ignore for FastAPI handlers |
| 0b97772 | 2026-05-16 | feat(models): add flux2-dev + flux2-dev-agent to compose |
| fa3f3df | 2026-05-16 | feat(csp): auto-register image-generator agent for FLUX dispatch |
| 338b3b2 | 2026-05-16 | build(flux2-dev): CUDA 12.4 Dockerfile with diffusers |
| ab69419 | 2026-05-16 | fix(flux2-dev): bump torch+pin transformers; healthcheck fix; e2e report |
| 28ada1d | 2026-05-18 | fix(csp): declaratively register gemma4 + image-generator agent + smoke-user perms |
| 6d79b71 | 2026-05-18 | fix(flux2-dev-agent): emit SSE chunks when stream=true |
| ef2fcc8 | 2026-05-18 | [both] docs: branch sync backlog for prod / feature-no-sso dual maintenance |
| 624f590 | 2026-05-18 | [both] docs: branch sync backlog for prod / feature-no-sso dual maintenance |
| 6b6463a | 2026-05-16 | feat(flux2-dev-agent): schemas for OpenAI chat completion shape |
| e344a02 | 2026-05-16 | feat(flux2-dev-agent): image_store for PNG persistence + URL generation |
| ca9286a | 2026-05-16 | feat(flux2-dev-agent): async HTTP client for flux2-dev backend |
| bbfa2db | 2026-05-16 | feat(flux2-dev-agent): gemma4-backed prompt translator with safe fallback |
| eb47a9c | 2026-05-16 | fix(flux2-dev-agent): broaden translator fallback to all exceptions |
| 0a6cfbb | 2026-05-16 | feat(flux2-dev-agent): chat handler that returns markdown image |
| 3ec0604 | 2026-05-16 | feat(flux2-dev-agent): FastAPI entrypoint with health/models/chat endpoints |
| 850f974 | 2026-05-16 | fix(flux2-dev-agent): tighten main entrypoint hardening |
| 0f87138 | 2026-05-16 | chore(flux2-dev-agent): silence pylance warnings on main.py |
| a76b8c7 | 2026-05-16 | chore(flux2-dev-agent): add pyright ignore for FastAPI handlers |
| 403215c | 2026-05-16 | build(flux2-dev-agent): Dockerfile for the shim container |
| 9f5c1df | 2026-05-16 | feat(flux2-dev): FastAPI inference server with injectable pipeline |
| bbbe1ba | 2026-05-16 | build(flux2-dev): CUDA 12.4 Dockerfile with diffusers |
| 2cd93ad | 2026-05-16 | feat(models): add flux2-dev + flux2-dev-agent to compose |
| a8dca0f | 2026-05-16 | fix(flux2-dev): bump torch+pin transformers; healthcheck fix; e2e report |
| b85d749 | 2026-05-18 | fix(flux2-dev-agent): emit SSE chunks when stream=true |
| b35f36f | 2026-05-18 | [adapted from fa3f3df+28ada1d] feat(csp): bring FLUX agent registration to prod compose |
| 4448e85 | 2026-05-18 | docs: update branch-sync-backlog after FLUX→prod port |
| 8d047ef | 2026-05-18 | Implement feature X to enhance user experience and fix bug Y in module Z |
| 0ca882e | 2026-05-18 | docs: implementation plan for ANILALM × FLUX pptx integration (Phase 6) |
| 2774663 | 2026-05-18 | feat(studio): add Slide.image_prompt for LLM-requested generated images |
| 3a2acf9 | 2026-05-18 | feat(studio): FluxImageProvider skeleton + cache key derivation |
| 8a2a2f3 | 2026-05-18 | feat(studio): FluxImageProvider.get_or_generate with on-disk cache |
| f795a30 | 2026-05-18 | feat(studio): semaphore-limit concurrent FLUX calls + error coverage |
| c0f669b | 2026-05-18 | feat(studio): rename _hydrate_image_refs→_hydrate_images + FLUX prompt path |
| 4d940c0 | 2026-05-18 | feat(studio): env-driven FluxImageProvider singleton wiring |
| eedf3ac | 2026-05-18 | feat(studio): teach LLM about image_prompt for on-demand FLUX illustrations |
| f816619 | 2026-05-18 | [both] feat(compose): wire FLUX env vars for studio FluxImageProvider |
| e848318 | 2026-05-18 | test(studio): e2e for FLUX image_prompt hydration pipeline |
| ba692f8 | 2026-05-18 | [both] docs(studio): FLUX image_prompt deployment notes + backlog update |
| 1f77986 | 2026-05-18 | fix(ingestion): skip uniform-color images at caption + persist |
| e34adf9 | 2026-05-18 | fix(csp): also emit logs to stdout for docker logs visibility |
| 5a4e1eb | 2026-05-18 | feat(pptx-renderer): per-request access log to stdout |
| 4707ddb | 2026-05-18 | fix(csp): propagate uvicorn.access logger so HTTP lines reach docker logs |
| 910253c | 2026-05-18 | fix(csp): add FastAPI middleware to log every request to stdout |
| 88894b4 | 2026-05-18 | docs(studio): import quality-fix spec from post-Phase-6 inspection |
| f46b690 | 2026-05-18 | fix(pptx-renderer): drop faux-italic on cover subtitle, darken+enlarge |
| b0654c2 | 2026-05-18 | feat(pptx-renderer): expand CONCEPT_MAP + group icons by domain |
| 8937ae2 | 2026-05-18 | feat(studio): stat_callout / two_column saturation enforcement |
| d370098 | 2026-05-18 | feat(studio): deterministic geometric QA via renderer |
| 265c11e | 2026-05-18 | feat(studio): layout post-validation + LLM rebalance pass |
| ebfea2e | 2026-05-18 | feat(studio): split image_focus into illustration (FLUX) vs diagram (graphviz) |
| 2515c29 | 2026-05-18 | docs(studio): round 2 patch spec — 7 patches addressing v2 regression |
| 186b3da | 2026-05-18 | fix(pptx-renderer): drop italic on section_break subtitle |
| 099a2a7 | 2026-05-18 | fix(pptx-renderer): correct renderStatCallout solo-mode centering math |
| d98c0d6 | 2026-05-18 | fix(studio): Column.bullets min 3→2, demote sparse two_column to icon_rows |
| 846bba1 | 2026-05-18 | feat(studio): broaden rebalancer trigger to V4-heavy decks |
| bd632e4 | 2026-05-18 | feat(studio): expand V4 enumeration keywords from 7 to ~40 |
| fd09c76 | 2026-05-18 | feat(pptx-renderer): grid-based local emptiness detection |
| ae5ca44 | 2026-05-18 | feat(studio): preemptive diagram-path strengthening + diagnostic helper |
| c7970b5 | 2026-05-19 | fix(csp): install graphviz in the ACTUAL Dockerfile compose uses |
| 8b404dc | 2026-05-19 | docs(studio): round 3 patch spec — 12 patches (H-P) over 2 parts |
| 37c1d6c | 2026-05-19 | feat(studio): V4 content-pattern detection complements title keywords |
| aed7482 | 2026-05-19 | feat(studio): strip LaTeX math mode in text normalizer |
| 494a50a | 2026-05-19 | feat(pptx-renderer): icon_rows fallback + concept map expansion |
| d9ede40 | 2026-05-19 | feat(pptx-renderer): auto-shrink section_break title for long strings |
| 3bc5b70 | 2026-05-19 | feat(studio): add theme field as palette successor with backwards compat |
| 1aa585d | 2026-05-19 | feat(pptx-renderer): theme scaffolding — THEMES bundle + dispatch wiring |
| 409db8a | 2026-05-19 | feat(pptx-renderer): implement warm_journal theme visual differentiation |
| 335e36f | 2026-05-19 | feat(pptx-renderer): implement academic_paper theme |
| d22f05f | 2026-05-19 | feat(pptx-renderer): implement executive_brief + startup_pitch themes |
| 58e84ee | 2026-05-19 | feat(studio): tone-based theme selection in LLM prompt |
| a5eee3b | 2026-05-19 | feat(studio): API theme_override for bypassing LLM theme selection |
| 4f65bfa | 2026-05-19 | fix(.gitignore): add n8n_DATA to ignore list |
| 8cf5f69 | 2026-05-19 | docs(studio): round 4 patch spec — 4 patches (R/S/Q/T) |
| 73b2fe3 | 2026-05-19 | fix(studio): strip JSON-eaten LaTeX control-char variants |
| 58a83f7 | 2026-05-19 | fix(studio): strip RAG citation markers from end of slide text |
| 55949af | 2026-05-19 | fix(pptx-renderer): warm_journal icon fallback dot too large |
| 3aee569 | 2026-05-19 | fix(studio-audit): catch image_focus disguise without real image |
| 1c0fdd8 | 2026-05-19 | feat(studio): deterministic title-keyword theme override |
| 0d809e9 | 2026-05-19 | fix(studio-diagram): normalize diagram_dot before graphviz render |
| 9541b3d | 2026-05-19 | feat(studio-audit): add [H-DIAG] logging to rebalance pipeline |
| 2a0b54b | 2026-05-19 | feat(studio): implement deterministic title-keyword theme override and enhance logging for rebalance diagnostics |
| 69e6ec0 | 2026-05-19 | fix(logging): idempotent setup_logging + re-call after alembic upgrade |
| 4de0ecf | 2026-05-20 | fix(logging): re-enable disabled named loggers after alembic fileConfig |
| ab1790a | 2026-05-20 | fix(studio-audit): split V4 into V4_CONTENT (strong) / V4_TITLE (hint) |
| 30a7e42 | 2026-05-20 | feat(audit): 拆分 V4 為 V4_CONTENT 和 V4_TITLE，調整違規判斷邏輯 |
| a49e2b0 | 2026-05-20 | Merge feature/studio-quality-fixes: Studio 品質修補 6 輪 + theme override |
| 4d3f6ed | 2026-05-20 | feat(studio-ui): theme picker — wire theme_override to frontend |
| 02e156e | 2026-05-20 | feat(studio-ui): wizard mode — recommend theme from scenario questions |
| eb66bcf | 2026-05-20 | Refactor code structure for improved readability and maintainability |
| c1ad2c3 | 2026-05-20 | fix(studio): strip multi-number citations like (參 [1], [8]) |
| 1ff4888 | 2026-05-20 | fix(pptx-renderer): white heroicon glyph for filled_pill (startup_pitch) |
| 3947d6a | 2026-05-20 | feat(flux2-dev): extend /generate contract — seed, N candidates, 3:1, list response |
| bdf20a5 | 2026-05-20 | feat(studio-flux): backend Stage 1 — rewriter + cover hero + 6 locked contracts |
| 1b8b5aa | 2026-05-20 | test(flux2-dev): align server tests with Stage 1 JSON contract |
| d0b212b | 2026-05-20 | fix(flux2-dev-agent): align flux_client with Stage 1 JSON contract |
| d84e615 | 2026-05-21 | feat(pptx-renderer): render cover hero image behind title slide |
| 2c6e79f | 2026-05-21 | chore(studio-flux): add [H-DIAG] logging to cover-hero pipeline |
| 268f59e | 2026-05-21 | fix(pptx-renderer): show cover hero on section_break-style cover |
| 741d5bf | 2026-05-21 | chore(studio-flux): remove cover-hero [H-DIAG] logging after verification |
| e06400f | 2026-05-21 | feat(studio-flux): 新增 FLUX 語意對齊圖像生成規格文檔，定義多階段合約及元件盤點 |
| 4ad03d6 | 2026-05-21 | fix(pptx-renderer): gradient title scrim on cover hero for legibility |
| 5886d3e | 2026-05-21 | feat(studio-flux): Stage 2 quality gate (Layer C) — N=2 候選 + 三道閘門 + 重試 + fallback |
| 31a1ab3 | 2026-05-21 | chore(studio-flux): add gate decision logging for Stage 2 verification |
| c160c04 | 2026-05-21 | feat(studio-flux): striping 閾值校準工具套件 (spec 5.5) |
| a1cbaef | 2026-05-21 | feat(flux-quality-gate): 更新 HF_ENERGY_THRESH 以提高圖像質量，新增 50 張圖像的校準數據 |
| 7064570 | 2026-05-21 | docs(studio-flux): Stage 2 去除 CLIP 閘、VLM 打分排序設計 (spec 5.5/5.6) |
| 743216a | 2026-05-21 | feat(studio-flux): VLM gate 回傳 0-1 排序分數 |
| 9cbe02c | 2026-05-21 | feat(studio-flux): 去除 CLIP 閘,改 striping+VLM 兩道閘並用 VLM 分數排序 (spec 5.5) |
| 07eeae1 | 2026-05-21 | docs(studio-flux): 修正 clip_score 欄位註解(CLIP 已 de-scope,恆 None) |
| cd07dc7 | 2026-05-21 | docs(studio-flux): Stage 3 brand.yaml 多品牌風格(依部門)設計 (spec §6.2) |
| 8637be7 | 2026-05-21 | docs(studio-flux): Stage 3 改採依內容自動推斷風格,作廢 brand.yaml 方案 |
| 3e93864 | 2026-05-21 | feat(studio-flux): 依內容自動推斷 deck 風格 infer_deck_style (spec §6) |
| 3dcf446 | 2026-05-21 | feat(studio-flux): 透傳 deck_style 至 hydration cover-hero 路徑 |
| e4d1ea8 | 2026-05-21 | feat(studio-flux): job pipeline 依內容推斷 deck 風格並注入 render (spec §6) |
| aef33fb | 2026-05-21 | fix(studio-flux): QA 重渲染路徑也帶 deck_style,避免回退預設風格 |
| 05c5a69 | 2026-05-21 | feat(studio-flux): 依內容自動推斷 deck 風格並注入 render (spec §6) |
| c9a2269 | 2026-05-21 | docs(studio-flux): Stage 4 插圖全用途路由設計 (spec §7, 後端核心) |
| 0efcb55 | 2026-05-21 | docs(studio-flux): Stage 4 spec 補釘 concept_en 取捨/計數順序/Path1-2 互斥 |
| fce12dd | 2026-05-21 | feat(studio-flux): _infer_image_use_case 推斷插圖 use_case (spec §7) |
| 032d439 | 2026-05-21 | feat(studio-flux): _apply_illustration_fallback 統一 fallback 寫入 (spec §6) |
| 0c80dda | 2026-05-21 | feat(studio-flux): _generate_slide_illustration 共用生成 helper (spec §3) |
| 5fce006 | 2026-05-21 | feat(studio-flux): 內文/章節插圖統一走 rewriter+閘門路由 + per-deck 上限 (spec §7) |
| b2d4d8c | 2026-05-21 | docs(studio-flux): Stage 4 加 CONTENT 密度門檻→image_focus,修正 renderer 零改動誤判 |
| b3a634e | 2026-05-21 | feat(studio-flux): CONTENT 插圖密度門檻→image_focus 顯示 (spec §4) |
| b09e011 | 2026-05-21 | test(pptx-renderer): image_focus 顯示 image_data / standard 忽略 的回歸 smoke test |
| 8422ab9 | 2026-05-21 | feat(studio-flux): 實作插圖全用途路由，統一處理插圖生成與上限控制 |
| d6eda6d | 2026-05-21 | docs(studio-flux): 收斂 FLUX 文件至 docs/superpowers/studio-flux/(spec/plans/history)+修內部引用 |
| adb6a71 | 2026-05-21 | docs: 頂層文件依主題收斂至子目錄,同步更新所有程式/文件引用路徑 |
| 762013b | 2026-05-21 | docs: 重寫 9 個子專案的雙語 README (中文主 + README.en.md 英文副) |
| bc025e6 | 2026-05-21 | docs(readme): 更新根 README 專案結構與失效連結 |
| c1d4973 | 2026-05-22 | Merge feature/studio-wizard into feature/studio-flux |
| 1c4f82f | 2026-05-22 | fix(studio): 處理 PR #9 code review 意見 + 修 Stage 4 遺留 e2e 測試 |
| 14fb146 | 2026-05-22 | Merge pull request #9 from zzw09773/feature/studio-flux |
| c1547bc | 2026-05-23 | test(pptx-renderer): cover hero P1 guard regression test |
| 8d63c18 | 2026-05-23 | Merge pull request #10 from zzw09773/feature/studio-flux |
| 20a1154 | 2026-05-23 | fix(auth): bump session TTL — access 15→60min, refresh 7→30days |
| 515aaf8 | 2026-05-23 | fix(compose): 處理 PR #11 review — token TTL 改變數插值 |
| fcc6832 | 2026-05-23 | Merge pull request #11 from zzw09773/feature/session-ttl-bump |
| d7b5970 | 2026-05-23 | docs(anila-studio): extraction plan v2 (post codex review) |
| 904306e | 2026-05-23 | chore(anila-studio): Phase 0 baseline + skeleton |
| a09242d | 2026-05-23 | feat(ingestion): add image search + blob HTTP endpoints for anila-studio extraction |
| e2ecefd | 2026-05-23 | feat(csp/backend): JWT HS256 → RS256 cutover + JWKS endpoint |
| 4437724 | 2026-05-23 | test(ingestion): chunk search contract test + dev JWT auto-keygen in conftest |
| 1825087 | 2026-05-23 | feat(csp/backend): Redis pub/sub + revocations endpoint for cross-service token-revoke |
| ee4e8a6 | 2026-05-23 | feat(csp/router): wire Phase 1 endpoints + integration smoke test |
| fe7319b | 2026-05-23 | chore(anila-studio): Phase 2 base — pyproject + config + main.py skeleton |
| b8c08c1 | 2026-05-23 | feat(anila-studio): JWKS client w/ TTL cache + key rotation refetch |
| 9ac1535 | 2026-05-23 | feat(anila-studio): cross-service JWT revocation cache (Redis pub/sub + cold-start sync) |
| f6bafc2 | 2026-05-23 | feat(anila-studio): csp_client thin async HTTP wrapper |
| cda78c8 | 2026-05-23 | feat(anila-studio): Phase 2 wiring — auth.py + lifespan + Dockerfile + compose |
| 1f9a55e | 2026-05-23 | feat(anila-studio): copy 8 service files from csp/backend |
| 3b9e4ed | 2026-05-23 | feat(anila-studio): copy schemas/studio.py |
| 56066c3 | 2026-05-23 | feat(anila-studio): copy studio.py from csp (broken imports — fixed in next 9 sub-commits) |
| df2e2ee | 2026-05-23 | refactor(anila-studio/studio.py): commit A — strip DB imports + remove db param |
| 31e3e11 | 2026-05-23 | refactor(anila-studio/studio.py): commit B — strip app.models imports |
| 7fc78ea | 2026-05-23 | refactor(anila-studio/studio.py): commit C — strip ingestion_pool + proxy_service imports |
| cc1bfdc | 2026-05-23 | refactor(anila-studio/studio.py): commit D — wire in csp_client + auth |
| f2b0f98 | 2026-05-23 | refactor(anila-studio/studio.py): commit E — _retrieve_chunks via csp_client HTTP |
| 1510ab9 | 2026-05-23 | refactor(anila-studio/studio.py): commit F — _retrieve_images via csp_client HTTP |
| 1682bc6 | 2026-05-23 | refactor(anila-studio/studio.py): commit G — _call_llm_chat via csp_client.proxy_chat_completions |
| 946a015 | 2026-05-23 | refactor(anila-studio/studio.py): commit H — _hydrate_images via csp_client.fetch_image_blob |
| af0ea10 | 2026-05-23 | refactor(anila-studio/studio.py): commit I — endpoint signatures + _run_pipeline use bearer+identity |
| 1bac26f | 2026-05-23 | feat(anila-studio): mount studio_router into main app |
| 01dc035 | 2026-05-23 | test(anila-studio): Phase 3 wiring smoke — FLUX / RAG / PPTX call paths |
| d724a4b | 2026-05-23 | test(anila-studio): copy 20 csp studio-coupled test files (Phase 4 prep) |
| 76a39ff | 2026-05-23 | test(anila-studio): adapt test_hydrate_images for Phase 3 signature |
| 29eaaa6 | 2026-05-23 | test(anila-studio): adapt test_layout_rebalance for Phase 3 bearer kw |
| 1187f82 | 2026-05-23 | test(anila-studio): adapt test_studio_flux_e2e for Phase 3 bearer kw |
| 3bdaa42 | 2026-05-23 | test(anila-studio): adapt test_studio_illustration_routing for Phase 3 bearer kw |
| b3664de | 2026-05-23 | chore(anila-studio): add numpy to dependencies (flux_quality_gate FFT) |
| 0012b61 | 2026-05-23 | chore(anila-studio): scripts/export-openapi.py — regenerate OpenAPI JSON |
| fded3cb | 2026-05-23 | feat(ANILALM): add openapi-typescript codegen for anila-studio types |
| 645d9e2 | 2026-05-23 | feat(ANILALM): route /api/studio through STUDIO_BASE_URL + codegen types |
| c5b242f | 2026-05-23 | chore(anila-studio): export OpenAPI schema to openapi/studio.openapi.json |
| 3091db7 | 2026-05-23 | refactor(csp/router): remove studio_router (extracted to anila-studio) |
| 676e4c2 | 2026-05-23 | refactor(csp): git rm 10 Studio source files (moved to anila-studio) |
| 2d20fe5 | 2026-05-23 | refactor(csp/tests): git rm 20 Studio test files (moved to anila-studio) |
| 13dd2b5 | 2026-05-23 | docs(anila-studio): Phase 8 — README + ADR + E2E runbook + cross-repo notes |
| cda6a6c | 2026-05-23 | fix(compose): anila-studio missing CSP_SERVICE_TOKEN env → cold-start 401 |
| 672dbc5 | 2026-05-23 | fix(nginx): /api/studio/ route to anila_studio_backend (not csp_backend) |
| cb4de04 | 2026-05-23 | fix(anila-studio): csp_client proxy URL prefix wrong (/api/proxy → ∅) |
| 9697647 | 2026-05-23 | fix(anila-studio): per-call timeout override — LLM proxy 180s |
| 8840186 | 2026-05-23 | fix(anila-studio/studio.py): _Gemma4VlmGate ctor missed Phase 3 G refactor |
| d9eb36e | 2026-05-23 | fix(anila-studio/Dockerfile): install from pyproject.toml (single source) |
| 126b7c2 | 2026-05-23 | fix(anila-studio): FLUX cache dir不再借 csp 的 INGESTION_UPLOAD_DIR |
| c3d9e89 | 2026-05-23 | fix(anila-studio): 內容品質 — rebalance 後重 normalize + 清 ● bullet prefix |
| 78b52ae | 2026-05-23 | revert(anila-studio): 撤回 strip_bullet_prefix — ● 是階層 marker 不是雜訊 |
| 4d5f8f4 | 2026-05-23 | feat(pptx-renderer): hierarchical bullets (● ◦ ▪) for standard layout |
| 788a402 | 2026-05-23 | fix(timeouts): LLM proxy 拉到 300s + 砍 retry — 解 ReadTimeout 12 分鐘空轉 |
| 34241a9 | 2026-05-23 | Merge pull request #12 from zzw09773/feature/anila-studio-extract |
| 861d75d | 2026-05-23 | refactor(anilalm): 製作台砍 4 種不適合內部部署的 artifact |
| 6cad080 | 2026-05-23 | chore(anila-studio): Phase 0 — 共用 infra for 4 new artifact kinds |
| 674dc9f | 2026-05-23 | feat(anila-studio): Report 深度報告完整 backend pipeline (HTML/PDF/DOCX) |
| fcfe753 | 2026-05-23 | feat(anila-studio): mindmap backend pipeline (LLM → DOT → SVG) |
| eb1018e | 2026-05-23 | feat(anila-studio): Infographic 完整 backend pipeline (HTML + chart + PDF) |
| 0f4c752 | 2026-05-23 | feat(anila-studio): datatable artifact pipeline (HTML/CSV/XLSX) |
| abd5941 | 2026-05-23 | feat(integration): Phase Z — 4 新 artifact router mount + ANILALM 全面接通 |
| 318af80 | 2026-05-23 | fix(nginx): /api/{studio,reports,mindmaps,infographics,datatables}/ → anila_studio_backend |
| b6878e0 | 2026-05-23 | fix(anila-studio/Dockerfile): chown ARTIFACTS_DIR to anila user |
| eb11d50 | 2026-05-25 | fix(anila-studio/Dockerfile): chromium 改 system-wide path 給 non-root user 用 |
| 841681d | 2026-05-25 | chore(datatable): retrieval diagnostic log |
| 3867552 | 2026-05-25 | fix(anilalm/WSStudio): timeline label 5 種 kind 各自獨立(原只 cover 2 種) |
| 9c933ac | 2026-05-25 | fix(datatable+infographic): LLM prompt 強化 + retrieval diagnostic log |
| ec5bc7b | 2026-05-25 | fix(datatable schema): rows 允許 0 個(主題不符 fallback 必需) |
| 14d268b | 2026-05-25 | fix(datatable): 拿掉 runtime 0-row guard(prompt 教 LLM 給 0 rows 是合法 fallback) |
| 4cb30a5 | 2026-05-25 | fix(retrieval): seed_query 中文化 + 過濾短 chunks(markdown artifact) |
| 3ab588b | 2026-05-25 | fix(infographic): chart 中文豆腐 — matplotlib 加 JP face 保底 + 顯式 addfont |
| 22608b7 | 2026-05-25 | fix(infographic): 真正繁體 typography — Dockerfile wget Noto Sans CJK TC OTF |
| 8653e6e | 2026-05-25 | Merge pull request #14 from zzw09773/feature/studio-features-complete |
| 92faba3 | 2026-05-25 | fix: usage CSV 時區 UTC+8 + ANILA_UI 生成圖片置中 (#15) |
| 6fcbdce | 2026-05-25 | sync(main→prod): 同步 main 的 204 個 commit 到 prod,保留 fork 區 + prod-only |
| 46930f4 | 2026-05-25 | fix(prod compose): 清除 dev fallback,改 fail-loud + share/ 路徑 |
| ec4dd57 | 2026-05-25 | fix(prod auth): restore fork 區漏帶的 5 個 module + router mount |
| 88bce2f | 2026-05-25 | Merge pull request #16 from zzw09773/sync/main-to-prod |
| 25a68ab | 2026-05-26 | fix(prod auth): 補回 PR #16 漏的 fork 區 + 補 main 的 /revocations endpoint |
| 3de7c1f | 2026-05-26 | feat(scripts): 加 deploy-prod.sh 內網部署腳本 |
| 2cf463f | 2026-05-26 | docs(prod root): 加 prod branch banner + 接 scripts/deploy-prod.sh + 重整 docs/ 索引 |
| 6156871 | 2026-05-26 | docs(prod sub-projects): 10 個子專案 README 加 prod 分支 banner + last updated |
| eb81022 | 2026-05-26 | docs(prod sub-projects en): mirror prod banner + last updated into 10 English README.en.md |
| 6427389 | 2026-05-26 | fix(nginx): 把當前主機 LAN IP (172.16.120.35) 加進 Host allowlist |
| 46ed491 | 2026-05-26 | docs(sync-backlog): 重寫對齊 5 branch SSOT 結構 |
| 70759f1 | 2026-05-26 | feat(deploy-prod.sh + README): 支援 3 條 prod branch (post 2026-05-26 restructure) |
| f061960 | 2026-05-26 | [dev-only] 砍 codeserver + gitlab service (dev-public 不需要) |
| 1082034 | 2026-05-26 | feat(infra): 帶 deploy-prod.sh + branch-sync-backlog + db cleanup script 進 main |
| 6ff6df1 | 2026-05-26 | [dev-only] backfill branch-sync-backlog + drop-card-auth-schema (從 main 取) |
| cdfa26a | 2026-05-26 | feat(templete): add openai-agents-python + antigravity-sdk-python reference snapshots |
| b19c511 | 2026-05-26 | subtree(anila-agent): pull upstream — add openai-agents + antigravity SDK snapshots in templete/ |
| dd871e8 | 2026-05-26 | sync(main → prod-intranet-card): pull templete zips + infra (deploy-prod.sh/sync-backlog/drop-script) |
| ce44b26 | 2026-05-26 | sync(main → dev-public): pull templete zips + infra (deploy-prod.sh/sync-backlog/drop-script) |
| 49a1c6f | 2026-05-26 | chore(gitignore): templete unzip dirs out of git (對齊 runtime_logic 模式) |
| 8ed579b | 2026-05-26 | sync(main → prod-intranet-card): gitignore templete unzip dirs |
| e83fe8b | 2026-05-26 | sync(main → dev-public): gitignore templete unzip dirs |
| 69048f4 | 2026-05-26 | docs(anila-agent): 加 3 份 SDK deep-dive 分析 + 整合 enhancement roadmap |
| a5e153f | 2026-05-26 | sync(main → prod-intranet-card): 4 份 anila-agent SDK deep-dive 報告 |
| a3329fa | 2026-05-26 | sync(main → dev-public): 4 份 anila-agent SDK deep-dive 報告 |
| f52fd9d | 2026-05-26 | feat(hooks): P0-1 補齊 lifecycle hooks (on_agent_start / on_handoff + AgentHooks per-agent 層) |
| e35929f | 2026-05-26 | feat(tools): P0-2 擴 ToolMetadata (is_read_only / is_destructive / concurrency_safe / cost_estimate) |
| 6204430 | 2026-05-26 | feat(core): P0-3 AnilaToolContext + FileStateCache (workspace + state injection 基底) |
| 214feb1 | 2026-05-26 | merge P0-1: 補齊 lifecycle hooks (on_agent_start / on_handoff + AgentHooks per-agent 層) |
| 16bb472 | 2026-05-26 | merge P0-2: ToolMetadata 擴 5 個欄位 + registry.find_by_metadata + 既有 tool 補 cost_estimate |
| 313b1f0 | 2026-05-26 | merge P0-3: AnilaToolContext + FileStateCache + safe_path (workspace 注入基底) |
| 8e43a86 | 2026-05-26 | sync(main → prod-intranet-card): P0-1 + P0-2 + P0-3 (lifecycle hooks + tool metadata + AnilaToolContext) |
| d3e5d90 | 2026-05-26 | sync(main → dev-public): P0-1 + P0-2 + P0-3 (lifecycle hooks + tool metadata + AnilaToolContext) |
| ed70739 | 2026-05-26 | feat(core): P0-5 Hook 三類強型別分類 (Inspect / Decide / Transform Protocol + HookExecutor) |
| 1be686a | 2026-05-26 | feat(guardrails): P0-6 Guardrails framework (Input/Output + Tool 三 behavior + tripwire) |
| ae617cb | 2026-05-26 | feat(tracing): P0-9 Tracing framework (Trace + Span + TracingProcessor + JSONL/Console impl) |
| 6597a2e | 2026-05-26 | feat(hooks): P0-4 Hook flavor 擴充 (command/prompt/http + decorator factory + hook chain) |
| 2703ba6 | 2026-05-26 | merge P0-4: Hook flavor 擴充 (command/prompt/http + decorator factory + chain) |
| d78593d | 2026-05-26 | merge P0-5: Hook 三類強型別 (Inspect/Decide/Transform Protocol + HookExecutor) |
| f7bf799 | 2026-05-26 | merge P0-6: Guardrails framework (Input/Output + Tool 三 behavior + tripwire) |
| dc35b9e | 2026-05-26 | merge P0-9: Tracing framework (Trace + Span + Processor + JSONL/Console) |
| b0c1267 | 2026-05-26 | sync(main → prod-intranet-card): P0 wave 2 (P0-4 hook flavor + P0-5 taxonomy + P0-6 guardrails + P0-9 tracing) |
| 1f06ce9 | 2026-05-26 | sync(main → dev-public): P0 wave 2 (P0-4 hook flavor + P0-5 taxonomy + P0-6 guardrails + P0-9 tracing) |
| cbe0ae3 | 2026-05-26 | feat(policy): P0-7 Policy DSL 基礎 (allow/deny/disable + priority + workspace_only) |
| caebdb9 | 2026-05-26 | feat(core): P0-8 AgentTool / sub-agent dispatch (sub-routine pattern) |
| cc8b11e | 2026-05-26 | merge P0-7: Policy DSL (allow/deny/disable + priority + workspace_only + YAML loader) |
| 5fc8649 | 2026-05-26 | merge P0-8: AgentTool / sub-agent dispatch (sub-routine pattern) |
| 3b63c88 | 2026-05-26 | sync(main → prod-intranet-card): P0 wave 3 完成 (P0-7 Policy DSL + P0-8 AgentTool) |
| 70601e4 | 2026-05-26 | sync(main → dev-public): P0 wave 3 完成 (P0-7 Policy DSL + P0-8 AgentTool) |
| b826ba6 | 2026-05-26 | feat(runtime): P1-11(第一段)ConnectionStrategy ABC + types + OpenAIAgentsConnection 預設 backend |
| 663ca55 | 2026-05-26 | feat(budget): P1-9 Token budget continuation (BudgetTracker + soft/hard threshold + PTLRetry) |
| 6ee0b1b | 2026-05-26 | feat(permission): P1-16 Permission rule grammar mini DSL (parse Verb(arg) → PolicyRule) |
| 0af61f1 | 2026-05-26 | feat(concurrency): P1-2 runTools partition (read-only 平行 / destructive 串行) |
| e607bb6 | 2026-05-26 | feat(memory): P1-6 CompactingSession + 3 個 Compactor 策略 (Micro/LlmSummary/Snip) |
| 6662fc8 | 2026-05-26 | merge P1-11(ABC): ConnectionStrategy ABC + types + OpenAIAgentsConnection 預設 backend |
| 3e4668c | 2026-05-26 | merge P1-6: CompactingSession + Micro/LlmSummary/Snip 三 compactor |
| d06b2af | 2026-05-26 | merge P1-2: runTools concurrency partition (read-only 平行 / destructive 串行) |
| bea4520 | 2026-05-26 | merge P1-9: Token budget continuation (BudgetTracker + soft/hard + PTLRetry) |
| c98eb13 | 2026-05-26 | merge P1-16: Permission rule grammar mini DSL (parse Verb(arg) → PolicyRule) |
| 40779ee | 2026-05-26 | sync(main → prod-intranet-card): P1 wave 4 (P1-2 concurrency + P1-6 compaction + P1-9 budget + P1-11 ABC + P1-16 permission) |
| 931fd4b | 2026-05-26 | sync(main → dev-public): P1 wave 4 (P1-2 concurrency + P1-6 compaction + P1-9 budget + P1-11 ABC + P1-16 permission) |
| 58395a1 | 2026-05-26 | feat(hook_context): P1-3 HookContext 三層 scope (Session / Turn / Operation) |
| fb8b065 | 2026-05-26 | feat(cost): P1-15 Cost tracker + USD pricing (預設 6 model + on-prem $0 + budget integration) |
| 14b6603 | 2026-05-26 | feat(agent_tool): P1-1 forkSubagent byte-identical prefix (prompt-cache 命中) |
| 779279d | 2026-05-26 | feat(mcp): P1-5 MCP integration (MCPServerManager + stdio/sse/http transport + 動態 tool 註冊) |
| e32981a | 2026-05-26 | feat(streaming): P1-7 三層 StreamEvent + AnilaStreamRunner + CLI renderer |
| fbde086 | 2026-05-26 | merge P1-5: MCP 整合 (MCPServerManager + 3 transport + 動態 tool 註冊) |
| 5f740b8 | 2026-05-26 | merge P1-1: forkSubagent byte-identical prefix (prompt-cache 命中) |
| 6785ccc | 2026-05-26 | merge P1-3: HookContext 三層 scope (Session / Turn / Operation) |
| e71d5c2 | 2026-05-26 | merge P1-7: Streaming (三層 StreamEvent + AnilaStreamRunner + CLI renderer) |
| 5b421de | 2026-05-26 | merge P1-15: Cost tracker + USD pricing (6 預設 model + on-prem + budget integration) |
| 9238020 | 2026-05-26 | sync(main → prod-intranet-card): P1 wave 5 (forkSubagent + HookContext + MCP + Streaming + CostTracker) |
| afe5702 | 2026-05-26 | sync(main → dev-public): P1 wave 5 (forkSubagent + HookContext + MCP + Streaming + CostTracker) |
| f45c3ed | 2026-05-26 | feat(coordinator): P1-13 coordinatorMode XML notification (CoordinatorMessage + parse / format / integration) |
| 3a84234 | 2026-05-26 | feat(prompts): P1-10 systemContext / userContext 兩段 builder (cache-friendly deterministic order) |
| 74204c3 | 2026-05-26 | feat(stop_hook): P1-14 Stop hook prevent-continuation (3 個內建 + RunHooks chain) |
| 6d756ad | 2026-05-26 | feat(triggers): P1-4 Triggers 子系統 (Periodic/FileChange/DbChange + TriggerManager) |
| b880b7b | 2026-05-26 | feat(hooks): P1-17 Hook event 擴充 (SUBAGENT_DISPATCH_* / MCP_* / COMPACTION_* / GUARDRAIL_TRIPWIRE / POLICY_DENY) |
| ce1487b | 2026-05-26 | merge P1-10: systemContext/userContext 兩段 prompt builder (cache-friendly deterministic) |
| 586b6d4 | 2026-05-26 | merge P1-4: Triggers 子系統 (Periodic/FileChange/DbChange + Manager + queue_message) |
| 7523797 | 2026-05-26 | merge P1-13: coordinatorMode XML notification (CoordinatorMessage + parse + nested tag) |
| 031cd9a | 2026-05-26 | merge P1-14: Stop hook prevent-continuation (3 內建 + RunHooks chain + streaming integration) |
| 59aa528 | 2026-05-26 | merge P1-17: Hook event 擴充 (11 新 event + 各 module fire 點) |
| bbc738b | 2026-05-26 | sync(main → prod-intranet-card): P1 wave 6 (Triggers + Coordinator XML + Stop hook + ctx 兩段 + 11 新 hook event) |
| 352f74e | 2026-05-26 | sync(main → dev-public): P1 wave 6 (Triggers + Coordinator XML + Stop hook + ctx 兩段 + 11 新 hook event) |
| 30668b1 | 2026-05-27 | feat(policy): P1-19 disable vs deny 二維工具控管 (filter_tool_descriptions + apply_policy_to_system_context) |
| 2c23d2e | 2026-05-27 | feat(tools): P1-18 ToolSearch + deferred tools (per-session active) |
| 3a97f34 | 2026-05-27 | feat(hitl): P1-8 RunState HITL pause/resume + tool approval + JsonFileStateStore + approval REPL |
| 08b1927 | 2026-05-27 | feat(providers): P2-2 RetryPolicy framework (network/retry_after/rate_limit/combined + with_retry decorator + PTL adapter) |
| fc9f0b9 | 2026-05-27 | feat(mcp): P1-12 McpBridge transport 配置擴充 (timeout + retry + heartbeat + reconnect + error 分類) |
| 6c55937 | 2026-05-27 | merge P2-2: RetryPolicy framework (network/retry_after/rate_limit/combined + with_retry + PTL adapter) |
| 5def539 | 2026-05-27 | merge P1-19: disable vs deny (filter_tool_descriptions + apply_policy_to_system_context) |
| 5ba800e | 2026-05-27 | merge P1-8: RunState HITL pause/resume + JsonFileStateStore + approval REPL |
| 705331a | 2026-05-27 | merge P1-12: McpBridge transport (timeout + retry + heartbeat + reconnect + 錯誤分類) |
| f278404 | 2026-05-27 | merge P1-18: ToolSearch + deferred tools (per-session active) |
| f7310c4 | 2026-05-27 | sync(main → prod-intranet-card): P1 wave 7 完整 (HITL + MCP transport + ToolSearch + disable vs deny + RetryPolicy) |
| 00df7fd | 2026-05-27 | sync(main → dev-public): P1 wave 7 完整 (HITL + MCP transport + ToolSearch + disable vs deny + RetryPolicy) |
| b93fb2b | 2026-05-27 | feat(agent): P2-1 Agent.as_tool shorthand + 對照 P0-8 doc |
| 4ad677b | 2026-05-27 | feat(tools): P2-4 FunctionTool 內掛 tool_guardrails (per-tool input/output chain + 2 example guardrails) |
| 62e1da5 | 2026-05-27 | feat(tools): P2-7 file-index fuzzy file search (FileIndex + fuzzy_search + meta-tool) |
| cb48c25 | 2026-05-27 | feat(memory): P2-6 SessionMemory + autoDream (跨 session 對話 summary + 背景 topic 抽取) |
| 06e8fd8 | 2026-05-27 | feat(tools): P2-12 Task / background task tool (TaskManager + 3 meta-tool + register_task_type registry) |
| a1e53fa | 2026-05-27 | merge P2-6: SessionMemory + autoDream (跨 session summary + topic 抽取 + 背景 dream task) |
| 62613e6 | 2026-05-27 | merge P2-1: Agent.as_tool shorthand + 對照 P0-8 doc |
| 6f0aab6 | 2026-05-27 | merge P2-12: Task / background task tool (TaskManager + 3 meta-tool + register_task_type) |
| 075e261 | 2026-05-27 | merge P2-4: FunctionTool tool-level guardrails (per-tool chain + 2 example) |
| 2dd7175 | 2026-05-27 | merge P2-7: file-index fuzzy file search (FileIndex + fuzzy_search + meta-tool) |
| 8b639ad | 2026-05-27 | sync(main → prod-intranet-card): P2 wave 8 (as_tool/tool guardrails/SessionMemory/file-index/Task) |
| 0238092 | 2026-05-27 | sync(main → dev-public): P2 wave 8 (as_tool/tool guardrails/SessionMemory/file-index/Task) |
| 90cbb6d | 2026-05-27 | feat(extensions): P2-3 prompt_with_handoff_instructions (deterministic + i18n + builder integration) |
| 8a8b2ce | 2026-05-27 | feat(policy): P2-8 policyLimits 組織政策 (LimitsEngine + LimitsStore + 跟 PolicyEngine / CostTracker 整合) |
| 60090ac | 2026-05-27 | feat(tracing): P2-9 queryTracking chainId/depth/query_id (multi-agent dispatch 多層追蹤) |
| 09180c2 | 2026-05-27 | feat(extensions): P2-13 draw_graph visualization (DOT + ASCII tree + cycle detection) |
| 41954f0 | 2026-05-27 | feat(cli): P2-14 run_demo_loop (streaming REPL + P1-7/P1-8/P1-15/P0-9 整合 + demo agent stubs) |
| 71b6957 | 2026-05-27 | merge P2-3: prompt_with_handoff_instructions (deterministic + i18n + builder integration) |
| 4a5cfae | 2026-05-27 | merge P2-8: policyLimits 組織政策 (LimitsEngine + InMemory/JsonFile stores + CostTracker integration) |
| afb0c0e | 2026-05-27 | merge P2-9: queryTracking chainId/depth/query_id (multi-agent dispatch 追蹤) |
| 426faea | 2026-05-27 | merge P2-13: draw_graph visualization (DOT + ASCII tree + cycle detection) |
| b67783c | 2026-05-27 | merge P2-14: run_demo_loop (streaming REPL + P1-7/P1-8/P1-15/P0-9 整合 + demo agents) |
| 4c87360 | 2026-05-27 | sync(main → prod-intranet-card): P2 wave 9 (handoff prompt/policy limits/query tracking/draw graph/demo REPL) |
| f9d7b7d | 2026-05-27 | sync(main → dev-public): P2 wave 9 (handoff prompt/policy limits/query tracking/draw graph/demo REPL) |
| 83aaf18 | 2026-05-27 | feat(p2-15): 9 個 misc patterns — 實作 5 個 (result_storage, compact_boundary, signature_blocks, error_buffer, analytics),4 個 mark backlog |
| f08818a | 2026-06-02 | fix(ui): UIUX + a11y batch 1 — 14 audit findings |
| 6f260aa | 2026-06-02 | fix(security): enforce secret guard path, persist ingestion audit logs, stop error leak |
| 4a095a1 | 2026-06-02 | fix(ui): UIUX batch 2a — upload % + SSE drop recovery |
| 2b436e3 | 2026-06-02 | docs(audit): add full project audit and security remediation reports |
| 65d0550 | 2026-06-02 | fix(ui): UIUX batch 2b — studio cancel button + Dashboard delete confirm modal |
| 96e97fc | 2026-06-02 | feat(agents): let agent owner self-issue a direct csk- credential |
| 96b6a52 | 2026-06-02 | merge(security): P1 資安硬化進 dev-public |
| 3403c5a | 2026-06-02 | fix(anila-ui): Modal focus management + dismissable announced error banner |
| eabded0 | 2026-06-02 | fix(csp-ui): TermModal focus management + remove dead components |
| 5a873da | 2026-06-02 | fix(anila-ui): accessible names for icon-only buttons |
| 6aa8847 | 2026-06-02 | fix(ui): studio cancel — use valid ArtifactState 'failed' + collection.id |
| 2660c6a | 2026-06-02 | refactor(studio): extract JSON helpers to services/llm_json (god-module split 1/n) |
| 6feb3dd | 2026-06-02 | docs(studio): god-module split plan (dependency-ordered extraction sequence) |
| a3059ac | 2026-06-02 | refactor(studio): extract tunables to services/studio_config (god-module split 2/n) |
| 4b43637 | 2026-06-02 | refactor(studio): extract LLM helpers to services/studio_llm (god-module split 3/n) |
| 0a3b071 | 2026-06-02 | refactor(studio): extract retrieval helpers to services/studio_retrieval (god-module split 4/n) |
| 9dd2b36 | 2026-06-02 | refactor(studio): extract render+FLUX pipeline to services/studio_render (god-module split 5/n) |
| 27ca2f8 | 2026-06-02 | refactor(studio): extract vision-QA loop to services/studio_vision_qa (god-module split 6/n) |
| a383fd8 | 2026-06-02 | refactor(studio): extract layout audit/rebalance to services/studio_layout (god-module split 7/n, final) |
| d3192ef | 2026-06-02 | docs(studio): mark god-module split plan complete (steps 2-7 done) |
| 8e0e3e9 | 2026-06-02 | refactor(studio): converge JSON helpers onto canonical llm_json (dedup, #113) |
| c6b5086 | 2026-06-02 | test(ingestion-worker): add unit coverage across modules + fix judge null-content crash (#113) |
| 35993f3 | 2026-06-03 | fix(security): gate Swagger /docs + /openapi.json behind ENABLE_API_DOCS (#117) |
| 23e665d | 2026-06-03 | fix(security): re-validate outbound URLs at call time (SSRF TOCTOU, #117) |
| a772fd6 | 2026-06-03 | docs(security): mark #117 Swagger/SSRF done + audit-commit verified-resolved |
| e51e941 | 2026-06-03 | feat(agent): build_agent retriever escape hatch + built-in CspHttpRetriever (#114) |
| 92b19a6 | 2026-06-03 | fix(agent): strip leaked gemma-native tool-call tokens from final_output (#114) |
| 21f2e94 | 2026-06-03 | feat(agent): CSP-dispatchable service-wrapper example + align CspHttpRetriever to consumer (#114) |
| 2300a54 | 2026-06-03 | chore(agent): Makefile + Dockerfile for the agent template (#114) |
| 88eed62 | 2026-06-03 | feat(security): row-level security on ingestion_images (defense-in-depth, #116) |
| 3b7a51d | 2026-06-03 | docs(security): mark #116 ingestion_images RLS code-complete (deploy = user) |
| af904ff | 2026-06-03 | fix(anilalm): WSSidebar native confirm/alert -> in-app Modal + banner (#112 #7) |
| 013c1ca | 2026-06-03 | feat(anilalm): clickable [N] citations that scroll to the source card (#112 #3) |
| d014b04 | 2026-06-03 | merge: studio god-module split (7-step) + LLM helper dedup (#113) into dev-public |
| c09f1c1 | 2026-06-03 | merge: ingestion-worker unit coverage + judge null-content fix (#113) into dev-public |
| 608cf03 | 2026-06-03 | merge: anila-agent escape hatch/CspHttpRetriever/runner sanitizer/service wrapper (#114) into dev-public |
| 049aab2 | 2026-06-03 | merge: anilalm UIUX — WSSidebar Modal/banner + clickable [N] citations (#112) into dev-public |
| 8a440dc | 2026-06-03 | merge: Swagger gate + SSRF call-time guard + audit re-review (#117) into dev-public |
| 828418e | 2026-06-03 | merge: ingestion_images RLS (migration 0037 + 3-path GUC + SECURITY DEFINER resolver) (#116) into dev-public |
| 095b80a | 2026-06-03 | feat(anila-ui): replace native confirm/alert with in-app Modal + toast (a11y) |
| e2659d4 | 2026-06-03 | feat(csp-frontend): replace native confirm/alert/prompt with in-app Modal + toast (a11y) |
| 857b289 | 2026-06-03 | merge: native confirm/alert/prompt -> in-app Modal + toast across anila-ui + csp frontend (a11y) |
| fab184d | 2026-06-03 | merge: agent owner self-issues a direct csk- credential (owner-or-admin) |
| c4b5782 | 2026-06-03 | fix(csp-frontend): expose owner self-issue of csk- in agent detail UI |
| 2deea50 | 2026-06-03 | fix(dev): relax S-117 URL guard so dev's http/internal agents pass |
| 6e1ef8a | 2026-06-03 | feat(csp): agent bound_collection + search accepts agent csk- scoped (Q1 P1) |
| b2d69bd | 2026-06-03 | feat(agent): one-key model (csk- for search) + fail-closed token default (Q1+Q3 P2) |
| 34841c8 | 2026-06-03 | feat(csp): POST /agents/{id}/test-connection probe (Q3 P3) |
| c1523ba | 2026-06-03 | feat(csp-frontend): register wizard — issue csk-, .env snippet, test connection (Q2+Q3 P4) |
| 3a6182b | 2026-06-03 | fix(csp-frontend): .env snippet CSP_BASE_URL — no misleading localhost |
| 8d075b0 | 2026-06-08 | docs(dev-guide): align onboarding to one-key csk- + register wizard + test-connection |
| 062a6c5 | 2026-06-08 | docs(agent-credentials): align protocol + runbooks to one-key/default-csk- |
| 580c2e3 | 2026-06-08 | docs(readme): agent-template onboarding + env to current state; csp env note |
| e4746b6 | 2026-06-08 | docs(platform): record agent csk- search-scope access decision (§7.5.5) |
| 085ceb3 | 2026-06-08 | fix(auth): studio token self-heal + logout CSRF + revocation-cache keepalive |
| f208dbb | 2026-06-08 | fix(studio): revocation subscriber uses get_message(timeout) not listen() |
| 2d70070 | 2026-06-08 | merge: dev-public hardening + 2-session work into main (exclude codeserver/gitlab cut) |
| 13fefb8 | 2026-06-08 | sync(main → prod-intranet-card): 2-session work, preserve card/SSO auth |
| 13c8a75 | 2026-05-27 | feat(memory): P2-5 ConversationIndex (turn / compaction 追蹤 + SessionContext + CompactingSession 整合) |
| 8393fde | 2026-05-27 | feat(cli): P2-10 Slash command framework + 10 內建 command (help/cost/budget/trace/tools/find/task/memory/graph/clear) |
| 2e8914c | 2026-05-27 | feat(skills): P2-11 Skill loader frontmatter (Skill + SkillLoader + SkillRegistry + 3 example skills) |
| 14c76ab | 2026-06-08 | sync(main → prod-intranet-card): P2-5 ConversationIndex + P2-10 Slash commands + P2-11 Skill loader |
| fccf430 | 2026-06-09 | feat(ingestion): cross-document relations (rule / LLM / similarity) + graph |
| 2a1c07b | 2026-06-09 | Merge branch 'main' into prod-intranet-card |
| 838eeb9 | 2026-06-09 | fix(ingestion): tighten citation extractor — drop numeral self-refs, generic & clause fragments |
| bfa8a50 | 2026-06-10 | Merge branch 'main' into prod-intranet-card |
| 86ea21c | 2026-06-12 | feat: Open WebUI gap features + extensible agent-function framework |
| d63f706 | 2026-06-12 | feat(intranet): deployment prep + card-only owner break-glass |
| 966d521 | 2026-06-12 | chore(config): docker-compose + nginx for intranet-card |
| af13c97 | 2026-06-12 | chore(certs): gitignore rotated .revoked-* TLS material |
| ed13e5c | 2026-06-12 | fix(security): real PKCS#7/CMS verification for card login — close auth-bypass CRITICAL |
| 8db9c60 | 2026-06-12 | fix(security): harden new endpoints from vuln audit (SSRF, authz, info-leak, input bounds) |
| c6491f3 | 2026-06-12 | fix(migrations): 0040 guard auth_providers/external_identities existence (fix no-SSO branch startup) |
| c67145e | 2026-06-12 | chore(release): bump platform version to 1.0.0 |
| 7c8b4c0 | 2026-06-12 | docs(runbook): V1.0.0 deployment notes — card real-verification, CA bundle, 0040 fix, re-package |
| 5697898 | 2026-06-13 | docs(readme): branch-tailored README for prod-intranet-card |
| 2156998 | 2026-06-13 | docs(readme): full rewrite of subproject READMEs (zh + en) |
| 59369f9 | 2026-06-13 | docs(readme): correct subproject READMEs against current source |
| 761ccdc | 2026-06-13 | chore(license): relicense project under GNU GPL v3.0 |
| c4f4e8f | 2026-06-14 | feat(deploy): one-shot interactive intranet deploy script (scripts/intranet-deploy.sh) |
| 163a4b6 | 2026-06-14 | feat(codeserver): mount ANILA repo source for in-intranet platform maintenance (secrets masked) |
| 376007a | 2026-06-14 | feat(deploy): support intranet-defaults.env for default secrets (out of git) + gitlab root password env |
| 4332c14 | 2026-06-14 | fix(deploy): harden intranet-defaults.env handling (3 security-review findings) |
| 22c9936 | 2026-06-14 | fix(deploy): extract full TLS chain (leaf + intermediates) from server.pfx, not leaf-only |
| 7e43bb2 | 2026-06-14 | feat(anila-agent): rebuild as lean 1.0.0 air-gapped Agentic RAG template |
| cd34e0b | 2026-06-15 | fix(intranet-deploy): provision JWT signing keypair + close live-rehearsal deploy gaps |
| 4cda33e | 2026-06-15 | feat(csp): developer guide MLSteam flow + LLM system-prompt generator |
| 7fe38e7 | 2026-06-15 | fix(prod-deploy): provision JWT keypair in deploy-prod.sh path too |
| 9933e2f | 2026-06-15 | harden(prod-deploy): tighten JWT key file perms (security review) |
| 1c1a18a | 2026-06-17 | docs(readme): audit sub-project READMEs + screenshot gallery (card branch) |
| 6eadd83 | 2026-06-22 | feat(intranet-deploy): default model-ca to CSPKI bundle + guard empty CA file |
| 1f5dd99 | 2026-06-22 | fix(stream): robust SSE content extraction for complete-message agents |
| 6ad1f4f | 2026-06-22 | style(csp-frontend): tokenize secret-banner code chips |
| c2af31e | 2026-06-22 | docs: add project-level AGENTS.md (Codex) + CLAUDE.md (Claude) |
| f3d3550 | 2026-06-22 | fix(ingestion/usage): 6 intranet-reported issues + review hardening |

---

## 本時代結束時（v1.1.0）的系統樣貌

- **多服務平台**：CSP 控制／資料平面（FastAPI + Postgres/pgvector + Redis + Vue admin）、`anila-core-router`（OpenAI 相容）、`ingestion-worker`、`anila-studio`、`pptx-renderer`、runtime chat UI、ANILALM（KB + Studio SPA），以及獨立的 `anila-models` stack（LLM／embedding／FLUX）。對外入口統一走 nginx。
- **認證／授權**：JWT RS256 + JWKS + Redis 跨服務撤銷；`prod-intranet-card` 具真實 PKCS#7/CMS 卡登驗章。專案改採 **GPL v3.0**，repo 為 PUBLIC。
- **分支**：**七分支 SSOT 模型**（`main` + 6 條 downstream），跨分支同步規則見 `AGENTS.md` §3。
- **生成能力**：Studio 產出 slides／reports／mindmaps／infographics／datatables；FLUX 語意對齊影像；跨文件關聯 RAG（rule／LLM／similarity）+ 圖。
- **部署**：內網 air-gapped，`intranet-deploy.sh`（一次性 bootstrap）／`deploy-prod.sh`（日常 lifecycle），CSPKI TLS 鏈。
- **版本**：**v1.0.0**（2026-06-14 tag）→ **v1.1.0**（2026-06-22 tag，6 項內網修正）。下一步 v1.2.0（員編 downstream + 移除跨租戶記憶 D8）屬後續 Era，不在本篇範圍。
