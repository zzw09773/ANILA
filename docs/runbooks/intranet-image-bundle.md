# 內網 image bundle — 打包、攜帶、載入、起棧

> **給明天要上 `.15` 做高階審查的操作者**。內網無外網,所有 image 必須以
> tar.gz 實體攜入。本文件是操作步驟,不是歷史敘事。
>
> 配套腳本:[`infra/deployment/intranet/build-and-export-for-intranet.sh`](../../infra/deployment/intranet/build-and-export-for-intranet.sh)
> 較舊的六月版全流程仍在 [`intranet-deployment-runbook.md`](./intranet-deployment-runbook.md),
> 與現行正式 `-p anila` / ASR profile **不一致處以本檔為準**。本機開發棧若仍叫 `anila-restart`，那是歷史 project 名，不是出貨包前綴。

---

## 0. 先做的決定(寫死,不要到現場再猜)

| 項目 | 本樹預演選擇 | 理由 |
|---|---|---|
| `COMPOSE_PROJECT_NAME` | 正式包:`anila`。本機若不能停 `anila` 活體棧:打包時另設 project 名並 overlay `codeserver` image,見 §8。 | build 出的 tag 前綴 = project 名。內網 `up` 的 `-p` **必須與 bundle 內 tag 一致**,否則會找錯 image。 |
| ASR profile | **預設不帶**(`INCLUDE_ASR=0`) | 平台開機沒語音。麥克風要 `/asr/health` 200 才出現。要開是開機後第二步(§5.1)。若預知之後要開、不想再跑一趟打包,打包時才設 `INCLUDE_ASR=1`(只帶映像;部署仍預設 0)。 |
| `asr-cpu.yml` | **打包不帶**;起棧視 GPU | overlay 只改 device/env,不改 image 名。`.15` 有 GPU → 不要加;無 GPU 才加(見 §5)。 |
| 模型 / 權重 | **不帶**(`WITH_MODELS` / `WITH_WEIGHTS` 不設) | 走 `.12` gateway;權重數十到數百 GB,審查不需要。 |
| docling 映像 | **不帶**(`WITH_DOCLING_IMAGE=0`,預設) | torch＋easyocr＋docling 數 GB。GPU 主機四件套（映像 tar + 權重 tar + `docker-compose.standalone.yml` + `.env.example`）要進包時才設 `WITH_DOCLING_IMAGE=1`（會一併要求 `DOCLING_WEIGHTS_DIR`）。**不要**把 `--profile docling-local` 加進平台 `up`。 |

---

## 1. 在這台開發機 build 與 export

```bash
cd /path/to/ANILA   # 從 restart/from-redesign 的 annotated tag export
# owner verdict (2026-08-14):bundle 的 MANIFEST.txt 會記錄 tag+commit;
# 內網端不 checkout branch,直接接收 bundle 與配套 repo 內容。

# 若映像尚未對齊目前碼(明天正式攜入前建議重建,會花時間):
# COMPOSE_PROJECT_NAME=anila \
# COMPOSE_ENV_FILE=.env \
# bash infra/deployment/intranet/build-and-export-for-intranet.sh /mnt/usb/anila-images-export

# 映像已存在、只想重包(本預演路徑):
COMPOSE_PROJECT_NAME=anila \
COMPOSE_ENV_FILE=.env \
SKIP_BUILD=1 \
SKIP_PULL=1 \
bash infra/deployment/intranet/build-and-export-for-intranet.sh /mnt/usb/anila-images-export

# 預知之後要開語音、這次就把映像帶上 USB(部署仍預設不起 ASR):
# INCLUDE_ASR=1 \
# COMPOSE_PROJECT_NAME=anila \
# COMPOSE_ENV_FILE=.env \
# bash infra/deployment/intranet/build-and-export-for-intranet.sh /mnt/usb/anila-images-export

# 若要把 docling 映像走完五段式並帶上 GPU 主機四件套（預設不帶）:
# ⚠ 沒設 SKIP_BUILD=1 會連平台映像一起重建。平台 tar 已在、且 01-images/
#   已有對應 docling tar 時可加 SKIP_BUILD=1 SKIP_PULL=1；缺 docling tar
#   時 SKIP_BUILD=1 會 fail-loud，不會靜默 build 10GB。
# WITH_DOCLING_IMAGE=1 \
# DOCLING_WEIGHTS_DIR=/path/to/fetch-docling-weights-output \
# COMPOSE_PROJECT_NAME=anila \
# COMPOSE_ENV_FILE=.env \
# bash infra/deployment/intranet/build-and-export-for-intranet.sh /mnt/usb/anila-images-export
```

