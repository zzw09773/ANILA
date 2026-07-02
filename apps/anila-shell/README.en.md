# ANILA Runtime UI (`anila-runtime-ui`)

> ANILA's frontend runtime (React + Vite, version 1.0.0): after login, end users chat with agents, share, hand off, upload attachments, and can let the primary LLM auto-dispatch via the `anila-router` pseudo-agent.

> 中文版本：[`README.md`](./README.md)

> 🌿 **Branch note**: This UI exists on every ANILA deployment branch. See the root [`README.md`](../../README.md) branch matrix and [`docs/branch-sync-backlog.md`](../../docs/branch-sync-backlog.md). **The login path is branch-dependent**: on `main` and most branches `login.jsx` is **local password only** (this build removed LDAP / OIDC / SSO entry points); on `prod-intranet-card`, `login.jsx` is removed in favour of myCSPPlatform's Vue `LoginView.vue` unified card / SSO / password entry; the `trial-military` slim build drops some advanced screens as needed.

---

## Overview

`anila-ui` (package `anila-runtime-ui`) is ANILA's public **single-page application (SPA)**. It holds no business logic or models, only: login / session management (httpOnly cookie + double-submit CSRF, no frontend token storage), conversation create / browse / persist + SSE streaming render, agent selection / parallel compare / share / handoff / attachments, and visualizing the typed SSE events (trace / interrupt / todos / tool call / spans) the backend sends.

For platform-wide architecture, compose and env vars, see the repo-root [`README.md`](../../README.md) and roadmap [`anila_plan.md`](../../anila_plan.md).

---

## Architecture & Stack (from `package.json`)

| Category | Details |
|---|---|
| Framework / routing | **React 18.3.1** · `react-router-dom` 6.30.1 |
| Build | **Vite 6.3.5** (`@vitejs/plugin-react` 4.4.1) |
| Markdown / math / highlight | `react-markdown` 9 + `remark-gfm` 4 / `remark-math` 6 + `rehype-katex` 7 / `rehype-highlight` 7 + `katex` 0.16 + `highlight.js` 11 |
| Diagrams | **`mermaid` 11.15.0** (flowchart rendering) |
| Testing | **Vitest 3.1.3** + `@testing-library/react` 16 + `jest-dom` 6 + `jsdom` 26 |

Scripts: `dev` (vite) / `build` (vite build) / `preview` / `test` (vitest run). **No `lint` script.** Styling: no `src/styles.css`, no Tailwind import; the whole UI relies on inline component styles + base CSS in `index.html`.

---

## Layout

```
anila-ui/
├── index.html · vite.config.js · vitest.setup.js
├── Dockerfile              # multi-stage: node:22-alpine build (npm install) → nginx:1.27-alpine serve
├── .env.example · docker/nginx.conf · docs/ · e2e/   # e2e/ holds only a legacy README (the Functions v1 stack was fully removed — no spec, no Playwright dep)
└── src/
    ├── main.jsx            # ReactDOM entry; BrowserRouter(basename=import.meta.env.BASE_URL)
    │                       #   + AuthProvider + ConfirmProvider; /login, /app/*(RequireAuth)
    ├── app.jsx             # ChatRuntime — agent selection, send, persist, orchestration
    ├── chat.jsx            # Sidebar / MessageBubble / Composer
    ├── collab.jsx          # ShareDialog / HandoffMenu / TagEditor
    ├── trust.jsx           # CitationsDrawer / ConfidentialWatermark
    ├── multiagent.jsx      # ParallelCompareView (2-3 agents)
    ├── agentic.jsx         # InterruptCard / TodoChecklist / FollowUpChips / PausedBadge
    ├── toolExecution.jsx   # ToolExecutionWidget + Terminal/Diff/FileTree/Plain
    ├── spanTree.jsx        # SpanTreeViewer (dev trace tree)
    ├── login.jsx           # Login page (local password; OIDC/SSO removed in this build)
    ├── markdown.jsx        # ReactMarkdown + KaTeX / highlight.js (incl. MarkdownImage + ImageLightbox)
    ├── banners.jsx         # BannerBar announcements (dismissals in localStorage)
    ├── changelog.jsx       # "What's New" modal (build-time, CHANGELOG_VERSION)
    ├── confirm.jsx         # ConfirmProvider + useConfirm / useToast (replaces native confirm/alert)
    ├── components.jsx · icons.jsx · tweaks.jsx · data.jsx
    ├── runtime/            # non-UI logic
    │   ├── api.js          # fetch wrapper (credentials:include + CSRF) + multipart + session helpers
    │   ├── auth.jsx · conversations.js · memory.js
    │   ├── sse.js          # SSE parser + dispatchSseEvent + streamChatCompletion + streamSessionAnswer
    │   ├── classified.js · classifyRetryQueue.js   # classified one-way latch + sessionStorage persist
    │   └── messageMeta.js · searchSynonyms.js · time.js · titleClean.js
    └── __tests__/          # Vitest (10 files: agentic / classified / classifyRetryQueue / messageMeta /
                            #   normalizeAgents / searchSynonyms / spanTree / sse / titleClean / toolExecution)
```

