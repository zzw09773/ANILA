# ANILA LM (ANILALM)

> AI learning-content generation sub-project: a research-notes-style knowledge-base frontend, plus a standalone `pptx-renderer` microservice that turns a slide-deck spec into `.pptx`.

> 📌 **This file is on the `prod` branch (NCSIST intranet deployment).** The Studio backend's 4 artifact pipelines (report / mindmap / infographic / datatable) have been extracted to [`anila-studio`](../anila-studio/) — ANILALM reaches them via nginx routes `/api/studio/` and `/api/{reports,mindmaps,infographics,datatables}/`. **No frontend module changes**.

## Overview

**ANILALM** is a sub-project under `<project_root>`. It gives researchers a one-stop "document → conversation → output" interface: upload PDFs / documents → build a knowledge base → query via chat → generate deep reports and slide-deck drafts directly. It is a SPA (single-page application) that talks to the myCSPPlatform backend for auth, ingestion, conversations, and an LLM proxy.

The sub-project contains two independent runnable units:

1. **Top-level ANILALM app** — a Vite + React + TypeScript frontend, built to static files and served by nginx.
2. **`pptx-skill/` (the pptx-renderer service)** — a standalone Node service (`server.js`) that uses Express + pptxgenjs to render a deck spec into `.pptx`. On the docker network it is exposed under the service name `pptx-renderer` on port `7100`, providing three endpoints:
   - `POST /render` — takes `{ spec }`, returns the `.pptx` binary (octet-stream).
   - `POST /screenshots` — takes a `.pptx` (base64 or a server-side path), converts to PDF via LibreOffice headless, then to PNG via Poppler, and returns per-slide images for vision QA.
   - `POST /qa-geometric` — runs a geometric layout check on rendered slides (overlap detection, largest empty region, etc.).
   - `GET /health` — returns `ok`.

> They serve different roles: the frontend is the user interface; `pptx-renderer` is the rendering engine called by the backend. The frontend never calls the renderer directly.

## Architecture & Stack

### Top-level ANILALM app (frontend)

From `package.json`:

| Module | Choice |
| --- | --- |
| Build | Vite 6 + React 18 + TypeScript 5.7 |
| Routing | react-router-dom v6 (`BrowserRouter` + nested Outlet guards) |
| State | Zustand (auth / workspace / artifacts) |
| HTTP | axios + interceptors (401 token refresh, `withCredentials`) |
| Markdown | marked + DOMPurify (LLM output is untrusted, two-layer XSS defense) |
| Icons | inline SVG (hand-rolled set, zero packages) |

npm scripts: `dev` (vite), `build` (`tsc -b && vite build`), `preview`, `typecheck`.

Runtime image (top-level `Dockerfile`): multi-stage, `node:22-alpine` build → `nginx:1.27-alpine` serving `dist/`. The `BASE_PATH` build-arg defaults to `/anilalm/`, matching the deployment path behind the ANILA reverse proxy.

### pptx-skill / pptx-renderer service

From `pptx-skill/package.json`:

| Dependency | Purpose |
| --- | --- |
| `express` ^5 | HTTP server |
| `pptxgenjs` ^3.12 | Produces `.pptx` |
| `sharp` ^0.33 | Image processing |
| `react` / `react-dom` / `react-icons` | icon resolution (`icons.js` maps concept names to Heroicons PNGs) |