腳本會:

1. `docker compose … config --images` **衍生**清單（project `anila`；不是手寫舊前綴 `anila-platform-*`）
2. 缺任何一張、或 `docker save` 失敗 → **拒絕打包並點名**(寧可失敗,不要靜默短包)
3. 寫出 `01-images/<safe>.tar.gz`(一 image 一檔)、`01-compose-images.images.txt`、
   `01-compose-images.files.txt`、`MANIFEST.txt`、`CHECKSUMS.sha256`、`INTRANET-LOAD.sh`

輸出目錄請放 **repo 外**(USB / `/tmp/...`);不要寫進 git 工作樹。

---

## 2. 要帶進內網的東西

最少攜帶整個 export 目錄:

| 檔案 | 用途 |
|---|---|
| `01-images/*.tar.gz` | 有效 compose 每一張 image 一檔(`INCLUDE_ASR=1` 才含語音) |
| `01-compose-images.images.txt` | 應有的 tag 清單 |
| `01-compose-images.files.txt` | 檔名 ↔ tag 對照(LOAD 腳本用) |
| `CHECKSUMS.sha256` | 媒體完整性 |
| `MANIFEST.txt` | 給 IT 對大小 / digest / 服務對照 |
| `INTRANET-LOAD.sh` | 內網一鍵 load |
| `intranet-image-overrides.yml` | 內網 `up` 時將 digest-pinned image 疊成已 load 的 tag-only reference |

另外還要(不在 image bundle 內,但沒有就起不來):

- 內網用 `.env`(祕密;**不要**把本機 `CARD_DEV_*` / mock CA 變數帶過去)
- `secrets/jwt-{private,public}.pem`(缺 → JWKS 500、登入炸)
- TLS:`server.crt` / `server.key`(或從 `server.pfx` 抽)
- 模型 CA:`share/pki/model-ca.pem`(可用 repo 內 `cspki_ca_bundle.pem` 那條 CSPKI 鏈)
- 與 bundle 對應的 repo 樹(compose / nginx 設定要跟 image 對得上):由
  `restart/from-redesign` 的 annotated tag export,`MANIFEST.txt` 記錄 tag+commit;
  內網端不 checkout branch,只接收 bundle

預演量級(本機 2026-08-02,當時含 gitlab + asr-decoder,不含模型):大約十 GB 級;
2026-09-26 起出貨不再帶 GitLab 映像。
出發前看 `du -sh` 與 `MANIFEST.txt` 當日數字,USB 預留餘裕。

---

## 3. 抵達內網後驗媒體

在拷貝目標目錄:

```bash
cd /path/to/anila-images-export
sha256sum -c CHECKSUMS.sha256
# 對 MANIFEST.txt 的「Bundle files」段:檔名、大小一致
ls -lh *.tar.gz
# 可選:不 load 先看各 tarball 裡的 repo:tag
python3 - <<'PY'
import gzip, tarfile, json
from pathlib import Path
root = Path(".")
for ln in (root/"01-compose-images.files.txt").read_text().splitlines():
    fname, img = ln.split("\t", 1)
    tags=set()
    with gzip.open(root/"01-images"/fname, "rb") as gz, tarfile.open(fileobj=gz, mode="r|") as tf:
        for m in tf:
            if m.name.endswith("manifest.json"):
                for e in json.load(tf.extractfile(m)):
                    tags.update(e.get("RepoTags") or [])
    ok = img in tags or (img+":latest") in tags
    print(("OK" if ok else "MISSING"), img, "->", sorted(tags))
PY
# 應涵蓋 01-compose-images.images.txt 每一行;且沒有舊前綴 anila-platform-*
```

`INTRANET-LOAD.sh` 也會在 load 前跑 `sha256sum -c` 並確認預期 tarball 存在。

---

## 4. Load(不要弄髒正在跑的驗證棧)

