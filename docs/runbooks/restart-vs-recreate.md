# 套設定要 recreate,不是 restart

> 一句話:**改了 `.env` 或 compose,就一定是 `docker compose up -d`。**
> `docker restart` / `docker compose restart` 把同一個容器停掉再開,
> **不會重讀 `.env`、也不會重讀 compose** —— 容器的環境變數是 `create` 當下就
> 固定在容器設定裡的東西,restart 只是讓同一份設定再跑一次。

## 為什麼這個坑特別難發現

改了設定、`restart`、容器變 healthy、`docker compose ps` 全綠 ——
**沒有任何錯誤訊息**。症狀跟「這個設定根本沒生效」一模一樣,
因為那正是實際發生的事。人會回頭去懷疑自己改錯了鍵名或改錯了檔案。

確認變數真的進到容器裡(**指名要看的那一個鍵,不要把整份 env 印出來**):

```bash
docker exec <容器名> printenv EMBEDDING_TIMEOUT   # 沒有輸出 = 沒進去
```

這不是推論,是量過的(2026-08-06,獨立的 `-p wthygiene-envtest` 拋棄式 compose
專案,一個 alpine 容器帶一個 `.env` 插值變數):

| 步驟 | `printenv` 讀到的值 | 容器 ID |
|---|---|---|
| `up -d`(`.env` = first) | `first` | — |
| 改 `.env` = second,`docker restart` | **還是 `first`** | **沒換,同一個容器** |
| 同樣的 `.env`,`up -d` | `second` | **換了(= recreate)** |

## 最容易中招的服務:ingestion-worker

`ingestion-worker` 沒有自己的設定檔,**整包行為都是 compose 的 `environment:`
插值進去的**(`infra/compose/platform.yml:265-292`):資料庫、Redis、
embedding 端點與金鑰、SECRET_KEY、SSRF 旗標、VLM captioning 那一組全部都是。
換句話說,對這個服務而言「改設定」與「recreate」是同一件事,沒有中間狀態。

csp / router / anila-studio 同理,只是它們還有健康檢查與 `/health` 可以旁證;
ingestion-worker 是背景 worker,**沒有人在前面幫你發現它吃的是舊設定**。

## deploy-prod.sh 目前的實況(2026-08-06 逐條核對)

`infra/deployment/scripts/deploy-prod.sh` 裡**沒有任何一條** `docker restart` /
`docker compose restart`。四條會讓服務吃到新設定的路徑,全部是 recreate 語意:

錨點用**函式名**、不用行號:這張表的上一版釘的是行號,而同一包後面的改動就讓
它們全部失準了(同一個腐化形狀在這個專案已經記過帳)。函式名活得比行號久。

| subcommand | 函式 | 實際做的事 |
|---|---|---|
| `deploy` | `cmd_deploy()` | `docker compose build` 之後 `docker compose up -d`(全棧) |
| `up` | `cmd_up()` | `docker compose up -d`(不 build) |
| `restart` | `cmd_restart()` | `cmd_down` + `cmd_up` → `down` 之後 `up -d`;**名字叫 restart,做的是 recreate** |
| `rebuild <svc>` | `cmd_rebuild()` | `docker compose build <svc>` 之後 `docker compose up -d <svc>` |

自己核對一遍(不必相信這張表)。⚠ 要排掉註解 —— 檔頭那段警告本身就寫著
`docker restart` 這幾個字,直接 grep 會有兩筆註解命中,那不是程式碼:

```bash
# 應該零命中(有命中就是真的有人加了 restart 路徑)
grep -nE '^[^#]*docker (compose [a-z-]* )?restart' infra/deployment/scripts/deploy-prod.sh
# 對照:所有 recreate 路徑
grep -n 'docker compose up -d' infra/deployment/scripts/deploy-prod.sh
```

⚠ `restart` 這個 subcommand 的名字會誤導 —— 它不是 `docker restart`。
新增會起服務的 subcommand 時,**照這四條的樣子寫 `up -d`**,不要用 `restart`
省那幾秒;省下來的那幾秒會在半夜變成「設定改了但沒生效」的兩小時。

## recreate 完還有兩件事

1. **reload nginx**。recreate 會讓容器換 IP,而 nginx 的 `upstream` 區塊只在
   載入設定時解析一次 DNS → **全站 502 但所有容器都是綠的**。
   `deploy-prod.sh` 的 `deploy` / `up` 路徑已經內建(`reload_nginx()`);
   **手動只 recreate 單一服務時沒有人幫你做**:
   ```bash
   docker exec anila-nginx nginx -t && docker exec anila-nginx nginx -s reload
   ```
2. **改的是 bind-mount 進去的單一檔案時,連 `up -d` 都不夠**。Docker 用 inode
   綁定,而 git 改檔是「建新檔取代舊檔」(新 inode),容器還抓著舊的那個 ——
   一樣沒有錯誤訊息。那種情況要 `up -d --force-recreate <svc>`
   (nginx 設定檔就是這樣掛過的,見 `CLAUDE.md` §4 的操作陷阱)。

