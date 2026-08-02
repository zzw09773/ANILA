# 內網 image bundle — 打包、攜帶、載入、起棧

> **給明天要上 `.15` 做高階審查的操作者**。內網無外網,所有 image 必須以
> tar.gz 實體攜入。本文件是操作步驟,不是歷史敘事。
>
> 配套腳本:[`infra/deployment/intranet/build-and-export-for-intranet.sh`](../../infra/deployment/intranet/build-and-export-for-intranet.sh)
> 較舊的六月版全流程仍在 [`intranet-deployment-runbook.md`](./intranet-deployment-runbook.md),
> 與重啟樹現行 `-p anila-restart` / ASR profile **不一致處以本檔為準**。

---

## 0. 先做的決定(寫死,不要到現場再猜)

| 項目 | 本樹預演選擇 | 理由 |
|---|---|---|
| `COMPOSE_PROJECT_NAME` | 明天正式包:`anila-restart`(先停棧再 save)。預演若不能停棧:見 §8 的 `anila-pack-rehearsal`。 | build 出的 tag 前綴 = project 名。內網 `up` 的 `-p` **必須與 bundle 內 tag 一致**,否則會找錯 image。 |
| ASR profile | **打包進去**(`INCLUDE_ASR=1`,預設) | 審查棧含語音;`asr-gateway` + `asr-decoder`(`anila/asr-decoder:0.1.0`)必須在 bundle。 |
| `asr-cpu.yml` | **打包不帶**;起棧視 GPU | overlay 只改 device/env,不改 image 名。`.15` 有 GPU → 不要加;無 GPU 才加(見 §5)。 |
| 模型 / 權重 | **不帶**(`WITH_MODELS` / `WITH_WEIGHTS` 不設) | 走 `.12` gateway;權重數十到數百 GB,審查不需要。 |

---

## 1. 在這台開發機 build 與 export

```bash
cd /path/to/ANILA   # 含要部署的 commit;預演用 wt-pack-rehearsal 亦可

# 若映像尚未對齊目前碼(明天正式攜入前建議重建,會花時間):
# COMPOSE_PROJECT_NAME=anila-restart \
# COMPOSE_ENV_FILE=.env \
# INCLUDE_ASR=1 \
# bash infra/deployment/intranet/build-and-export-for-intranet.sh /mnt/usb/anila-images-export

# 映像已存在、只想重包(本預演路徑):
COMPOSE_PROJECT_NAME=anila-restart \
COMPOSE_ENV_FILE=.env \
INCLUDE_ASR=1 \
SKIP_BUILD=1 \
SKIP_PULL=1 \
bash infra/deployment/intranet/build-and-export-for-intranet.sh /mnt/usb/anila-images-export
```

腳本會:

1. `docker compose … config --images` **衍生**清單(不是手寫 `anila-platform-*`)
2. 缺任何一張、或 `docker save` 失敗 → **拒絕打包並點名**(寧可失敗,不要靜默短包)
3. 寫出 `01-images/<safe>.tar.gz`(一 image 一檔)、`01-compose-images.images.txt`、
   `01-compose-images.files.txt`、`MANIFEST.txt`、`CHECKSUMS.sha256`、`INTRANET-LOAD.sh`

輸出目錄請放 **repo 外**(USB / `/tmp/...`);不要寫進 git 工作樹。

---

## 2. 要帶進內網的東西

最少攜帶整個 export 目錄:

| 檔案 | 用途 |
|---|---|
| `01-images/*.tar.gz` | 有效 compose(含 ASR)每一張 image 一檔 |
| `01-compose-images.images.txt` | 應有的 tag 清單 |
| `01-compose-images.files.txt` | 檔名 ↔ tag 對照(LOAD 腳本用) |
| `CHECKSUMS.sha256` | 媒體完整性 |
| `MANIFEST.txt` | 給 IT 對大小 / digest / 服務對照 |
| `INTRANET-LOAD.sh` | 內網一鍵 load |

另外還要(不在 image bundle 內,但沒有就起不來):