`pptx-skill/Dockerfile` uses `node:22-bookworm-slim` (not alpine) and adds `libreoffice-core` / `libreoffice-impress` (`.pptx → PDF`), `poppler-utils` (PDF → PNG), `fonts-noto-cjk` (CJK fonts, so Chinese text isn't rendered as boxes), and `tini` (PID-1 reaper, so SIGTERM propagates cleanly to the soffice child processes). `node_modules` is vendored (`npm ci --omit=dev`) for air-gapped builds. Defaults: `PORT=7100`, `PPTX_TMP_DIR=/var/anila/pptx-out`.

`server.js` is deliberately schema-light: the CSP backend runs Pydantic validation first, so a spec arriving at the renderer is already structurally valid; the renderer only checks payload size (`MAX_PAYLOAD=10mb`), slide-count cap (`MAX_SLIDES=60`), and the `/screenshots` path (no traversal).

## Layout

```
ANILALM/
├── package.json                # frontend: react / axios / zustand / marked / dompurify / react-router
├── Dockerfile                  # frontend image: Vite build → nginx
├── vite.config.ts              # proxies /api, /v1, /v2 to VITE_CSP_BACKEND
├── index.html                  # Vite entry
├── docker/                     # nginx.conf and deployment config
├── _design/                    # legacy prototype (single-file HTML + Figma artboards), kept for reference, not built
├── src/
│   ├── main.tsx / App.tsx      # createRoot + ThemeProvider + BrowserRouter
│   ├── api/                    # axios client + auth/collections/documents/jobs/conversations/chat
│   ├── store/                  # auth.ts / workspace.ts / artifacts.ts (Zustand)
│   ├── routes/                 # ProtectedRoute / LoginPage / DashboardPage / WorkspacePage
│   ├── workspace/              # WSSidebar / WSChat / WSStudio / CommandModal / ArtifactViewer / useJobStream
│   ├── studio/generators.ts    # generateReport / generateSlides (call /v1/chat/completions)
│   ├── theme/                  # tokens.ts + ThemeContext.tsx
│   ├── components/             # Icon / ThemeSwitch / Field / Modal / MarkdownPreview ...
│   └── utils/format.ts
└── pptx-skill/                 # ── standalone pptx-renderer service ──
    ├── server.js               # Express app: /render /screenshots /qa-geometric /health (port 7100)
    ├── icons.js                # concept-name → Heroicons PNG resolver (required by server.js at startup)
    ├── package.json            # vendored runtime deps (express / pptxgenjs / sharp / react-icons)
    ├── Dockerfile              # node:22-bookworm-slim + LibreOffice + Poppler + Noto CJK + tini
    ├── SKILL.md / pptxgenjs.md / editing.md   # skill docs and pptxgenjs reference
    ├── scripts/                # helper scripts an operator can run inside the container
    └── tests/                  # smoke tests
        ├── test_image_focus_render.js   # against a live renderer: asserts image_focus embeds the image, standard does not
        └── test_local_emptiness.js      # inline check of findLargestEmptyRegion's empty-region detection
```

## Setup & Run

### Frontend (dev mode)

```bash
cd <project_root>/ANILALM
npm install                       # node_modules already present
cp .env.example .env              # edit VITE_CSP_BACKEND / VITE_DEFAULT_CHAT_MODEL as needed
npm run dev                       # http://localhost:5174
```

The dev server proxies `/api`, `/v1`, `/v2` to `VITE_CSP_BACKEND` (default `http://localhost:8000`, i.e. the myCSPPlatform backend). Make sure the backend is up first:

```bash
curl -sf http://localhost:8000/health
```

### pptx-renderer service (container)

`pptx-renderer` is defined as a service in the repo-root `docker-compose-dev.yml`:

- `build.context: ANILALM/pptx-skill`
- `expose: "7100"` — docker-network only (no host port mapping); the CSP backend connects via the service name `pptx-renderer:7100`.
- healthcheck hits `http://127.0.0.1:7100/health`.

Start it from the repo root:

```bash
cd <project_root>
docker compose -f docker-compose-dev.yml up -d pptx-renderer
```

Run locally (without compose):

```bash
cd <project_root>/ANILALM/pptx-skill
node server.js                    # listening on :7100
```

### Running the smoke tests

`test_image_focus_render.js` needs a running renderer (it exercises the full PptxGenJS pipeline and cannot inline the function under test). It defaults to `http://localhost:7100`; override with `RENDERER_URL`:

```bash
cd <project_root>/ANILALM/pptx-skill
node server.js &                                   # or use the running container
node tests/test_image_focus_render.js
RENDERER_URL=http://pptx-renderer:7100 node tests/test_image_focus_render.js
```

`test_local_emptiness.js` inlines a copy of `findLargestEmptyRegion`, so it needs no server:

```bash
node tests/test_local_emptiness.js
```

> Note: `test_local_emptiness.js` carries a copy of a `server.js` function (because importing `server.js` immediately starts a listener). If the implementation in `server.js` changes, update this copy in lockstep.

## Integration

`pptx-renderer` is not called by the frontend; it is called by the **studio module of the myCSPPlatform backend** (`myCSPPlatform/backend/app/api/studio.py`, constant `RENDERER_BASE_URL = "http://pptx-renderer:7100"`):

1. When CSP receives a Studio "generate slides" request, an LLM first produces a deck spec (each slide has `title` / `bullets` / `layout_kind`, etc.).
2. For slides marked `image_focus`, the slide's `image_ref` is hydrated into inline `image_data` (bytes) before rendering — this is where studio FLUX on-the-fly illustrations get injected into the spec.
3. CSP issues `POST {RENDERER_BASE_URL}/render` with `{ spec }` and gets back `.pptx` bytes.
4. For vision / geometric QA afterwards, CSP calls `POST /screenshots` (to get PNGs) and `POST /qa-geometric`.

On `image_focus` render behavior (guarded by `test_image_focus_render.js`): only the `image_focus` layout draws `image_data`; `standard` / `stat_callout` / `quote` / `two_column` / `icon_rows` ignore `image_data`.

> Reusability: because the renderer is a standalone HTTP service, any future caller (n8n workflow node, CLI, bot) can hit the same `/render` endpoint, while the CSP container stays Python-only without embedding Node + LibreOffice.

## Related docs

Contracts and staged specs for studio FLUX image generation (paths relative to this file):

- [`../docs/superpowers/studio-flux/ANILA_Studio_FLUX_Spec.md`](../docs/superpowers/studio-flux/ANILA_Studio_FLUX_Spec.md) — the Studio FLUX master spec (multi-stage contract, component inventory).
- `../docs/superpowers/studio-flux/specs/` — staged design docs:
  - `2026-05-21-stage2-clip-descope-vlm-ranking-design.md`
  - `2026-05-21-stage3-brand-yaml-design.md`
  - `2026-05-21-stage3-content-inferred-style-design.md`
  - `2026-05-21-stage4-illustration-routing-design.md`
- `../docs/superpowers/studio-flux/plans/` — staged implementation plans:
  - `2026-05-21-stage2-clip-descope-vlm-ranking.md`
  - `2026-05-21-stage3-content-inferred-style.md`
  - `2026-05-21-stage4-illustration-routing.md`

See also `pptx-skill/SKILL.md` and `pptx-skill/pptxgenjs.md` (the renderer's internal skill and pptxgenjs reference).

---

> 繁體中文版本：[README.md](./README.md)
>
> **Last updated**: 2026-05-26 (sync PR #16 + Phase Z 4-artifact pipeline extracted to anila-studio + add prod banner)