## 改的是程式碼:連 `up -d` 都不夠,要先 build

recreate 換掉的是**容器**,不是**映像**。程式碼是 build 當下拷貝進映像的一份
副本,`up -d` 只是拿同一張舊映像再開一次 —— 症狀跟上面那個坑一模一樣:
容器全綠、沒有任何錯誤訊息、你的改動不在裡面。
上面 `cmd_rebuild()` 那一列的 `<svc>` 要填哪一個,就是這張表回答的問題。

表以**改動的目錄**為索引,不以服務為索引 —— 操作者手上有的是「我動了哪個
資料夾」,不是「我動了哪個映像」。證據欄寫**行號＋那一行的內容**:行號會腐化
(上一節記過同一筆帳),`COPY` 的那串字比行號活得久。

| 改的目錄 | 要 rebuild 的服務 | 證據:Dockerfile 的那一行 | build context |
|---|---|---|---|
| `services/csp/` | `csp` | `infra/docker/csp.Dockerfile:63` `COPY services/csp/ ./` | repo 根(`platform.yml:57`) |
| ⚠ `apps/csp-governance-ui/` | **`csp`** | 同一個檔 `:11` `:16` 進 `frontend-build` 階段,`:66` `COPY --from=frontend-build /build/dist /app/frontend-dist` | repo 根(`platform.yml:56-58`) |
| ⚠ `packages/anila-core/` | `csp`＋`ingestion-worker`＋`router` | `csp.Dockerfile:51`、`ingestion-worker/Dockerfile:35`、`anila-core-router/Dockerfile:15,17` —— 三份都是 `COPY packages/anila-core…` 之後 `pip install` | 三個都是 repo 根 |
| ⚠ `packages/anila-core/src/anila_core/security/` | 上面三個**再加** `asr-gateway` | `asr-gateway/Dockerfile:59-60` `COPY packages/anila-core/src/anila_core/security/ ./anila_core/security/`(只搬這個子套件,不是整包) | repo 根(`platform.yml:735`) |
| `services/ingestion-worker/` | `ingestion-worker` | `ingestion-worker/Dockerfile:39` `COPY services/ingestion-worker /tmp/ingestion-worker` | repo 根(`platform.yml:286`) |
| `services/anila-core-router/` | `router` | `anila-core-router/Dockerfile:35` `COPY services/anila-core-router/main.py ./` | repo 根(`platform.yml:364`) |
| `services/asr-gateway/` | `asr-gateway` | `asr-gateway/Dockerfile:27-28` `COPY services/asr-gateway/app/ ./app/` | repo 根(`platform.yml:735-736`) |
| ⚠ `services/asr-decoder/` | `asr-decoder`,**兩個棧各一張** | `asr-decoder/Dockerfile:62-63`;平台棧 `platform.yml:864-867`(tag `anila/asr-decoder:0.1.0`)、模型棧 `infra/models/docker-compose.yml:457-461`(tag `asr-decoder:0.1.0`) | `../../services/asr-decoder` |
| `services/anila-studio/` | `anila-studio` | `anila-studio/Dockerfile:64-65` `COPY app/ ./app/` | `../../services/anila-studio`(`platform.yml:459`) |
| `services/pptx-renderer/` | `pptx-renderer` | `pptx-renderer/Dockerfile:66` `COPY server.js ./`(另有 `:63` `:69` `:75`) | `../../services/pptx-renderer`(`platform.yml:439`) |
| ⚠ `apps/anila-shell/` | **`anila-ui`** | `anila-shell/Dockerfile:25` `COPY . ./` | `../../apps/anila-shell`(`platform.yml:530`) |
| `apps/anilalm/` | `anilalm` | `anilalm/Dockerfile:35` `COPY src ./src` | `../../apps/anilalm`(`platform.yml:509`) |
| ⚠ `infra/codeserver/` | `codeserver`＋`codeserver-init`(同一張) | `codeserver/Dockerfile:38` —— 整張映像**只有這一條 COPY**,搬的是 docker CLI,沒有任何 repo 原始碼 | `../codeserver`(`platform.yml:572-576`、`:597-598`) |

`anilalm`(`platform.yml:508-509`)與 `anila-ui`(`:529-530`)的 build 區塊**沒有**
`dockerfile:` 這個鍵 —— 沒寫時 compose 取 context 底下的 `Dockerfile`,其餘服務都寫明。

⚠ 那幾列的死法,一列一句:

- **`apps/csp-governance-ui/` → `csp`**:治理中心的原始碼放在 `apps/` 底下,
  卻是被 csp 的多階段 build 編進 **csp 映像**的。改了治理中心去重建「UI 映像」
  (`anila-ui`)等於什麼都沒換 —— **而且兩張映像都會 build 成功、容器都是綠的**。
  這條路徑一路到 `services/csp/app/main.py:514` 的 `/app/frontend-dist`。