- 內網用 `.env`(祕密;**不要**把本機 `CARD_DEV_*` / mock CA 變數帶過去)
- `secrets/jwt-{private,public}.pem`(缺 → JWKS 500、登入炸)
- TLS:`server.crt` / `server.key`(或從 `server.pfx` 抽)
- 模型 CA:`share/pki/model-ca.pem`(可用 repo 內 `cspki_ca_bundle.pem` 那條 CSPKI 鏈)
- **同一 commit** 的 repo 樹(compose / nginx 設定要跟 image 對得上)

預演量級(本機 2026-08-02,含 gitlab + asr-decoder,不含模型):大約十 GB 級;
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
# 應涵蓋 01-compose-images.images.txt 每一行;且沒有 anila-platform-*
```

`INTRANET-LOAD.sh` 也會在 load 前跑 `sha256sum -c` 並確認預期 tarball 存在。

---

## 4. Load(不要弄髒正在跑的驗證棧)

```bash
cd /path/to/anila-images-export
bash INTRANET-LOAD.sh
```

成功後應用 `*.images.txt` 內每個 tag 都能 `docker image inspect`。

⚠ **本機預演時不要對正在跑的 daemon 做會覆蓋/retag 執行中映像的 load**。
驗 bundle 用 §3 的 archive 巡檢即可;若一定要證明 loadable,load 到暫存 tag
命名空間再刪除,或換一台 throwaway Docker host。

---

## 5. 起棧

```bash
cd /path/to/ANILA   # 內網上的 repo

# 外部 network(模型棧用;gateway-only 審查也常已存在)
docker network create anila-models-net 2>/dev/null || true

# 有 GPU 的 .15(目標組態)— 含 ASR,不要 asr-cpu overlay
COMPOSE_PROJECT_NAME=anila-restart \
docker compose --env-file .env -p anila-restart \
  -f compose.yaml --profile asr \
  up -d --no-build

# 若該主機沒有 nvidia container runtime,改用 CPU overlay(審查權宜):
# docker compose --env-file .env -p anila-restart \
#   -f compose.yaml -f infra/compose/asr-cpu.yml --profile asr \
#   up -d --no-build
```

### 起棧後必做:reload nginx

```bash
docker compose -p anila-restart exec nginx nginx -s reload
```

**原因**:upstream 區塊的 DNS 只在載入設定時解析一次。`up -d` recreate 任何
服務後,nginx 可能仍指向舊 IP → **全站 502,但容器全綠**。這是本 repo 付過學費的坑。

### 改了 `infra/nginx/anila.conf` 時

該檔是 **單一檔 bind-mount**。git 改檔會換 inode,容器仍握著舊 inode、無錯誤訊息。
此時要:

```bash
docker compose -p anila-restart up -d --force-recreate nginx
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
```

預期大致:

| 路徑 | status | content-type 方向 |
|---|---|---|
| `/` | 200 | HTML(治理中心) |
| `/anila/` | 200 | HTML(shell) |
| `/anilalm/` | 200 | HTML |
| `/asr/health` | 200 | **JSON**(不是 text/html) |
| `/router/health` | 200 | **JSON**(不是 text/html) |

### 6.2 csp 內部探測 — 容器沒有 `curl`

```bash
CSP=$(docker compose -p anila-restart ps -q csp)
docker exec "$CSP" python3 -c 'import httpx; r=httpx.get("http://127.0.0.1:8000/api/health", timeout=5); print(r.status_code, r.headers.get("content-type"), r.text[:200])'
```

用 `docker exec … curl` 會得到空輸出 → **假陰性**。

### 6.3 其他

- `docker compose -p anila-restart ps` 全部 healthy / running
- alembic head 與出發前一致(本樹預演當下為 `r1_0031`,以當日為準)
- 登入路徑:卡登或 break-glass 依內網 `.env`,**不要**帶本機 mock 讀卡變數

---

## 7. 已知陷阱(再列一次)

1. **`docker restart` ≠ recreate** — 不重載 `.env` / compose;套設定一律 `up -d`。
2. **nginx upstream DNS 過期** — `up -d` 後一定 `nginx -s reload`(§5)。
3. **nginx conf bind-mount inode** — 改 `anila.conf` 要 `--force-recreate nginx`。
4. **csp 無 curl** — 用 `python3 -c "import httpx; …"`。
5. **驗 Content-Type** — 不要被 SPA `200 text/html` 騙。
6. **project 名必須一致** — 打包 `anila-restart-*` ↔ 內網 `-p anila-restart`。
7. **缺 JWT keypair** — JWKS 500、登入與 studio 全滅;先產 `secrets/jwt-*.pem`。
8. **`SSL_CERT_FILE` / `ANILA_MODEL_CA_FILE` 是取代信任庫** — 指到空/壞檔,所有出向 https 掛。

---

## 8. 本機陷阱:sisidsdaemon 會讓「對正在跑的 image 做 docker save」失敗

症狀:

```text
Error response from daemon: open /var/lib/docker/overlay2/.../merged/run/sisidsdaemon.pid: no such file or directory
```

腳本在最終打包前會**逐張 preflight save**並點名失敗的 tag。不要略過。

### ⚠ 更正(2026-08-02 實測):停棧**沒有用**

本節原本寫「先停棧再 export」是推薦解法。**實測推翻**:2026-08-02 演練時整棧
`stop`(15 個容器全數停止,`ps -q` 為 0)後直接 export,同樣那四張
(`anila-codeserver:local`、`anila-restart-anila-studio`、`anila-restart-asr-gateway`、
`anila-restart-ingestion-worker`)照樣 save 失敗,錯誤訊息一字不差。

IDS 毒的是 **overlay 層本身**,不是「容器正在跑」這件事。停棧不會讓被毒的層恢復。
照原建議做的人會停掉整個平台、等九分鐘、然後拿到一樣的失敗。

**實際可行的解法**:

`REBUILD_ON_SAVE_FAIL=1` —— save 失敗時對「有 `build:` 的服務」立刻
`compose build --no-cache` 該服務並馬上再 save,搶在 IDS 再次掃描前完成。

```bash
COMPOSE_PROJECT_NAME=anila-restart INCLUDE_ASR=1 \
SKIP_BUILD=1 SKIP_PULL=1 REBUILD_ON_SAVE_FAIL=1 \
  bash infra/deployment/intranet/build-and-export-for-intranet.sh /tmp/anila-intranet-bundle
