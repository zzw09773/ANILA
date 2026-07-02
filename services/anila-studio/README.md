# anila-studio

> 從 `myCSPPlatform/backend` 抽出的**內容生成服務**：RAG 找你上傳過的文件 → LLM 生內容 → 渲染成多種產出。原本只生簡報，現已長成**五種 artifact**：簡報（slides）、報告（reports）、心智圖（mindmaps）、資訊圖（infographics）、資料表（datatables）。

> 中文為主版 · [English version](README.en.md)

> 🌿 **分支對照**：本服務存在於 `main` / `prod-intranet-card` / `prod-public-passwd` / `prod-military-passwd` / `dev-public` / `dev-military`。**`trial-military` 精簡版不含本服務**。分支策略見根目錄 [`README.md`](../README.md) 的分支對照表與 [`docs/branch-sync-backlog.md`](../docs/branch-sync-backlog.md)。

---

## 為什麼獨立

- **改渲染 / 生圖邏輯不必 rebuild csp**：dev loop 由分鐘級降到秒級。
- **單一職責**：csp 是 control plane（auth / models / ingestion / proxy 計費），anila-studio 只做內容生成。
- **HTTP-only 對外**：與 csp 之間透過 `csp_client` 走 HTTP，**不**共用 DB。

服務版本 **`0.1.0`**（`pyproject.toml` / `config.APP_VERSION` / `/health` 一致）。抽出決策見 [`docs/superpowers/anila-studio/extraction-decision.md`](../docs/superpowers/anila-studio/extraction-decision.md)（2026-05-23 / PR #12）。

---

## 技術棧

- Python `>=3.11`，runtime image `python:3.11-slim`，port **8100**。
- 核心：FastAPI `>=0.110,<1.0`、uvicorn[standard] `>=0.27`、httpx `>=0.27,<1.0`、python-jose[cryptography] `>=3.3`（RS256/JWKS）、pydantic `>=2.6` + pydantic-settings `>=2.2`、redis `>=5.0`、cachetools `>=5.3`、numpy `>=1.26`、opencc-python-reimplemented `>=0.1.7`。
- **artifact 渲染堆疊**（新增）：jinja2 `>=3.1`、playwright `>=1.43`（Chromium → PDF）、pypandoc `>=1.13`、matplotlib `>=3.8`、openpyxl `>=3.1`、markdown-it-py `>=3.0`、python-docx `>=1.1`。
- dev：pytest、pytest-asyncio、respx、fakeredis、freezegun。
- Dockerfile system 套件：`graphviz`、`pandoc`、`curl`、`wget`、`ca-certificates`、`fonts-noto-cjk` + Chromium/Playwright 相依 libs + `fontconfig`；build 時下載 Noto Sans CJK TC OTF 字型並 `playwright install chromium` 到 `/opt/playwright-browsers`；非 root user `anila`（uid 10001）。

---

## 結構

```
anila-studio/
├── pyproject.toml          # 核心 + artifact 渲染堆疊
├── Dockerfile              # python:3.11-slim + graphviz/pandoc/chromium/noto-cjk + non-root
├── scripts/export-openapi.py    # 重 gen openapi/studio.openapi.json（唯一 script）
├── openapi/studio.openapi.json  # 3.1.0；codegen 給 ANILALM
├── app/
│   ├── main.py             # FastAPI lifespan + /health readiness gate；註冊 5 個 router
│   ├── config.py · auth.py # env settings；RS256 JWT verify via JWKS + revocation cache
│   ├── api/                # studio.py reports.py mindmaps.py infographics.py datatables.py
│   ├── clients/csp_client.py    # 包 csp HTTP API
│   ├── schemas/            # studio.py report.py mindmap.py infographic.py datatable.py
│   ├── services/           # 26 模組（見下方分組）
│   └── templates/          # infographic/base.html.j2 + report/*.html.j2
└── tests/                  # 40 個 test 檔，約 451 個 test function
```

`services/` 分組（26 個）：
- **Slide 管線**：`studio_config` / `studio_retrieval` / `studio_llm` / `studio_render` / `studio_vision_qa` / `studio_layout` / `studio_job_service` / `studio_text_normalizer`（簡轉繁 s2twp + 清理）/ `llm_json`（lenient JSON 解析）。
- **FLUX 生圖**：`flux_image_provider` / `flux_prompt_rewriter` / `flux_quality_gate`（VLM ranking + FFT striping）/ `flux_style` / `diagram_renderer`（Graphviz dot→PNG）/ `geometric_qa`。
- **Auth / infra**：`jwks_client`（拉 csp JWKS + cache）/ `revocation_cache`（Redis pub/sub + cold-start，fail-closed）。
- **其他 artifact**：`report_job_service` / `report_renderer` / `report_runner`、`mindmap_job_service` / `mindmap_renderer`、`infographic_job_service` / `infographic_renderer`、`datatable_job_service` / `datatable_exporter`。

---

