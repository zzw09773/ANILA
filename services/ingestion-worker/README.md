# ingestion-worker

> ANILA 文件攝取（ingestion）的非同步背景 worker：把一份上傳文件跑完 **parse → chunk → embed → index**，並抽取跨文件關係，另附 chunking 策略評估（evaluator）。它是「我的知識庫」入口背後真正把文件變成可檢索向量的引擎。

> English mirror：[`README.en.md`](./README.en.md)

> 🌿 **分支對照**：本 worker 存在於所有 ANILA 部署分支，內容跨分支一致。分支策略見根目錄 [`README.md`](../../README.md) 的分支對照表（現行單一 `main`；舊七分支模型已失效，見根目錄 README）。

---

## 在 monorepo 的位置（重構後 §17.1 版圖）

重構把原始碼收斂為 `services/` · `apps/` · `packages/` · `infra/` 四層。本服務落在：

```
services/ingestion-worker/     ← 本服務（Arq worker，無 HTTP 對外）
packages/anila-core/           ← 共用 SDK（parser / chunker / 向量儲存 / 安全工具）
infra/compose/platform.yml     ← compose 定義（根目錄 compose.yaml 為 shim → include 它）
```

啟停一律走根目錄 compose shim：`compose.yaml` → `infra/compose/platform.yml`（prod stack，project `anila`）；`compose.dev.yaml` → `infra/compose/dev.yml`（dev stack）。部署腳本在 `infra/deployment/{scripts,intranet}/`。

---

## 這不是 HTTP 服務

