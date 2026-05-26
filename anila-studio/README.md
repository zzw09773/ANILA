# anila-studio

> [English version](README.en.md) · 此檔為中文主版

> 📌 **此檔屬 `prod` 分支(中科院內網部署版)**。anila-studio cold-start 會去 csp `/api/auth/revocations` 同步 token revocation cache,prod 的 `myCSPPlatform/backend/app/api/auth.py` 必須既有 SSO/card endpoints **又有** main 帶來的 `GET /api/auth/revocations`(這是 PR #16 sync 時踩雷的點,已在 2026-05-26 補回)。

從 `myCSPPlatform/backend` 抽出來的 **簡報生成服務**:RAG 找你之前上傳的文件 → LLM 生大綱 → FLUX 生插圖 → PPTX 渲染。

## 為什麼獨立

- **改插圖邏輯不必 rebuild csp**:dev loop 由分鐘級降到秒級
- **單一職責**:csp 是 control plane(auth / models / ingestion / proxy 計費),anila-studio 只做 deck 生成
- **HTTP-only 對外**:跟 csp 之間透過 `csp_client` 走 HTTP,**不**共用 DB

## 結構

```
anila-studio/
├── pyproject.toml          # fastapi / httpx / jose / redis / cachetools / opencc / numpy
├── Dockerfile              # python:3.11-slim + apt graphviz + non-root user
├── scripts/
│   ├── export-openapi.py   # 重 gen openapi/studio.openapi.json
│   └── generate-jwt-keypair.py  # (csp 端 dev helper,留作參考)
├── openapi/
│   └── studio.openapi.json # codegen 給 ANILALM 用的 contract
├── app/
│   ├── main.py             # FastAPI lifespan + /health readiness gate
│   ├── config.py           # env settings (CSP_BASE_URL / REDIS_URL / FLUX_BACKEND_URL ...)
│   ├── auth.py             # RS256 JWT verify via JWKS + revocation cache check
│   ├── api/
│   │   └── studio.py       # /api/studio/slides/jobs (4 endpoint)
│   ├── clients/
│   │   └── csp_client.py   # 5 個 thin async function 包 csp HTTP API
│   ├── services/
│   │   ├── jwks_client.py             # 拉 csp /.well-known/jwks.json + cache
│   │   ├── revocation_cache.py        # Redis pub/sub 訂 csp token revoke
│   │   ├── flux_image_provider.py     # FLUX backend client + image cache
│   │   ├── flux_quality_gate.py       # VLM ranking + striping detection (FFT)
│   │   ├── flux_prompt_rewriter.py    # 改寫 prompt
│   │   ├── flux_style.py              # style routing
│   │   ├── studio_job_service.py      # in-memory job state machine
│   │   ├── studio_text_normalizer.py  # 簡轉繁 + 文字清理
│   │   ├── diagram_renderer.py        # Graphviz dot → PNG
│   │   └── geometric_qa.py            # 投影片幾何 QA
│   └── schemas/
│       └── studio.py       # pydantic SlidesSpec / JobStatus / GenerateSpecRequest 等
└── tests/                  # 91 個 test(自家 + 從 csp 搬過來)
```

## 跑起來

### 本機 dev

```bash
cd anila-studio
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"

# 跑 test(不需 docker)
.venv/bin/pytest

# 啟服務(需要 csp 在 :8000、redis 在 :6379、flux2-dev、pptx-renderer)
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8100
```

### Docker

```bash
docker compose -f docker-compose-dev.yml up -d --build anila-studio
docker logs anila-platform-dev-anila-studio-1 -f
```

### Health check

```bash
curl http://localhost:8100/health
# {"status":"ok","service":"anila-studio","version":"0.1.0","ready":true,
#  "deps":{"revocation_cache":true}}
```

Lifespan startup 期間 `/health` 回 503 + `ready=false`,等 JWKS + revocation cache cold-start 完才綠燈。

## 對 csp 的依賴

| Endpoint | 用途 |
|---|---|
| `GET /.well-known/jwks.json` | 拉 RS256 public key 驗 JWT |
| `GET /api/auth/revocations?since=` | 啟動時 cold-start sync 撤銷清單 |
| `POST /api/ingestion/collections/{id}/search` | RAG chunk 檢索 |
| `POST /api/ingestion/collections/{id}/images/search` | RAG image 檢索 |
| `GET /api/ingestion/images/{id}/blob` | 拉原始 image bytes |
| `GET /api/ingestion/collections/{id}` | 拿 collection 中介資料 |
| `POST /api/proxy/v1/chat/completions` | LLM(走 csp proxy 維持計費) |

## Redis pub/sub

訂 channel `anila:auth:token-revoke`(csp publish):
```json
{"user_id": 42, "revoked_at_version": 3, "ts": "...", "schema_version": 1}
```

Redis 失聯時 anila-studio fail-closed:`/health` 503 + 所有需要 auth 的 endpoint 503,不允許「降到 TTL-only」(plan v2 R14 修正)。

## Frontend(ANILALM)如何呼叫

`ANILALM/src/api/studio.ts` 透過 `STUDIO_BASE_URL` 環境變數指向 anila-studio:

```typescript
// .env
VITE_STUDIO_BASE_URL=     # 留空 → vite dev proxy or nginx reverse proxy
```

TypeScript types 從 `anila-studio/openapi/studio.openapi.json` codegen,執行:
```bash
cd ANILALM && npm run gen:studio-types
```

## 重 gen OpenAPI(改了 schema 後)

```bash
cd anila-studio && .venv/bin/python scripts/export-openapi.py
cd ../ANILALM && npm run gen:studio-types
```

## 重要環境變數

| 變數 | 預設 | 說明 |
|---|---|---|
| `CSP_BASE_URL` | `http://csp:8000` | csp control plane |
| `REDIS_URL` | `redis://redis:6379/0` | 跟 csp 共用 Redis |
| `REDIS_REVOCATION_CHANNEL` | `anila:auth:token-revoke` | Redis pub/sub channel |
| `FLUX_BACKEND_URL` | `http://flux2-dev:8000` | FLUX 後端 |
| `RENDERER_BASE_URL` | `http://pptx-renderer:7100` | pptx renderer |
| `JWT_KID` | `anila-v1` | JWT kid header,對應 JWKS |
| `JWT_ALGORITHMS` | `("RS256",)` | 接受的簽算法 |
| `JWKS_REFRESH_SECONDS` | `3600` | JWKS 重 fetch 間隔 |
| `REVOCATION_CACHE_TTL_SECONDS` | `2592000` | 30 天,對齊 csp retention |
| `INTERNAL_TIMEOUT_SECONDS` | `30.0` | csp_client HTTP timeout |
| `FLUX_CACHE_DIR` | `/var/anila/anila-studio-flux-cache` | FLUX cache 目錄 |

## 部署注意事項

- csp 端 `JWT_PRIVATE_KEY_PATH` 必須存在(`scripts/generate-jwt-keypair.py` 預生或 Vault 注入);anila-studio 不需要 private key,只需 csp `/.well-known/jwks.json` 可達
- anila-studio 啟動會 fail-fast 若 csp `/api/auth/revocations` 不可達 ── 確保 docker-compose 的 `depends_on: csp` 跟 health gate 對齊
- `ALLOW_AUTO_KEYGEN=true` 只用 dev / test,**prod 絕對不開**

## 抽出歷史

- 設計文件:`docs/superpowers/anila-studio/plans/2026-05-23-extraction-plan.md`(plan v2,經 codex 13 條 finding 修訂)
- Phase 0:baseline + skeleton(`anila-studio/MIGRATION_BASELINE.md`)
- Phase 1:csp 加 `/images/search` / `/images/{id}/blob` / JWKS / Redis revocation publisher
- Phase 2:anila-studio service skeleton(JWKS / revocation_cache / csp_client / auth)
- Phase 3:搬 10 service 檔 + studio.py 9 sub-commit A→I 改造(DB→HTTP)
- Phase 4:搬 20 test 檔
- Phase 5:ANILALM 切到 anila-studio + openapi-typescript codegen
- Phase 6:E2E 驗收(user 觸發)── 見 `docs/superpowers/anila-studio/plans/2026-05-23-e2e-runbook.md`
- Phase 7:csp 端刪 27 個 dead 檔
- Phase 8:本檔案

---

**Last updated**: 2026-05-26(同步 PR #16 + 加 prod banner;cold-start dep 與 csp `/api/auth/revocations` 的關係寫進 banner)