```bash
cd /path/to/anila-images-export
bash INTRANET-LOAD.sh
```

成功後應用 `*.images.txt` 內每個 image reference 都能驗證;含 `@sha256:` 的 pinned
reference 會由 loader 以去掉 digest 的 tag-only form inspect,但報告仍保留原始 pinned form。

⚠ **本機預演時不要對正在跑的 daemon 做會覆蓋/retag 執行中映像的 load**。
驗 bundle 用 §3 的 archive 巡檢即可;若一定要證明 loadable,load 到暫存 tag
命名空間再刪除,或換一台 throwaway Docker host。

---

## 5. 起棧

```bash
cd /path/to/ANILA   # 內網上的 repo

# 將 bundle 產生的 override 放到 compose.yaml 同一層
cp /path/to/anila-images-export/intranet-image-overrides.yml .

# 外部 network(模型棧用;gateway-only 審查也常已存在)
docker network create anila-models-net 2>/dev/null || true

# 平台本體。不要加 --profile asr。
# ⚠ 不要加 --profile docling-local：平台主機是 CPU-only，docling 在獨立 GPU 主機。
# 語音與 docling 是開機後第二步(§5.1),預設沒功能才不會靜默失敗。
COMPOSE_PROJECT_NAME=anila \
docker compose --env-file .env -p anila \
  -f compose.yaml -f intranet-image-overrides.yml \
  up -d --no-build
```

`INTRANET-LOAD.sh` 若看到 `06-docling-gpu-host.tar` / `06-docling-image.files.txt`，
**不會**在平台主機 `docker load` docling 映像，也不解開權重。把四件套複製到 GPU
主機再 load／解／`docker compose -f docker-compose.standalone.yml`。步驟見
`services/docling-service/README.md`。

### 5.1 開機後第二步:語音 / docling

預設沒這兩樣。殼上麥克風不畫、匯入走 native parser。缺 GPU／權重／端點時不要先起服務。

**語音**(解碼端與權重已就緒之後):

```bash
# 映像不在包內時,先回有外網的機器 INCLUDE_ASR=1 重包並 load。
INCLUDE_ASR=1 ASR_OVERLAY=infra/compose/asr-cpu.yml \
  bash infra/deployment/intranet/intranet-deploy.sh /path/to/image-bundle
# 或手動(映像已 load):
# docker compose --env-file .env -p anila \
#   -f compose.yaml -f intranet-image-overrides.yml -f infra/compose/asr-cpu.yml --profile asr \
#   up -d --no-build
# GPU 主機把 asr-cpu.yml 換成 asr-gpu.yml,或拿掉 overlay。
docker compose -p anila exec nginx nginx -s reload
```

細節與 token、協定選錯會拒開機:`docs/runbooks/asr-voice-input.md`。

**docling**:平台側維持 `DOC_PARSER=native`,直到 GPU 主機四件套全綠。
**不要**在平台 `up` 加 `--profile docling-local`。閘門與步驟:
`services/docling-service/README.md`。連不上不准靜默退回 native。

### 起棧後必做:reload nginx

```bash
docker compose -p anila exec nginx nginx -s reload
```

**原因**:upstream 區塊的 DNS 只在載入設定時解析一次。`up -d` recreate 任何
服務後,nginx 可能仍指向舊 IP → **全站 502,但容器全綠**。這是本 repo 付過學費的坑。

### 改了 `infra/nginx/anila.conf` 時

該檔是 **單一檔 bind-mount**。git 改檔會換 inode,容器仍握著舊 inode、無錯誤訊息。
此時要:

```bash
docker compose -p anila -f compose.yaml -f intranet-image-overrides.yml up -d --force-recreate nginx
# 然後再確認健康;不要只 reload
```

⚠ 不要把檔案 `docker cp` 進 `conf.d/`——會變成第二份設定,`resolver` 重複宣告 → `nginx -t` 失敗。

---

## 6. 驗證起來了

### 6.1 五個入口(看 Content-Type,不要只看 200)

SPA catch-all 會對不存在路徑回 `200 text/html`,看起來像「有服務」。

