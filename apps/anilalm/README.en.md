# ANILA LM (ANILALM)

> Knowledge-base frontend + Studio entry: a research-notes-style SPA (Vite + React + TS), plus a standalone `pptx-renderer` (pptx-skill) microservice that renders a deck spec into `.pptx`.

> 中文版本：[README.md](./README.md)

> 🌿 **Branch note**: This subproject exists on `main` / `prod-intranet-card` / `prod-public-passwd` / `prod-military-passwd` / `dev-public` / `dev-military`. **The `trial-military` slim build does not include it.** See the root [`README.md`](../../README.md) branch matrix and [`docs/branch-sync-backlog.md`](../../docs/branch-sync-backlog.md).

---

## Overview

**ANILALM** offers a one-stop "documents → conversation → output" interface: upload → build a knowledge base → query by conversation → generate in-depth reports and multiple artifacts. It is an SPA wiring into services/csp (CSP) for auth / ingestion / conversations / LLM proxy, with artifact generation going through [`anila-studio`](../../services/anila-studio/) (five kinds: slides / reports / mindmaps / infographics / datatables). It mounts at the nginx `/anilalm/` subpath.

It contains two independent execution units:

1. **The top-level ANILALM app** (package `anilalm` v1.0.0) — a Vite + React + TS frontend.
2. **The `pptx-renderer` service** (package `anilalm-pptx-skill` v0.1.0; formerly `ANILALM/pptx-skill`, now at [`services/pptx-renderer`](../../services/pptx-renderer/)) — a standalone Node/Express service (`server.js`) using pptxgenjs + headless LibreOffice + Poppler to render a deck spec into `.pptx`. On the docker network it is reachable as `pptx-renderer:7100`, **called server-to-server by anila-studio; the frontend never calls it directly**. Endpoints:
   - `GET /health` → `ok`
   - `POST /render` — body `{ spec }`, returns the `.pptx` binary (with `X-Pptx-Job-Id` / `X-Pptx-Path` headers; empty `spec.slides` → 400, over `MAX_SLIDES` → 413)
   - `POST /screenshots` — body `{ pptxPath }` or `{ pptxBase64 }` (path must be under TMP_ROOT, anti-traversal); soffice→PDF→pdftoppm(`-r 96`)→PNG, returns `{ images:[{index,mime,base64}] }`
   - `POST /qa-geometric` — body `{ pptxBase64 }`; JSZip unzip + slide XML parse, returns `{ defects:[{slide_index,severity,kind,detail}] }`

---

## Architecture & Stack

### Top-level ANILALM app (frontend)

| Module | Version (`package.json`) |
| --- | --- |
| Build | Vite 6.0.5 + React 18.3.1 + TypeScript 5.7.2 |
| Routing | react-router-dom 6.28.0 (`BrowserRouter` + nested Outlet guards) |
| State | Zustand 5.0.2 (auth / workspace / artifacts) |
| HTTP | axios 1.7.9 + interceptors (401 token refresh, `withCredentials`) |
| Markdown | marked 14.1.3 + DOMPurify 3.2.3 (LLM output treated as untrusted; two-layer XSS defense) |
| Type codegen | openapi-typescript 7.13.0 (dev) |
| Icons | inline SVG (custom set, 0 packages) |

Scripts: `dev` (vite) / `build` (`tsc -b && vite build`) / `preview` / `typecheck` / `gen:studio-types` (`bash scripts/gen-studio-types.sh`, runs `openapi-typescript` against `../../services/anila-studio/openapi/studio.openapi.json` → `src/api/studio-types.gen.ts`). Runtime image: `node:22-alpine` build (`npm install`) → `nginx:1.27-alpine` serving `dist/`; `ARG BASE_PATH=/anilalm/`, `ARG VITE_DEFAULT_CHAT_MODEL=gpt-4o-mini`; `EXPOSE 80`, healthcheck wget `/health`.

### pptx-renderer service (`services/pptx-renderer`, formerly `pptx-skill/`)