## 端點（5 種 artifact，皆為 async job 模式）

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
cd anila-studio
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest                                            # test（不需 docker）
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8100   # 需 csp:8000 / redis:6379 / flux2-dev / pptx-renderer
# 或：docker compose -f docker-compose-dev.yml up -d --build anila-studio
```

Health：`curl http://localhost:8100/health` → `{"status":"ok","service":"anila-studio","version":"0.1.0","ready":true,"deps":{"revocation_cache":true}}`。Lifespan startup 期間回 503 + `ready=false`（status `"degraded"`），等 JWKS + revocation cache cold-start 完才綠燈。

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
| `POST /v1/chat/completions` | LLM（走 csp proxy 維持計費；**注意：無 `/api/proxy` 前綴**） |

另直連外部 `pptx-renderer`（`{RENDERER_BASE_URL}/render` / `/screenshots` / `/qa-geometric`）與 FLUX 後端。

### Redis pub/sub

訂 channel `anila:auth:token-revoke`（csp publish）。Redis 失聯時 **fail-closed**：`/health` 503 + 所有需 auth 的 endpoint 503，不允許「降到 TTL-only」（plan v2 R14）。

---

## 重要環境變數（取自 `config.py`）

| 變數 | 預設 | 說明 |
|---|---|---|
| `APP_NAME` / `APP_VERSION` / `LOG_LEVEL` | `anila-studio` / `0.1.0` / `INFO` | 服務識別 / log |
| `CSP_BASE_URL` | `http://csp:8000` | csp control plane |
| `CSP_SERVICE_TOKEN` | `""` | 選用 s2s token；有設則隨 revocations cold-start 帶 `X-CSP-Service-Token` |
| `REDIS_URL` / `REDIS_REVOCATION_CHANNEL` | `redis://redis:6379/0` / `anila:auth:token-revoke` | 跟 csp 共用 Redis + 撤銷 channel |
| `JWT_KID` / `JWT_ALGORITHMS` / `JWT_LEEWAY_SECONDS` | `anila-v1` / `("RS256",)` / `60` | JWT 設定 |
| `JWKS_REFRESH_SECONDS` / `REVOCATION_CACHE_TTL_SECONDS` | `3600` / `2592000`（30 天） | JWKS 重抓 / 撤銷 deny-list TTL |
| `INTERNAL_TIMEOUT_SECONDS` / `INTERNAL_TIMEOUT_CONNECT` / `INTERNAL_LLM_TIMEOUT_SECONDS` | `30.0` / `5.0` / `300.0` | csp_client 讀 / 連線 / LLM 長逾時 |
| `FLUX_CACHE_DIR` | `/var/anila/anila-studio-flux-cache` | FLUX cache |
| `ARTIFACTS_DIR` | `/var/anila/anila-studio-artifacts` | **report/mindmap/infographic/datatable 產出持久化根目錄** |
| `FLUX_BACKEND_URL` / `RENDERER_BASE_URL` | `http://flux2-dev:8000` / `http://pptx-renderer:7100` | FLUX 後端 / pptx renderer |

> 注意：`FLUX_BACKEND_URL` / `RENDERER_BASE_URL` 雖在 `config.py`，但渲染路徑實際多由 `os.environ` 直接讀（`studio_render.py` 讀 `FLUX_BACKEND_URL`、`geometric_qa.py` 讀 `RENDERER_BASE_URL`、`studio_config.py` 另 hardcode renderer URL）。另兩個 FLUX 旋鈕**只在 env、不在 config.py**：`FLUX_MAX_CONCURRENT`（4）、`FLUX_TIMEOUT_SECONDS`（180）。

---

## Frontend（ANILALM）如何呼叫

`ANILALM/src/api/studio.ts` 透過 `VITE_STUDIO_BASE_URL` 指向 anila-studio。TypeScript types 從 `openapi/studio.openapi.json` codegen：

```bash
cd anila-studio && .venv/bin/python scripts/export-openapi.py   # 改 schema 後
cd ../ANILALM && npm run gen:studio-types
```

---

## 部署注意事項

- csp 端 `JWT_PRIVATE_KEY_PATH` 必須存在（csp `scripts/generate-jwt-keypair.py` 預生或 Vault 注入）；anila-studio 不需 private key，只需 csp `/.well-known/jwks.json` 可達。
- anila-studio 啟動會 fail-fast 若 csp `/api/auth/revocations` 不可達。
- `ALLOW_AUTO_KEYGEN`（**csp 端**旋鈕，anila-studio 程式碼內無此變數）僅 dev/test。

---

## 相關文件

- 抽出計畫 / E2E：`docs/superpowers/anila-studio/plans/`
- Studio / FLUX 主規格：[`../docs/superpowers/studio-flux/ANILA_Studio_FLUX_Spec.md`](../docs/superpowers/studio-flux/ANILA_Studio_FLUX_Spec.md)
- 平台整體：[`../README.md`](../README.md) · 分支策略：[`../docs/branch-sync-backlog.md`](../docs/branch-sync-backlog.md)