```bash
for p in / /anila/ /anilalm/ /asr/health /router/health; do
  printf '%-16s ' "$p"
  curl -sk -o /dev/null -w '%{http_code} %{content_type}\n' "https://localhost$p"
done
# 預設 /asr/health 不是 200(語音沒開)。做完 §5.1 才應是 200 JSON。
```

預期大致:

> `/anilalm/` 自 2026-09-26 起應回 **200**（或先 301 再 200）。ANILA LM 已開放。
> 若仍是 503「尚未開放」，是舊閘門還沒套上，見 [`anilalm-release-gate.md`](./anilalm-release-gate.md)。

| 路徑 | status | content-type 方向 |
|---|---|---|
| `/` | 200 | HTML(治理中心) |
| `/anila/` | 200 | HTML(shell) |
| `/anilalm/` | **200**（裸路徑可能先 **301**） | HTML — ANILA LM 已開放；若是 503 見 `anilalm-release-gate.md` |
| `/asr/health` | **502 / 非 200**(預設) | 語音沒開。§5.1 之後才應是 **200 JSON** |
| `/router/health` | 200 | **JSON**(不是 text/html) |

### 6.2 csp 內部探測 — 容器沒有 `curl`

```bash
CSP=$(docker compose -p anila ps -q csp)
docker exec "$CSP" python3 -c 'import httpx; r=httpx.get("http://127.0.0.1:8000/api/health", timeout=5); print(r.status_code, r.headers.get("content-type"), r.text[:200])'
```

用 `docker exec … curl` 會得到空輸出 → **假陰性**。

### 6.3 其他

- `docker compose -p anila ps` 全部 healthy / running
- alembic head 與出發前一致(本樹預演當下為 `r1_0031`,以當日為準)
- 登入路徑:卡登或 break-glass 依內網 `.env`,**不要**帶本機 mock 讀卡變數

---

## 7. 已知陷阱(再列一次)

1. **`docker restart` ≠ recreate** — 不重載 `.env` / compose;套設定一律 `up -d`。
2. **nginx upstream DNS 過期** — `up -d` 後一定 `nginx -s reload`(§5)。
3. **nginx conf bind-mount inode** — 改 `anila.conf` 要 `--force-recreate nginx`。
4. **csp 無 curl** — 用 `python3 -c "import httpx; …"`。
5. **驗 Content-Type** — 不要被 SPA `200 text/html` 騙。
6. **project 名必須一致** — 打包 `anila-*` ↔ 內網 `-p anila`。
7. **缺 JWT keypair** — JWKS 500、登入與 studio 全滅;先產 `secrets/jwt-*.pem`。
8. **`SSL_CERT_FILE` / `ANILA_MODEL_CA_FILE` 是取代信任庫** — 指到空/壞檔,所有出向 https 掛。

---

## 8. [已 superseded] 本機陷阱:sisidsdaemon 會讓「對正在跑的 image 做 docker save」失敗

> **Superseded 2026-08-13:** built images 已改由隔離 `docker-container` buildx
> 直接輸出 tar，並以 tar-mode scan + load-verify 取代本節的 built-image
> save/preflight/rebuild 路徑；本節以下內容保留為歷史故障紀錄，不是目前操作指示。

症狀:

```text
Error response from daemon: open /var/lib/docker/overlay2/.../merged/run/sisidsdaemon.pid: no such file or directory
```

腳本在最終打包前會**逐張 preflight save**並點名失敗的 tag。不要略過。

### ⚠ 更正(2026-08-02 實測):停棧**沒有用**

本節原本寫「先停棧再 export」是推薦解法。**實測推翻**:2026-08-02 演練時整棧
`stop`(15 個容器全數停止,`ps -q` 為 0)後直接 export,同樣那四張
(`anila-codeserver:local`、`anila-anila-studio`、`anila-asr-gateway`、
`anila-ingestion-worker`)照樣 save 失敗,錯誤訊息一字不差。

IDS 毒的是 **overlay 層本身**,不是「容器正在跑」這件事。停棧不會讓被毒的層恢復。
照原建議做的人會停掉整個平台、等九分鐘、然後拿到一樣的失敗。

**實際可行的解法**:

`REBUILD_ON_SAVE_FAIL=1` —— save 失敗時對「有 `build:` 的服務」立刻
`compose build --no-cache` 該服務並馬上再 save,搶在 IDS 再次掃描前完成。

