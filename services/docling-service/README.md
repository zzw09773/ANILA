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

docling 是 GPU 主機上的 HTTP 端點，不是平台映像。有兩種跑法，依它跟平台的相對位置選。

映像與權重都是選配，都要到 GPU 主機。權重 tar（`05-weights-docling.tar`）是
**複製、不要移動**——檔名列在 `CHECKSUMS.sha256` 裡，搬走之後再驗 checksum 會擋死。

### ① 同機（少見：這台機器同時是平台且有 GPU，不開 host port）

`infra/compose/platform.yml` 的 docling 區（`profiles: ["docling-local"]`），
要 GPU 的主機再疊 `infra/compose/docling-gpu.yml`：

```bash
docker compose -p anila-restart \
  -f compose.yaml -f infra/compose/docling-gpu.yml \
  --profile docling-local up -d
```

CPU-only 平台主機：**不要**加 `--profile docling-local`，也不要疊 `docling-gpu.yml`。
沒加 profile 時 docling 容器不起、`up` 不因它失敗。

### ② 跨機（預設故事：獨立 GPU 主機，平台以 http 打進來）

氣隙包在 `WITH_DOCLING_IMAGE=1` 時帶上 GPU 主機四件套。預設
`WITH_DOCLING_IMAGE=0`，不用這功能的人不扛數 GB 的 torch／easyocr／docling。

打包走與其他 built image **同一條**五段式：`buildx bake` 直出 tar → 雜物掃描
掃 tar → 真的 `docker load` 驗回。不是旁路、也不把 `--profile docling-local`
加進平台有效組態。

在 GPU 主機（全新機器可整段複製；先解開四件套裡的 `.env` 再解權重，
映像 tar 檔名由打包腳本依實際 tag 衍生，寫在 `06-docling-image.files.txt`
第一欄，不要假設檔名）：

```bash
# 1. 解開 06-docling-gpu-host.tar（docker-compose.standalone.yml、.env.example、README.md）
tar -xf 06-docling-gpu-host.tar
cp .env.example .env          # 填 token / 權重路徑 / 卡號
set -a
# shellcheck disable=SC1091
. ./.env
set +a

# 2. 映像 tar（檔名見 06-docling-image.files.txt 第一欄 / MANIFEST）
gunzip -c "01-images/$(cut -f1 06-docling-image.files.txt)" | docker load

# 3. 權重 tar：複製（不要移動）過來，解到 .env 的 DOCLING_MODEL_HOST_DIR
mkdir -p "$DOCLING_MODEL_HOST_DIR"
tar -xf 05-weights-docling.tar -C "$DOCLING_MODEL_HOST_DIR"

docker compose -f docker-compose.standalone.yml -p docling up -d
```

平台側 csp 的 `DOCLING_URL` 設成 `http://<這台>:9100`，`DOCLING_SERVICE_TOKEN`
兩邊一致。純 http 需在平台側開 `ANILA_ALLOW_HTTP_ENDPOINT=1`（P0.2 分域旗標）。

- 權重來源：在有網路的機器跑 `infra/deployment/offline/fetch-docling-weights.sh`，
  產出一個可在氣隙內掛載的目錄（含 `DOCLING-WEIGHTS-MANIFEST.txt`）。
- `DOCLING_OCR_LANGS`／`DOCLING_TABLE_STRUCTURE`／`DOCLING_PICTURE_DESCRIPTION`
  與平台側（in-process 時代）同名同義。

> 🔴 **上線閘門**（開機後第二步，不是平台 `up` 的一部分）：以下條件**全部**滿足，才可把平台側 `DOC_PARSER` 設成
> `docling`。任一條沒滿足：維持 `DOC_PARSER=native`。
>
> 1. 打包時 `WITH_DOCLING_IMAGE=1`，且 docling 映像已走完五段式（buildx bake
>    直出 tar → 雜物掃描 → 真的 `docker load` 驗回）。
> 2. GPU 主機已取得四件套：映像 tar、`05-weights-docling.tar`、
>    `docker-compose.standalone.yml`、`.env.example`（後兩件與 README 在
>    `06-docling-gpu-host.tar`）。
> 3. 權重 tar 已**複製**（不要移動）到 GPU 主機，解到 `DOCLING_MODEL_HOST_DIR`
>    （目錄內有 `DOCLING-WEIGHTS-MANIFEST.txt`）。
> 4. GPU 主機用 standalone compose 起得來，`GET /health` 為 200；平台側
>    `DOCLING_URL` 指向該端點，`DOCLING_SERVICE_TOKEN` 兩端一致。
> 5. 平台主機 **沒有**加 `--profile docling-local`（CPU-only 平台永遠不起這容器）。

## 映像體積（已知成本）

2026-08-20 對已出貨 tag `anila/docling-service:0.1.0` 實測（`du`，不是估的）：

| 路徑 | 大小 |
|---|---|
| `/usr/local/cuda-12.6`（`nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04`） | 2.0G |
| `/opt/venv/lib/python3.12/site-packages/nvidia/*`（torch cu126 wheels） | 3.6G |
| `libcublas.so.12` | 兩份各 108244960 bytes（base 與 pip 各一） |
| `libcudnn.so.9` | 只在 pip 那份（base 這次沒有第二份） |

刪掉容器內 `/usr/local/cuda-12.6` 之後 CPU 上 `import torch` / `easyocr` / `docling` 仍成功。未改 Dockerfile 去換 `-base`：本機 docker 的 `nvidia-container-runtime` 二進位不在 PATH，`--gpus all` 與 `--runtime=nvidia` 都起不來，無法重跑 GPU POST L312.pdf 的 markdown sha256 回歸。數字當已知成本，不拿未驗證的瘦身出貨。

## 測試

本服務測試用 fake converter，不載 docling 權重，`pytest tests/` 可離線跑。
platform 主機上**無法** py 收集這些測試（docling 套件不在那），照
`PRETAG-RESIDUALS` 的判準：沒被宣告的套件不會被跑。
