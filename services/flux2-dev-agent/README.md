# flux2-dev-agent

> **圖像繪製 agent 包裝層** — 把 `flux2-dev` 的原始推論後端，包成一支 OpenAI `/v1/chat/completions` 相容的 agent。CSP 在 model registry 以 `model_type=agent`、名稱 `image-generator` 註冊它；Router 判斷使用者要繪圖時分派到這裡。在 stack 裡以服務名 `flux2-dev-agent` 出現於 external network `anila-models-net`。

> 中文為主版；English mirror：[`README.en.md`](./README.en.md)。技術名詞、指令、程式碼一律保留英文。

> 🌿 **分支對照**：本服務由 `anila-models` 模型 stack 建置與供裝（見 [`infra/models/`](../../infra/models/README.md)），存在於使用該 stack 的 ANILA 部署分支。分支策略見根目錄 [`README.md`](../../README.md) 的分支對照表與 [`docs/branch-sync-backlog.md`](../../docs/branch-sync-backlog.md)。

---

## 這是什麼

聊天流程裡，Router 發現使用者想繪圖時會分派 `image-generator` agent，把 OpenAI chat 請求（經 CSP proxy）forward 到本 shim。shim 負責把「口語需求」變成「一張落地好的圖 + Markdown 連結」：

1. **`prompt_translator.py`** — 呼叫 CSP proxy 的 `gemma4`，把中文口語改寫成 FLUX 友善的英文 prompt。任何錯誤（非 200、malformed、None）一律 **fallback 原文**，不讓整個請求失敗；`enabled=False` 時為 pure pass-through（kill switch）。
2. **`flux_client.py`** — 以 async context manager 呼 FLUX 後端的 **OpenAI 相容 Images API** `POST {base}/v1/images/generations`（body `{model, prompt, n:1, size, response_format:"b64_json"}`；base 沒帶 `/v1` 會自動補；設 `FLUX_API_KEY` 才帶 `Authorization: Bearer`），解出第一個 `b64_json` 還原 PNG（bytes）。aspect ratio 以內建對映表轉 `size`（16:9→1792x1024、1:1→1024x1024…未知比例 fallback `1024x1024`）。非 200 / 無 data / 非 JSON → `FluxBackendError`。
3. **`image_store.py`** — 校驗 PNG magic bytes、以 `uuid4` 命名寫進 share volume，回傳 public URL。
4. **`chat_handler.py`** — 串接前三者，組出 OpenAI 形狀回應，assistant 內容為 `已為您繪製：\n\n![](url)`。

```
Router → CSP proxy → flux2-dev-agent  POST /v1/chat/completions
                       ├─ prompt_translator → CSP /v1/chat/completions (gemma4)   ← 出向帶 Bearer
                       ├─ flux_client       → FLUX 後端 POST {base}/v1/images/generations（OpenAI 相容）
                       ├─ image_store       → 寫 /share/flux，回 /uploads/flux/<uuid>.png
                       └─ chat_handler      → OpenAI 回應（Markdown 圖片連結）
```

> `flux_client` 只送 `{prompt, aspect_ratio}`（不帶 seed / num_candidates），故每次取回單一候選、用 agent 端設定的 `DEFAULT_ASPECT_RATIO`；chat 內容不解析長寬比。

---

## 對外端點

`build_app` 把四個協作元件當參數注入（測試改注入 mock），module-level `app` 由 env 組出、供 `uvicorn app.main:app` 載入。

| 端點 | 方法 | 說明 |
|------|------|------|
| `/health` | GET | `{"status": "ok"}` |
| `/v1/models` | GET | 回 `image-generator`（OpenAI list 形狀：`id`/`object`/`created`/`owned_by`，另帶 `model_type:"agent"` 標記，與 CSP 註冊資訊一致） |
| `/v1/chat/completions` | POST | JSON 或 SSE（見下） |

**請求 schema**（`ChatCompletionRequest`）：標準 OpenAI body（`model` + 非空 `messages`）+ ANILA 擴充 `anila_session_id`、`anila_handoff`（CSP 逐字 forward 而接受；handler 實際只用 `last_user_text()` 與 `model`）。空 `messages` → `422`。

**JSON vs SSE**：`stream=false`（預設）回單一 `chat.completion`；`stream=true` 回 `text/event-stream`——依序 emit role chunk、content chunk（含 Markdown 圖）、finish chunk、`[DONE]`。Router 分派 streaming 請求時**期待 SSE**；若在 stream 模式回 JSON，Router 會 emit 0 個 content chunk，前端聊天顯示空白（有 regression 測試守住）。

**錯誤**：底層 flux / translator / store 任一 raise → 端點統一轉 `502`（`detail: "image generation failed"`），且**不把 exception 原文洩漏**進 response body（regression 測試守住）。

---

## 認證邊界（重要）

- **本 shim 不做任何入向認證／授權**。三個端點都無 auth，設計假設是：只在 `anila-models-net` 內網、**僅 CSP proxy 可達**（host port 完全沒開）。這是 intranet／CSP-only 的信任模型——**不要**把本服務直接對外曝露。
- 唯一的 Bearer 憑證是**出向**：`prompt_translator` 呼 CSP `/v1/chat/completions`（翻譯）時帶 `Authorization: Bearer <CSP_API_KEY>`。
- Agent Registry（[doc 05](../../docs/anila-redesign-docs/05-agent-registry-and-runtime-protocol.md)）的 7-state 審批與 trace-test gate 治理**在 CSP 治理中心**，不在本 shim；shim 只是被治理、被分派的執行端。seed 註冊時 `image-generator` 的 `approval_status` 為 `approved`。

