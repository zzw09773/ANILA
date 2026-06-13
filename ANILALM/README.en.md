# ANILA LM (ANILALM)

> Knowledge-base frontend + Studio entry: a research-notes-style SPA, plus a standalone `pptx-renderer` microservice that turns a deck spec into `.pptx`.

> 中文版本：[README.md](./README.md)

> 🌿 **Branch note**: This subproject exists on `main` / `prod-intranet-card` / `prod-public-passwd` / `prod-military-passwd` / `dev-public` / `dev-military`. **The `trial-military` slim build does not include it** (knowledge-base management SPA and Studio deck generation removed). See the root [`README.md`](../README.md) branch matrix and [`docs/branch-sync-backlog.md`](../docs/branch-sync-backlog.md).

---

## Overview

**ANILALM** offers researchers a one-stop "documents → conversation → output" interface: upload PDFs / documents → build a knowledge base → query by conversation → generate in-depth reports and slide drafts. It is an SPA wiring into myCSPPlatform for auth, ingestion, conversations and LLM proxy, with deck generation going through [`anila-studio`](../anila-studio/).

It contains two independent execution units:

1. **The top-level ANILALM app** — a Vite + React + TypeScript frontend, built to static files served by nginx, mounted at the nginx `/anilalm/` subpath.
2. **`pptx-skill/` (the pptx-renderer service)** — a standalone Node service (`server.js`) using Express + pptxgenjs to render a deck spec into `.pptx`. On the docker network it is reachable as `pptx-renderer:7100`, with endpoints:
   - `POST /render` — takes `{ spec }`, returns the `.pptx` binary (octet-stream).
   - `POST /screenshots` — takes a `.pptx` (base64 or server path), LibreOffice headless → PDF → Poppler → PNG, returning per-slide images for vision QA.
   - `POST /qa-geometric` — geometric layout checks on rendered slides (overlap, largest empty region).
   - `GET /health` — returns `ok`.

> The frontend never calls the renderer directly; the renderer is called by anila-studio.

---

## Architecture & Stack

### Top-level ANILALM app (frontend)

| Module | Choice |
| --- | --- |
| Build | Vite 6 + React 18 + TypeScript 5.7 |
| Routing | react-router-dom v6 (`BrowserRouter` + nested Outlet guards) |
| State | Zustand (auth / workspace / artifacts) |
| HTTP | axios + interceptors (401 token refresh, `withCredentials`) |
| Markdown | marked + DOMPurify (LLM output treated as untrusted; two-layer XSS defense) |
| Icons | inline SVG (custom set, 0 packages) |

npm scripts: `dev` (vite), `build` (`tsc -b && vite build`), `preview`, `typecheck`, `gen:studio-types`. Runtime image (top-level `Dockerfile`): multi-stage, `node:22-alpine` build → `nginx:1.27-alpine` serving `dist/`; `BASE_PATH` build-arg defaults to `/anilalm/`.

### pptx-skill / pptx-renderer service

| Dependency | Use |
| --- | --- |
| `express` ^5 | HTTP server |
| `pptxgenjs` ^3.12 | generates `.pptx` |
| `sharp` ^0.33 | image processing |
| `react` / `react-dom` / `react-icons` | icon resolution (`icons.js` maps concept names to Heroicons PNGs) |

`pptx-skill/Dockerfile` uses `node:22-bookworm-slim` (not alpine) and adds `libreoffice-core` / `libreoffice-impress` (`.pptx → PDF`), `poppler-utils` (PDF → PNG), `fonts-noto-cjk` (CJK fonts), and `tini` (PID-1 reaper so SIGTERM reaches the soffice child). `node_modules` is vendored (`npm ci --omit=dev`, air-gap build). Defaults `PORT=7100`, `PPTX_TMP_DIR=/var/anila/pptx-out`. `server.js` is schema-light (CSP has already done Pydantic validation); it only checks payload size (`MAX_PAYLOAD=10mb`), slide count (`MAX_SLIDES=60`), and the `/screenshots` path (anti-traversal).

---

## Layout

