# Changelog

本檔記錄 ANILA 所有重要的**已完成/已出貨**變更。前瞻待辦見 [`docs/ROADMAP.md`](docs/ROADMAP.md)。

格式依 [Keep a Changelog](https://keepachangelog.com/)；版本對應 air-gap 交付 tag（`v1.0.0–`，於 `prod-intranet-card`）。日期取自 git tag / commit。

---

## [Unreleased]

> 已合入分支、尚未打 tag 的變更。

### Added
- Router 回覆個人化：依使用者記憶重組回覆（直答 inline rule + dispatched `_recompose_reply`，含 fail-safe 與 classified-skip）(`828a553`, 2026-06-23)
- `csk-` inbound-guard onboarding 片段 + 「CSP 派送中」dispatch badge（給無法 import anila_core 的 agent 用）(`f9dc0d3`, 2026-06-24)
- 同源 nginx `/anila` 入口（:443，trailing-slash prefix）(`673f0bd` / `7740f60`)

### Fixed
- 記憶事實抽取在 `MEMORY_LLM_MODEL` 未註冊時 fallback 到第一個可用 registry LLM（air-gap 韌性）(`f3ce4d9`, 2026-06-24)
- SSE 串流對 complete-message agents 的 robust content extraction (`ca5c040`)
- credential dispatch 取最近 issued-or-rotated 的有效憑證 (`0c6639c`)

---

## [1.2.0] - 2026-06-23

### Added
- **員編 downstream forwarding**：`X-ANILA-User-Id` 由 PK 改為員編；兩個 builder 目的地驅動（model 不拿 service token）(`80f60d4`)（card-only）

### Security
- 移除 cross-tenant memory pull（D8）

---

## [1.1.0] - 2026-06-22

### Fixed
- 6 件內網回報的 ingestion / usage 問題 + review hardening (`f3d3550`)（刪 collection 500、編碼、parser、重嵌、多檔…）

### Changed
- air-gap **load-only patch** 流程：外網預建 image tar + `load-patch`，`.15` 純 air-gap 不在機上 build

---

## [1.0.0] - 2026-06-14 — 平台基線

首個 air-gap 內網交付基線，涵蓋此前建置的核心能力。

### Added — 平台 / Router
- **v7 LLM-as-Router + agent registry + dispatch**：OpenAI-compatible `/v1`、proxy agent-vs-model resolve、SSE + usage 結算 + identity header 注入、per-agent `csk-` 憑證、admin 審核、developer console
- 兩步 register 精靈 + test-connection 探針 + fail-closed inbound-guard + **LLM system-prompt 產生器** (`32e6bba`, 2026-06-15)
- anila-core SDK：router app-factory、`dispatch_to_agent`、`RemoteAgentRegistry`、`CSPPlatformProvider`、CLI

### Added — RAG / 知識
- **跨文件關聯**（rule / LLM / similarity 抽取 → `document_relations` 邊表 → 1-hop 擴展 → cytoscape 關聯圖）(`fccf430`, 2026-06-09)
- **parent-child 階層 chunking**；**per-user 記憶層**（結構化 facts + 跨對話 halfvec recall，注入 chat）

### Added — 端使用者
- **ANILA_UI** runtime chat（Router-first、cookie-only、agent dispatch、handoff、tool trace、SSE）
- **Open WebUI 對等功能** + extensible agent-function framework：stop-generation、continue、IME-safe Enter、全文搜尋、公告 banner、response branching、structured feedback、per-message usage、mermaid、時間分組、drag-drop、shared links、server-synced settings (`0ba4e03`, 2026-06-12)
- **anila-studio** artifacts：slides / reports / mindmaps / infographics / datatables（pptx via LibreOffice）

### Added — anila-agent
- 重建為 **lean 1.0.0 air-gapped Agentic RAG template**：openai-agents 0.17.5、retrieval-first cited agent、hybrid recall、`/deep-research` pipeline、native MCP client、HITL RunState、tool policy DSL、offline wheelhouse、179 測試綠 (`1a3f8ec`, 2026-06-14)

### Security
- **卡登 PKCS#7/CMS 真實驗證**：CMS 簽（signedAttrs + messageDigest）+ 釘選 CSPKI Root 鏈 + nonce 防重放 — **關閉先前 auth-bypass CRITICAL**；16/16 測試綠（card 分支）(`ed13e5c`, 2026-06-12)
- 弱點稽核 hardening：SSRF guard parity（memory/proxy 出向驗 URL）、authz（404 消枚舉）、info-leak、input bounds、auto_seed 隨機密碼 (`c8c3344`, 2026-06-12)
- JWT **RS256-only** + 明確 algorithm allowlist + fail-closed keypair provisioning (`cd34e0b`, 2026-06-15)
- secure-default compose（card：`${VAR:?required}` + `ANILA_ALLOW_DEV_SECRET:-0`）；`startup_security` dev-default gate
- RLS：runtime 用 `csp_app` 非超級用戶 + FORCE ROW LEVEL SECURITY + `SET LOCAL` scoping

### Deploy
- air-gap 內網 bundle（source + 預建 image + 一鍵 deploy）+ CSPKI TLS（`cspki_ca_bundle.pem`）+ `intranet-deploy.sh` / `deploy-prod.sh` + 三份 runbook

---

## [0.x] - 2026-04-27 — Onyx handover（pre-baseline）

- 子專案（onyx / myCSPPlatform / AgenticRAG）合併進單一 repo 的交接。詳見 `docs/changelog/2026-04-27-onyx-handover.md`。

---

*日期為 git tag / commit author date（UTC）。`prod-intranet-card` 帶 `v1.0.0`–`v1.2.0` tag；`prod-public-passwd` 等分支落後未 tag（見 ROADMAP「分支同步原則」）。*