```bash
COMPOSE_PROJECT_NAME=anila INCLUDE_ASR=1 \
SKIP_BUILD=1 SKIP_PULL=1 REBUILD_ON_SAVE_FAIL=1 \
  bash infra/deployment/intranet/build-and-export-for-intranet.sh /tmp/anila-intranet-bundle
```

⚠ 這個旗標會對失敗的服務 `--no-cache` 重建,而 `anila-codeserver:local` 與
`anila/asr-decoder:0.1.0` 是**寫死的共用 tag**——重建會 retag 正在跑的那張。
所以**執行前要先停棧**(停棧對 IDS 無效,但對「不要動到正在服務的映像」有效),
或改用另一個 `COMPOSE_PROJECT_NAME` 加 overlay 換 tag(見下)。

**上游 image**(pg/redis/nginx/n8n)沒有 `build:`,救不回來——那幾張如果被毒到
只能 `docker pull` 重抓,內網無網路時就必須從有網路的機器重新打包。

**替代路徑**(棧完全不能動時):用另一個 `COMPOSE_PROJECT_NAME`(例如
`anila-pack-rehearsal`)build 出新 tag 再 save,並用 overlay 把 `codeserver` 的
寫死 tag 換掉。⚠ 這樣產出的 bundle 內 tag 前綴是那個 project 名,
**內網起棧的 `-p` 必須同名**,否則 compose 找不到映像。

---

## 9. 預演驗收時做過什麼(給交接)

- 腳本改為自 `docker compose config --images` 衍生清單;`COMPOSE_PROJECT_NAME` 參數化。
- 當時 `INCLUDE_ASR=1` 預設(v1.2.1 起改為 0,見 §0 / §5.1);缺圖 / save 失敗都 fail-loud 並點名。
- 對執行中的 `anila-*` 直接 save 時,studio / ingestion-worker / asr-gateway / codeserver 被本機 sisidsdaemon 擋下 → 改以 `anila-pack-rehearsal` project + codeserver image overlay 產出完整 bundle(證明腳本與媒體流程;內網起棧 `-p` 要與 bundle 內 tag 前綴一致,或明天停棧後用 `anila` 重包)。
- Bundle 以 archive 內 `manifest.json` 的 `RepoTags` 驗齊服務 image,並 `bash -n INTRANET-LOAD.sh`。
- **沒有**對本機 daemon `docker load` 回寫執行中 tag。

---

## 10. 前置備貨:opencc(prompt-wire 合併後新增,2026-08-03)

`opencc-python-reimplemented==0.1.7` 成為 **anila-core 的基礎依賴**
(`packages/anila-core/pyproject.toml:44`),用於 assistant 訊息落庫前的 zh-TW 正規化。

**傳遞影響三個映像**(它們的 Dockerfile 都 COPY anila-core):
`anila-agent`、`ingestion-worker`、`anila-core-router`。

⚠ **這三個映像若在內網重建,會卡在 `pip install opencc-python-reimplemented`。**
氣隙沒有 PyPI,症狀是 build 停在下載、operator 拿到一個跟語音／中文完全無關的錯誤。

**兩條路,擇一:**

1. **帶映像進去,不要在內網 build**(推薦,也是本 runbook 的主線)。
   映像裡已經裝好,不需要 wheel。起棧務必帶 `--no-build`。
2. 若真的需要在內網重建,先在有網路的機器備好 wheel:
   ```bash
   pip download opencc-python-reimplemented==0.1.7 -d /tmp/anila-wheelhouse
   ```
   連同 bundle 一起帶進去,build 時指向該目錄。

**驗證有沒有裝到**(在內網起棧後):
```bash
docker compose -p anila exec -T csp python3 -c "import opencc; print('opencc ok')"
```

### 順帶:zh-TW 正規化刻意不改的字

`packages/anila-core/src/anila_core/text/domain_terms.py:12-14` 明確**不**把
「質量」改成「品質」——國防／工程語境的「質量」多半是 mass(質量守恆、彈頭質量、
質量流率),無條件改寫會污染物理術語。簡體「质量」仍會經 s2twp 轉成「質量」
(字形轉換,非詞替換)。這是刻意的,不要當成漏字補上去。
