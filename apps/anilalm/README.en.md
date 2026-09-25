# ANILA LM — My Knowledge Base / Output Center (`anilalm`)

> ANILA's knowledge & output SPA (Vite + React + TypeScript, v1.0.0). Hosts "**My Knowledge Base**" (collections / documents / search / chat) and the "**Output Center**" (Studio: five artifact kinds — slides / report / mindmap / infographic / datatable). Mounted at the same-origin nginx subpath `/anilalm/`, reached from ANILA Shell's four-entry navigation.

> 繁體中文原文: [`README.md`](./README.md)

> 🌿 **Branch note**: This subproject exists on `main` / `prod-intranet-card` / `prod-public-passwd` / `prod-military-passwd` / `dev-public` / `dev-military`; the slim **`trial-military`** build does **not** include it. See the root [`README.md`](../../README.md) (current line is a single `main`; the old seven-branch model is retired).
>
> Design lineage (convergence record): [`docs/anila-redesign-docs/00-product-constitution.md`](../../docs/anila-redesign-docs/00-product-constitution.md), [`01-domain-model.md`](../../docs/anila-redesign-docs/01-domain-model.md) (Task domain), [`09-api-event-contracts.md`](../../docs/anila-redesign-docs/09-api-event-contracts.md) (Task / artifact contracts). Current authority: [`PLAN.md`](../../PLAN.md) (state + order of work); spec: [`SYSTEM-MAP.md`](../../SYSTEM-MAP.md).

---

## 1. Product role

The SPA delivers a "documents → conversation → output" flow: upload to build a knowledge base → query by chat → generate artifacts. It is **frontend only**, wiring into [`services/csp`](../../services/csp/) (CSP) for auth / ingestion / conversation / LLM proxy, and [`services/anila-studio`](../../services/anila-studio/) for artifact generation.

In the ANILA product constitution (doc 00 §2) this SPA implements two first-class user entries at once — "My Knowledge Base" and "Output Center"; ANILA Shell's sidebar enters via the origin-absolute path `/anilalm`.

---

## 2. Task-first output (redesign slice 8b)

**Before every Studio output job, a CSP Task is created first**, so the resulting artifact-job / artifact / trace all bind to one governance unit (doc 09 §2 Task API).

- `src/api/tasks.ts` — `createArtifactTask()` → `POST /api/tasks` (`task_type:'generate_artifact'`, `source_scope` defaults to `'project'`, `selected_collection_ids`, `requested_output_type`). Returns `TaskBinding { taskId, sourceSnapshotId?, traceId? }`, threaded into the Studio job body. Types mirror `services/csp/app/schemas/contracts/tasks.py` and doc 01 (`TaskType` / `SourceScope` / `RequestedOutputType`).
- **Resilience contract**: any failure (endpoint not yet deployed / auth / network) returns `null` + a zh-TW `console.warn`; generation **proceeds without a task binding** — it must never break because governance metadata could not attach.
- Call sites: `src/workspace/CommandModal.tsx` (slides) and `src/studio/generators.ts` (report / mindmap / infographic / datatable) — all five kinds create a Task first.

### Every artifact is an async job

| Artifact | Submit | Poll / download |
|---|---|---|
| slides | `POST /api/studio/slides/jobs` → 202 `JobStatus` | `GET …/{id}` poll → `GET …/{id}/pptx` → `DELETE …/{id}` cancel (`src/api/studio.ts`, via `STUDIO_BASE_URL`) |
| report | `POST /api/reports/jobs` | frontend drafts a JSON spec via `chatComplete` first, then creates the job (`src/studio/generators.ts`) |
| mindmap / infographic / datatable | `POST /api/{kind}/jobs` | same job model |

`src/workspace/WSStudio.tsx`'s polling effect drives done / failed transitions and download off the artifact's `state === 'pending'`; the user can keep working while the pipeline runs.

---

## 3. Tech stack (from `package.json`)

| Module | Version |
| --- | --- |
| Build | **Vite 6.0.5 + React 18.3.1 + TypeScript 5.7.2** |
| Router | `react-router` 7.18.2 (`>=7.18.2 <8`; `BrowserRouter` + nested Outlet guards) |
| State | **Zustand 5.0.2** (auth / workspace / artifacts) |
| HTTP | `axios` 1.7.9 + interceptors (401 refresh, `withCredentials`) |
| Markdown | `marked` 14.1.3 + `DOMPurify` 3.2.3 (LLM output treated as untrusted, double-layer XSS defense) |
| Type codegen | `openapi-typescript` 7.13.0 (dev) |
| Icons | inline SVG (hand-rolled, 0 packages) |

`scripts`: `dev` / `build` (**`tsc -b && vite build`**) / `preview` / `typecheck` (`tsc -b --noEmit`) / `gen:studio-types`. **Verification gates = `typecheck` + `build`** (this subproject has no unit-test framework).

### `gen:studio-types` flow

`bash scripts/gen-studio-types.sh` runs `openapi-typescript` over `services/anila-studio/openapi/studio.openapi.json`, emitting `src/api/studio-types.gen.ts`. Re-run after anila-studio schema changes; the generated types catch studio job-contract drift at build time.

