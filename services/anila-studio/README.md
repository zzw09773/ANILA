# anila-studio

> 從 `myCSPPlatform/backend` 抽出的**內容生成服務**（即重構後的「產出中心／產出引擎」）：RAG 找你上傳過的文件 → LLM 生內容 → 渲染成多種產出。原本只生簡報，現為**五種 artifact**：簡報（slides）、報告（reports）、心智圖（mindmaps）、資訊圖（infographics）、資料表（datatables）。

> 中文為主版 · [English version](README.en.md)

> 🌿 **分支對照**：本服務存在於多數部署分支；精簡的 `trial-military` build 不含本服務。分支策略見根目錄 [`README.md`](../../README.md) 的分支對照表與 [`docs/branch-sync-backlog.md`](../../docs/branch-sync-backlog.md)。

---

## 在 monorepo 的位置（重構後 §17.1 版圖）

```
services/anila-studio/         ← 本服務（FastAPI，port 8100）
services/pptx-renderer/        ← 下游 Node 渲染器（.pptx / 截圖 / 幾何 QA）
apps/anilalm/                  ← 前端「我的知識庫」＋製作台，經 openapi codegen 呼叫本服務
infra/compose/platform.yml     ← compose 定義（根目錄 compose.yaml 為 shim → include 它）
```

啟停走根目錄 compose shim：`compose.yaml` → `infra/compose/platform.yml`（project `anila-platform`）；`compose.dev.yaml` → `infra/compose/dev.yml`。部署腳本在 `infra/deployment/{scripts,intranet}/`。

### 為什麼獨立

- **改渲染 / 生圖邏輯不必 rebuild csp**：dev loop 由分鐘級降到秒級。
- **單一職責**：csp 是治理中心（auth / models / ingestion / proxy 計費），anila-studio 只做內容生成。
- **HTTP-only 對外**：與 csp 之間透過 `csp_client` 走 HTTP，**不共用 DB**。服務不 import `anila_core`。

服務版本 **`0.1.0`**（`pyproject.toml` / `config.APP_VERSION` / `/health` 一致）。

---

## 重構帶來的能力（本服務相關的 Slice）

| Slice 能力 | 在本服務的落點 |
|---|---|
| **Artifact 合約 + Redis job store** | `job_store.py`：`PersistedJob` 投影寫 Redis（key 前綴 `anila-studio:jobs:`、TTL 7 天），studio **重啟後**仍能回答重啟前 job 的狀態查詢；best-effort，Redis 斷線只退化為 in-memory、不阻擋啟動。`job_reporting.py`：向 CSP 回報 `POST /v1/artifact-jobs`（建立）、`PATCH /v1/artifact-jobs/{id}`（終態 / 進度）、`POST /v1/artifacts`（產出落地）。 |
| **Full Trace spans + `/v1/traces` ingest** | `studio_trace.py`：`producer:"studio"`，一個 root `studio.job` span + 每個管線步驟一個 `studio.stage` span，批次 `POST {csp}/v1/traces/{trace_id}/spans`（≤256 spans/批）。無 `trace_id` → emitter 全程 no-op；ship 失敗 drop-and-log，永不中斷生成。 |
| **Task spine（`task_id`）** | 建立 job 的 request payload 帶 `task_id` / `source_snapshot_id` / `trace_id`，由 `job_lifecycle.py` 的 `JobReportContext` 貫串五條管線的 `*JobUpdater`，並隨 artifact-job / artifact / trace span 一起傳給 CSP。 |
| **四級分類（passthrough）** | `classification_level`（無機密／營業秘密／密／機密）由 `PersistedJob` 攜帶、`POST /v1/artifacts` 回傳、並寫入 trace span attributes；studio 不自行升降級，只做繼承傳遞。 |
| **Model Gateway** | LLM 一律走 CSP `POST /v1/chat/completions` proxy（維持 token 計費），不直連模型。 |
| **JWKS / 撤銷認證** | `jwks_client`（拉 csp JWKS + cache）＋ `revocation_cache`（Redis pub/sub + cold-start，**fail-closed**）。 |

> 上述 (2)(3) 與 CSP 回報皆 **fire-and-forget**（retry-once、log-not-raise）：CSP 掛掉絕不能弄壞生成。`STUDIO_ARTIFACT_REPORTING=false` 可整組靜音（含 spans）。跨切面協調集中在 `job_lifecycle.py`，各管線本身不變。

