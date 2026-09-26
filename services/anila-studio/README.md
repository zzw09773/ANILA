# anila-studio

> 從 `myCSPPlatform/backend` 抽出的**內容生成服務**（即重構後的「產出中心／產出引擎」）：RAG 找你上傳過的文件 → LLM 生內容 → 渲染成多種產出。原本只生簡報，現為**五種 artifact**：簡報（slides）、報告（reports）、心智圖（mindmaps）、資訊圖（infographics）、資料表（datatables）。

> 中文為主版 · [English version](README.en.md)

> 🌿 **分支對照**：本服務存在於多數部署分支；精簡的 `trial-military` build 不含本服務。分支策略見根目錄 [`README.md`](../../README.md) 的分支對照表（現行單一 `main`；舊七分支模型已失效，見根目錄 README）。

---

## 在 monorepo 的位置（重構後 §17.1 版圖）

```
services/anila-studio/         ← 本服務（FastAPI，port 8100）
services/pptx-renderer/        ← 下游 Node 渲染器（.pptx / 截圖 / 幾何 QA）
apps/anilalm/                  ← 前端「我的知識庫」＋製作台，經 openapi codegen 呼叫本服務
infra/compose/platform.yml     ← compose 定義（根目錄 compose.yaml 為 shim → include 它）
```

啟停走根目錄 compose shim：`compose.yaml` → `infra/compose/platform.yml`（project `anila`）；`compose.dev.yaml` → `infra/compose/dev.yml`。部署腳本在 `infra/deployment/{scripts,intranet}/`。

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
| **trace_id** | 只當 artifact job 的關聯 id 送給 CSP，不再 POST span。 |
| **Task spine（`task_id`）** | 建立 job 的 request payload 帶 `task_id` / `source_snapshot_id` / `trace_id`，由 `job_lifecycle.py` 的 `JobReportContext` 貫串五條管線的 `*JobUpdater`，並隨 artifact-job / artifact / trace span 一起傳給 CSP。 |
| **四級分類（passthrough）** | `classification_level`（無機密／營業秘密／密／機密）由 `PersistedJob` 攜帶、`POST /v1/artifacts` 回傳、並寫入 trace span attributes；studio 不自行升降級，只做繼承傳遞。 |
| **Model Gateway** | LLM 一律走 CSP `POST /v1/chat/completions` proxy（維持 token 計費），不直連模型。 |
| **JWKS / 撤銷認證** | `jwks_client`（拉 csp JWKS + cache）＋ `revocation_cache`（Redis pub/sub + cold-start，**fail-closed**）。 |

> 上述 (2)(3) 與 CSP 回報皆 **fire-and-forget**（retry-once、log-not-raise）：CSP 掛掉絕不能弄壞生成。`STUDIO_ARTIFACT_REPORTING=false` 可整組靜音（含 spans）。跨切面協調集中在 `job_lifecycle.py`，各管線本身不變。

---

## 投影片管線要知道的事（2026-09-02 之後）

- **模型從哪裡來**：治理中心「模型角色」。簡報用 `slides`，視覺檢查用 `vision`。studio 每 60 秒問 `GET /api/models/roles/{role}`。沒設就失敗，訊息點名角色（例如「簡報模型尚未在治理中心設定」），不退回環境變數。建議簡報指到 nothink 版本：thinking 版的修正那一通會超過 300 秒上限。
- **兩段式產生**（`ANILA_STUDIO_TWO_PASS`，預設開）：先出大綱（每張的證據型態＋檢索問句）→ 每張各查一次 → 照大綱寫。大綱壞掉就退回單段式。
- **版型**：standard / section_break / stat_callout / quote / two_column / icon_rows / image_focus / **process**（流程步驟）/ **table**（原生表格）/ **sources**（結尾資料來源，管線自己寫）。每張內容頁底部有「資料來源：檔名」腳註，來自模型寫的 `[N]`。
- **品質檢查**：幾何檢查（渲染器 `/qa-geometric`，會拿到每頁版型，封面／章節頁不判留白）＋視覺檢查（每頁一通 VLM）。修正失敗不會丟掉已渲染的簡報，只加 warning。
- **成品與預覽**：`.pptx` 落在 `ARTIFACTS_DIR/slides/{job_id}.pptx`，每頁 PNG 在 `slides/{job_id}/NN.png`；`GET /api/studio/slides/jobs/{id}/preview`（清單）、`/preview/{n}`（PNG）。studio 重啟後仍可下載。
- **治理回報**：`POST /v1/artifact-jobs` 用 `requester_user_id`（＋卡片使用者的 `employee_id`）、整數 `task_id`；`POST /v1/artifacts` 仍要求綁 task 或 snapshot（csp 憲章 §6），沒有 ALM task 的簡報不會登記成 artifact。
- **繁體轉換**：OpenCC `s2tw`（只轉字形）＋一張自己維護的技術詞表；「程序」「項目」「文件」這類法規本義詞不動。

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
└── tests/                  # pytest；不需 docker
```

`services/` 分組（30 個模組）：
- **Slide 管線**：`studio_config` / `studio_retrieval` / `studio_llm` / `studio_render` / `studio_vision_qa` / `studio_layout` / `studio_job_service` / `studio_text_normalizer`（簡轉繁 s2twp + 清理）/ `llm_json`（lenient JSON 解析）。
- **圖**：`diagram_renderer`（Graphviz dot→PNG）/ `geometric_qa`。生成配圖走 CSP，不在本機模型。
- **其他 artifact**：`report_job_service` / `report_renderer` / `report_runner`、`mindmap_job_service` / `mindmap_renderer`、`infographic_job_service` / `infographic_renderer`、`datatable_job_service` / `datatable_exporter`。
- **跨切面 job 協調**：`job_lifecycle`（`JobReportContext` + `*JobUpdater` 協調）/ `job_store`（Redis `PersistedJob`）/ `job_reporting`（CSP artifact 回報）。不再上傳 span。
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
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8100     # 需 csp:8000 / redis / pptx-renderer
# 或（於 repo 根）：docker compose up -d --build anila-studio   # → infra/compose/platform.yml
```

