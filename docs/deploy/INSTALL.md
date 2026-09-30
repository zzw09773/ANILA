# 第一次安裝

在一台還沒有 ANILA 的內網主機上，從出貨包裝好平台並登入。之後的更新與回復見 [`UPDATE.md`](UPDATE.md)。

本文照 2026-09-30 在演練機 `172.16.120.35` 從零實裝的過程寫成，每一步都實際跑過。那次演練撞到的問題都已修進腳本，列在文末。

---

## 0. 需要什麼

| 項目 | 說明 |
|---|---|
| 出貨包 | 開發機打包產生的 `anila-YYYY.MM.DD-N.tar.gz`，約 4 GB。還要有打包結束時印出的 SHA256 |
| 主機 | Linux，Docker 與 docker compose 2.17 以上。帳號要在 `docker` 群組 |
| sudo | 只有第 2 步建兩個目錄時要用。之後都用一般帳號 |
| 磁碟 | 安裝目錄與 Docker 目錄各留約出貨包的四倍（約 20 GB） |
| 模型 | 模型不在出貨包裡，由模型主機提供。安裝完才在治理中心登錄 |

---

## 1. 在開發機打包

工作目錄必須乾淨（`git status` 沒有變更）。在 repo 根目錄：

```bash
bash scripts/release/build-release.sh
```

有快取時約 20 分鐘，第一次或清過快取時更久。結束時印出：

```
<SHA256>  dist/releases/anila-YYYY.MM.DD-N.tar.gz
✓ 已產出 …
```

把 SHA256 抄下來，和壓縮檔一起帶進內網。

> 打包用隔離的 buildx builder `anila-pkg` 直接輸出映像，不經本機 Docker。
> 這台開發機的賽門鐵克 IDS 會弄壞本機 Docker 的映像層，舊的 `docker save` 做法會失敗。
> 清磁碟時若刪了 `buildx_buildkit_anila-pkg0_state`，只是快取沒了，下次打包會重建。

---

## 2. 主機準備（每台做一次）

把出貨包放到主機上，例如 `~/anila-YYYY.MM.DD-N.tar.gz`，先核對 SHA256：

```bash
sha256sum ~/anila-YYYY.MM.DD-N.tar.gz     # 要和打包時印出的值一樣
```

建安裝目錄與安裝記錄目錄。只有這一步要 sudo，擁有者設成之後執行安裝的帳號：

```bash
sudo install -d -o "$USER" -g "$USER" -m 755 /opt/anila
sudo install -d -o "$USER" -g "$USER" -m 700 /var/lib/anila
```

解開出貨包，跑預檢：

```bash
tar -xzf ~/anila-YYYY.MM.DD-N.tar.gz -C /opt/anila
bash /opt/anila/anila-YYYY.MM.DD-N/preflight.sh ~/anila-YYYY.MM.DD-N.tar.gz
```

預檢不改任何東西。每一條 `✗` 都附上要執行的指令，處理完再跑一次，直到最後一行是「全部通過」。

### 443 被別的服務佔用時

預檢會寫「埠 443 被 xxx 佔用」。在第一次安裝**之前**指定另一個埠：

```bash
mkdir -p /opt/anila/state
umask 077
printf 'NGINX_HTTPS_PORT=8443\n' >> /opt/anila/state/.env
```

安裝程式會沿用這個值，入口檢查也走這個埠。之後網址要帶埠：`https://<主機>:8443/`。
正式機 443 若是空的，不必做這一步。

---

## 3. 第一次安裝

**在終端機直接執行，不要把輸出導到檔案。** 安裝程式會把新產生的密鑰印在畫面上一次；導到檔案，密鑰就留在那個檔裡。

```bash
bash /opt/anila/anila-YYYY.MM.DD-N/anila-update.sh /opt/anila/anila-YYYY.MM.DD-N
```

它只問一題：

```
站台名稱 ANILA_HOST（院內這台的名稱或 IP）：
```

輸入同仁在瀏覽器要打的名稱或 IP。之後依序會看到：

| 階段 | 畫面 | 時間（演練機） |
|---|---|---|
| 核對 | `manifest.txt: OK`、`這台還沒有在跑的平台，略過公告與更新前備份` | 數秒 |
| 載入映像 | 15 行 `Loaded image: anila-bundle/…` | 約 10 分鐘 |
| 密鑰 | `已產生密鑰（只顯示這一次，另存 /opt/anila/state/generated-secrets.txt）` | 數秒 |
| 憑證 | `沒有現成的 TLS 憑證，先簽一張…的自簽憑證` | 數秒 |
| JWT | `已產生 JWT 簽章金鑰` | 數秒 |
| 啟動 | 建網路、volume、容器，等資料庫與 CSP 健康 | 約 3 分鐘 |
| 入口 | `✓ 入口已開啟，登入頁回應 200` | |
| 完成 | `✓ 更新完成：none → YYYY.MM.DD-N` | |

`/opt/anila/state/generated-secrets.txt`（權限 600）存著管理員密碼與資料庫密碼。抄進密碼管理器後，這個檔可以留著（只有安裝帳號讀得到），也可以刪掉。

### 中途失敗時

照原樣再跑同一行指令。演練時第一次停在 JWT 那一步，修好後直接重跑就裝完了，不必先清任何東西。已產生的密鑰與憑證會沿用，不會重生。

失敗原因看畫面最後幾行，以及：

```bash
cat /opt/anila/state/operations.log
docker compose -f /opt/anila/current/compose.yaml -f /opt/anila/current/.anila-images.yml -p anila logs --tail 80 csp
```

---

