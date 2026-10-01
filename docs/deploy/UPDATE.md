# 出貨與內網更新

一個人、兩道指令。開發機有網路；內網主機沒有，所以內網只載入出貨包，不建置、不拉取。

## 開發機（有網路）

工作目錄必須乾淨（`git status` 沒有變更）。在 repo 根目錄：

```bash
bash scripts/release/build-release.sh
```

版本是台北日期 `YYYY.MM.DD-N`，同一天再打包會變成 `-2`、`-3`。產出在 `dist/releases/`：

- 目錄 `anila-YYYY.MM.DD-N/`
- 壓縮檔 `anila-YYYY.MM.DD-N.tar.gz`

裡面有：這一版 HEAD 的原始碼、正式 compose 需要的每一張映像（含 redis、codeserver、n8n、asr-gateway；後三個預設不起）、清單（版本、git commit、映像內容、每個檔案的 SHA256）、以及 `anila-update.sh`。沒有 `.env`、`secrets/`、憑證私鑰、備份。映像是從 git HEAD 的乾淨檢出建置，工作目錄裡被忽略的檔不會進映像，清單上的 commit 就是這些映像的內容。每一張 `docker save` 封存都逐層掃描，含 redis；某一層放進私鑰、後面的層把它刪掉，仍然算違規。一般檔裡的 `BEGIN … PRIVATE KEY` 也算。內容例外只留給已知的公開憑證，以及 code-server 基底層裡已釘雜湊的 httpolyglot 公開測試 fixture `server.key`。其他私鑰會讓打包停下來。結束時印出壓縮檔的 SHA256。

把壓縮檔帶進內網。不要在內網跑 `build-release.sh`。

## 內網主機（沒有網路）

第一次安裝（含主機準備、`preflight.sh` 預檢、443 被佔用時換埠、登入與初始設定）照 [`INSTALL.md`](INSTALL.md)。這一節是腳本本身的行為說明。

沒有安裝記錄時，第一次安裝的預設位置是 `/opt/anila`。解在別的地方，腳本會停並印出下面這兩行。記錄寫在安裝包外面的 `/var/lib/anila/install-anchor`（權限 600，root 執行時擁有者是 root）。這份記錄一旦存在，就是唯一依據：停服務、還原、回復、第一次安裝、認領，都要目前的安裝根目錄與 compose 專案跟記錄相同。不會把既有記錄改寫成另一個根目錄，另外指定安裝根目錄也不能繞過。版本目錄裡的 `state/release.state` 不能拿來冒充。從 git 工作樹對專案 `anila` 會直接拒絕。第一次、而且還沒有記錄時：

```bash
sudo tar -xzf anila-YYYY.MM.DD-N.tar.gz -C /opt/anila
sudo bash /opt/anila/anila-YYYY.MM.DD-N/anila-update.sh /opt/anila/anila-YYYY.MM.DD-N
```

之後每次更新：

```bash
sudo bash /opt/anila/anila-update.sh <新包.tar.gz>
```

也可以先解開，執行新包裡的 `anila-update.sh`。兩種都可以。已安裝的更新程式核對清單之後，會把工作交給新出貨包裡那一支，所以這一版對更新流程的修正立刻生效。更新前先在治理中心貼公告；同仁登入後會看到。

腳本依序做這些事：

