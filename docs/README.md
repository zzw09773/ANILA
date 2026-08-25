# ANILA `docs/` 文件索引

> ⚠ 2026-08-17 盤點：**本索引已過期,內容停在 2026-07-02**——其後新增的文件(HANDOFF 系列、`runbooks/`、`designs/`、`specs/`、`ui-sweep/` 等)未列入,且下方「prod 分支／main 分支」的說法源自**已不存在的多分支模型**。現行狀態與執行順序見 `PLAN.md`;目錄結構請以實際檔案樹為準。

> 📌 **先讀 [`WHERE-THINGS-LIVE.md`](./WHERE-THINGS-LIVE.md)**（2026-08-25）——四層地圖：團隊協作／敏感稽核／專案文件／跨專案制度，**含每一層的收容條件與常見的放錯位置**。
>
> ANILA 平台技術 / 治理文件總入口。實作 source 在 repo 各子專案,本目錄是「為什麼這樣做」的記錄處。
>
> ⚠️ **這是 prod 分支(中科院內網部署版)的 docs**,比 main 分支多 `governance/` / `runbooks/` / `branch-sync-backlog.md` 等 prod-only 文件。同步策略見 [`branch-sync-backlog.md`](./branch-sync-backlog.md)。

---

## 目錄結構

```
docs/
├── README.md                   <-- 本檔(索引)
├── branch-sync-backlog.md      <-- main ↔ prod 同步策略 + 永久 fork 區清單 (已失效,見警語)
│
├── handoffs/                   <-- session 移交紀錄 (HANDOFF 系列,12 份)
│
├── archive/                    <-- 歷史文件,按原目錄名分群
│   ├── agent-framework/        <-- anila-agent / runtime 架構 (8 份)
│   ├── agenticrag/             <-- AgenticRAG 解耦 / 增強計畫 (3 份)
│   ├── anila-core/             <-- anila-core 邊界 / runtime 設計 (2 份)
│   ├── briefing/               <-- 對外簡報 / RFC (2 份)
│   ├── changelog/              <-- 重大變更紀錄 (1 份)
│   ├── history/                <-- 平台沿革 (4 份)
│   └── planning/               <-- 早期 sprint 計畫 (2 份)
│
├── governance/                 <-- ISO 42001 合規文件 ⭐ (prod-only,10 份)
│   ├── README.md
│   ├── iso-42001-compliance.md <-- 合規主索引
│   ├── ai-policy.md
│   ├── roles-responsibilities.md
│   ├── risk-register.md
│   ├── aiia-template.md
│   ├── data-governance.md
│   ├── model-card-template.md
│   ├── ai-incident-response.md
│   └── third-party-ai-register.md
│
├── runbooks/                   <-- 操作手冊
│   ├── intranet-deployment-runbook.md   <-- 中科院內網部署手冊
│   ├── rotate-tls-cert.md
│   ├── service-token-cutover.md
│   └── legacy-agent-bootstrap.md
│
├── ingestion/                  <-- ingestion 平台設計 / parent-child RAG (2 份)
├── platform/                   <-- 多服務整合 / SSO migration (2 份)
├── guides/                     <-- developer guide (1 份)
└── specs/                      <-- studio-flux / studio-wizard / anila-studio 子計畫（原名 superpowers/，2026-08-17 更名）
    ├── studio-flux/            <-- FLUX 圖像生成 Stage 1-4 spec / plans / history
    ├── studio-wizard/          <-- studio 嚮導模式設計
    ├── anila-studio/           <-- anila-studio 抽取決策
    ├── specs/                  <-- 詳細 spec
    └── plans/                  <-- 階段計畫
```

## 我要找什麼?

### 部署 / 維運

| 我想知道… | 看這份 |
|---|---|
| **內網部署怎麼跑** | [`runbooks/intranet-deployment-runbook.md`](./runbooks/intranet-deployment-runbook.md) + `infra/deployment/scripts/deploy-prod.sh` |
| **main → prod 同步策略 / 永久 fork 區清單** | [`branch-sync-backlog.md`](./branch-sync-backlog.md) ⭐ |
| **TLS 私鑰怎麼輪換** | [`runbooks/rotate-tls-cert.md`](./runbooks/rotate-tls-cert.md) |
| **Service token 怎麼 cutover** | [`runbooks/service-token-cutover.md`](./runbooks/service-token-cutover.md) |
| **legacy agent bootstrap 流程** | [`runbooks/legacy-agent-bootstrap.md`](./runbooks/legacy-agent-bootstrap.md) |

### AI 治理(prod-only,中科院內網部署必看)