---

## 環境變數

| 變數 | 預設（程式碼） | 說明 |
|------|----------------|------|
| `FLUX_BACKEND_URL` | `http://flux2-dev:8000` | OpenAI 相容 Images API 的 base URL（伺服器根或含 `/v1` 皆可，client 自動補版本段） |
| `FLUX_MODEL` | `flux.2-dev` | Images API request 的 `model` 欄位 |
| `FLUX_API_KEY` | `""` | 有值才帶 `Authorization: Bearer`；留空不帶（本機 flux2-dev 免驗） |
| `CSP_BASE_URL` | `http://csp:8000` | 翻譯 callback 目的地 |
| `CSP_API_KEY` | `""`（models compose 以 `INTERNAL_PLATFORM_API_KEY` fail-loud 帶入） | 空值 → 翻譯自動停用並 warn，FLUX 收原文 |
| `GEMMA_MODEL` | `gemma4` | 翻譯用 LLM |
| `ENABLE_PROMPT_TRANSLATION` | `1` | 僅在 `CSP_API_KEY` 非空時生效 |
| `SHARE_DIR` | `/share/flux` | PNG 落地路徑（bind mount） |
| `PUBLIC_URL_PREFIX` | `/uploads/flux` | 回前端的 URL 前綴 |
| `DEFAULT_ASPECT_RATIO` | `16:9` | 送 flux2-dev 的固定長寬比 |
| `FLUX_TIMEOUT_SECONDS` | `180`（models compose 蓋成 `240`） | 後端逾時 |

---

## 目錄結構

```
services/flux2-dev-agent/
├── app/
│   ├── main.py              # build_app + /health + /v1/models + /v1/chat/completions（含 SSE）
│   ├── schemas.py           # OpenAI chat 形狀 + anila_session_id / anila_handoff 擴充
│   ├── prompt_translator.py # gemma4 via CSP proxy；錯誤 fallback 原文
│   ├── flux_client.py       # 呼 OpenAI 相容 /v1/images/generations，解第一張 PNG
│   ├── image_store.py       # PNG 校驗 + 寫 share volume + public URL
│   └── chat_handler.py      # 串接四者 → OpenAI 回應
├── Dockerfile               # python:3.11-slim（無 GPU；healthcheck 用 urllib，image 無 curl）
├── requirements.txt · pyproject.toml
└── tests/                   # 27 個測試（pytest-asyncio + respx；asyncio_mode=auto）
```

---

## 本機開發與測試

測試以 `respx` 攔 HTTP、`AsyncMock` 注入協作元件，**不需 GPU、不連任何後端**：

```bash
python -m venv .venv
.venv/bin/pip install -e 'services/flux2-dev-agent[test]'   # 於 repo 根；或先進目錄再 pip install -e '.[test]'
cd services/flux2-dev-agent && ../../.venv/bin/python -m pytest -q
# → 27 passed
```

> `[test]` extra = `pytest` + `pytest-asyncio` + `respx`；`pyproject.toml` 設 `asyncio_mode = "auto"`，故 async 測試不必逐一標 `@pytest.mark.asyncio`。本 repo 未提交 `.venv`。

---

## 部署

由 `anila-models` 模型 stack 建置與供裝（build context `../../services/flux2-dev-agent`），**不屬平台 stack**，兩者以 external network `anila-models-net` 互通。日常操作見 [`infra/models/README.md`](../../infra/models/README.md)。

- `python:3.11-slim`、無 GPU；`depends_on: flux2-dev (service_healthy)`；只 `expose: "8000"`，不開 host port。
- healthcheck 用 Python `urllib`（slim image 無 `curl`，用 curl 會 exit 127 假性 unhealthy）。
- share volume：host 的 `share/uploads/flux` bind 到容器 `/share/flux`；nginx 以 `location /uploads/` 對外服務（見 `infra/nginx/anila.conf`），故回前端的圖片連結是 `/uploads/flux/<uuid>.png`。

### dev / prod 共用語意（依現行 compose 核實）

- flux 兩服務由**模型 stack**（`infra/models/docker-compose.yml`，project `anila-models`）提供，非平台 stack；平台 dev（`infra/compose/dev.yml`）與 prod（`infra/compose/platform.yml`）都 join 同一 external network `anila-models-net` 才連得到。
- **dev 與 prod 皆**在 CSP seed 註冊 `image-generator` agent（端點 `http://flux2-dev-agent:8000`）並為 `anila-studio` 連上 `FLUX_BACKEND_URL`。差別在 prod 用 `${FLUX_AGENT_BASE_URL-…}` / `${FLUX_BACKEND_URL-…}`（`${VAR-default}` 無冒號語意）做成**可關閉旗標**：設成空字串即讓 `get_flux_provider()` 回 `None`（Studio 繪圖 OFF）或把 agent 端點改指他處；dev 則預設開啟。

---

## 相關文件

- 推論後端：[`flux2-dev`](../flux2-dev/README.md) · 模型 stack：[`infra/models`](../../infra/models/README.md)
- FLUX 規格：[`ANILA_Studio_FLUX_Spec.md`](../../docs/specs/studio-flux/ANILA_Studio_FLUX_Spec.md)（§3.2 `/generate` 合約）
- Router 分派：[`anila-core-router`](../anila-core-router/README.md)
- 重設計文件：[`05-agent-registry-and-runtime-protocol.md`](../../docs/anila-redesign-docs/05-agent-registry-and-runtime-protocol.md)、[`00-product-constitution.md`](../../docs/anila-redesign-docs/00-product-constitution.md)