| Dependency | Version |
| --- | --- |
| `express` | ^5.2.1 |
| `pptxgenjs` | ^3.12.0 |
| `sharp` | ^0.33.5 (raster handling for the Heroicons PNGs loaded by `icons.js`) |
| `react` / `react-dom` / `react-icons` | ^18.3.1 / ^18.3.1 / ^5.4.0 (`icons.js` concept name → Heroicons PNG) |

> `jszip` (used by `/qa-geometric`) is `require`d but not listed in `package.json`; it resolves via the lockfile / transitively.

`Dockerfile`: base `node:22-bookworm-slim` (not alpine), using **`npm ci --omit=dev`** (not a vendored COPY). apt packages: `libreoffice-core` / `libreoffice-impress` (`.pptx → PDF`), `poppler-utils` (PDF → PNG), `fonts-noto-cjk` + **`fonts-noto-cjk-extra`**, `tini` (PID-1 reaper so SIGTERM reaches soffice), `ca-certificates`. ENV `PORT=7100`, `PPTX_TMP_DIR=/var/anila/pptx-out` (the `server.js` code fallback is `/tmp/pptx-out`). `MAX_PAYLOAD=10mb`, `MAX_SLIDES=60`. `server.js` is schema-light (CSP already did Pydantic validation); it only checks payload size / slide count / `/screenshots` path.

---

## Layout

```
apps/anilalm/
├── package.json                # anilalm v1.0.0: react / axios / zustand / marked / dompurify / react-router
├── Dockerfile                  # frontend image: node:22-alpine (npm install) → nginx
├── vite.config.ts              # /api, /v1, /v2 → VITE_CSP_BACKEND; /api/studio → VITE_ANILA_STUDIO_BACKEND
├── tsconfig*.json · index.html · docker/(nginx.conf) · _design/ · scripts/gen-studio-types.sh
├── src/
│   ├── main.tsx / App.tsx / types.ts / vite-env.d.ts
│   ├── api/                    # client.ts(axios + STUDIO_BASE_URL) · auth · chat · collections ·
│   │                           #   conversations · documents · jobs · search · studio · studio-types.gen.ts
│   ├── store/                  # auth.ts / workspace.ts / artifacts.ts (Zustand)
│   ├── routes/                 # ProtectedRoute / LoginPage / DashboardPage / WorkspacePage
│   ├── workspace/              # WSSidebar / WSChat / WSStudio / CommandModal / ArtifactViewer /
│   │                           #   StudioWizard / ThemePicker / useJobStream
│   ├── studio/                 # generators.ts / themeMapping.ts / themes.ts
│   ├── theme/                  # ThemeContext.tsx / tokens.ts
│   ├── components/             # ErrorBoundary / Field / Icon / MarkdownPreview / Modal / Spinner / ThemeSwitch
│   └── utils/format.ts
└── README.md / README.en.md

services/pptx-renderer/         # ── standalone pptx-renderer service (formerly ANILALM/pptx-skill) ──
├── server.js                   # Express: /render /screenshots /qa-geometric /health (port 7100)
├── icons.js · package.json (anilalm-pptx-skill v0.1.0)
├── Dockerfile                  # node:22-bookworm-slim + npm ci + LibreOffice + Poppler + Noto CJK(+extra) + tini
├── SKILL.md / pptxgenjs.md / editing.md
├── scripts/                    # Python helpers (add_slide / clean / thumbnail + office/)
└── tests/                      # 4 files: test_cover_hero_guard / test_hierarchy_bullets /
                                #   test_image_focus_render / test_local_emptiness
```

---

## Setup & Run

### Frontend (dev)

```bash
cd apps/anilalm
npm install
cp .env.example .env              # adjust VITE_CSP_BACKEND / VITE_ANILA_STUDIO_BACKEND / VITE_DEFAULT_CHAT_MODEL
npm run dev                       # http://localhost:5174
```

The dev server proxies `/api`, `/v1`, `/v2` to `VITE_CSP_BACKEND` (default `http://localhost:8000`) and `/api/studio` to `VITE_ANILA_STUDIO_BACKEND` (default `http://localhost:8100`). Ensure the backend is up: `curl -sf http://localhost:8000/health`.

