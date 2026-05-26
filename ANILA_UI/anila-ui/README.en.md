# ANILA Runtime UI (`anila-runtime-ui`)

> The ANILA platform frontend Runtime (React + Vite): after logging in, end users chat with agents, share/hand off conversations, upload attachments, and can let the main LLM auto-dispatch via the `anila-router` pseudo-agent.

> 繁體中文版本：[`README.md`](./README.md)

> 📌 **This file is on the `prod` branch (NCSIST intranet deployment).** `src/login.jsx` has been deleted on prod (users go through myCSPPlatform's Vue `LoginView.vue` — the unified entry for smart-card / SSO / password); `src/runtime/auth.jsx` runs the SSO flow (main has the plain password-only flow). Markdown images in [`src/markdown.jsx`](./src/markdown.jsx) have a `MarkdownImage` + `ImageLightbox` component pair that opens a fullscreen preview on click (PR #15). Permanent fork-zone list: see [`../../docs/branch-sync-backlog.md`](../../docs/branch-sync-backlog.md).

---

## Overview

`anila-ui` (package name `anila-runtime-ui`) is the **single-page application (SPA)** that fronts the ANILA platform. It holds no business logic or models itself; it only handles:

- Login / session management (httpOnly cookie + CSRF, no frontend token storage).
- Conversation create / browse / persist, and SSE streaming render.
- Agent selection, side-by-side comparison, and share / handoff / attachment collaboration.
- Visualizing the typed SSE events the backend emits (trace, interrupt, todos, tool call, spans, etc.).

For overall platform architecture, compose startup, and environment variables, see the repo-root [`README.md`](../../README.md) and the roadmap [`anila_plan.md`](../../anila_plan.md); this file focuses on the UI sub-project itself.

---

## Architecture & Stack

Read from `package.json` (`name: anila-runtime-ui`, `type: module`):

| Category | Detail |
|---|---|
| Framework | **React 18** (`react` / `react-dom` `^18.3`) |
| Routing | `react-router-dom` `^6.30` |
| Build tool | **Vite 6** (`@vitejs/plugin-react`) |
| Markdown / math / highlight | `react-markdown` + `remark-gfm` / `remark-math` + `rehype-katex` / `rehype-highlight` + `katex` + `highlight.js` |
| Testing | **Vitest 3** + `@testing-library/react` + `jsdom` (config in the `test` block of `vite.config.js` and `vitest.setup.js`) |

Styling: there is **no** `src/styles.css` or Tailwind import in this repo; the whole UI relies on inline component styles plus the base CSS embedded in `index.html`.

---

## Layout

```
anila-ui/
├── index.html              # SPA entry HTML (with base CSS)
├── vite.config.js          # Vite + react plugin + vitest config
├── vitest.setup.js         # Test setup (afterEach cleanup)
├── Dockerfile              # Multi-stage: node:22 build → nginx:1.27 serve
├── .env.example            # VITE_* env var template
├── docker/nginx.conf       # Runtime-stage nginx SPA config (incl. /health)
├── docs/                   # Sub-project progress notes (restore-ui-progress.md)
├── e2e/                    # E2E notes (README.md)
└── src/
    ├── main.jsx            # ReactDOM mount entry
    ├── app.jsx             # ChatRuntime — agent selection, send, persist
    ├── chat.jsx            # Sidebar / MessageBubble / Composer
    ├── collab.jsx          # ShareDialog / HandoffMenu / TagEditor
    ├── trust.jsx           # CitationsDrawer / ConfidentialWatermark
    ├── multiagent.jsx      # ParallelCompareView (2-3 agents side by side)
    ├── agentic.jsx         # InterruptCard / TodoChecklist / FollowUpChips / PausedBadge
    ├── toolExecution.jsx   # ToolExecutionWidget + Terminal/Diff/FileTree/Plain
    ├── spanTree.jsx        # SpanTreeViewer (dev-only)
    ├── login.jsx           # Login page (local credentials + OIDC)
    ├── markdown.jsx        # ReactMarkdown wrapper + KaTeX / highlight.js
    ├── components.jsx      # Shared UI components
    ├── icons.jsx           # Hand-rolled SVG icon set
    ├── tweaks.jsx          # Visual tweak panel
    ├── data.jsx            # mock / placeholder data
    ├── runtime/            # Non-UI logic (fetch / auth / SSE / persist, etc.)
    │   ├── api.js          # fetch wrapper + cookie + CSRF + multipart + session helpers
    │   ├── auth.jsx        # AuthProvider + useAuth hook
    │   ├── conversations.js# CSP control-plane endpoint wrappers
    │   ├── memory.js       # /api/memory/* wrappers (per-user facts / chunks)
    │   ├── sse.js          # SSE parser + dispatchSseEvent + resume helper
    │   ├── classified.js   # classified one-way latch helper
    │   ├── classifyRetryQueue.js # sessionStorage retry queue (latch persist)
    │   ├── messageMeta.js  # Message metadata normalization / persistence
    │   ├── searchSynonyms.js # Tag search synonym expansion
    │   ├── time.js         # Relative time / ISO format
    │   └── titleClean.js   # Post-processing of LLM auto-generated titles
    └── __tests__/          # Vitest (agentic / toolExecution / spanTree / sse / messageMeta / classified / ...)
```

---

## Setup & Run

### Local development

```bash
cd ANILA_UI/anila-ui
cp .env.example .env.local      # edit if CSP / Router are not on localhost
npm install
npm run dev                     # Vite dev server, :5173
```

Open <http://localhost:5173>; the first visit redirects to `/login`. Sign in with a CSP account (local credentials or OIDC) to start chatting.

### Scripts (`package.json`)

| Command | Purpose |
|---|---|
| `npm run dev` | Vite dev server (HMR) `:5173` |
| `npm run build` | Production build into `dist/` |
| `npm run preview` | Serve the production build locally |
| `npm test` | Vitest tests (`vitest run`) |

### Docker (standalone build)

The `Dockerfile` is multi-stage: `node:22-alpine` build → `nginx:1.27-alpine` serve (`docker/nginx.conf`, `EXPOSE 80`, `/health` returns `ok`).

```bash
docker build \
  --build-arg VITE_CSP_BASE_URL=http://csp.example:8000 \
  --build-arg VITE_ROUTER_BASE_URL=http://router.example:9000 \
  -t anila-runtime-ui .
docker run -p 8080:80 anila-runtime-ui
```

### Run with CSP / Router (compose)

`anila-ui` is defined in both the repo-root [`docker-compose.yml`](../../docker-compose.yml) and [`docker-compose-dev.yml`](../../docker-compose-dev.yml): build context `ANILA_UI/anila-ui`, only `expose: 80` (no host port mapping), healthcheck hits `/health`, `depends_on` `csp` + `router` (service_healthy), reached externally through the `nginx` reverse proxy. Build args default `VITE_CSP_BASE_URL` to an empty string (→ same-origin relative URLs) and `VITE_ROUTER_BASE_URL` to `/router`.

```bash
cd ../../
docker compose up -d            # brings up csp-db / csp / redis / router / anila-ui / nginx, etc.
# UI is served via nginx, default https://localhost:4443/
```

---

## Integration

| Target | Path | Auth |
|---|---|---|
| **CSP Control Plane** | `/api/*` | cookie session (httpOnly + CSRF, 401 auto-refresh) |
| **CSP Data Plane** | `/v1/*` | same cookie; SDK / curl may also use `Authorization: Bearer sk-…` |
| **ANILA Router** | `/v1/*`, `/v1/sessions/{id}/{state,answer}` | same CSP cookie; `model=anila-router` pseudo-agent |

Environment variables (`.env.example`; Vite inlines them into the client bundle at build/dev time):

| Variable | Purpose | Default |
|---|---|---|
| `VITE_CSP_BASE_URL` | CSP Control Plane (`/api/*`) + Data Plane (`/v1/*`) base | `http://localhost:8000` |
| `VITE_ROUTER_BASE_URL` | ANILA Router base (`anila-router` pseudo-agent) | `http://localhost:9000` |

When unset, `src/runtime/api.js` emits a `console.warn` at boot; empty values fall back to relative paths, which only work when a reverse proxy fronts both services.

Key backend endpoints: `GET/POST /api/conversations` (incl. `{id}` plus `/messages`, `/shares`), `POST /api/attachments` (multipart), `POST /api/handoffs` (incl. `/accept`, `/reject`, `/cancel`), `POST /v1/chat/completions` (SSE), and `/api/memory/*`. **Classified rules are decided by the backend**: when an agent has `requires_encryption=true` or SSE meta carries `classified=true`, the conversation one-way latches to classified with no downgrade UI (latch persistence is ensured by `runtime/classifyRetryQueue.js` reaching CSP).

---

## Related docs

- Platform overview: [`../../README.md`](../../README.md), roadmap [`../../anila_plan.md`](../../anila_plan.md)
- CSP (this UI's backend): [`../../myCSPPlatform/README.md`](../../myCSPPlatform/README.md)
- Router (implements the `anila-router` pseudo-agent): [`../../anila-core-router/README.md`](../../anila-core-router/README.md)
- Agent template (dispatchable by this UI): [`../../anila-agent/README.md`](../../anila-agent/README.md)
- Service-token cutover runbook: [`../../docs/runbooks/service-token-cutover.md`](../../docs/runbooks/service-token-cutover.md)
- License: [`../../LICENSE`](../../LICENSE)

---

**Framework**: React + Vite · **Talks to**: CSP (`/api/*` + `/v1/*` cookie) + Router (`/v1/*` cookie, `model=anila-router`) — both fronted by `nginx`.

**Last updated**: 2026-05-26 (sync PR #15 lightbox + PR #16 fork-zone follow-up + add prod banner)