---

## 技術棧

- Python `>=3.11`，runtime image `python:3.11-slim`，port **8100**（compose 內僅 `expose`、不對 host 發佈）。
- 核心：FastAPI `>=0.110,<1.0`、uvicorn[standard] `>=0.27`、httpx `>=0.27,<1.0`、python-jose[cryptography] `>=3.3`（RS256/JWKS）、pydantic `>=2.6` + pydantic-settings `>=2.2`、redis `>=5.0`、cachetools `>=5.3`、numpy `>=1.26`、opencc-python-reimplemented `>=0.1.7`（簡轉繁）。
- **artifact 渲染堆疊**：jinja2 `>=3.1`、playwright `>=1.43`（headless Chromium → PDF）、pypandoc `>=1.13`（HTML → DOCX）、matplotlib `>=3.8`、openpyxl `>=3.1`（XLSX）、markdown-it-py `>=3.0`、python-docx `>=1.1`。
- dev：pytest、pytest-asyncio、respx、fakeredis、freezegun。
- Dockerfile system 套件：`graphviz`、`pandoc`、`curl`、`wget`、`ca-certificates`、`fonts-noto-cjk` + Chromium/Playwright 相依 libs + `fontconfig`；build 階段下載 Noto Sans CJK TC 單面 OTF（Regular + Bold）並 `playwright install chromium` 到 `/opt/playwright-browsers`；跑非 root user `anila`（uid 10001）。

---

## 結構

```
services/anila-studio/
├── pyproject.toml          # 核心 + artifact 渲染堆疊
├── Dockerfile              # python:3.11-slim + graphviz/pandoc/chromium/noto-cjk + non-root(anila 10001)
├── scripts/export-openapi.py    # 重 gen openapi/studio.openapi.json（唯一 script）
├── openapi/studio.openapi.json  # 3.1.0；codegen 給 ANILALM
├── app/
│   ├── main.py             # FastAPI lifespan（JWKS + 撤銷 + JobStore）+ /health readiness gate；註冊 5 個 router
│   ├── config.py · auth.py # env settings；RS256 JWT verify via JWKS + revocation cache
│   ├── api/                # studio.py reports.py mindmaps.py infographics.py datatables.py
│   ├── clients/csp_client.py    # 包 csp HTTP API
│   ├── schemas/            # studio.py report.py mindmap.py infographic.py datatable.py
│   ├── services/           # 30 模組（見下方分組）
│   └── templates/          # infographic/base.html.j2 + report/*.html.j2
└── tests/                  # 41 個 test 檔；511 收集、508 綠、3 個既有 FLUX 紅（見「測試」）
```

`services/` 分組（30 個模組）：
- **Slide 管線**：`studio_config` / `studio_retrieval` / `studio_llm` / `studio_render` / `studio_vision_qa` / `studio_layout` / `studio_job_service` / `studio_text_normalizer`（簡轉繁 s2twp + 清理）/ `llm_json`（lenient JSON 解析）。
- **FLUX 生圖**：`flux_image_provider` / `flux_prompt_rewriter` / `flux_quality_gate`（VLM ranking + FFT striping）/ `flux_style` / `diagram_renderer`（Graphviz dot→PNG）/ `geometric_qa`。
- **其他 artifact**：`report_job_service` / `report_renderer` / `report_runner`、`mindmap_job_service` / `mindmap_renderer`、`infographic_job_service` / `infographic_renderer`、`datatable_job_service` / `datatable_exporter`。
- **跨切面 job 協調（新）**：`job_lifecycle`（`JobReportContext` + `*JobUpdater` 協調）/ `job_store`（Redis `PersistedJob`）/ `job_reporting`（CSP artifact 回報）/ `studio_trace`（span emitter）。
- **Auth / infra**：`jwks_client`（拉 csp JWKS + cache）/ `revocation_cache`（Redis pub/sub + cold-start，fail-closed）。

---

## 端點（五種 artifact，皆為 async job 模式）

