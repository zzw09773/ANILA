> ⚠ **2026-08-01 P2.1**：本計畫／規格所描述的 `csk-` 入向守衛與靜態服務憑證路徑，
> 已由派工 JWT＋JWKS 驗簽取代。下文保留原設計決策軌跡；現行接入見
> `docs/guides/developer-guide.md`。

# 設計：核發 csk- 時一併附上「入向守門碼 + 出向 RAG 用法」可複製區塊

- **日期**：2026-06-23
- **狀態**：設計定案（待寫 plan → 實作）
- **分支範圍**：main 起 → cherry-pick 散 7 分支；**`trial-military` 例外**（移除了 `DeveloperAgentsView.vue`，前端 wiring N/A）
- **撰寫**：Claude
- **緣由**：使用者回報三點 —— ① `anila_core` 非可安裝套件，已開發好的（非模板）agent 碰不到守門 middleware；② csk- 貼進 .env 後 `test-connection` 竟通過（其實是偽陽性：探針只送對的 token、`status != 401` 即判 accepted，無 middleware 的 agent 也永不回 401）；③ 想要「只貼 .env 前段」就能上手的便利路徑。

## 1. 背景與關鍵事實（皆已核實）

- **`anila-core` 是正規套件**（`pyproject.toml` name=`anila-core` v0.14.0）但**未發佈到任何 index**；air-gap 內網要搬源碼/wheel 才裝得到，且還得手動把 middleware 接進既有 app —— 對「已開發好的」非模板 agent 是純摩擦。
- **csk- 一把兩用**（`search.py` 確認）：
  - **入向**：CSP Router → agent 帶 `X-CSP-Service-Token: csk-…`，agent 端 middleware 比對自己的 csk- 證明來者是 Router。
  - **出向**：agent → CSP 帶 `Authorization: Bearer csk-…` 打 `POST /api/ingestion/collections/{id}/search` 做 RAG（`resolve_search_principal` 解析 csk- → agent owner，且**硬綁 `bound_collection_id`**、最小權限）。
- **官方 middleware fail-OPEN**（`anila-core/.../middleware/auth.py:84,274`）：沒設 token 時放行（為本機 dev）。**本 onboarding snippet 故意 fail-CLOSED**：沒設 token 直接 `raise` 不讓啟動 —— 這才堵掉第 ② 點的偽陽性根因（部署了卻沒驗 = 洞，不是方便）。
- **既有前端基建**：`bootstrapSnippets.js`（per-language 取得 csk- 的範例）+ `BootstrapHowToTabs.vue`（tab+copy）僅教「**怎麼拿到** csk-」，**無一教入向守門**。csk- 直發/輪替的結果在 `DeveloperAgentsView.vue`（行 792/823/883）只吐 csk- 字串。本功能補的就是這塊。

## 2. 已定案決策

| # | 決策 | 選擇 |
|---|---|---|
| D1 | 語言覆蓋 | **只做 Python**（FastAPI/Starlette；同事 OpenAI-compatible agent 最可能命中；其餘語言之後再補） |
| D2 | 區塊範圍 | **入向守門碼 + 出向 RAG 用法**（同一把 csk- 兩用，核發當下一次講清楚） |
| D3 | 守門失效模式 | **fail-CLOSED**：`CSP_SERVICE_TOKEN` 未設 → 啟動時 `raise RuntimeError`（對比官方 middleware 的 fail-open） |
| D4 | csk- 出現位置 | 只填進 **`.env` 行**；middleware/查詢程式一律 `os.environ[...]` 讀，**不硬編進程式碼** |
| D5 | 後端 | **不動**（csk- 本就在 issue-static / rotate response；snippet 純前端呈現，沿用 frontend-only 慣例） |
| D6 | 前端出現點 | csk- 一次性結果三處：**① 詳情 modal 的 issue-static ② rotate**（皆走 `issuedSecret.kind==='csk'`，單一掛點覆蓋）**③ register 精靈 step-2**（`newAgentCsk`，手上有 `registeredAgent.bound_collection_id`）。三處皆吐一次性 csk- 供貼上 |
| D9 | 環境變數對齊 | 用平台既有命名 **`CSP_BASE_URL` / `CSP_SERVICE_TOKEN` / `ANILA_COLLECTION_ID`**（與精靈 `newAgentEnvSnippet` 一致）；不 bake admin 瀏覽器 origin（可能是 localhost，精靈已警告）。出向 RAG 區塊**僅在 agent 已綁 collection 時顯示**、內嵌真實 collection id |
| D7 | 公開路徑 | 守門碼放行 `{"/health","/docs","/openapi.json","/redoc"}`，與官方 `_PUBLIC_PATHS` 對齊 |
| D8 | 分支 | main 起、散 7 分支；trial-military 因無 developer view 而 N/A |

## 3. 設計（as-built 提議）