| 我想知道… | 看這份 |
|---|---|
| **ISO 42001 合規現況** | [`governance/iso-42001-compliance.md`](./governance/iso-42001-compliance.md) ⭐ |
| **平台 AI 政策** | [`governance/ai-policy.md`](./governance/ai-policy.md) |
| **角色責任 RACI** | [`governance/roles-responsibilities.md`](./governance/roles-responsibilities.md) |
| **AI 風險登錄** | [`governance/risk-register.md`](./governance/risk-register.md) |
| **新 agent 上線前 AIIA 範本** | [`governance/aiia-template.md`](./governance/aiia-template.md) |
| **新模型 model card 範本** | [`governance/model-card-template.md`](./governance/model-card-template.md) |
| **資料治理** | [`governance/data-governance.md`](./governance/data-governance.md) |
| **AI 事件怎麼分級處理** | [`governance/ai-incident-response.md`](./governance/ai-incident-response.md) |
| **第三方 AI 供應商登錄** | [`governance/third-party-ai-register.md`](./governance/third-party-ai-register.md) |

### 架構 / 設計

| 我想知道… | 看這份 |
|---|---|
| **`anila-core` 怎麼運作** | [`anila-core/anila-core-runtime-design.md`](./archive/anila-core/anila-core-runtime-design.md) |
| **`anila-core` 與 csp 的邊界** | [`anila-core/anila-core-boundary.md`](./archive/anila-core/anila-core-boundary.md) |
| **`anila-agent` template 怎麼設計** | [`agent-framework/anila-agent-framework-architecture.md`](./archive/agent-framework/anila-agent-framework-architecture.md) |
| **agent runtime 移植決策** | [`agent-framework/anila-agent-framework-porting-decisions.md`](./archive/agent-framework/anila-agent-framework-porting-decisions.md) |
| **openai-agents runtime 深入** | [`agent-framework/runtime-logic-openai-agents-deep-dive.md`](./archive/agent-framework/runtime-logic-openai-agents-deep-dive.md) |
| **CSP ↔ Agent bootstrap 協定** | [`agent-framework/csp-agent-bootstrap-protocol.md`](./archive/agent-framework/csp-agent-bootstrap-protocol.md) |
| **Ingestion pipeline 設計** | [`ingestion/ingestion-platform-design.md`](./ingestion/ingestion-platform-design.md) |
| **Parent-child RAG 設計** | [`ingestion/parent-child-rag-design.md`](./ingestion/parent-child-rag-design.md) |

### 計畫 / 規劃

| 我想知道… | 看這份 |
|---|---|
| **目前 sprint 在做什麼** | [`planning/sprint-7x-plan.md`](./archive/planning/sprint-7x-plan.md) |
| **SSO 切換進度** | [`platform/sso-migration.md`](./platform/sso-migration.md) |
| **GitLab / n8n / ANILA LM 整合** | [`platform/multi-service-integration-plan.md`](./platform/multi-service-integration-plan.md) |
| **AgenticRAG 解耦 / Phase 1 計畫** | [`agenticrag/agenticrag-decouple-from-anila-core.md`](./archive/agenticrag/agenticrag-decouple-from-anila-core.md) + [`agenticrag/agenticrag-phase1-plan.md`](./archive/agenticrag/agenticrag-phase1-plan.md) |
| **AgenticRAG 後續增強** | [`agenticrag/agenticrag-enhancement-plan.md`](./archive/agenticrag/agenticrag-enhancement-plan.md) |

### Studio / FLUX

| 我想知道… | 看這份 |
|---|---|
| **anila-studio 為何要從 csp 抽出** | [`specs/anila-studio/`](./specs/anila-studio/) |
| **FLUX 圖像生成 4 階段(rewriter / quality gate / deck style / use case routing)** | [`specs/studio-flux/`](./specs/studio-flux/) |
| **Studio 嚮導模式(theme override)** | [`specs/studio-wizard/`](./specs/studio-wizard/) |

### 其他

| 我想知道… | 看這份 |
|---|---|
| **Memory 層怎麼設計(route 3)** | [`briefing/anila-memory-layer-rfc.md`](./archive/briefing/anila-memory-layer-rfc.md) |
| **新進開發者要看什麼** | [`guides/`](./guides/) 或根 `docs/developer-guide.md` |
| **Onyx 為什麼從 monorepo 移出** | [`changelog/2026-04-27-onyx-handover.md`](./archive/changelog/2026-04-27-onyx-handover.md) |

---

## prod 分支文件慣例

- 每份文件頂端標 **狀態**(Active / Stable / Deprecated)+ **last updated**
- 同主題的 sibling docs 用 `**Companion docs**:` 列在頂端
- 跨 subdir 連結用 `../<subdir>/<file>.md` 相對路徑
- 重大變更走 PR review;過時文件不直接刪,改放 `specs/<topic>/history/` 或加 `[ARCHIVED]` 前綴
- **prod-only 文件**(governance / runbooks / branch-sync-backlog)不要 cherry-pick 回 main
- 文件結構同步策略:main 重組 docs/ 時(例:把根目錄 `.md` 收進 `<topic>/` 子目錄),prod 跟著對齊,以免兩邊路徑漂移後 cross-link 全壞

---

**Last updated**: 2026-05-26(main → prod sync 後重整目錄索引,加入 fork 區策略指標)· **Maintainers**: ANILA 平台團隊