1. 進入腳本、尚未讀入任何程式之前，先用 `sha256sum -c` 核對清單本身。壓縮檔裡的 `manifest.txt` 與 `manifest.sha256` 必須是同一目錄的那一對。不符就停，不動正在跑的平台。接著再核對每個檔案，並拿這包裡已核對 SHA256 的 `images.tsv`，逐欄對上每一條映像行的服務、映像名、封存路徑與 digest。對不上、缺一條或重複就停，還沒有停服務。
2. 若平台已經在跑，檢查資料庫裡有沒有生效中的公告。治理中心先貼「將於幾點更新」這類公告，同仁登入看得到。沒有公告會警告，並詢問要不要繼續（預設否）。沒有終端機、也沒有公告時，不會只說「已取消」，會說明沒有終端機可以詢問。排程要在沒有公告時繼續，執行前設定 `ANILA_UPDATE_ASSUME_YES=1`。
3. 更新、回復、認領開始前會在安裝根目錄取得排他鎖（`state/update.lock`）。另一個正在進行就停，也不清別人解開的暫存。沒有人持有鎖時，才清掉上次留下的解壓目錄。若已經有上一版，資料庫必須先回應；沒有回應就停，不會把它當成第一次安裝，也不做備份或載入映像。沒有安裝記錄、但這台已有同名的 compose 專案或 volume，也不是第一次安裝，腳本會停並說明怎麼認領。然後先停掉會寫入的服務（csp、router、anila-studio、ingestion-worker、anilalm，以及入口、備份與其他會寫資料的服務），再備份資料庫，並用硬連結快照 `state/share` 裡的上傳、附件、靜態檔。Studio 成品在具名 volume `anila-studio-artifacts`，用輔助容器掛上該 volume，把內容打包進同一份備份。備份放在 `/opt/anila/state/share/backups/pre-update/<舊版本>/`，目錄 700、檔案 600。同一處記下備份時間與當時的 alembic 版本。硬連結不複製未改過的位元組，所以多留一份快照不會把磁碟用掉一倍；檔案被換掉才佔新空間。刪掉線上的檔不會立刻還空間，要等這份快照被清掉。只留最近兩版（含 studio 的打包檔），映像也一樣。正在跑的映像標成 `<compose 專案>/<服務>:<舊版本>` 與 `<compose 專案>/<服務>:running`，不會改到 `anila-csp:latest`、`redis:7-alpine` 或其他專案的標籤。清理也只刪這個專案命名空間裡的舊標籤。`docker compose` 的目錄必須是 `/opt/anila/versions/<版本>`，而且對得上安裝記錄；另外疊一層 `.anila-images.yml`，讓 compose 用上面那個 running 標籤。停寫入之後，備份、解壓、載入、準備目錄、啟動或健康檢查失敗，都會把上一版拉起來。資料庫要不要還原，看目前的 alembic 版本是否與備份記下的版本不同；不同才還原，相同就不還原資料庫。資料庫一旦確認可用，之後的失敗、取消或打錯版本都會寫資料庫稽核；寫不進去就留待寫標記。
4. 壓縮包解到 `/opt/anila` 底下，腳本結束就刪掉這份暫存。`docker load` 之後用清單裡的映像 ID 核對（不靠 manifest-list 的 RepoDigest；redis 的 pull digest 仍釘在打包腳本與 `images.tsv`），再標成這個專案的版本標籤與 running 標籤。原始碼放到 `/opt/anila/versions/<版本>`，還原成功後 `current` 才指到它。`.env` 維持指向 `state/.env` 的連結，密鑰寫進那份檔並保持權限 600，不會落到版本目錄。憑證與上傳檔也留在 `state/`。然後 `docker compose up -d --no-build --pull never`，這時先不起 nginx。資料庫遷移在 CSP 啟動時跑。
5. 先等內部服務健康、CSP `/health` 正常（最多五分鐘）。通過之後才啟動 nginx，並確認登入頁回應 200。沒過就把入口停掉，並回到上一版。
6. 失敗就自動回到上一版的映像與原始碼。目前的 alembic 版本與備份記下的版本相同時不還原資料庫；不同時才把備份還原進一顆空的資料庫，核對版本與備份當時相同，再把線上庫改名保留、換上還原庫。檔案與 studio volume 先在安裝根目錄裡的暫存核對（跟資料同一顆檔案系統；跨裝置才改成真正的複製），再整份換上；studio volume 會清掉快照之後才出現的檔案。這一步失敗不會被略過，會記成回復失敗。若資料庫還原停在暫存庫、線上那顆沒被換掉，仍把上一版的服務拉起來，同時寫下這次更新失敗與回復失敗，然後以非零結束。沒有上一版可回復時也會留下失敗紀錄。入口同樣等內部健康檢查過了才開。

