# ANILA

ANILA 是中山科學研究院院內的 AI 工作平台，給同仁在瀏覽器裡使用。管理員與開發者另外有治理中心，用來管模型、帳號、知識庫與自行掛上的助手。

## 現在能做什麼

同仁登入後進對話（`/anila/`）。對話預設走平台入口，由路由服務依治理中心登錄的模型與已核准助手決定交給誰；也可以自己指定模型。設定裡可以建立純文字 skill，送出該則訊息時套用。語音辨識在治理中心啟用且健康時，對話框才出現麥克風。

「我的知識庫」（`/anilalm/`）用來建知識庫、上傳文件、依文件提問，並產生簡報、報告、心智圖、資訊圖與資料表。文件的解析、切塊與向量由匯入工作處理。

治理中心在網站根路徑。管理員在這裡管理模型與 API 金鑰、看用量、設定單價與額度、維護部門與使用者。擁有者、管理員、開發者與單位管理員看得到這個入口；一般同仁的日常操作在對話。

## 各部分怎麼接

瀏覽器只進 nginx。埠是 80（轉到 HTTPS）、443、4443，可用環境變數改。其餘服務的埠是容器內埠，不對主機公開，除非下面另註。

| 目錄 | 做什麼 |
|---|---|
| `apps/anila-shell` | 對話介面。compose 服務名 `anila-ui`，容器埠 80，nginx 路徑 `/anila/` |
| `apps/anilalm` | 知識庫與產出。容器埠 80，nginx 路徑 `/anilalm/` |
| `apps/csp-governance-ui` | 治理中心。建進 CSP 映像，由 CSP 在 `/` 提供 |
| `services/csp` | 登入、帳號、模型、對話、知識庫 API，以及對模型的代理。容器埠 8000 |
| `services/anila-core-router` | 對話路由。compose 服務名 `router`，容器埠 9000。平台入口模型 `anila-router` 指到這裡 |
| `services/anila-studio` | 簡報、報告、心智圖、資訊圖、資料表。容器埠 8100 |
| `services/pptx-renderer` | 把簡報規格畫成檔案。容器埠 7100，只給 studio 呼叫 |
| `services/ingestion-worker` | 文件解析、切塊、嵌入、寫入向量 |
| `services/asr-gateway` | 語音切句並轉到治理中心登錄的解碼端。容器埠 8200，nginx 路徑 `/asr/` |
| `packages/anila-core` | 路由、記憶、文件處理與出向位址檢查的 Python 程式庫。router、CSP、匯入工作會用到 |
| `packages/anila-agent` | 進階助手範例。`make test` 跑單元測試 |
| `packages/anila-agent-quickstart` | 助手快速起步。一般開發者改 `agent.py` 即可，步驟在 [`docs/guides/developer-guide.md`](docs/guides/developer-guide.md) |

另外，Postgres（`csp-db`）只聽 `127.0.0.1:5433`，PgBouncer 在容器網路上的 5432，Redis 在 6379。模型主機不在這份 compose 裡，位址在治理中心登錄。

一次對話的路徑是：瀏覽器 → nginx → shell；送出的 `/v1/`、`/v2/` 進 CSP。CSP 看到平台入口 `anila-router` 時轉到 router。router 向 CSP 讀目前的主路由模型與助手清單，再把請求送到登錄的模型或助手。知識庫產出的 `/api/studio`、`/api/reports`、`/api/mindmaps`、`/api/infographics`、`/api/datatables` 由 nginx 直接轉到 anila-studio；簡報檔再由 studio 請 pptx-renderer 產生。

出處：[`infra/compose/platform.yml`](infra/compose/platform.yml)、[`infra/nginx/anila.conf`](infra/nginx/anila.conf)。

## 安裝與更新

需要 Linux、Docker，以及 Docker Compose 2.17 以上。模型不在出貨包裡。第一次安裝見 [`docs/deploy/INSTALL.md`](docs/deploy/INSTALL.md)，出貨、更新與回復見 [`docs/deploy/UPDATE.md`](docs/deploy/UPDATE.md)。

## 開發

開發站與正式站分開，compose project 是 `anila-platform-dev`，nginx 聽 8080／8443／9443：

```bash
docker compose -f compose.dev.yaml up -d --build
```

測試：

```bash
cd services/csp && python -m pytest
cd packages/anila-core && PYTHONPATH=src python -m pytest
cd packages/anila-agent && python -m pytest -m 'not live'
cd packages/anila-agent-quickstart && python -m pytest
cd apps/anila-shell && npm test
cd apps/csp-governance-ui && npm test
cd apps/anilalm && npm test
cd services/pptx-renderer && npm test
bash scripts/release/tests/test_release_flow.sh
```

`anila-shell` 的 `npm test` 是 `vitest run`。`anilalm` 的 `npm test` 含 `vitest run` 與 `node --test`。`csp-governance-ui` 的 `npm test` 是 `node --test tests/*.test.mjs`。`anila-studio`、`asr-gateway`、`ingestion-worker`、`anila-core-router` 也是在該目錄執行 `python -m pytest`。

## 安全邊界

平台給內網用。模型、文件解析與語音的位址只在治理中心登錄。`packages/anila-core/src/anila_core/security/url_guard.py` 檢查出向位址：未列入信任主機的拒絕，loopback、link-local 與雲端 metadata 一律拒絕。產品程式不呼叫院外服務。

正式安裝把登入設成 `card-only`（插卡）。這個模式仍保留擁有者的密碼登入，其他帳號不能用密碼。

分類有四級：無機密、營業秘密、密、機密。見 `services/csp/app/schemas/contracts/classification.py`。

`services/anila-studio` 不 import `anila_core`，由 `services/anila-studio/tests/test_no_anila_core_import.py` 守著。

`.env`、`secrets/`、`*.pem`、`*.key` 與 API 金鑰不進版控。

## 其他文件

- 運行狀態：[`docs/CURRENT-STATUS.md`](docs/CURRENT-STATUS.md)
- 同仁與管理員手冊：[`docs/user-manual/`](docs/user-manual/)
- 掛助手：[`docs/guides/developer-guide.md`](docs/guides/developer-guide.md)
- 維運步驟：[`docs/runbooks/`](docs/runbooks/)
- 操作時覺得不該這樣：[`docs/FRICTION-LOG.md`](docs/FRICTION-LOG.md)
- 文件索引：[`docs/README.md`](docs/README.md)

本專案版權所有 © 2026 中山科學研究院（NCSIST）。授權見 [`LICENSE`](LICENSE)（GNU GPL v3）。