| Artifact | 路徑 | 下載格式 |
|---|---|---|
| Slides | `POST /api/studio/slides/jobs`（202）· `GET /…/{id}` · `GET /…/{id}/pptx` · `DELETE /…/{id}` | `.pptx` |
| Reports | `POST /api/reports/jobs` · `GET /…/{id}` · `GET /…/{id}/download/{fmt}` · `DELETE` | `html` / `pdf` / `docx` |
| Mindmaps | `POST /api/mindmaps/jobs` · `GET` · `GET /…/download/{fmt}` · `DELETE` | `svg` / `dot` |
| Infographics | `POST /api/infographics/jobs` · `GET` · `GET /…/download/{fmt}` · `DELETE` | `html` / `pdf` |
| Datatables | `POST /api/datatables/jobs` · `GET` · `GET /…/download/{fmt}` · `DELETE` | `html` / `csv` / `xlsx` |

外加 `GET /health`。`openapi/studio.openapi.json`（3.1.0）已含全部五族。

---

## 跑起來

```bash
cd services/anila-studio
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest                                    # test（不需 docker）
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8100     # 需 csp:8000 / redis / flux2-dev / pptx-renderer
# 或（於 repo 根）：docker compose up -d --build anila-studio   # → infra/compose/platform.yml
```

Health：`curl http://localhost:8100/health` → `{"status":"ok","service":"anila-studio","version":"0.1.0","ready":true,"deps":{"revocation_cache":true}}`。Lifespan startup 期間回 503 + `ready=false`（status `"degraded"`），等 JWKS + revocation cache cold-start 完才綠燈。JobStore 為 best-effort，不影響 readiness。

### 測試（511 收集 · 508 綠 · 3 既有紅）

`.venv/bin/python -m pytest` 收集 **511** 個 test（41 檔），**508 通過**、**3 個既有 FLUX 紅**——全在 `tests/test_hydrate_images.py`（`test_hydrate_image_prompt_calls_flux` / `test_cover_hero_path_generates_via_rewriter` / `test_mixed_slides_all_resolved`），為既有已知失敗、非本次 regression。測試不需 docker（csp / redis 皆以 respx / fakeredis mock）。

---

## 對 csp 的依賴

| Endpoint | 用途 |
|---|---|
| `GET /.well-known/jwks.json` | 拉 RS256 public key 驗 JWT |
| `GET /api/auth/revocations?since=` | 啟動時 cold-start sync 撤銷清單 |
| `GET /api/ingestion/collections/{id}` | collection 中介資料 |
| `POST /api/ingestion/collections/{id}/search` | RAG chunk 檢索 |
| `POST /api/ingestion/collections/{id}/images/search` | RAG image 檢索 |
| `GET /api/ingestion/images/{id}/blob` | 原始 image bytes |
| `POST /v1/chat/completions` | LLM（走 csp proxy 維持計費；**無 `/api/proxy` 前綴**） |
| `POST /v1/artifact-jobs` · `PATCH /v1/artifact-jobs/{id}` · `POST /v1/artifacts` | artifact-job / artifact 回報（Slice 8b，fire-and-forget） |
| `POST /v1/traces/{trace_id}/spans` | Full Trace span ingest（producer `studio`，fire-and-forget） |

另直連下游 `pptx-renderer`（`{RENDERER_BASE_URL}/render` · `/screenshots` · `/qa-geometric`）與 FLUX 後端。CSP 回報 / trace 的認證沿用使用者 bearer JWT（CSP 以 RS256 + JWKS 重驗，維持代理語意）；若設有 legacy `CSP_SERVICE_TOKEN` 則另帶 `X-CSP-Service-Token`。

### Redis pub/sub

訂 channel `anila:auth:token-revoke`（csp publish）。Redis 失聯時撤銷快取 **fail-closed**：`/health` 503 + 所有需 auth 的 endpoint 503，不允許「降到 TTL-only」。（JobStore 用同一 Redis，但斷線只退化為 in-memory，不 fail-closed。）

---

## 重要環境變數（取自 `config.py`）