### pptx-renderer service (container)

Defined in the repo-root shim `compose.dev.yaml` (actual file `infra/compose/dev.yml`): `build.context: ../../services/pptx-renderer`, `expose: "7100"` (no host port; reached by anila-studio as `pptx-renderer:7100`), healthcheck `http://127.0.0.1:7100/health`.

```bash
cd <repo_root> && docker compose -f compose.dev.yaml up -d pptx-renderer
# or locally: cd services/pptx-renderer && node server.js   # :7100
```

> The production stack (`compose.yaml`, not `-dev`) also manages both `pptx-renderer` and `anilalm` (anila-studio connects via `RENDERER_BASE_URL=http://pptx-renderer:7100`); just drop the `-f compose.dev.yaml` flag to target the default compose.

### Smoke tests

```bash
cd services/pptx-renderer
node tests/test_image_focus_render.js        # needs a live renderer; RENDERER_URL can override
node tests/test_local_emptiness.js           # inline, no server needed
```

---

## Environment variables

| Variable | Default | Notes |
|---|---|---|
| `VITE_CSP_BACKEND` | `http://localhost:8000` | dev proxy: `/api`, `/v1`, `/v2` (CSP) |
| `VITE_ANILA_STUDIO_BACKEND` | `http://localhost:8100` | dev proxy: `/api/studio/*` (anila-studio) |
| `VITE_STUDIO_BASE_URL` | `""` | browser-visible anila-studio base (`src/api/client.ts`); empty = vite proxy / same-origin nginx |
| `VITE_DEFAULT_CHAT_MODEL` | `gpt-4o-mini` | default chat model (`src/studio/generators.ts` + Dockerfile ARG) |
| `BASE_PATH` | `/anilalm/` (Dockerfile ARG; commented in `.env.example`) | SPA URL prefix; `vite.config.ts` reads `BASE_PATH \|\| VITE_BASE_PATH \|\| '/'` |

---

## Integration

The frontend talks only to **CSP** (`/api`, `/v1`, `/v2`) and **anila-studio** (`/api/studio/*` and `/api/{reports,mindmaps,infographics,datatables}/*`); `pptx-renderer` is called server-to-server by **anila-studio** (there is no renderer reference anywhere in the frontend `src/`). The frontend reaches studio via `VITE_STUDIO_BASE_URL` (`studioFetch` in `src/api/studio.ts`).

All artifacts are async-job: `POST /api/{kind}/jobs` (slides returns 202 + JobStatus) → poll `GET …/{id}` → `GET …/{id}/download/{fmt}` (slides uses `/pptx`) → `DELETE …/{id}` to cancel. Slides' `generateSlides` also does a frontend `chatComplete` to draft a JSON spec; report/mindmap/infographic/datatable are pure backend jobs.

`image_focus` rendering (`server.js`): the `image_focus` layout draws `image_data` left, bullets right; `standard` / `stat_callout` / `quote` / `two_column` / `icon_rows` ignore `image_data`. Exceptions: `section_break` and the auto-prepended cover (`image_gen_meta.use_case==='cover_hero'`) consume `image_data` as a full-bleed hero.

> Reusability: the renderer is a standalone HTTP service, so any future caller (n8n / CLI / bot) can hit the same `/render`, keeping the CSP container Python-only.

---

## Related docs

- Studio FLUX spec: [`../../docs/superpowers/studio-flux/ANILA_Studio_FLUX_Spec.md`](../../docs/superpowers/studio-flux/ANILA_Studio_FLUX_Spec.md)
- Stage designs / plans: `../../docs/superpowers/studio-flux/specs/`, `../../docs/superpowers/studio-flux/plans/`
- anila-studio service: [`../../services/anila-studio/README.md`](../../services/anila-studio/README.md)
- Platform: [`../../README.md`](../../README.md) · Branch strategy: [`../../docs/branch-sync-backlog.md`](../../docs/branch-sync-backlog.md)
- renderer internals: `../../services/pptx-renderer/SKILL.md`, `../../services/pptx-renderer/pptxgenjs.md`