> Tests live in **`src/__tests__/`** (not `runtime/__tests__/`).
>
> ⚠️ `e2e/README.md` is **stale**: the Functions v1 Playwright stack it describes (`functions.spec.js`, sandbox/egress compose, `http://localhost:3001`) was removed. There is currently **no Playwright E2E** in this subproject — do not follow that file.

---

## Setup & Run

```bash
cd ANILA_UI/anila-ui
cp .env.example .env.local      # edit if CSP / Router aren't on localhost
npm install && npm run dev      # Vite dev server :5173
```

Docker (multi-stage `node:22-alpine` build → `nginx:1.27-alpine` serve, `EXPOSE 80`, `/health` returns `ok`):

```bash
docker build \
  --build-arg VITE_CSP_BASE_URL=http://csp.example:8000 \
  --build-arg VITE_ROUTER_BASE_URL=http://router.example:9000 \
  --build-arg BASE_PATH=/anila/ -t anila-runtime-ui .
docker run -p 8080:80 anila-runtime-ui
```

> This subproject's `Dockerfile` build-arg defaults are `VITE_CSP_BASE_URL=http://localhost:8000`, `VITE_ROUTER_BASE_URL=http://localhost:9000`, `BASE_PATH=/`. The repo-root compose overrides them to same-origin / `/router` and fronts the UI via the main nginx (default `https://localhost:4443/`).

### Testing

```bash
npm test          # vitest run — the 10 unit tests under src/__tests__/ (runtime pure logic + a few components)
```

Vitest unit tests only; there is currently no E2E in this subproject (see the `e2e/` note in the layout above).

---

## Integration

| Target | Path | Auth |
|---|---|---|
| **CSP Control Plane** | `/api/*` | cookie session (httpOnly + CSRF, auto-refresh on 401) |
| **CSP Data Plane** | `/v1/*` | same cookie; SDK / curl may also use `Authorization: Bearer sk-…` |
| **ANILA Router** | `/v1/sessions/{id}/{state,answer}` | same CSP cookie; `model=anila-router` |

Env vars (`.env.example`, inlined by Vite at build/dev time): `VITE_CSP_BASE_URL` (default `http://localhost:8000`), `VITE_ROUTER_BASE_URL` (default `http://localhost:9000`). `BASE_PATH` is a build-arg (not `VITE_`); `vite.config.js` reads `BASE_PATH || VITE_BASE_PATH || '/'`.

Backend endpoints actually called (from `runtime/*.js`): `/api/conversations` (incl. `/search`, `/{id}`, `/messages`, `/messages/{mid}/{rating,edit}`, `/shares`, `/{id}/classify`), `/api/attachments` (multipart), `/api/handoffs` (incl. `/{id}/{accept,reject,cancel}`), `/api/auth/refresh`, `/api/agents/{ref}/functions`, `/api/users/me/ui-settings`, `/api/banners/active`, `/api/memory/*`; streaming `POST /v1/chat/completions` (SSE, with `X-CSRF-Token`, numeric conversations send `X-ANILA-Conversation-Id`, reads back `X-Anila-Session-Id`); Router `GET /v1/sessions/{id}/state`, `POST /v1/sessions/{id}/answer` (SSE resume).

**Classified rules are decided by the backend**: when an agent is `requires_encryption=true` or the SSE meta carries `classified=true`, the conversation one-way latches to classified with no downgrade UI (latch persistence is ensured to reach CSP by `runtime/classifyRetryQueue.js`).

---

## Related docs

- Platform: [`../../README.md`](../../README.md), roadmap [`../../anila_plan.md`](../../anila_plan.md), branch strategy [`../../docs/branch-sync-backlog.md`](../../docs/branch-sync-backlog.md)
- CSP: [`../../myCSPPlatform/README.md`](../../myCSPPlatform/README.md) · Router: [`../../anila-core-router/README.md`](../../anila-core-router/README.md) · License: [`../../LICENSE`](../../LICENSE)

---

**Framework**: React + Vite · **Talks to**: CSP (`/api/*` + `/v1/*` cookie) + Router (`/v1/sessions/*` cookie, `model=anila-router`) — both fronted by `nginx`.