## 4. 驗證

```bash
H=https://<主機>[:埠]
for p in / /anila/ /anilalm/ /asr/health /router/health; do
  printf '%-16s ' "$p"; curl -sk -o /dev/null -w '%{http_code} %{content_type}\n' "$H$p"
done
docker ps --filter name=anila- --format '{{.Names}}\t{{.Status}}'
```

演練機的結果（這就是正常）：

| 路徑 | 結果 |
|---|---|
| `/` | `200 text/html`（治理中心） |
| `/anila/` | `200 text/html`（同仁用的平台） |
| `/anilalm/` | `200 text/html` |
| `/asr/health` | `200 application/json` |
| `/router/health` | `200 application/json` |

看內容類型，不要只看 200：打錯的路徑也會回 `200 text/html`。
容器共 13 個在跑（`csp-credential-dirs` 做完初始化就結束，正常）。

### 用管理員帳號登入

平台是插卡登入。第一次沒有任何人註冊過，用管理員帳號進去：

1. 開 `https://<主機>[:埠]/login?show_alternatives=1`
2. 點「**其他登入方式**」，帳號密碼欄位才會出現
3. 帳號 `admin`，密碼是 `generated-secrets.txt` 裡的 `ADMIN_PASSWORD`

進去後儀表板應顯示這一版的版本號、服務健康全綠、最後一次備份成功。

---

## 5. 初始設定（登入後，依序）

安裝完的平台沒有任何模型與單位。以下每一項在畫面上都會標「需設定」或給空清單，不會靜默壞掉。

1. **模型**：治理中心「模型」→「註冊模型」，登錄模型主機提供的端點。然後在同一頁「模型角色」指定七個角色：主路由、平台嵌入、簡報、生圖、視覺、摘要、知識庫對話。沒指定主路由，同仁問的第一個問題就會失敗。
2. **單位**：治理中心「部門」建立單位清單。沒有單位，同仁插卡註冊時選不到單位。
3. **卡片首次擁有者**：把負責核准同仁的人的員工編號填進 `.env`，再重建 CSP：
   ```bash
   cd /opt/anila/current
   # 編輯 /opt/anila/state/.env 的 CARD_INITIAL_OWNERS=員編1,員編2
   docker compose -f compose.yaml -f .anila-images.yml -p anila up -d --no-build --pull never csp
   ```
4. **外部服務**：治理中心「外部服務」填文件解析（docling）與語音解碼端的位址。沒填時匯入用內建解析器、麥克風不出現。
5. **HTTPS 憑證**：把院內正式憑證放到 `/opt/anila/state/certs/server.crt` 與 `server.key`（覆蓋自簽的那張），再重建入口：
   ```bash
   cd /opt/anila/current
   docker compose -f compose.yaml -f .anila-images.yml -p anila up -d --no-build --pull never --force-recreate nginx
   ```

code-server 與 n8n 的映像已載入，預設不啟動。要用時的指令在安裝結束時會印出，也寫在 [`UPDATE.md`](UPDATE.md)。

---

## 6. 目錄

| 路徑 | 內容 |
|---|---|
| `/opt/anila/current` | 指向目前版本的原始碼與 compose（`versions/<版本>`） |
| `/opt/anila/state/.env` | 設定與密鑰（600）。每個版本目錄的 `.env` 都連到這裡 |
| `/opt/anila/state/secrets/` | JWT 簽章金鑰 |
| `/opt/anila/state/certs/` | HTTPS 憑證 |
| `/opt/anila/state/share/` | 上傳檔、附件、更新前備份 |
| `/opt/anila/state/operations.log` | 每次安裝、更新、回復的紀錄 |
| `/var/lib/anila/install-anchor` | 安裝記錄。存在時，更新只能對同一個根目錄與專案 |

---

## 附錄：2026-09-30 演練撞到並已修正的問題

第一次在另一台主機從零實跑打包與安裝。以下每一項在那之前都只被替身測試過。

| # | 問題 | 修正 |
|---|---|---|
| 1 | 443 被別的服務佔用時，入口檢查仍打 443，會把健康的安裝判成失敗 | 入口檢查跟 `NGINX_HTTPS_PORT` 走 |
| 2 | 打包從乾淨檢出建置，沒有 `.env`，compose 一開始就停 | 建置時給佔位值（不進映像） |
| 3 | 打包退回 `docker save`，會被賽門鐵克 IDS 弄壞 | 改回隔離 buildx builder 直接輸出 tar |
| 4 | 主機用 containerd 儲存時，載入後的映像 ID 與清單不同，每張都被拒 | 清單同時記兩種 ID，對上其一即可 |
| 5 | 映像私鑰掃描誤判 OpenSSL、Python 套件原始碼裡的標頭字串 | 標頭後要有金鑰內容才算；經 DeepSeek 安全審查補齊 PGP、SSH2、單行等寫法 |
| 6 | 上游套件自帶的測試金鑰在映像層裡 | 在帶進來的同一層刪掉；執行時需要的（gnutls 自我測試、ssh2 偵測金鑰）以路徑加雜湊放行 |
| 7 | 逐層掃描對 n8n 要跑七小時以上 | 每層只解一次；約 4 分鐘 |
| 8 | 原始碼封存只要出現「PRIVATE KEY」字樣就拒絕 | 用與映像掃描同一套規則 |
| 9 | 產生 JWT 時找開發機才有的映像名 `anila-csp:latest` | 改用載入時標的 `anila/csp:running` |
| 10 | 安裝程式把密鑰印在畫面上，導到檔案就留在檔裡 | 本文第 3 步註明不要導到檔案 |