| 變數 | 預設 | 說明 |
|---|---|---|
| `APP_NAME` / `APP_VERSION` / `LOG_LEVEL` | `anila-studio` / `0.1.0` / `INFO` | 服務識別 / log |
| `CSP_BASE_URL` | `http://csp:8000` | csp 治理中心 |
| `CSP_SERVICE_TOKEN` | `""` | 選用 s2s token；有設則 cold-start / 回報帶 `X-CSP-Service-Token` |
| `REDIS_URL` / `REDIS_REVOCATION_CHANNEL` | `redis://redis:6379/0` / `anila:auth:token-revoke` | 跟 csp 共用 Redis + 撤銷 channel |
| `JWT_KID` / `JWT_ALGORITHMS` / `JWT_LEEWAY_SECONDS` | `anila-v1` / `("RS256",)` / `60` | JWT 設定 |
| `JWKS_REFRESH_SECONDS` / `REVOCATION_CACHE_TTL_SECONDS` | `3600` / `2592000`（30 天） | JWKS 重抓 / 撤銷 deny-list TTL |
| `INTERNAL_TIMEOUT_SECONDS` / `INTERNAL_TIMEOUT_CONNECT` / `INTERNAL_LLM_TIMEOUT_SECONDS` | `30.0` / `5.0` / `300.0` | csp_client 讀 / 連線 / LLM 長逾時 |
| `FLUX_BACKEND_URL` / `RENDERER_BASE_URL` | `http://flux2-dev:8000` / `http://pptx-renderer:7100` | FLUX 後端（OpenAI 相容 Images API base URL，伺服器根或含 `/v1` 皆可）/ pptx renderer |
| `FLUX_CACHE_DIR` | `/var/anila/anila-studio-flux-cache` | FLUX cache |
| `ARTIFACTS_DIR` | `/var/anila/anila-studio-artifacts` | **report/mindmap/infographic/datatable 產出持久化根目錄**；download endpoint 由此讀回 |
| `JOB_STORE_KEY_PREFIX` / `JOB_STORE_TTL_SECONDS` | `anila-studio:jobs:` / `604800`（7 天） | Redis JobStore key 前綴 / TTL |
| `STUDIO_ARTIFACT_REPORTING` | `true` | CSP artifact-job / artifact / trace span 回報總開關 |

> 注意：部分渲染路徑直接讀 `os.environ`（`geometric_qa.py` 讀 `RENDERER_BASE_URL`、`studio_render.py` 用 `FLUX_BACKEND_URL`）；另有四個 FLUX 旋鈕**只在 env、不在 config.py**：`FLUX_MODEL`（Images API 的 model 欄位，預設 `flux.2-dev`）、`FLUX_API_KEY`（有值才帶 Bearer）、`FLUX_MAX_CONCURRENT`、`FLUX_TIMEOUT_SECONDS`。compose 內 `FLUX_BACKEND_URL` 空字串 → studio 生圖停用（內網無 FLUX）。2026-07 起 FLUX 呼叫走 OpenAI 相容 `POST {base}/v1/images/generations`（`{model, prompt, n, size, response_format:"b64_json"}` → `{created, data:[{b64_json}]}`）。

---

## Frontend（ANILALM）如何呼叫

`apps/anilalm/src/api/studio.ts` 透過 `VITE_STUDIO_BASE_URL` 指向 anila-studio。TypeScript types 從 `openapi/studio.openapi.json` codegen：

```bash
cd services/anila-studio && .venv/bin/python scripts/export-openapi.py   # 改 schema 後重出 openapi
cd ../../apps/anilalm && npm run gen:studio-types                        # → scripts/gen-studio-types.sh
```

---

## 部署注意事項

- csp 端固定使用 `secrets/jwt-private.pem`（由 csp 部署腳本預生或 Vault 注入）；anila-studio 不需 private key，只需 csp `/.well-known/jwks.json` 可達。
- anila-studio 啟動會 fail-fast 若 csp `/api/auth/revocations` 不可達（撤銷快取 fail-closed）。
- **`docker restart` 不重載 `.env`/compose；套設定一律 `up -d`。**

---

## 相關文件

- 重構設計權威：[`../../docs/anila-redesign-docs/`](../../docs/anila-redesign-docs/)（`00-product-constitution.md` 憲章、`09-api-event-contracts.md` artifact / trace 合約、`02-system-architecture.md` JobStore 失效模型）
- Studio / FLUX 主規格：[`../../docs/superpowers/studio-flux/ANILA_Studio_FLUX_Spec.md`](../../docs/superpowers/studio-flux/ANILA_Studio_FLUX_Spec.md)
- 平台整體：[`../../README.md`](../../README.md) · 分支策略：[`../../docs/branch-sync-backlog.md`](../../docs/branch-sync-backlog.md)
