# pptx-renderer

> The **slide-rendering backend** for ANILA's Artifact Center (`產出中心`): a Node HTTP service wrapping [PptxGenJS](https://gitbrent.github.io/PptxGenJS/) + LibreOffice headless. anila-studio's slide pipeline posts a structured spec here and gets back a real `.pptx`, turns a `.pptx` into screenshots, and runs deterministic geometric QA. It is purely server-to-server and is not exposed to end users.

> 中文版本：[`README.md`](./README.md)

> 🌿 **Branch note**: This service is content-identical across ANILA deployment branches. See the root [`README.md`](../../README.md) branch matrix and [`docs/branch-sync-backlog.md`](../../docs/branch-sync-backlog.md).

---

## Where it sits in the monorepo (post-redesign §17.1 layout)

```
services/pptx-renderer/        ← this service (Node 22 + LibreOffice + Poppler, port 7100)
services/anila-studio/         ← the only upstream caller (the Artifact Center engine)
infra/compose/platform.yml     ← compose definition (root compose.yaml is a shim that includes it)
```

Bring-up goes through the root compose shim: `compose.yaml` → `infra/compose/platform.yml` (project `anila-platform`). In compose this service only `expose`s 7100 and is **not host-published** — it is anila-studio's internal rendering backend, not a "project entry" (`專案入口`) GUI service that would go through the Service Registry / Launch Token.

---

## Endpoints (`server.js`)

| Method / Path | Body | Response |
|---|---|---|
| `GET /health` | — | `text/plain` `ok` |
| `POST /render` | `{ spec }` | `.pptx` binary (octet-stream) |
| `POST /screenshots` | `{ pptxPath \| pptxBase64 }` | `{ images: [{ index, mime, base64 }] }` (per-slide PNG) |
| `POST /qa-geometric` | `{ pptxBase64 }` | `{ defects: [{ slide_index, ... }] }` |

- **`/render`**: `spec.slides` must be a non-empty array (else 400); more than `MAX_SLIDES` (60) returns 413. Theme resolution priority is `spec.theme` → legacy `spec.palette` → default `corporate_navy` (the upstream CSP schema validator maps palette→theme first; direct callers may still send a legacy palette).
- **`/screenshots`**: LibreOffice headless (`.pptx` → PDF) → Poppler (PDF → PNG). When given `pptxPath` there is a path-traversal guard — only files that resolve under `TMP_ROOT` are accepted; alternatively supply `pptxBase64` inline.
- **`/qa-geometric`**: unzips the `.pptx`, parses each `ppt/slides/slideN.xml` shape geometry, and returns a deterministic list of layout defects (overlaps / bleed / empty regions, etc.) for studio's vision-QA loop (no LLM needed).
- Listens on `0.0.0.0:PORT` (`PORT` defaults to 7100).

> Inputs are deliberately schema-light: the upstream (CSP / studio) has already Pydantic-validated the structure before calling here; this service only checks **what can actually break** — payload size (`MAX_PAYLOAD` 10 MB), the slide-count cap, and `/screenshots` path traversal.

---

## Stack & dependencies

