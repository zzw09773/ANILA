# ANILA Governance Center — CSP Governance Console (`csp-platform`)

> The governance console for CSP (Control / Security Plane) (Vue 3 + Vite, v1.0.0). This is ANILA's **Admin / Developer / Service Admin control plane**, managing identity, models, the Agent Registry, the Service Registry, knowledge governance, classification & the one-way latch, and Trace / Audit / Usage. **It is not a regular user's day-to-day entry** (product constitution doc 00 §2).

> 繁體中文原文: [`README.md`](./README.md)

> 🌿 **Branch note**: The governance center exists on every deployment branch (login method varies by branch; `prod-intranet-card` uses the PKI ID card). See the root [`README.md`](../../README.md) and [`docs/branch-sync-backlog.md`](../../docs/branch-sync-backlog.md).
>
> Design authority: [`docs/anila-redesign-docs/00-product-constitution.md`](../../docs/anila-redesign-docs/00-product-constitution.md), [`03-csp-governance-control-plane.md`](../../docs/anila-redesign-docs/03-csp-governance-control-plane.md), [`04`](../../docs/anila-redesign-docs/04-model-gateway-design.md) models, [`05`](../../docs/anila-redesign-docs/05-agent-registry-and-runtime-protocol.md) agents, [`07`](../../docs/anila-redesign-docs/07-registered-gui-service-platform.md) services, [`08`](../../docs/anila-redesign-docs/08-classified-latch-and-policy-engine.md) classification, [`12`](../../docs/anila-redesign-docs/12-frontend-visual-redesign.md) visual redesign.

---

## 1. Product role

Per the product constitution (doc 00 §2), the governance center is an **admin-facing control plane** hosting these governance domains:

```text
Governance Center (CSP)
├── Identity / departments / roles
├── Model governance (Model Gateway)
├── Agent Registry (approval)
├── Service Registry (GUI service registration + launch)
├── Knowledge governance (collections / chunking / evaluator)
├── Classification & one-way latch
└── Trace / Audit / Usage
```

Regular users work from ANILA Shell's four entries and **do not enter** the governance center; access here is role-tiered (owner / admin / developer).

---

## 2. Institutional-blue visual redesign (doc 12)

The console moved from a "terminal / hacker" aesthetic (carbon-black base + mint terminal-green + all-monospace) to a neutral, official, trustworthy **institutional-blue console**:

