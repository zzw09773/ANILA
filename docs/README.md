# ANILA `docs/` 文件索引

> ANILA 平台技術 / 治理文件總入口。實作 source 在 repo 各子專案;本目錄是「為什麼這樣做」的記錄處。

---

## 目錄結構

```
docs/
├── README.md                       <-- 本檔(索引)
├── developer-guide.md              <-- 新進開發者入門
│
├── governance/                     <-- ISO 42001 合規文件 ⭐
│   ├── iso-42001-compliance.md     <-- 合規主索引
│   ├── ai-policy.md
│   ├── roles-responsibilities.md
│   ├── risk-register.md
│   ├── aiia-template.md
│   ├── data-governance.md
│   ├── model-card-template.md
│   ├── ai-incident-response.md
│   └── third-party-ai-register.md
│
├── architecture/                   <-- 當前架構設計
│   ├── anila-core-runtime-design.md
│   ├── anila-core-boundary.md
│   ├── anila-agent-framework-architecture.md
│   ├── anila-agent-framework-porting-decisions.md
│   ├── csp-agent-bootstrap-protocol.md
│   ├── ingestion-platform-design.md
│   ├── parent-child-rag-design.md
│   └── runtime-logic-openai-agents-deep-dive.md
│
├── planning/                       <-- 進行中的計畫 / roadmap
│   ├── sprint-7x-plan.md
│   ├── sso-migration.md
│   ├── multi-service-integration-plan.md
│   ├── agenticrag-phase1-plan.md
│   └── agenticrag-enhancement-plan.md
│
├── runbooks/                       <-- 操作手冊
│   ├── intranet-deployment-runbook.md
│   ├── rotate-tls-cert.md
│   ├── service-token-cutover.md
│   └── legacy-agent-bootstrap.md
│
├── briefing/                       <-- 對外簡報 / RFC
│   ├── anila-briefing.pptx
│   └── anila-memory-layer-rfc.md
│
├── changelog/                      <-- 重大變更紀錄
│   └── 2026-04-27-onyx-handover.md
│
└── archive/                        <-- 已停用 / 已交接的文件
    ├── onyx-application-plan.md
    ├── onyx-target-system-api-spec.md
    └── agenticrag-decouple-from-anila-core.md
```

---

## 我要找什麼?

| 我想知道… | 看這份 |
|---|---|
| **ISO 42001 合規現況** | [`governance/iso-42001-compliance.md`](./governance/iso-42001-compliance.md) |
| **平台 AI 政策** | [`governance/ai-policy.md`](./governance/ai-policy.md) |
| **新 agent 上線前要做什麼** | [`governance/aiia-template.md`](./governance/aiia-template.md) + [`governance/README.md`](./governance/README.md) |
| **新模型註冊要做什麼** | [`governance/model-card-template.md`](./governance/model-card-template.md) |
| **AI 風險登錄與進度** | [`governance/risk-register.md`](./governance/risk-register.md) |
| **AI 事件怎麼處理** | [`governance/ai-incident-response.md`](./governance/ai-incident-response.md) |
| **`anila-core` 怎麼運作** | [`architecture/anila-core-runtime-design.md`](./architecture/anila-core-runtime-design.md) |
| **`anila-agent` template 怎麼設計** | [`architecture/anila-agent-framework-architecture.md`](./architecture/anila-agent-framework-architecture.md) |
| **CSP ↔ Agent bootstrap 協定** | [`architecture/csp-agent-bootstrap-protocol.md`](./architecture/csp-agent-bootstrap-protocol.md) |
| **Ingestion pipeline 設計** | [`architecture/ingestion-platform-design.md`](./architecture/ingestion-platform-design.md) |
| **目前 sprint 在做什麼** | [`planning/sprint-7x-plan.md`](./planning/sprint-7x-plan.md) |
| **SSO 切換進度** | [`planning/sso-migration.md`](./planning/sso-migration.md) |
| **GitLab / n8n / ANILA LM 整合** | [`planning/multi-service-integration-plan.md`](./planning/multi-service-integration-plan.md) |
| **怎麼在內網部署** | [`runbooks/intranet-deployment-runbook.md`](./runbooks/intranet-deployment-runbook.md) |
| **TLS 私鑰怎麼輪換** | [`runbooks/rotate-tls-cert.md`](./runbooks/rotate-tls-cert.md) |
| **Service token 怎麼 cutover** | [`runbooks/service-token-cutover.md`](./runbooks/service-token-cutover.md) |
| **Memory 層怎麼設計** | [`briefing/anila-memory-layer-rfc.md`](./briefing/anila-memory-layer-rfc.md) |
| **新進開發者要看什麼** | [`developer-guide.md`](./developer-guide.md) |

---

## 文件慣例

- 每份文件頂端標 **狀態** + **last updated**
- 同主題的 sibling docs 用 `**Companion docs**:` 列在頂端
- 跨 subdir 連結用 `../<subdir>/<file>.md` 相對路徑
- 重大變更走 PR review,過時文件搬到 `archive/`(不直接刪,保留 git history 仍可查)

---

**Last updated**: 2026-05-16 · **Maintainers**: ANILA 平台團隊