`ingestion-worker` **沒有 HTTP endpoint、沒有 health route**。它是 [Arq](https://arq-docs.helpmanual.io/)（以 Redis 為後端的 async job queue）驅動的 worker process：CSP enqueue job，worker 取出後執行。entrypoint：

```bash
arq ingestion_worker.main.WorkerSettings
```

`main.py` 的 `WorkerSettings` 註冊 **三個** job function（CSP 以 `pool.enqueue_job(<name>, <arg>)` 送出，見 `services/csp/app/services/ingestion_queue.py`）：

```text
ingest_document(document_id)                    # 完整攝取一份文件
evaluate_strategies(eval_run_id)                # 評估一個 chunking eval run
reresolve_collection_relations(collection_id)   # 重抽整個 collection 的關係
```

Arq 重試 / 逾時策略（`main.py`）：`max_tries=3`、`job_timeout=300`（秒）、`keep_result=3600`（讓 CSP 一小時內輪詢得到結果）。`on_startup` 開一個共享 `PgPool` + 建 `Embedder`，`on_shutdown` 收乾淨。

### 治理首頁那盞燈是怎麼來的（維運看這段）

沒有 HTTP health route，不代表沒有健康訊號。worker 用的是 **arq 的預設心跳**：每 3600 秒把一行狀態寫進 Redis 的 `arq:queue:health-check`，TTL = 3601 秒。CSP 的**服務健康總覽**同時看兩個訊號 —— 這把 key，以及 docker DNS 解不解得出 `ingestion-worker` 這個名字（**容器沒在跑就解不出來**）。兩個訊號交叉出四格，再加上兩種「問不到 Redis」的情形：

| 名稱解析 | key | 卡片 | 什麼時候會看到 |
|---|---|---|---|
| 解得出 | 在 | 綠（`可連線`） | 正常運作中（包含正在解析大文件時） |
| 解得出 | 不在 | 紅（`無法連線`） | 容器還在，但 process 卡死超過 3601 秒 |
| 解不出 | 在 | 紅（`無法連線`） | **容器停了／崩了**，而且是在最近 3601 秒內 |
| 解不出 | 不在 | 黃（`此部署未啟用`） | 這個部署沒起 ingestion-worker；**或**它已經消失超過 3601 秒 |
| （任一） | 問不到 | 黃（`探測失敗`） | Redis 連不上，無從判斷 —— 先看 Redis 那張卡 |
| （任一） | 沒答完 | 黃（`探測逾時`） | Redis 沒在 4 秒內回答；同樣是「問不到」，不是「它很好」 |

**各狀態實際的偵測時間（這是系統真的做得到的數字，不是目標值）：**

- **容器停掉 / 崩掉** → **下一次開啟總覽就是紅的**（DNS 記錄立刻消失，而 key 還在）。這是最常見的死法，也是唯一快的一格。
- **容器還在跑但 process 卡死** → 最久 **3601 秒（約 1 小時）** 才翻紅，因為只有 key 過期能反映它。
- **消失超過 3601 秒** → 退化成黃色「此部署未啟用」。到那時 key 也過期了，從 CSP 看出去，「早就不在」與「本來就沒部署」是同一件事，猜哪一個都是瞎猜。
- **沒部署** → 一直是黃色，正確。

> 📌 **計畫性停機也是紅的。** 你自己 `docker compose stop ingestion-worker` 之後，這張卡會紅**最多 3601 秒**才退成黃色的「此部署未啟用」。卡片分不出「你關的」跟「它掛的」——維護期間看到紅燈是預期行為，不用追。

> ⚠ **`PDF_OCR_FALLBACK` 的天花板不是這個 3601 秒。** 預設 `false`。一旦開成 `true`，抽不到文字的 PDF 會逐頁走 OCR／VLM，解析時間從幾十秒跳到接近 1800 秒等級。這段文字以前拿 1800 秒去比 3601 秒的心跳 TTL、說「餘裕從約 40 倍縮到約 2 倍」——**比錯對象了**。先撞到的是 `src/ingestion_worker/main.py:71` 的 `job_timeout = 300`（同檔 :21 寫「5 分鐘一份」）。**1800 秒不是吃掉餘裕，是超出宣告預算 6 倍。**
>
> 而且它不會乾脆地被砍掉：`extract_text` 是**同步**呼叫（`handlers.py:736`），照本節下面那張表會佔住 arq 的事件迴圈；arq 用 `asyncio.wait_for` 執行 `job_timeout`（`arq/worker.py:591`），**而 `wait_for` 打不斷阻塞的同步呼叫，計時器在迴圈被佔住時連跑都跑不了**。所以結果是不確定的兩種壞：要嘛整份跑完、300 秒的預算被無聲超過（期間**佇列裡其他 ingest 全部排隊等**）；要嘛迴圈一放開計時器就補跳，把已經花掉的 1800 秒 VLM 全部作廢並重試（`max_tries=3`）。兩種都不是「5 分鐘一份」承諾的事。
>
> 📌 **2026-08-07 之前，這筆成本一次也沒發生過。** 觸發判斷把解析器自己插進去的 `[[IMAGE:<id>]]` 佔位符當成「抽到的文字」，每個 24 字，所以**兩頁的純掃描件就有 48 字**、越過 40 字門檻、被判成「有文字，不需要 OCR」——旗標開了也不會有掃描件走進 OCR。**上面那些數字是現在才真的會發生的，不是一直都在的。**
>
> **現在的判斷規則**（`packages/anila-core/src/anila_core/ingestion/ocr.py`）：扣掉佔位符之後量，並且**丟掉「在多數頁上重複出現的短行」**（頁碼、文件編號、浮水印，以及本院每頁都有的密級標示——長度不是分辨依據，**會重複才是**），然後問「還有幾頁帶得起文字」；**帶文字的頁數不到一半就判定是掃描件**。舊的「每頁平均字數」會被一頁一萬字的目錄蓋掉整份 200 頁的掃描件，所以換成數頁數。
>
> ⚠ **這條規則換掉了一組破綻，也換來另一組。** 舊規則（每頁平均字數）被「每頁一個夠長的標示」整組打死；新規則不吃長度，但吃「重複程度」。**下面每一條都是實測出來的，不是推想的**（`needs_ocr_fallback` 直接餵資料量的）：
>
> **A. 漏抓：掃描件判成「有文字」→ 不會 OCR，而且沒有任何訊息**
>
> | 情況 | 例 | 為什麼漏 |
> |---|---|---|
> | 頁首**用字**逐頁不同 | `Page 3 of 50 - Section Environmental Limits`（章名逐頁換） | 不同的行＝不重複＝不算裝飾 |
> | 輪替的簽章／校閱者 | `Reviewed by inspector A - internal`（A–G 輪流） | 同上 |
> | 裝飾**超過 80 字** | 實測 85 字的密級＋分發名單抬頭 | 超過 80 字的重複行一律當內容，不當裝飾 |
> | 奇偶頁**兩種**頁首交替 | 各出現 50% 的頁 | 低於「60% 的頁」門檻 |
> | 標示只蓋在**部分**頁 | 100 頁中 59 頁有 | 同上，59% < 60% |
>
> 📌 **只變數字的不算漏**：`Page 3 of 250 - Section 4.2` 這種**只有數字在變**的頁首會被正規化成同一行，照樣抓得到。會漏的是**換字**，不是換數字。
>
> **B. 誤觸發：真的有文字的文件被判成掃描件 → 它的文字會被 OCR 結果整份取代**
>
> | 情況 | 實測 | 後果 |
> |---|---|---|
> | 大量**制式表單**（欄位標籤重複、填的值不同） | 250 份 → 判成掃描件 | 標籤被當裝飾丟掉，剩下的值太短撐不起每頁 20 字 |
> | **版型統一的簡報** | 60 頁 → 判成掃描件 | 每頁只有標題＋一兩句，扣掉重複的版面文字就不夠 |
>
> ⚠ **B 比 A 嚴重。** A 只是「維持現狀、沒有變好」；B 是**把本來讀得到的文字換掉**，而且照上面那段的算法，一份 250 頁的文件跑 OCR **必然撞爛 300 秒的 `job_timeout`**（250 頁遠超過推算的約 16 頁預算）。**如果你要收的就是制式表單或簡報，不要開這個旗標。**
>
> ⚠ **另一格單獨的漏**：**單頁**掃描件，如果那一頁的標示本身就超過 40 字。一頁的文件沒有「重複」可言，分不出裝飾和內容。例：一張 `CONFIDENTIAL - NCSIST Internal Use Only - Page 1 of 1` 的單頁掃描 → 判成有文字、不會走 OCR。多頁不受影響。
>
> 判斷本身有測試守著（`packages/anila-core/tests/test_pdf_ocr_trigger.py`）。但**仍然沒有任何測試會在你把旗標打開的時候提醒你成本**——那是這段文字的工作。
>
> ⚠ **內建 PDF OCR 頁數上限（100）調大是錯的方向。** 它同時是兩件事的上限：能 OCR 幾頁，以及這個 job 要跑多久。調大 → 更久 → 更確定撞上上面那個 300 秒衝突。要選的值是「`ceil(頁數 ÷ PDF_OCR_CONCURRENCY) × 單頁 VLM 秒數` 塞得進 300 秒」的值；**拿上面 1800 秒／100 頁／並行 4 反推，單頁約 72 秒，也就是大約 16 頁**（推算值，不是量到的）。**一份文件如果需要比這更多頁，它就不該走這條路。**
>
> ⚠ **而超出上限的頁，會連它原生抽到的文字一起不見。** OCR 成功時 `content` 是被**整份取代**的，不是合併，所以一份 120 頁（前 100 頁掃描、後 20 頁是真文字）的文件，OCR 只蓋前 100 頁，**後 20 頁那 1880 個字一個都不會留下**。這件事以前只有一行 log。現在四種損失都寫進 `metadata`，而且是跟 `ocr_used` 放在同一個 dict 裡（`ocr_lossy` + `ocr_losses`），因為 `ocr_used: True` 單獨看起來就像成功：
>
> | `ocr_losses` 欄位 | 意思 | 會在哪裡爆出來 |
> |---|---|---|
> | `native_text_chars_dropped` | 被丟掉的原生抽取字數 | 那些頁在檢索上直接消失 |
> | `pages_not_ocred` | 超過上限、沒被 OCR 的頁數 | 同上，而且是使用者在原稿上讀得到的字 |
> | `page_boundaries_lost` | OCR 結果沒有 `\f` | `pdf-page` chunker 把整份看成一頁，「第 4 頁」的引用全錯 |
> | `image_placeholders_dropped` | `[[IMAGE:…]]` 錨點沒了、`.images` 還在 | caption 步驟照跑照付 VLM，但寫不回任何地方 |
>
> ⚠ **`ocr_lossy` / `ocr_losses` 不是警報，現在沒有任何人看得到它。** `handlers.py:736` 拿到的 `parse_meta` 只傳給 `chunker.chunk`（`:804`）就結束了，**沒有寫進資料庫、沒有進文件狀態、UI 上沒有任何地方會顯示**。使用者看到的仍然只是「已索引」。唯一看得到的是 **worker 的 log**（截斷是 ERROR、其餘三項是 WARNING），而那要你自己去翻。**別假設它會通知你**——要它變成看得到的東西，得有人把它接上文件狀態，那是還沒做的事。
>
> ⚠ **csp 不吃這組旗標，這是刻意的。** csp 的 `/api/ingestion/chunking-preview`（`services/csp/app/api/ingestion/preview.py:240`）呼叫**同一支** `extract_text`，但 `infra/compose/platform.yml` 的 csp 區塊把 `PDF_OCR_FALLBACK` **寫死成 `"false"`**，不吃 `${PDF_OCR_FALLBACK}`。理由：OCR 是 1800 秒等級的同步工作，ingest 有佇列可以扛，HTTP request 沒有。**後果是預覽與實際 ingest 會給出不同的分塊**——掃描件在預覽裡看起來仍然是空的／只有圖片佔位符，實際 ingest 才會走 OCR。**看到這個差異不要當成 bug。**

> ⚠ **為什麼心跳不調快。** 三個 handler 全部在 arq 的事件迴圈上做同步工作：`extract_text` 量到 400 頁 PDF 佔住迴圈 33–89 秒、1000 頁 81–103 秒（同一份程式碼，區間差異來自主機負載），而 `evaluate_strategies` 與 `reresolve_collection_relations` 是**逐份文件跑迴圈**（一個 collection 可以有上百份）。心跳的 TTL 一旦短於這個時間，**正在正常工作的 worker 就會被畫成死的** —— 而那正是這張卡最不能犯的錯。3601 秒蓋得住平台會收的任何文件（單檔上限 50 MB）。
>
> 要把心跳調快，前提是先把**每一個 handler 的每一條阻塞路徑**都搬離事件迴圈（清單見下），不是只搬 `ingest_document` 的 parse。`tests/test_worker_liveness.py` 擋著這件事。

#### 事件迴圈上的同步工作（要動心跳前先把這張表清乾淨）

> 這張表是 **2026-08-05 的一次盤點，不是保證**。它是人工掃出來的，沒有任何自動檢查會在有人新增一條阻塞路徑時提醒你 —— 動 handler 的人有責任同步更新它。第八條（`split_segments`）就是驗收補上的，我第一次盤點時漏了。

| Handler | 位置 | 阻塞的東西 |
|---|---|---|
| `ingest_document` | `handlers.py` parse 段 | `open().read()` + `extract_text`（量到 33–102 秒） |
| `ingest_document` | `handlers.py` chunk 段 | `chunker.chunk`（量到 0.03–0.6 秒） |
| `ingest_document` | `handlers.py` semantic 前處理 | `SemanticChunker.split_segments`（934k 字量到 0.12 秒） |
| `ingest_document` | `handlers.py` `_uniform_color` | PIL `Image.open` + `convert` + 取樣 `getpixel`，**每張內嵌圖一次** |
| `ingest_document` | `handlers.py` `_persist_images` | `open(...,"wb").write()` + `os.chmod`，**每張圖一次** |
| `evaluate_strategies` | `evaluator.py` `_load_sample_docs` | `open().read()` + `extract_text`，**每份樣本文件一次** |
| `evaluate_strategies` | `evaluator.py` `_chunk_doc` | `chunker.chunk` + `SemanticChunker.split_segments`，**每個策略 × 每份文件一次** |
| `reresolve_collection_relations` | `handlers.py` 尾段迴圈 | `open().read()` + `extract_text`，**整個 collection 每份 indexed 文件一次** |

### `ingest_document` 管線（附進度 pct）

| pct | 階段 | 說明 |
|---|---|---|
| 5 | 起始 | 標記 `running` |
| 15 | Parse | `anila_core.ingestion.parsers.extract_text` 走 parser registry |
| 22 | Caption + 落地 images | 選用 VLM：把 `[[IMAGE:<id>]]` 佔位符換成描述、寫 `ingestion_images` + caption embedding |
| 30 | Chunk | 依 `chunking_config`（預設 `hierarchical`）分 parent / leaf |
| 60 | Embed leaf | 逐 chunk 呼叫 embedding endpoint |
| 85 | Index | `CollectionScopedPgVectorStore`：先 `add_parent_chunks` 再 `index_chunks` |
| 100 | 完成 | 更新計數、標記 `indexed` |

**關係抽取（皆 best-effort、皆可關）** 在攝取尾段跑：① regex 引用邊（`relations.py`）② LLM 邊（`llm_relations.py`）③ embedding 主題相似邊（`similarity_relations.py`）。`reresolve_collection_relations` 則對整個 collection 每份 `indexed` 文件重跑三種邊（單份 parse 失敗即跳過）。

---

## 架構與技術棧

- 語言 / runtime：Python `>=3.11`；`hatchling` 打包，原始碼在 `src/ingestion_worker`。
- Job queue：`arq>=0.26`（Redis 後端）。
- 共用 SDK：`anila-core[rag]>=0.14.0` — parser registry、chunking plugins、`PgPool`、`CollectionScopedPgVectorStore`、`VisionProvider`、`IngestionError` 錯誤體系，以及安全工具（憑證解密 `decrypt_credential`、SSRF 防護 `validate_outbound_url`）。`[rag]` extra 帶入 parser 堆疊（pymupdf4llm、python-docx、odfpy、striprtf、Pillow）。
- DB / 向量：`asyncpg>=0.29` + `pgvector>=0.3`，寫 csp-db；chunk 向量欄位 `halfvec(4000)`（migration 0015）。
- HTTP client：`httpx>=0.27`（embedding / VLM / relation-LLM / judge）。
- 設定：`pydantic-settings>=2.0`（`WorkerSettings`，env 載入，`case_sensitive=False`）。

### `.doc` 需要 antiword（Dockerfile 已裝）

legacy `.doc`（二進位 Word）由 `anila-core` 的 `DocParser` 呼叫 **antiword** CLI 轉純文字；缺它時 `.doc` 上傳會在 parse 階段報 `antiword is required for .doc parsing`。Dockerfile 用 `apt-get install antiword` 補上；`DocParser` 以 `["antiword", "--", file_path]` 呼叫，用 `--` 擋掉檔名以 `-` 開頭的 argument injection。

### 向量維度合約（重要）

embedding endpoint 回傳 NV-embed-V2 原生 4096 維、不支援 OpenAI `dimensions` 截斷，故 worker 在 **client 端截斷**到 `EMBEDDING_DIM`（預設 4000，對齊 `halfvec(4000)`；halfvec HNSW 上限即 4000 維），並在每次回應後 assert 維度——不符即拋 `E_EMBED_DIM_MISMATCH`（fail-fast，不讓錯誤維度拖到 asyncpg INSERT 才爆）。

---

## 目錄結構

```
services/ingestion-worker/
├── Dockerfile            # build context = repo root；先裝 packages/anila-core[rag] 再裝本 worker；apt 裝 antiword
├── pyproject.toml
├── src/ingestion_worker/
│   ├── main.py           # Arq WorkerSettings + 三個 job function 註冊 + on_startup/shutdown + retry
│   ├── settings.py       # env 載入的設定（DB / Redis / embedding / vision / relation / similarity）
│   ├── handlers.py       # ingest_document + reresolve_collection_relations 主流程
│   ├── embedder.py       # OpenAI 相容 embedding client（維度截斷 + assert）
│   ├── evaluator.py      # evaluate_strategies handler
│   ├── judge.py          # LLM-as-judge 評分（憑證解密 + SSRF guard）
│   ├── relations.py      # regex 引用邊（rule-based）
│   ├── llm_relations.py  # LLM 關係抽取
│   ├── similarity_relations.py  # embedding 主題相似邊
│   └── parsers.py        # anila_core.ingestion.parsers.extract_text 相容 re-export
└── tests/                # 8 個 test 檔（parsers / uniform_color / llm_relations / handlers_helpers /
                          #   embedder / judge / settings / evaluator_metrics）— 共 144 個 test
```

---

## 啟動與測試

```bash
# 在 stack 中（建議）— 根目錄 compose shim
docker compose up -d --build ingestion-worker            # → infra/compose/platform.yml
# dev stack：docker compose -f compose.dev.yaml up -d --build ingestion-worker

# 本機開發 / 測試（先裝 anila-core，再裝本 worker）
cd services/ingestion-worker
.venv/bin/python -m pytest         # 144 tests；asyncio_mode=auto；testpaths=tests
.venv/bin/ruff check src tests
```

> 新建虛擬環境時，安裝順序與 Dockerfile 一致：先 `pip install -e 'packages/anila-core[rag]'`，再 `pip install -e 'services/ingestion-worker[dev]'`。

compose 中（`infra/compose/platform.yml`）：build context = repo root；`depends_on`（皆 `service_healthy`）`csp-db` / `redis` / `csp`；volume `share/uploads/ingestion`（host）→ 容器 `/var/anila/ingestion-uploads`；CMD `arq ingestion_worker.main.WorkerSettings`；`restart: unless-stopped`。**`docker restart` 不重載 `.env`/compose；套設定一律 `up -d`。**

### 環境變數（取自 `settings.py`，compose 覆寫值另註）

| 變數 | 預設 | 說明 |
|------|------|------|
| `DATABASE_URL` | `postgresql://csp_app:csp@csp-db:5432/csp` | asyncpg DSN，**必須**用 `csp_app` 角色（受 RLS，非 superuser） |
| `REDIS_URL` | `redis://redis:6379` | Arq 佇列後端 |
| `EMBEDDING_BASE_URL` | `http://csp:8000/v1`（compose 預設；`host.docker.internal` 已被 url_guard 結構性拒絕） | embedding endpoint |
| `EMBEDDING_MODEL` / `EMBEDDING_API_KEY` | `nvidia/NV-embed-V2` / `not-set` | 模型 / Bearer token |
| `EMBEDDING_DIM` / `EMBEDDING_TIMEOUT_SECONDS` | `4000` / `30.0` | 截斷維度（對齊 halfvec(4000)）/ 逾時 |
| `UPLOAD_DIR` | `/var/anila/ingestion-uploads` | 與 CSP 共用的上傳 blob 目錄 |
| `SSL_CERT_FILE` | 未設定 | 內部 HTTPS VLM/OCR 的選用 CA bundle；`verify=True` 不因自簽憑證而關閉 |
| `PG_POOL_MIN` / `PG_POOL_MAX` | `1` / `5` | 連線池（亦上限並行度） |
| `ENABLE_IMAGE_CAPTIONS` | `true` | VLM caption 總開關 |
| `VISION_URL` | `""`（compose `http://csp:8000/v1`） | VLM endpoint；空字串停用 caption |
| `VISION_API_KEY` | `not-set` | 呼叫 CSP 的 token。圖說與 PDF OCR 用哪顆模型由治理中心的視覺角色決定 |
| `VISION_CONCURRENCY` / `VISION_TIMEOUT_SECONDS` / `VISION_MAX_IMAGE_BYTES` | `4` / `60.0` / `8 MiB` | 並行 / 逾時 / 超過跳過 caption |
| `ENABLE_RELATION_LLM` | `true` | LLM 關係抽取總開關 |
| `RELATION_LLM_URL` | `""` | 空字串停用 LLM 邊 |
| `RELATION_LLM_MODEL` / `RELATION_LLM_API_KEY` / `RELATION_LLM_VERIFY_SSL` | `gemma4` / `not-set` / `false` | 模型 / token / TLS |
| `RELATION_LLM_TIMEOUT_SECONDS` / `RELATION_LLM_MAX_CHARS` / `RELATION_LLM_MAX_CANDIDATES` | `120.0` / `12000` / `200` | 逾時 / 輸入上限 / 候選上限 |
| `ENABLE_SIMILARITY_EDGES` | `true` | embedding 相似邊總開關 |
| `SIMILARITY_TOP_K` / `SIMILARITY_MIN` / `SIMILARITY_MAX_DOCS` | `3` / `0.75` / `500` | 每文件連 K 個近鄰 / cosine 下限 / 超過略過重算 |
| `DOC_PARSER` / `DOCLING_OCR_LANGS` | `native` / `ch_tra,en` | Docling 路由與 OCR 語系；由 anila-core 直接讀取，compose 負責提供 |

> `SECRET_KEY`、`ANILA_ENV`、`ANILA_ALLOW_*` 由 `anila-core` 安全模組消費（憑證解密 / SSRF / http 端點 fail-closed），compose 由環境注入。

---

## 與其他服務的關係

- **CSP（治理中心）**：上游。enqueue job + 輪詢進度。**Embedding / VLM / relation-LLM 呼叫一律路由經 CSP `/v1` proxy**（compose 指向 `http://csp:8000/v1`），由 CSP `proxy_service` 統一寫 `token_usage`，worker 不自行記帳（`embed()` 收到的 `user_id` 直接 `del`）。對 CSP 以 **`ingestion-worker` 系統 API key** 認證（Model Gateway 的每服務金鑰；compose 由 `INTERNAL_PLATFORM_API_KEY` 注入）。外連前 `anila-core` 依 `ANILA_ENV` / `ANILA_ALLOW_*` 做 http 端點 fail-closed 與 SSRF 檢查。
- **csp-db**：以 `csp_app`（受 RLS）連線；RLS-scoped 寫入用 `SET LOCAL anila.collection_id`。讀 documents / collections / eval_runs / user_llm_credentials，寫 chunks / images / `document_relations` / 狀態 / 計數。
- **Redis**：Arq 佇列後端。
- **共用上傳目錄**：CSP 寫、worker 讀；captioned 圖存 `<UPLOAD_DIR>/anila-images/<doc_id>/`。
- **Judge / relation LLM 的使用者憑證**：使用者自帶憑證（`user_llm_credentials`，AES）即時解密；外連前 `validate_outbound_url`（SSRF），憑證物件 `__repr__` 遮罩 key。

> 本 worker 早於重構的 Task spine / Full Trace / Artifact 合約，且**不參與**它們：它不讀 `X-ANILA-Task-Id`、不發 trace span、不設定分類等級。與重構相關的只有 §17.1 版圖、compose shim，以及經 CSP 的 Model Gateway 金鑰 + `ANILA_ENV` fail-closed 出向守衛。

---

## 相關文件

- [`../../docs/ingestion/ingestion-platform-design.md`](../../docs/ingestion/ingestion-platform-design.md)（含 evaluator §6.5 LLM-as-judge）
- [`../../docs/ingestion/parent-child-rag-design.md`](../../docs/ingestion/parent-child-rag-design.md)
- [`../../docs/archive/anila-core/anila-core-boundary.md`](../../docs/archive/anila-core/anila-core-boundary.md)
- 重構設計沿革（收斂紀錄）：[`../../docs/anila-redesign-docs/`](../../docs/anila-redesign-docs/)（`00-product-constitution.md` 憲章、`02-system-architecture.md` 系統架構）。現行權威＝[`PLAN.md`](../../PLAN.md)（現況與執行順序）、規格＝[`SYSTEM-MAP.md`](../../SYSTEM-MAP.md)。
- 平台整體：[`../../README.md`](../../README.md) · 現行 `main`（舊七分支模型已失效）