Health：`curl http://localhost:8100/health` → `{"status":"ok","service":"anila-studio","version":"0.1.0","ready":true,"deps":{"revocation_cache":true}}`。Lifespan startup 期間回 503 + `ready=false`（status `"degraded"`），等 JWKS + revocation cache cold-start 完才綠燈。JobStore 為 best-effort，不影響 readiness。

### 測試

`PYTHONPATH=$PWD python3 -m pytest -q`。csp / redis 以 respx / fakeredis 模擬，不需 docker。生圖角色有設時打 CSP `/v1/images/generations`；沒設則不留配圖框。

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
| `GET /api/models/roles/image_generation` | 生圖角色；健康才配圖 |
| `POST /v1/images/generations` | 經 CSP 生圖（使用者憑證） |
| `POST /v1/artifact-jobs` · `PATCH /v1/artifact-jobs/{id}` · `POST /v1/artifacts` | artifact-job / artifact 回報（Slice 8b，fire-and-forget） |


另直連下游 `pptx-renderer`（`{RENDERER_BASE_URL}/render` · `/screenshots` · `/qa-geometric`）。生圖不直連模型主機。CSP 回報 / trace 的認證沿用使用者 bearer JWT（CSP 以 RS256 + JWKS 重驗，維持代理語意）；若設有 legacy `CSP_SERVICE_TOKEN` 則另帶 `X-CSP-Service-Token`。

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
| `RENDERER_BASE_URL` | `http://pptx-renderer:7100` | pptx renderer |
| 生圖 | 治理中心 `image_generation` | 有設且健康才經 CSP `/v1/images/generations` 配圖 |
| `ARTIFACTS_DIR` | `/var/anila/anila-studio-artifacts` | **report/mindmap/infographic/datatable 產出持久化根目錄**；download endpoint 由此讀回 |
| `JOB_STORE_KEY_PREFIX` / `JOB_STORE_TTL_SECONDS` | `anila-studio:jobs:` / `604800`（7 天） | Redis JobStore key 前綴 / TTL |
| `STUDIO_ARTIFACT_REPORTING` | `true` | CSP artifact-job / artifact / trace span 回報總開關 |

> 生圖不讀本機位址。角色健康時，Studio 用呼叫者的憑證打 CSP `POST /v1/images/generations`。沒設就不配生成圖片。`geometric_qa.py` 仍直接讀 `RENDERER_BASE_URL`。

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

- 重構設計沿革（收斂紀錄）：[`../../docs/anila-redesign-docs/`](../../docs/anila-redesign-docs/)（`00-product-constitution.md` 憲章、`09-api-event-contracts.md` artifact / trace 合約、`02-system-architecture.md` JobStore 失效模型）。現行權威＝[`PLAN.md`](../../PLAN.md)（現況與執行順序）、規格＝[`SYSTEM-MAP.md`](../../SYSTEM-MAP.md)。
- 平台整體：[`../../README.md`](../../README.md) · 現行 `main`（舊七分支模型已失效）