```

⚠ 這個旗標會對失敗的服務 `--no-cache` 重建,而 `anila-codeserver:local` 與
`anila/asr-decoder:0.1.0` 是**寫死的共用 tag**——重建會 retag 正在跑的那張。
所以**執行前要先停棧**(停棧對 IDS 無效,但對「不要動到正在服務的映像」有效),
或改用另一個 `COMPOSE_PROJECT_NAME` 加 overlay 換 tag(見下)。

**上游 image**(pg/redis/nginx/n8n/gitlab)沒有 `build:`,救不回來——那幾張如果被毒到
只能 `docker pull` 重抓,內網無網路時就必須從有網路的機器重新打包。

**替代路徑**(棧完全不能動時):用另一個 `COMPOSE_PROJECT_NAME`(例如
`anila-pack-rehearsal`)build 出新 tag 再 save,並用 overlay 把 `codeserver` 的
寫死 tag 換掉。⚠ 這樣產出的 bundle 內 tag 前綴是那個 project 名,
**內網起棧的 `-p` 必須同名**,否則 compose 找不到映像。

---

## 9. 預演驗收時做過什麼(給交接)

- 腳本改為自 `docker compose config --images` 衍生清單;`COMPOSE_PROJECT_NAME` 參數化。
- `INCLUDE_ASR=1` 預設;缺圖 / save 失敗都 fail-loud 並點名。
- 對執行中的 `anila-restart-*` 直接 save 時,studio / ingestion-worker / asr-gateway / codeserver 被本機 sisidsdaemon 擋下 → 改以 `anila-pack-rehearsal` project + codeserver image overlay 產出完整 bundle(證明腳本與媒體流程;內網起棧 `-p` 要與 bundle 內 tag 前綴一致,或明天停棧後用 `anila-restart` 重包)。
- Bundle 以 archive 內 `manifest.json` 的 `RepoTags` 驗齊服務 image,並 `bash -n INTRANET-LOAD.sh`。
- **沒有**對本機 daemon `docker load` 回寫執行中 tag。