- **`packages/anila-core/` → 三張(動到 `security/` 是四張)**:漏掉任何一張,
  就會出現「router 已經用新規則、csp 還在用舊規則」的半新半舊狀態,兩邊都不報錯。
  `asr-gateway` 只搬 `security/` 子套件,所以動 `anila_core` 其他地方它不受影響;
  動 `security/` 則四張全要。
- **`services/asr-decoder/` → 兩個棧兩個 tag**:只重建平台棧那張,模型棧
  (`profiles: ["intranet"]`)那張仍是舊碼。`platform.yml:839` 警告的是「不要同時開」,
  不是這件事。
- **`apps/anila-shell/` → 服務叫 `anila-ui`**:目錄名跟服務名對不上,
  `docker compose build anila-shell` 會直接說沒這個服務(這種錯至少會報錯)。
- **`infra/codeserver/`(反向)**:repo 是**掛**進 codeserver 的
  (`platform.yml:628` `${CODESERVER_WORKSPACE:-../..}:/home/coder/workspace`),
  所以改平台程式碼**永遠不需要**重建這張映像;要重建的只有 code-server 本身要換版時。

⚠ `anilalm` / `anila-ui` 有一種**改 `.env` 卻不是 recreate 就好**的例外:
`VITE_*` 與 `BASE_PATH` 是 **build args**(`platform.yml:510-512`、`:531-537`),
build 當下就編進 JS bundle 了。這兩個服務改到那幾個鍵,`up -d` 不夠,要先 build。

**拉的、不是 build 的**(改 repo 不會動到它們;換版本或 digest 才要 `up -d`):
`csp-db`(`platform.yml:33`)、`redis`(`:271`)、`nginx`(`:399`,tag＋digest 都釘死)、
`n8n`(`:649`)、`gitlab`(`:684`)。平台 16 個服務裡有 `build:` 的是 11 個、
共 10 張映像(`codeserver-init` 與 `codeserver` 共用 `anila-codeserver:local`)。
nginx 的設定檔不在映像裡,是 bind mount(`platform.yml:406`)—— 那條路走上一節第 2 點。

`infra/compose/dev.yml` 的 8 個 build 指向**同一組** context 與 Dockerfile
(例:`dev.yml:61-63` = `platform.yml:56-58`),`asr-cpu.yml` / `asr-gpu.yml` 只改
`asr-decoder` 的執行期設定、沒有 `build:` 區塊。所以這張表對 dev 棧一樣成立。

自己核對一遍(不必相信這張表)。這張表是兩條 grep 推出來的,同樣兩條可以重推:

```bash
# ① 每個服務的 build context 與 Dockerfile
grep -nE 'build:|context:|dockerfile:' infra/compose/platform.yml
# ② 每張映像實際把哪些原始碼搬了進去(跨界的那幾條就藏在這裡)
grep -rnE '^[[:space:]]*(COPY|ADD)[[:space:]]' --include=Dockerfile --include='*.Dockerfile' . \
  | grep -v node_modules
```

② 要找的形狀是**「這個 Dockerfile 有沒有 COPY 別人家的目錄」**:`COPY apps/…`
出現在 csp 的 Dockerfile 裡、`COPY packages/…` 出現在四個服務裡 —— 上面所有的 ⚠
就是這個形狀的全部命中。新增服務或改動 Dockerfile 之後把 ② 跑一次,
多出來的跨界 COPY 就是要補進這張表的新列。

⚠ ② 會多撈到 `packages/anila-agent/Dockerfile:32`(同樣是 `COPY packages/anila-core`),
但 `anila-agent` **不是這個棧的服務** —— `platform.yml` 裡沒有它,它跑在 MLSteam 的
Lab(`CLAUDE.md` §1)。`services/flux2-dev*`(在 `infra/models/docker-compose.yml`)、
`cht/`、`infra/loadtest/stub/` 同理:有 Dockerfile,但這個 compose 棧不 build 它們。
表上沒有它們不是漏列,是不同棧 —— **但 `anila-agent` 那一筆是真的跨界:
動到 `packages/anila-core` 時,它在 MLSteam 上那張映像也是舊的**
(其餘那幾個沒有 `COPY packages/…`,不受影響)。

映像裡到底是不是新的那份,`asr-gateway` 留了指紋可以當場量
(`services/asr-gateway/Dockerfile:51-53`、`:63`):

```bash
docker exec anila-asr-gateway-1 cat /app/.url_guard.sha256
sha256sum packages/anila-core/src/anila_core/security/url_guard.py
# 兩邊不同 = 跑的是舊碼,要 docker compose build asr-gateway 再 up -d
```