這台已經有同名的 compose 專案或 volume、卻沒有安裝記錄時，不要直接更新。先備份再認領。認領會把舊部署寫在版本目錄裡的 `.env`（若還在）收進 `state/.env`，再像更新一樣準備目錄：站台設定、JWT、模型 CA、檔案所有權、網路，然後才啟動。新產生的密鑰用十六進位。成功會在 `operations.log` 與資料庫稽核留下動作 `adopt`。專案 `anila` 同時寫外面的 anchor；已有不同根目錄的記錄時拒絕，不會覆寫：

```bash
sudo bash /opt/anila/anila-update.sh adopt /opt/anila/versions/<目前版本>
```

第一次安裝沒有上一版，問站台名稱 `ANILA_HOST`；HTTPS 埠被佔用才再問。其餘密鑰自動產生（十六進位），不印在畫面上，只寫在 `/opt/anila/state/generated-secrets.txt`（權限 600）。用 `sudo cat` 讀，再用產生的管理員密碼登入。卡片首次擁有者先是空的；要指定員工編號，之後改既有的 `CARD_INITIAL_OWNERS` 再重建 csp。沒有院內憑證時會先簽一張自簽憑證，正式憑證換成同一路徑即可，不要放進出貨包。

codeserver、n8n、asr-gateway 的映像在包裡，預設不起。codeserver 與 n8n 留到最後演練。腳本印出的指令帶目前這版的 compose 檔與專案名，同樣是 `--no-build --pull never`：

```bash
docker compose -f /opt/anila/current/compose.yaml -p anila --profile maint up -d --no-build --pull never codeserver
COMPOSE_PROFILES=ops docker compose -f /opt/anila/current/compose.yaml -p anila up -d --no-build --pull never n8n
```

asr-gateway 跟平台一起啟動；解碼端在治理中心「外部服務」設定，沒設定或不健康時麥克風自己藏起來。文件解析也在別台，同樣在治理中心設定，這包不含文件解析服務。

## 手動回復

```bash
sudo bash /opt/anila/anila-update.sh rollback
```

會先停掉會寫入的服務，再用備份當時的時間計算會消失的對話、訊息、文件（備份之後到現在的新增都會消失），畫面上仍會看到目前版本，然後要求打字輸入要回到的版本。打錯或取消會把平台拉起來，不還原，並寫資料庫稽核（寫不進去就留待寫標記）。打對才還原：先還原進空資料庫，核對 alembic 版本，把線上庫改名保留（不先刪），檔案快照也留著上一份。還原成功之後才把 `current` 指到要回去的版本，先做內部健康檢查，通過才開 nginx。健康檢查過了才丟掉改名保留的舊庫、清掉檔案上一份。資料庫或檔案還原失敗時，線上資料庫保持原樣，映像標籤與 `current` 指回正在跑的那一版，把那一版拉起來，並寫下失敗。健康檢查沒過也一樣：資料庫改回來、檔案換回、映像標籤與 `current` 切回正在跑的那一版再啟動。回復成功後沒有再上一版，要等下一次更新。

每次更新或回復，不論成功或失敗（含還沒開始改版本就停下來），都追加一行到 `/opt/anila/state/operations.log`（權限 600）。這份檔在資料庫外面，還原不會清掉。還原或回復完成後，同一筆用 `psql` 直接寫進資料庫稽核，csp 容器沒起來也寫得了。寫不進去會標成待寫，下一輪再補。治理中心儀表板仍顯示 CSP 映像裡的版本。

## 模型

模型不跟平台這包走。`model-serve.sh`、權重下載與分塊在 `infra/deployment/archive/model-side/`，用法不變，路徑改了。平台舊腳本 `intranet-deploy.sh`、`build-and-export-for-intranet.sh`、`anila-serve.sh` 已退役，在 `infra/deployment/archive/intranet-legacy/`。直接執行或 `source` 都會停。檔案內容還在，離線測試會讀。