- Runtime: Node **22** (`node:22-bookworm-slim`). `package.json` version `0.1.0` (name `anilalm-pptx-skill`).
- Direct deps (`package.json`): `express ^5.2.1`, `pptxgenjs ^3.12.0`, `react ^18.3.1` + `react-dom ^18.3.1` + `react-icons ^5.4.0` (for `icons.js`'s Heroicons resolver), `sharp ^0.33.5` (raster image work).
- **Vendored node_modules (air-gap friendly)**: this service **intentionally commits `node_modules` into git** (see the `.gitignore` note) so intranet / air-gapped builds never touch npm. The Dockerfile installs from the pinned lockfile with `npm ci --omit=dev`. To refresh: `rm -rf node_modules package-lock.json && npm install`, then commit.
- System deps (Dockerfile): `libreoffice-core` + `libreoffice-impress` (`.pptx` → PDF), `poppler-utils` (PDF → PNG), `fonts-noto-cjk` + `fonts-noto-cjk-extra` (CJK glyphs, otherwise Chinese renders as ????), `tini` (PID-1 reaper so SIGTERM propagates cleanly to the soffice child, avoiding zombies that wedge container restart), `ca-certificates`.

---

## Layout

```
services/pptx-renderer/
├── Dockerfile          # node:22-bookworm-slim + libreoffice + poppler + noto-cjk + tini; npm ci --omit=dev
├── server.js           # Express app: /health /render /screenshots /qa-geometric (the single entry point)
├── icons.js            # concept → Heroicons PNG resolver (required at server.js startup; missing it → MODULE_NOT_FOUND)
├── package.json · package-lock.json
├── scripts/            # office helpers (pack/unpack/validate/soffice, OOXML schemas, python thumbnail/add_slide/clean)
├── tests/              # 4 node smoke tests (run manually, see below)
├── SKILL.md · pptxgenjs.md · editing.md   # reference docs for generating / editing pptx (introspectable in-container)
└── LICENSE.txt
```

---

## Run & test

```bash
# In the stack (recommended) — root compose shim
docker compose up -d --build pptx-renderer      # → infra/compose/platform.yml

# Run it directly
cd services/pptx-renderer
npm ci --omit=dev        # if the vendored node_modules is absent
node server.js           # listens on :7100 (PORT overridable)
curl http://localhost:7100/health   # → ok
```

### Testing (manual node smoke tests, not a CI suite)

`tests/` holds 4 node scripts, each run with `node tests/<file>.js`; most need a **running renderer** (server.js or a container, addressed via `RENDERER_URL`):

| Test | What it checks |
|---|---|
| `test_cover_hero_guard.js` | only `image_gen_meta.use_case === 'cover_hero'` promotes an image to a full-bleed title-slide background |
| `test_hierarchy_bullets.js` | hierarchical bullet markers (●/◦/▪ → indentLevel 0/1/2) parse correctly |
| `test_image_focus_render.js` | `image_data` renders on `image_focus` layouts but NOT on `standard` |
| `test_local_emptiness.js` | `findLargestEmptyRegion` empty-region classification (**inlines a copy** of the function; needs no server) |

---

## Environment variables

| Variable | Default | Notes |
|---|---|---|
| `PORT` | `7100` | HTTP listen port |
| `PPTX_TMP_DIR` | `/tmp/pptx-out` in code; Dockerfile sets `/var/anila/pptx-out` | scratch dir for `.pptx` / conversion work; also the `/screenshots` path-traversal allow-list root |
| `NODE_ENV` | `production` (Dockerfile) | Node environment |

---

## Relationship to other services

- **anila-studio (the only upstream)**: the slide pipeline `studio_render.py` `POST {RENDERER_BASE_URL}/render` for the `.pptx`; vision-QA uses `/screenshots`; `geometric_qa.py` `POST /qa-geometric` for the defect list. `RENDERER_BASE_URL` defaults to `http://pptx-renderer:7100`.
- **No DB, no auth**: purely internal server-to-server; compose does not publish a host port, so only the in-stack anila-studio calls it over the docker network. It does **not** participate in CSP's JWKS / revocation / Task / Trace / classification machinery — those all happen upstream in studio before a spec is handed here.

> Redesign context: this service is the Artifact Center's engine for materialising slide specs into files and screenshots; its only Slice ties are the §17.1 layout and the compose shim. It is **not** a Service Registry "project entry" (`專案入口`) GUI service (those register via in-house PKI-card SSO + Launch Token + iframe policy).

---

## Related docs

- `SKILL.md` / `pptxgenjs.md` / `editing.md`: technical reference for generating and editing `.pptx`.
- Upstream engine: [`../anila-studio/README.en.md`](../anila-studio/README.en.md)
- Redesign design lineage (convergence record): [`../../docs/anila-redesign-docs/`](../../docs/anila-redesign-docs/) (`00-product-constitution.md`). Current authority: [`PLAN.md`](../../PLAN.md) (state + order of work); spec: [`SYSTEM-MAP.md`](../../SYSTEM-MAP.md).
- Platform overview: [`../../README.md`](../../README.md) · Branch strategy: [`../../docs/branch-sync-backlog.md`](../../docs/branch-sync-backlog.md)