```
ANILALM/
├── package.json                # frontend: react / axios / zustand / marked / dompurify / react-router
├── Dockerfile                  # frontend image: Vite build → nginx
├── vite.config.ts              # /api, /v1, /v2 proxy to VITE_CSP_BACKEND
├── index.html
├── docker/                     # nginx.conf and deployment config
├── _design/                    # old prototypes (kept for design reference, not built)
├── src/
│   ├── main.tsx / App.tsx      # createRoot + ThemeProvider + BrowserRouter
│   ├── api/                    # axios client + auth/collections/documents/jobs/conversations/chat/studio
│   ├── store/                  # auth.ts / workspace.ts / artifacts.ts (Zustand)
│   ├── routes/                 # ProtectedRoute / LoginPage / DashboardPage / WorkspacePage
│   ├── workspace/              # WSSidebar / WSChat / WSStudio / CommandModal / ArtifactViewer / useJobStream
│   ├── studio/generators.ts    # generateReport / generateSlides
│   ├── theme/ · components/ · utils/format.ts
└── pptx-skill/                 # ── standalone pptx-renderer service ──
    ├── server.js               # Express: /render /screenshots /qa-geometric /health (port 7100)
    ├── icons.js                # concept name → Heroicons PNG resolver
    ├── package.json            # vendored runtime deps
    ├── Dockerfile              # node:22-bookworm-slim + LibreOffice + Poppler + Noto CJK + tini
    ├── SKILL.md / pptxgenjs.md / editing.md
    ├── scripts/
    └── tests/                  # smoke tests (test_image_focus_render.js / test_local_emptiness.js)
```

---

## Setup & Run

### Frontend (dev)

```bash
cd ANILALM
npm install
cp .env.example .env              # adjust VITE_CSP_BACKEND / VITE_DEFAULT_CHAT_MODEL as needed
npm run dev                       # http://localhost:5174
```

The dev server proxies `/api`, `/v1`, `/v2` to `VITE_CSP_BACKEND` (default `http://localhost:8000`). Ensure the backend is up first: `curl -sf http://localhost:8000/health`.

### pptx-renderer service (container)

`pptx-renderer` is defined in the repo-root `docker-compose-dev.yml`: `build.context: ANILALM/pptx-skill`, `expose: "7100"` (no host port; reached by anila-studio as `pptx-renderer:7100`), healthcheck `http://127.0.0.1:7100/health`.

```bash
cd <repo_root> && docker compose -f docker-compose-dev.yml up -d pptx-renderer
# or locally: cd ANILALM/pptx-skill && node server.js   # :7100
```

### Smoke tests

```bash
cd ANILALM/pptx-skill
node server.js &                                   # or a running container
node tests/test_image_focus_render.js              # needs a live renderer (full PptxGenJS pipeline)
RENDERER_URL=http://pptx-renderer:7100 node tests/test_image_focus_render.js
node tests/test_local_emptiness.js                 # inlines findLargestEmptyRegion, no server needed
```

> `test_local_emptiness.js` embeds a copy of a `server.js` function (importing server.js would start a listener); if `server.js` changes, update the copy.

---

## Integration

`pptx-renderer` is not called by the frontend; it is called by the **[`anila-studio`](../anila-studio/) service** (extracted from csp on 2026-05-23, PR #12). The frontend points at anila-studio via `VITE_STUDIO_BASE_URL`:

1. After receiving a deck-generation request, anila-studio first runs an LLM via csp `/api/proxy/v1/chat/completions` to produce a deck spec (each slide has `title` / `bullets` / `layout_kind` etc.).
2. Slides marked `image_focus` have their `image_ref` hydrated into inline `image_data` (bytes) before rendering — studio FLUX's just-in-time illustrations are injected here; anila-studio fetches raw image bytes via `GET /api/ingestion/images/{id}/blob`.
3. anila-studio `POST {RENDERER_BASE_URL}/render` with `{ spec }`, getting back `.pptx` bytes.
4. For vision / geometric QA it then calls `POST /screenshots` (PNGs) and `POST /qa-geometric`.

`image_focus` render behaviour (guarded by `test_image_focus_render.js`): only the `image_focus` layout draws `image_data`; `standard` / `stat_callout` / `quote` / `two_column` / `icon_rows` all ignore it.

> Reusability: since the renderer is a standalone HTTP service, any future caller (n8n node, CLI, bot) can hit the same `/render`, and the CSP container stays Python-only with no embedded Node + LibreOffice.

---

## Related docs

- Studio FLUX spec: [`../docs/superpowers/studio-flux/ANILA_Studio_FLUX_Spec.md`](../docs/superpowers/studio-flux/ANILA_Studio_FLUX_Spec.md) (multi-stage contract, component inventory)
- Stage designs / plans: `../docs/superpowers/studio-flux/specs/`, `../docs/superpowers/studio-flux/plans/`
- anila-studio service: [`../anila-studio/README.md`](../anila-studio/README.md)
- Platform: [`../README.md`](../README.md) · Branch strategy: [`../docs/branch-sync-backlog.md`](../docs/branch-sync-backlog.md)
- renderer internals: `pptx-skill/SKILL.md`, `pptx-skill/pptxgenjs.md`
