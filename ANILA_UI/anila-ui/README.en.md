# ANILA Runtime UI (`anila-runtime-ui`)

> ANILA's frontend runtime (React + Vite): after login, end users chat with agents, share, hand off, upload attachments, and can let the primary LLM auto-dispatch via the `anila-router` pseudo-agent.

> 中文版本：[`README.md`](./README.md)

> 🌿 **Branch note**: This UI exists on every ANILA deployment branch. See the root [`README.md`](../../README.md) branch matrix and [`docs/branch-sync-backlog.md`](../../docs/branch-sync-backlog.md). **The login path is branch-dependent**: most branches use local password login (`src/login.jsx` + `src/runtime/auth.jsx`); on `prod-intranet-card`, `src/login.jsx` is removed in favour of myCSPPlatform's Vue `LoginView.vue` unified card / SSO / password entry. The `trial-military` slim build drops some advanced screens (e.g. multi-agent parallel compare, as needed).

---

## Overview

`anila-ui` (package `anila-runtime-ui`) is ANILA's public **single-page application (SPA)**. It holds no business logic or models, only:

- Login / session management (httpOnly cookie + CSRF, no frontend token storage).
- Creating, browsing, persisting conversations, and SSE streaming render.
- Agent selection, side-by-side comparison, share / handoff / attachment collaboration.
- Visualizing the typed SSE events the backend sends (trace, interrupt, todos, tool call, spans).

For platform-wide architecture, compose startup and env vars, see the repo-root [`README.md`](../../README.md) and roadmap [`anila_plan.md`](../../anila_plan.md); this file focuses on the UI subproject itself.

---

## Architecture & Stack

From `package.json` (`name: anila-runtime-ui`, `type: module`):

| Category | Details |
|---|---|
| Framework | **React 18** (`react` / `react-dom` `^18.3`) |
| Routing | `react-router-dom` `^6.30` |
| Build | **Vite 6** (`@vitejs/plugin-react`) |
| Markdown / math / highlight | `react-markdown` + `remark-gfm` / `remark-math` + `rehype-katex` / `rehype-highlight` + `katex` + `highlight.js` |
| Testing | **Vitest 3** + `@testing-library/react` + `jsdom` |

Styling: there is **no** `src/styles.css` or Tailwind import; the whole UI relies on inline component styles + the base CSS embedded in `index.html`.

---

## Layout

```
anila-ui/
├── index.html              # SPA entry HTML (incl. base CSS)
├── vite.config.js          # Vite + react plugin + vitest config
├── vitest.setup.js
├── Dockerfile              # multi-stage: node:22 build → nginx:1.27 serve
├── .env.example            # VITE_* env var template
├── docker/nginx.conf       # runtime-stage nginx SPA config (incl. /health)
├── e2e/                    # E2E notes
└── src/
    ├── main.jsx            # ReactDOM mount entry
    ├── app.jsx             # ChatRuntime — agent selection, send, persist
    ├── chat.jsx            # Sidebar / MessageBubble / Composer
    ├── collab.jsx          # ShareDialog / HandoffMenu / TagEditor
    ├── trust.jsx           # CitationsDrawer / ConfidentialWatermark
    ├── multiagent.jsx      # ParallelCompareView (2-3 agents)
    ├── agentic.jsx         # InterruptCard / TodoChecklist / FollowUpChips / PausedBadge
    ├── toolExecution.jsx   # ToolExecutionWidget + Terminal/Diff/FileTree/Plain
    ├── spanTree.jsx        # SpanTreeViewer (dev-only)
    ├── login.jsx           # Login page (local password + OIDC; removed on prod-intranet-card)
    ├── markdown.jsx        # ReactMarkdown wrapper + KaTeX / highlight.js (incl. MarkdownImage + ImageLightbox)
    ├── components.jsx · icons.jsx · tweaks.jsx · data.jsx
    └── runtime/            # non-UI logic
        ├── api.js          # fetch wrapper + cookie + CSRF + multipart + session helpers
        ├── auth.jsx        # AuthProvider + useAuth hook
        ├── conversations.js# CSP control-plane endpoint wrappers
        ├── memory.js       # /api/memory/* wrappers
        ├── sse.js          # SSE parser + dispatchSseEvent + resume helper
        ├── classified.js · classifyRetryQueue.js  # classified one-way latch + persist
        ├── messageMeta.js · searchSynonyms.js · time.js · titleClean.js
        └── __tests__/      # Vitest
```

