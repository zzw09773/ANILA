# services/docling-service

Docling 文件解析的**遠端 HTTP 服務**（佈局感知 PDF/DOCX/PPTX/XLSX/HTML 轉換）。

> 2026-08-17 擁有者裁決：ANILA 永遠部署在 CPU-only 伺服器。所有 GPU 工作
> （含 docling 的 layout model / EasyOCR / Granite 圖片描述）都以 HTTP 端點
> 抵達，不在平台進程內跑。本服務是 docling 的那個端點；平台側只有一個 httpx
> client（見 `packages/anila-core/src/anila_core/ingestion/docling_parser.py`）。

## 職責邊界

- **這台服務做**：收到 multipart 文件 → docling convert → 回 wire contract 的 JSON。
- **這台服務不做**：切塊、embedding、檢索、RLS（那是 csp / ingestion-worker 的事）。
- **不在平台映像**：docling / torch / easyocr 只活在**這張**映像，平台映像零相依。

## Wire contract

```
POST /parse                     auth: X-Token: <DOCLING_SERVICE_TOKEN>
  Content-Type: multipart/form-data
    file                文件位元組；副檔名驅動格式偵測
    ocr_langs           optional, comma-separated, default "ch_tra,en"
    table_structure     optional bool, default true
    picture_description optional bool, default false
  200 -> {"markdown","title","page_count","ocr_applied",
          "images":[{"id","page","caption","png_b64"}]}
  non-2xx -> {"detail": str}

GET /health -> 200 {"status":"ok","model_ready":bool}
```

## 部署

docling 有**兩種**跑法，依它跟平台的相對位置選（同 asr-decoder）：

### ① 同機（docling 與平台在同一台機器上，不開 host port）

`infra/compose/platform.yml` 的 docling 區（`profiles: ["docling-local"]`），
要 GPU 的主機疊 `infra/compose/docling-gpu.yml`：

```bash
docker compose -p anila-restart \
  -f compose.yaml -f infra/compose/docling-gpu.yml \
  --profile docling-local up -d
```

### ② 跨機（docling 跑在獨立 GPU 主機，平台以 http 打進來）

用 **`docker-compose.standalone.yml`**（本目錄）。平台主機叫它：

```bash
# 權重先解到本機（見下），再起：
cp .env.example .env
docker compose -f docker-compose.standalone.yml -p docling up -d
```

平台側 csp 的 `DOCLING_URL` 設成 `http://<這台>:9100`，`DOCLING_SERVICE_TOKEN`
兩邊一致。純 http 需在平台側開 `ANILA_ALLOW_HTTP_ENDPOINT=1`（P0.2 分域旗標）。

- 權重來源：在有網路的機器跑 `infra/deployment/offline/fetch-docling-weights.sh`，
  產出一個可在氣隙內掛載的目錄（含 `DOCLING-WEIGHTS-MANIFEST.txt`）。
- `DOCLING_OCR_LANGS`／`DOCLING_TABLE_STRUCTURE`／`DOCLING_PICTURE_DESCRIPTION`
  與平台側（in-process 時代）同名同義。

> 🔴 **上線閘門（2026-08-17）**：這張 docling 映像**尚未通過本專案的交付閘門**
> （雜物掃描＋真的 `docker load` 驗回）。`infra/deployment/intranet/build-and-export-for-intranet.sh:93`
> 目前只帶 `--profile asr`，**沒有** `--profile docling-local`——docling 映像
> 不在氣隙 bundle 的 build／export 範圍。**因此在這張映像通過交付閘門、進得來
> 氣隙之前，`DOC_PARSER=docling` 不得在內網啟用**（平台側 DOC_PARSER 維持 `native`
> 預設不變）。進氣隙的工程與撤除程序見
> `~/anila-deliverables/queued-docling-image-delivery-20260817.md`。

## 測試

本服務測試用 fake converter，不載 docling 權重，`pytest tests/` 可離線跑。
platform 主機上**無法** py 收集這些測試（docling 套件不在那），照
`PRETAG-RESIDUALS` 的判準：沒被宣告的套件不會被跑。
