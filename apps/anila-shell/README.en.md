# ANILA Shell — Task Center Runtime (`anila-runtime-ui`)

> The end-user interface of ANILA (React + Vite, v1.0.0). After signing in, people chat with a model or an assistant, open usage and memory, launch a registered project entry, and open the knowledge base from the sidebar. Slides, reports, and the other outputs live on that knowledge-base page.

> 繁體中文原文: [`README.md`](./README.md)

- Current state: [`docs/CURRENT-STATUS.md`](../../docs/CURRENT-STATUS.md).

---

## 1. Product role

The sidebar (`buildShellEntries()` in `src/shellNav.jsx`) is these five items:

```text
ANILA
├── 對話 (Chat)           ← default view
├── 我的知識庫            → /anilalm (knowledge base and outputs share this page; when the gate is off the row shows「即將推出」and cannot be clicked)
├── 專案入口              → ServicesPanel
├── 用量
└── 記憶
```

owner, admin, developer, and unit admins also see 治理中心, which links to the same origin `/`.

The shell holds no models. It guards the session, creates conversations, renders the stream (trace, interrupt, todos, tool call, spans), and shows the classification watermark, trace, and audit. Labels use product words. ANILALM, Studio, and CSP do not appear on screen.

### No login page (unified SSO)

`src/main.jsx` registers only `/app/*` (`RequireAuth`) plus a catch-all `Navigate → /app`. When unauthenticated, `RequireAuth` does a **full-page redirect** to the governance console's `/login` (the Vue `LoginView.vue`, served same-origin on 443), carrying `next` for return. There is **no** `login.jsx` in the shell.

---

## 2. Core capabilities

| Capability | Files | Summary |
|---|---|---|
| **Sidebar** | `src/shellNav.jsx` | `buildShellEntries()` yields chat, knowledge base, project entry, usage, and memory. `canSeeGovernance()` (owner, admin, developer, and `is_unit_admin`) appends 治理中心. `originHref()` links `/anilalm` and `/` with origin-absolute paths. Mounted in the `src/chat.jsx` sidebar. |
| **Task spine** | `src/runtime/tasks.js`, `src/runtime/sse.js`, `src/app.jsx` | On first send, `createTaskForConversation()` → `POST /api/tasks` (`task_type:"query"`, `source_scope:"none"`); `taskId` is cached on conversation state; `sse.js` sends the **`X-ANILA-Task-Id`** header so CSP binds the dispatch to the same Task. Any failure returns `null` + a zh-TW `console.warn`; chat continues task-less. |
| **Trace Explorer** | `src/runtime/traces.js`, `src/spanTree.jsx` | `fetchTrace()` → `GET /api/traces/{trace_id}` returns persisted `{trace_id, task_id, spans[]}`; `spansToTree()` builds a tree from flat spans; `TraceExplorer` renders a "檢視軌跡 (View trace)" control backed by `SpanTreeViewer`. Failures degrade to a "no trace data" notice — never throws. |
| **Four-level classification** | `src/trust.jsx`, `src/runtime/classified.js`, `src/runtime/classifyRetryQueue.js` | `watermarkLevel()` derives the Traditional Chinese level from the conversation. `ClassificationWatermark` is a corner badge (密 = warn, 機密 = danger-strong; 無機密 and 營業秘密 do not get the full-screen watermark). `ConfidentialWatermark` tiles "level · reader · read time (to the minute)" across the screen, and recomputes the time when the conversation changes. The server decides the latch. `classifyRetryQueue` delivers it to CSP. |
| **Project Entry (ServicesPanel)** | `src/services.jsx` | `fetchServices()` → `GET /api/services`, and on 404 falls back to `GET /api/platform-links`. `resolveLaunch()` calls `POST /api/services/{id}/launch` for registry services. `new_tab` → `window.open(_,'_blank','noopener')`; `iframe` → an in-site sandboxed overlay (`sandbox="allow-scripts allow-same-origin allow-forms"`, `referrerPolicy="no-referrer"`) with a "provided by <name>" notice. |

---

## 3. Tech stack (from `package.json`)

| Area | Detail |
|---|---|
| Framework / router | **React 18.3.1** · `react-router` 7.18.2 (`>=7.18.2 <8`) |
| Build | **Vite 6.3.5** (`@vitejs/plugin-react` 4.4.1) |
| Markdown / math / highlight | `react-markdown` 9 + `remark-gfm`/`remark-math` + `rehype-katex`/`rehype-highlight` + `katex` + `highlight.js` |
| Diagrams | **`mermaid` 11.16.1** |
| Testing | **Vitest 3.1.3** + `@testing-library/react` + `jest-dom` + `jsdom` |

`scripts`: `dev` / `build` / `preview` / `test` (`vitest run`). **No `lint` script**. No Tailwind / global stylesheet — inline component styles + base CSS in `index.html` with system `--font-sans/--font-mono` stacks (air-gap safe, no external fonts).