---

## Setup & Run

### Local development

```bash
cd ANILA_UI/anila-ui
cp .env.example .env.local      # edit if CSP / Router aren't on localhost
npm install
npm run dev                     # Vite dev server :5173
```

Open <http://localhost:5173>; first visit redirects to `/login`; sign in with a CSP account to start chatting.

### Scripts

| Command | Effect |
|---|---|
| `npm run dev` | Vite dev server (HMR) `:5173` |
| `npm run build` | production build to `dist/` |
| `npm run preview` | run the production build locally |
| `npm test` | Vitest (`vitest run`) |

### Docker (standalone build)

Multi-stage: `node:22-alpine` build → `nginx:1.27-alpine` serve (`docker/nginx.conf`, `EXPOSE 80`, `/health` returns `ok`).

```bash
docker build --build-arg VITE_CSP_BASE_URL=http://csp.example:8000 \
  --build-arg VITE_ROUTER_BASE_URL=http://router.example:9000 -t anila-runtime-ui .
docker run -p 8080:80 anila-runtime-ui
```

### Running with CSP / Router (compose)

`anila-ui` is defined in the repo-root [`docker-compose.yml`](../../docker-compose.yml) and [`docker-compose-dev.yml`](../../docker-compose-dev.yml): build context `ANILA_UI/anila-ui`, only `expose: 80` (no host port), healthcheck `/health`, `depends_on` `csp` + `router` (service_healthy), exposed via `nginx`. Build args default `VITE_CSP_BASE_URL` to empty (→ same-origin relative URL) and `VITE_ROUTER_BASE_URL` to `/router`.

```bash
cd ../../ && docker compose up -d     # brings up csp-db / csp / redis / router / anila-ui / nginx
# UI is exposed via nginx, default https://localhost:4443/
```

---

## Integration

| Target | Path | Auth |
|---|---|---|
| **CSP Control Plane** | `/api/*` | cookie session (httpOnly + CSRF, auto-refresh on 401) |
| **CSP Data Plane** | `/v1/*` | same cookie; SDK / curl may also use `Authorization: Bearer sk-…` |
| **ANILA Router** | `/v1/*`, `/v1/sessions/{id}/{state,answer}` | same CSP cookie; `model=anila-router` pseudo-agent |

Env vars (`.env.example`, inlined into the client bundle by Vite at build/dev time):

| Variable | Use | Default |
|---|---|---|
| `VITE_CSP_BASE_URL` | CSP Control Plane (`/api/*`) + Data Plane (`/v1/*`) base | `http://localhost:8000` |
| `VITE_ROUTER_BASE_URL` | ANILA Router base (`anila-router` pseudo-agent) | `http://localhost:9000` |

When unset, `src/runtime/api.js` `console.warn`s at boot; empty values fall back to relative paths, only correct when a reverse proxy fronts both services.

Main backend endpoints: `GET/POST /api/conversations` (incl. `{id}` / `/messages` / `/shares`), `POST /api/attachments` (multipart), `POST /api/handoffs` (incl. `/accept` `/reject` `/cancel`), `POST /v1/chat/completions` (SSE), `/api/memory/*`. **Classified rules are decided by the backend**: when an agent is `requires_encryption=true` or the SSE meta carries `classified=true`, the conversation one-way latches to classified with no downgrade UI (latch persistence is ensured to reach CSP by `runtime/classifyRetryQueue.js`).

---

## Related docs

- Platform: [`../../README.md`](../../README.md), roadmap [`../../anila_plan.md`](../../anila_plan.md), branch strategy [`../../docs/branch-sync-backlog.md`](../../docs/branch-sync-backlog.md)
- CSP (this UI's backend): [`../../myCSPPlatform/README.md`](../../myCSPPlatform/README.md)
- Router (`anila-router` pseudo-agent): [`../../anila-core-router/README.md`](../../anila-core-router/README.md)
- Agent template: [`../../anila-agent/README.md`](../../anila-agent/README.md) · License: [`../../LICENSE`](../../LICENSE)

---

**Framework**: React + Vite · **Talks to**: CSP (`/api/*` + `/v1/*` cookie) + Router (`/v1/*` cookie, `model=anila-router`) — both fronted by `nginx`.