### §1 產物 A：入向守門碼（Python，fail-closed）
核發後區塊呈現 `.env` 一行（csk- 已填）+ ~18 行 middleware：
```python
# .env
CSP_SERVICE_TOKEN=csk-<freshly-issued>

# main.py / app.py
import hmac, os
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

_CSP_TOKEN = os.environ.get("CSP_SERVICE_TOKEN", "")
if not _CSP_TOKEN:                                    # fail-CLOSED
    raise RuntimeError("CSP_SERVICE_TOKEN unset — 把後台 csk- 貼進 .env")

_PUBLIC = {"/health", "/docs", "/openapi.json", "/redoc"}

class AnilaInboundGuard(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path in _PUBLIC:
            return await call_next(request)
        token = request.headers.get("X-CSP-Service-Token", "")
        if not token:
            return JSONResponse({"detail": "missing X-CSP-Service-Token"}, status_code=401)
        if not hmac.compare_digest(token, _CSP_TOKEN):
            return JSONResponse({"detail": "invalid service token"}, status_code=403)
        return await call_next(request)

app.add_middleware(AnilaInboundGuard)
```

### §2 產物 B：出向 RAG 用法（同一把 csk-，契約已核實）
```python
import os
import httpx

CSP_BASE_URL  = os.environ["CSP_BASE_URL"].rstrip("/")   # 從 agent 機器可達的 CSP host（別用 localhost）
COLLECTION_ID = <agent 綁定的 collection id>              # 已綁 collection 才顯示這段、內嵌真實 id

def search_rag(query: str, top_k: int = 5) -> dict:
    r = httpx.post(
        f"{CSP_BASE_URL}/api/ingestion/collections/{COLLECTION_ID}/search",
        json={"query": query, "top_k": top_k},
        headers={"Authorization": f"Bearer {os.environ['CSP_SERVICE_TOKEN']}"},
        timeout=30.0,
    )
    r.raise_for_status()           # csk- 只能搜自己綁定的 collection，搜別的 → 403
    return r.json()
```

### §3 前端落點
- **新檔** `frontend/src/components/agents/inboundGuardSnippets.js`：builder `pythonInboundGuard(ctx)` / `pythonOutboundRag(ctx)`（`ctx={csk, collectionId?}`；csk- 只進 `.env` 行；出向**僅 collectionId 為數字時**回字串、否則回 `''`）+ `buildGuardSnippets(ctx)`。
- **新檔** `frontend/src/components/agents/AgentGuardPanel.vue`：入向守門碼**恆顯示**、出向 RAG **僅 `snippets.outboundRag` 非空時顯示**；各自 copy 按鈕；props `csk`(必)+`collectionId`(選)；沿用 `TermBox`/`TermButton`。
- **改檔** `frontend/src/views/DeveloperAgentsView.vue` **兩處**掛 `AgentGuardPanel`：① 詳情 modal 的 csk- 橫幅（`issuedSecret.kind==='csk'`，帶 `issuedSecret.value` + `detailAgent.bound_collection_id`）② register 精靈 step-2 `.env` 區之後、verify 之前（帶 `newAgentCsk` + `registeredAgent.bound_collection_id`）。並修正行 438「Only shown for bsk-」過時註解。

## 4. Non-goals
- 不改後端（不新增 endpoint、不改 response schema）。
- 不改 `test-connection` 偽陽性邏輯（另議；本功能只補 onboarding，不動既有探針）。
- 不做 Node/Go 版本（D1，之後再補）。
- 不替既有 `bootstrapSnippets.js`/`BootstrapHowToTabs.vue` 加守門 tab（fork/bootstrap 路徑模板已內建 middleware，重複易混淆）。

## 5. 風險與緩解
| 風險 | 緩解 |
|---|---|
| snippet 給錯（守門失效或假安全） | 對齊官方 `auth.py`（hmac.compare_digest、`_PUBLIC_PATHS`）；fail-closed；交 Codex 審 |
| 出向範例 path/body 過時 | 已核實 `POST /api/ingestion/collections/{id}/search` + `SearchRequest`；snippet 註明 csk- 硬綁 collection |
| csk- 外洩進原始碼 | D4：csk- 只進 `.env` 行，程式一律讀 env |
| trial-military cherry-pick 撞 modify/delete | 該分支無 developer view → 跳過 view wiring；snippet 檔可不落（功能 N/A） |

## 6. 測試
- 前端：`npm run build` 通過（非只 tsc）；若有單元測試框架，加 `inboundGuardSnippets` 純函式測（csk- 進 .env 行、未硬編進程式、fail-closed 字樣存在、`/api/ingestion/collections/{id}/search` 路徑正確）。
- 人工：核發 csk- → 結果區塊出現兩塊、copy 可用、csk- 正確帶入 .env 行。

## 附錄：關鍵程式位置
- 後端（唯讀參照，不改）：`agents.py:886` issue-static、`:1031` rotate、`ingestion/search.py:462` search、`anila-core/.../middleware/auth.py` 官方守門。
- 前端（改/新增）：`components/agents/inboundGuardSnippets.js`（新）、`components/agents/AgentGuardPanel.vue`（新）、`views/DeveloperAgentsView.vue`（改，issue-static/rotate 結果區）。
