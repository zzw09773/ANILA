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
