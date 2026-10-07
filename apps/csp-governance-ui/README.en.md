# ANILA Governance Center — CSP Governance Console (`csp-platform`)

> The governance console for CSP (Control / Security Plane) (Vue 3 + Vite, v1.0.0). Administrators use it for identity, models, the agent registry, the service registry, knowledge governance, classification and the one-way latch, and trace, audit, and usage. It is not the day-to-day page for a regular user.

> 繁體中文原文: [`README.md`](./README.md)

- Current state: [`docs/CURRENT-STATUS.md`](../../docs/CURRENT-STATUS.md).

---

## 1. Product role

The governance center is the administrator's console. It covers:

```text
Governance Center (CSP)
├── Identity / departments / roles
├── Model governance (Model Gateway)
├── Agent Registry (approval)
├── Service Registry (GUI service registration + launch)
├── Knowledge governance (collections / chunking)
├── Classification & one-way latch
└── Trace / Audit / Usage
```

Regular users stay in ANILA Shell. owner, admin, developer, and unit admins can open this console from the Shell.

---

## 2. Visual system

The default theme is light. Tokens live in `src/assets/styles/tokens.css`: `:root` is light, `[data-theme="dark"]` is blue-grey. `src/composables/useTheme.js` defaults to light (`localStorage` key `anila.theme`; dark is used only when nothing is stored and the OS is dark). `index.html` sets `data-theme` in an inline script before Vue mounts, so the first paint does not flash from dark to light.

Type is the system font (`--font-sans` for the UI, `--font-mono` only for IDs, tokens, timestamps, and numbers). No webfont is downloaded. `index.html` sets `lang="zh-TW"`.

The `Term*` components under `src/components/cli/` (Badge, Field, Modal, Stat) are a shared UI kit. The names are older than the light theme.

### Login (card-first)

`src/views/LoginView.vue` leads with the **PKI ID card as the primary hero card**: detect card → enter PIN → card signs (`handleDetectCard` / `handleCardLogin`). Local username/password and OIDC SSO are collapsed under a secondary "other sign-in methods" section with reduced visual weight.

---

## 3. Views inventory (`src/router/index.js` is authoritative)

Routes are two-tier: `/login` (public) and `/` (`AppLayout`, `requiresAuth`) with children. The `beforeEach` guard tiers by `stores/auth.js` roles (`isAdmin` = admin∪owner; `isOwner`; `isDeveloper` = developer∪admin∪owner).

| Domain | View (route) | Highlights |
|---|---|---|
| Dashboard | `DashboardView` (`/`) | platform overview (`dashboard/PlatformCard.vue`) |
| Model governance | `ModelsView` (`models`) | **five-state health** (`utils/healthStatus.js`: unknown / healthy / degraded / unhealthy / disabled, normalizing legacy online/connecting/offline) + **per-model keys** (`has_api_key`: model key set / uses global key; `api_key` write-only). Model Gateway |
| Agent Registry | `DeveloperAgentsView` (`developer/agents`, developer) + `DeveloperGuideView` | **three-state approval** (`utils/approvalStatus.js`: registered / approved / disabled). No seven-state machine and no trace-test gate. The test-connection probe remains. |
| Service Registry | `PlatformLinksView` (`platform-links`), `ServiceAccessView`, `ServiceClientsView` | registered GUI services (`utils/serviceRegistry.js`: `launch_mode` new_tab/iframe, `config_source` env_seeded/db field locking, `classification_ceiling` four-level: 無機密 / 營業秘密 / 密 / 機密); service-token management. Service Registry |
| Knowledge governance | `KnowledgeCollectionsView`, `ChunkingPreviewView`, `CollectionDetailView` (developer) | collection inspector, chunking-strategy comparison wizard; relation graph via `components/RelationGraph.vue` (cytoscape) |
| Identity / departments | `UsersView`, `DepartmentsView` (admin) | users, departments, roles |
| Audit / usage | `AuditLogsView`, `UsageView` | audit; usage charted with echarts (`charts/UsageLineChart.vue`, `TimeRangeSelector.vue`) |
| Platform misc | `ApiKeysView`, `AlertsView`, `BannersView` (admin), `TrustedHostsView` (admin, SSRF allow-list) | keys, alerts, banners, SSRF trusted-host list |

---

## 4. Tech stack (from `package.json`)

| Area | Detail |
|---|---|
| Framework / router / state | **Vue 3.5.13** · `vue-router` 4.5.0 · `pinia` 2.3.0 |
| Charts | `echarts` 5.6.0 (`>=5.6.0 <6`) + `vue-echarts` 7.0.3 (`>=7.0.3 <8`); relation graph `cytoscape` 3.34.0 |
| HTTP | `axios` 1.7.9 |
| Styling | `tailwindcss` 3.4.17 + `postcss` + `autoprefixer` (build-time); design tokens via `src/assets/styles/tokens.css` |
| Build | **Vite 6.0.5** (`@vitejs/plugin-vue` 5.2.1) |

`scripts`: `dev` / `build` / `preview` / `test` (`node --test tests/*.test.mjs`). The utils (`healthStatus` / `approvalStatus` / `serviceRegistry`) are pure functions; tests read the source directly.

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
    ├── api/            # see src/api/: agents / models / usage / auditLogs / apiKeys /
    │                   #   services / serviceClients / serviceAccessGrants / platformLinks /
    │                   #   trustedHosts / ingestion* / users / departments / banners / alerts /
    │                   #   caAuth / chunkingPreview / client
    ├── stores/         # apiKeys / auth / models / usage (pinia)
    ├── utils/          # approvalStatus (3-state) / healthStatus (5-state) / serviceRegistry
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
docker compose -f compose.yaml build csp        # name: anila → infra/compose/platform.yml
docker compose -f compose.yaml up -d csp
# day-2 lifecycle via infra/deployment/scripts/deploy-prod.sh; intranet update via docs/deploy/UPDATE.md
```

### Verification gates

```bash
npm test && npm run build
# backend tests live in services/csp: cd services/csp && .venv/bin/python -m pytest
```

---

## 7. Related docs

- Current state: [`docs/CURRENT-STATUS.md`](../../docs/CURRENT-STATUS.md).
- Backend: [`../../services/csp/README.md`](../../services/csp/README.md)
- Adjacent entries: task center [`../anila-shell/README.en.md`](../anila-shell/README.en.md) · knowledge base / output center [`../anilalm/README.en.md`](../anilalm/README.en.md)
- Platform: [`../../README.md`](../../README.md)