- **Light-first**: `src/assets/styles/tokens.css` was rewritten (all variable names kept, so styles cascade automatically); `:root` = light (official blue), `[data-theme="dark"]` retained but retuned to soft blue-grey.
- **Theme resolution**: `src/composables/useTheme.js` defaults to light (`localStorage` key `anila.theme`; only flips to dark when there's no stored preference and the OS is dark); `index.html` applies `data-theme` in an inline pre-mount script to avoid a dark→light first-paint flash.
- **Typography**: system font stacks (`--font-sans` for the UI, `--font-mono` only for IDs, tokens, timestamps, numerics) — air-gap safe, no external webfonts.
- **Language**: full Traditional Chinese, Taiwan usage (doc 11 language policy); `index.html` `lang="zh-TW"`.

> The `Term*` components under `src/components/cli/` (Badge / Field / Modal / Stat …) are a shared UI kit; the **names are historical** — the visual is now institutional blue, not a terminal.

### Login (card-first)

`src/views/LoginView.vue` leads with the **PKI ID card as the primary hero card**: detect card → enter PIN → card signs (`handleDetectCard` / `handleCardLogin`). Local username/password and OIDC SSO are collapsed under a secondary "other sign-in methods" section with reduced visual weight.

---

## 3. Views inventory (`src/router/index.js` is authoritative)

Routes are two-tier: `/login` (public) and `/` (`AppLayout`, `requiresAuth`) with children. The `beforeEach` guard tiers by `stores/auth.js` roles (`isAdmin` = admin∪owner; `isOwner`; `isDeveloper` = developer∪admin∪owner).

| Domain | View (route) | Highlights |
|---|---|---|
| Dashboard | `DashboardView` (`/`) | platform overview (`dashboard/PlatformCard.vue`) |
| Model governance | `ModelsView` (`models`) | **five-state health** (`utils/healthStatus.js`: unknown / healthy / degraded / unhealthy / disabled, normalizing legacy online/connecting/offline) + **per-model keys** (`has_api_key`: model key set / uses global key; `api_key` write-only). doc 04 |
| Agent Registry | `DeveloperAgentsView` (`developer/agents`, developer) + `DeveloperGuideView`, `AgentRuntimeConfigView` | **seven-state approval** (`utils/approvalStatus.js`: draft / pending_connection_test / pending_trace_test / pending_security_review / approved / rejected / disabled) + **trace-test gate** (`isApprovable` requires `trace_test_passed_at`; not passed → not approvable, backend returns 409) + test-connection probe. doc 05 |
| Service Registry | `PlatformLinksView` (`platform-links`), `ServiceAccessView`, `ServiceClientsView` | registered GUI services (`utils/serviceRegistry.js`: `launch_mode` new_tab/iframe, `config_source` env_seeded/db field locking, `classification_ceiling` five-level); service-token management. doc 07 |
| Classification | `ClassificationInventoryView` (`classification-inventory`, admin) | pre-cutover classification inventory (doc 08 §15) |
| Knowledge governance | `KnowledgeCollectionsView`, `ChunkingPreviewView`, `CollectionDetailView`, `EvaluatorView` (developer) | collection inspector, chunking-strategy comparison wizard, evaluator; relation graph via `components/RelationGraph.vue` (cytoscape) |
| Identity / departments | `UsersView`, `DepartmentsView` (admin) | users, departments, roles |
| Audit / usage | `AuditLogsView`, `UsageView` | audit; usage charted with echarts (`charts/UsageLineChart.vue`, `TimeRangeSelector.vue`) |
| Platform misc | `ApiKeysView`, `AlertsView`, `BannersView` (admin), `TrustedHostsView` (admin, SSRF allow-list) | keys, alerts, banners, SSRF trusted-host list |

---

## 4. Tech stack (from `package.json`)

| Area | Detail |
|---|---|
| Framework / router / state | **Vue 3.5.13** · `vue-router` 4.5.0 · `pinia` 2.3.0 |
| Charts | `echarts` 5.5.1 + `vue-echarts` 7.0.3; relation graph `cytoscape` 3.34.0 |
| HTTP | `axios` 1.7.9 |
| Styling | `tailwindcss` 3.4.17 + `postcss` + `autoprefixer` (build-time); design tokens via `src/assets/styles/tokens.css` |
| Build | **Vite 6.0.5** (`@vitejs/plugin-vue` 5.2.1) |

`scripts`: `dev` / `build` / `preview`. **No `test` script — `npm run build` is the verification gate.** The utils (`healthStatus` / `approvalStatus` / `serviceRegistry`) are deliberately pure functions so they can adopt vitest later with zero changes.

---

## 5. Layout

```
apps/csp-governance-ui/
├── index.html (lang=zh-TW, data-theme=light, pre-mount theme script) · vite.config.js · tailwind.config.js · postcss.config.js
└── src/
    ├── main.js (createApp + pinia + router + main.css) · App.vue · router/index.js
    ├── views/          # see §3 inventory (+ LoginView.vue)
    ├── components/
    │   ├── layout/     # AppLayout / AppHeader / AppSidebar / AppStatusBar
    │   ├── cli/        # Term* shared UI kit (Badge / Field / Modal / Stat / Section …)
    │   ├── charts/     # UsageLineChart / TimeRangeSelector (echarts)
    │   ├── agents/     # AgentGuardPanel / BootstrapHowToTabs / bootstrapSnippets / inboundGuardSnippets
    │   ├── dashboard/  # PlatformCard
    │   └── RelationGraph.vue (cytoscape)
    ├── api/            # 26 modules: agents / models / usage / auditLogs / apiKeys / agentCredentials /
    │                   #   services / serviceClients / serviceAccessGrants / platformLinks /
    │                   #   classificationInventory / trustedHosts / ingestion* (collections/documents/jobs/
    │                   #   evalRuns/llmCredentials/relations) / users / departments / banners / alerts /
    │                   #   caAuth / chunkingPreview / client
    ├── stores/         # apiKeys / auth / models / usage (pinia)
    ├── utils/          # approvalStatus (7-state) / healthStatus (5-state) / serviceRegistry
    ├── composables/    # useTheme (light-first) / useDialog
    └── assets/styles/  # tokens.css (institutional blue) / main.css
```

---

## 6. Run & deploy

### Local dev

```bash
cd apps/csp-governance-ui
npm install
npm run dev            # Vite dev server :5173
```

`vite.config.js` proxies `/api`, `/v1`, `/v2` to `http://localhost:8000` (CSP backend); confirm it first: `curl -sf http://localhost:8000/health`. The UI is served at the origin root `/` (`vite.config.js` sets no `base`).

### Production (ships inside the CSP container)

The governance center **has no standalone compose service**: it is built by the `frontend-build` stage of [`infra/docker/csp.Dockerfile`](../../infra/docker/csp.Dockerfile) (`node:22-alpine` → `npm install` → `npm run build`), copied into the CSP image as `/app/frontend-dist`, and served by the **CSP FastAPI backend** at the origin root `/` (`services/csp/app/main.py` mounts `/assets` + SPA fallback).

So **changing the governance center = rebuilding the `csp` image**:

```bash
docker compose -f compose.yaml build csp        # name: anila-platform → infra/compose/platform.yml
docker compose -f compose.yaml up -d csp
# day-2 lifecycle via infra/deployment/scripts/deploy-prod.sh; intranet bootstrap via infra/deployment/intranet/intranet-deploy.sh
```

### Verification gates

```bash
npm run build          # the only frontend gate (this UI has no unit tests)
# backend tests live in services/csp: cd services/csp && .venv/bin/python -m pytest
```

---

## 7. Related docs

- Design authority: [`../../docs/anila-redesign-docs/`](../../docs/anila-redesign-docs/) (constitution 00 / control plane 03 / models 04 / agents 05 / services 07 / classification 08 / language 11 / visual 12)
- Backend: [`../../services/csp/README.md`](../../services/csp/README.md)
- Adjacent entries: task center [`../anila-shell/README.en.md`](../anila-shell/README.en.md) · knowledge base / output center [`../anilalm/README.en.md`](../anilalm/README.en.md)
- Platform: [`../../README.md`](../../README.md) · branch policy [`../../docs/branch-sync-backlog.md`](../../docs/branch-sync-backlog.md)

---

**Framework**: Vue 3 + Vite · **Serves**: Governance Center (admin control plane, origin `/`) · **Talks to**: CSP (`/api`, `/v1`, `/v2`) · **Ships in**: the CSP image (not a standalone service).