---

## 4. Layout

```
apps/anilalm/
├── package.json · Dockerfile (node:22-alpine build → nginx; ARG BASE_PATH=/anilalm/)
├── vite.config.ts (/api, /v1, /v2 → CSP; /api/studio, /api/reports, /api/mindmaps, /api/infographics, /api/datatables → Studio)
├── tsconfig*.json · index.html · .env.example · docker/ · scripts/gen-studio-types.sh
└── src/
    ├── main.tsx / App.tsx / types.ts / vite-env.d.ts
    ├── api/          # client.ts(axios + STUDIO_BASE_URL) · auth · chat · collections · conversations ·
    │                 #   documents · jobs · search · studio · studio-types.gen.ts · tasks.ts
    ├── store/        # auth.ts / workspace.ts / artifacts.ts (Zustand)
    ├── routes/       # ProtectedRoute / DashboardPage / WorkspacePage (login is the Console /login)
    ├── workspace/    # WSSidebar / WSChat / WSStudio / CommandModal / StudioWizard / ArtifactViewer /
    │                 #   ThemePicker / useJobStream
    ├── studio/       # generators.ts (5 artifact kinds, all create a Task first) / themeMapping.ts / themes.ts
    ├── theme/        # ThemeContext.tsx / tokens.ts
    ├── components/   # ErrorBoundary / Field / Icon / MarkdownPreview / Modal / Spinner / ThemeSwitch
    └── utils/format.ts
```

> **`pptx-renderer` is not part of this subproject**: the slide-rendering service now lives standalone at [`services/pptx-renderer`](../../services/pptx-renderer/) and is called server-to-server by **anila-studio** (`pptx-renderer:7100`); the frontend never calls it. See that service's own README / `SKILL.md`.

---

## 5. Run & deploy

### Local dev

```bash
cd apps/anilalm
npm install
cp .env.example .env               # edit VITE_CSP_BACKEND / VITE_ANILA_STUDIO_BACKEND as needed
npm run dev                        # http://localhost:5174
```

The dev server proxies `/api`, `/v1`, `/v2` to `VITE_CSP_BACKEND` (default `http://localhost:8000`) and `/api/studio` to `VITE_ANILA_STUDIO_BACKEND` (default `http://localhost:8100`). Confirm the backend first: `curl -sf http://localhost:8000/health`.

### Container (monorepo compose)

The SPA's compose service is **`anilalm`** (build context `apps/anilalm`, `BASE_PATH=/anilalm/`), managed by the root shim `compose.yaml` (`name: anila`) → `infra/compose/platform.yml`; dev is `compose.dev.yaml` → `infra/compose/dev.yml`. Reverse-proxied same-origin at `/anilalm/`.

```bash
docker compose -f compose.yaml up -d anilalm
# day-2 lifecycle via infra/deployment/scripts/deploy-prod.sh
```

### Verification gates

```bash
npm run typecheck      # tsc -b --noEmit
npm run build          # tsc -b && vite build (the real gate; not tsc alone)
```

---

## 6. Environment variables

| Var | Default | Purpose |
|---|---|---|
| `VITE_CSP_BACKEND` | `http://localhost:8000` | dev proxy: `/api`, `/v1`, `/v2` (CSP) |
| `VITE_ANILA_STUDIO_BACKEND` | `http://localhost:8100` | dev proxy: `/api/studio/*` (anila-studio) |
| `VITE_STUDIO_BASE_URL` | `""` | browser-visible anila-studio base (`src/api/client.ts`); empty = vite proxy / same-origin nginx |
| Console role `knowledge_chat` | — | default chat model, fetched at runtime. An explicit UI choice still wins. Unset → the screen names the role |
| `BASE_PATH` | `/anilalm/` (Dockerfile ARG) | SPA URL prefix; `vite.config.ts` reads `BASE_PATH \|\| VITE_BASE_PATH \|\| '/'` |

---

## 7. Related docs

- Design lineage (convergence record): [`../../docs/anila-redesign-docs/`](../../docs/anila-redesign-docs/) (constitution 00 / domain 01 / contracts 09). Current authority: [`PLAN.md`](../../PLAN.md) (state + order of work); spec: [`SYSTEM-MAP.md`](../../SYSTEM-MAP.md).
- Backend services: CSP [`../../services/csp/README.md`](../../services/csp/README.md) · Studio [`../../services/anila-studio/README.md`](../../services/anila-studio/README.md) · Renderer [`../../services/pptx-renderer/`](../../services/pptx-renderer/)
- Adjacent entries: task center [`../anila-shell/README.en.md`](../anila-shell/README.en.md) · governance [`../csp-governance-ui/README.en.md`](../csp-governance-ui/README.en.md)
- Platform: [`../../README.md`](../../README.md) · current `main` (old seven-branch model retired)

---

**Framework**: Vite + React + TypeScript · **Serves**: My Knowledge Base + Output Center · **Talks to**: CSP (`/api`, `/v1`, `/v2`) + anila-studio (`/api/studio/*`, `/api/{reports,mindmaps,infographics,datatables}/*`), all creating a CSP Task first.