---

## 4. Layout

```
apps/anila-shell/
├── index.html · vite.config.js · vitest.setup.js
├── Dockerfile              # node:22-alpine build (npm ci) → nginx:1.30.4-alpine (digest-pinned) serve; EXPOSE 80
├── .env.example · docker/nginx.conf · docs/
└── src/
    ├── main.jsx            # entry; BrowserRouter(basename=BASE_URL) + AuthProvider + ConfirmProvider; /app/* only, no login page
    ├── app.jsx             # ChatRuntime — agent selection, send, Task creation, Trace Explorer, watermarks, ServicesPanel
    ├── shellNav.jsx        # four-entry ShellNav + admin-gated governance (originHref same-origin links)
    ├── chat.jsx            # Sidebar (mounts ShellNav) / MessageBubble / Composer
    ├── services.jsx        # ServicesPanel (fetchServices / resolveLaunch / IframeOverlay)
    ├── trust.jsx           # citations / confidence / ClassificationWatermark / ConfidentialWatermark / AuditWatermark
    ├── spanTree.jsx        # spansToTree + SpanTreeViewer + TraceExplorer (檢視軌跡)
    ├── collab.jsx · multiagent.jsx · agentic.jsx · toolExecution.jsx · markdown.jsx · banners.jsx · changelog.jsx · confirm.jsx · components.jsx · icons.jsx · tweaks.jsx · data.jsx
    ├── runtime/            # non-UI logic: api.js (fetch + CSRF) · auth.jsx · conversations.js · memory.js ·
    │                       #   sse.js (SSE parser + X-ANILA-Task-Id) · tasks.js · traces.js ·
    │                       #   classified.js · classifyRetryQueue.js · messageMeta.js · searchSynonyms.js · time.js · titleClean.js
    └── __tests__/          # Vitest (17 files)
```

---

## 5. Run, deploy, test

### Local dev

```bash
cd apps/anila-shell
cp .env.example .env.local      # edit only if CSP / Router are not on localhost
npm install && npm run dev      # Vite dev server :5173
```

### Container (monorepo compose)

The shell's compose service is **`anila-ui`** (build context `apps/anila-shell`), managed by the root compose shim: `compose.yaml` (`name: anila`) → `infra/compose/platform.yml`; dev is `compose.dev.yaml` → `infra/compose/dev.yml`. Production builds with `BASE_PATH=/anila/` and is reverse-proxied same-origin at 443 `/anila/` (shared SSO cookie).

```bash
docker compose -f compose.yaml up -d anila-ui        # built/up with the full stack
# day-2 lifecycle (status/logs/restart) via infra/deployment/scripts/deploy-prod.sh
```

### Testing

```bash
npm test        # vitest run — 17 files / 222 tests (all green at time of writing)
```

Vitest unit tests only; there is no Playwright E2E.

---

## 6. Backend contracts & env

| Peer | Path | Auth |
|---|---|---|
| CSP control plane | `/api/*` | same-origin cookie session (httpOnly + double-submit CSRF, auto-refresh on 401) |
| CSP data plane | `/v1/*` | same cookie; SDK/curl may also use `Authorization: Bearer sk-…` |
| ANILA Router | `/v1/sessions/{id}/{state,answer}` | same CSP cookie; `model=anila-router` |

Env vars (`.env.example`, inlined at Vite build/dev time): `VITE_CSP_BASE_URL` (default `http://localhost:8000`), `VITE_ROUTER_BASE_URL` (default `http://localhost:9000`). `BASE_PATH` is a build-arg (not `VITE_`); `vite.config.js` reads `BASE_PATH || VITE_BASE_PATH || '/'`.

Endpoints actually called (from `src/runtime/*.js` and components): `/api/tasks`, `/api/traces/{id}`, `/api/services` (+ `/{id}/launch`), `/api/platform-links`, `/api/conversations*`, `/api/attachments`, `/api/handoffs*`, `/api/auth/refresh`, `/api/agents/{ref}/functions`, `/api/banners/active`, `/api/memory/*`; streaming `POST /v1/chat/completions` (with `X-CSRF-Token`, `X-ANILA-Task-Id`, `X-ANILA-Conversation-Id`); Router `GET /v1/sessions/{id}/state`, `POST /v1/sessions/{id}/answer`.

---

## 7. Related docs

- Platform: [`../../README.md`](../../README.md)
- Current state: [`docs/CURRENT-STATUS.md`](../../docs/CURRENT-STATUS.md).
- Adjacent entries: knowledge base / output center [`../anilalm/README.en.md`](../anilalm/README.en.md) · governance [`../csp-governance-ui/README.en.md`](../csp-governance-ui/README.en.md)
- Backend: [`../../services/csp/README.md`](../../services/csp/README.md) · Router [`../../services/anila-core-router/README.md`](../../services/anila-core-router/README.md)

