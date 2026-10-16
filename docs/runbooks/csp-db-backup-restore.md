# Runbook — 備份與還原

> 給凌晨兩點、平台掛了、只剩你一個人的時候用。
> 每日備份是 compose 的 `backup` 服務，不是 crontab，也不要再跑 `backup-csp-db.sh`。
> 本樹是公開 repo：密碼不進 git、不進檔名、不進 log。

腳本：`infra/deployment/scripts/backup-loop.sh`（排程）、`restore-all.sh`（整份還原）、`restore-csp-db.sh`（只還原資料庫）。

---

## 1. 平常：什麼都不用排

`docker compose up` 會帶起 `backup`。它跟資料庫用同一張 `anila-pgvector:local`，不另做要搬進內網的映像。帳密就是 `csp-db` 那組 `CSP_DB_PASSWORD`，沒有新的祕密。

- 容器一起來就先備份一輪，之後每 **24 小時**再跑一輪。
- 這一輪失敗的話，大約 **15 分鐘**後再試，不會空等一天。
- 重建這個容器會立刻再跑一輪。不要把排程寫進 crontab。

產出在 repo 的 **`share/backups/`**。要換宿主機目錄，在 `.env` 設 `ANILA_BACKUP_DIR`（只影響宿主機路徑；容器裡面固定寫 `/backups`）。

檔案擁有者用 `.env` 裡的 `UID`／`GID`（跟 code-server 同一組）。那就是你 SSH 登入這台機器的帳號，另一台才拉得到。沒設的話檔案歸 root。

`status.json` 以只讀方式掛進 CSP（`/var/anila/backup-status/status.json`，路徑寫死、不另設環境變數，CSP 只讀這一支）。治理中心儀表板有「最後一次備份」（時間、結果、大小）。最後一次成功**超過** 36 小時（剛好 36 不算），或最近一輪失敗，會進既有的告警（`backup:stale`）。

### 1.1 一輪裡面有什麼

| 檔案 | 內容 |
|---|---|
| `db.dump` | `pg_dump -Fc`，整庫 |
| `files-uploads.tar` | `share/uploads`（含匯入原檔） |
| `files-attachments.tar` | `share/attachments` |
| `files-static.tar` | `share/static` |
| `files-pki.tar` | `share/pki`（公開 CA） |
| `files-quickstart.tar` | `share/quickstart` |
| `files-studio-artifacts.tar` | Studio 成品 volume |
| `files-router-sessions.tar` | 路由會話（不含 `state/`，那裡可能有後援憑證） |
| `files-n8n.tar` | n8n 資料 volume |

寫入先放在 `.incoming/`，完成才改名到 `daily/YYYYMMDD-HHMMSS/`（目錄名是 UTC 時間戳）。`LATEST` 是一行文字，指向最近一次成功的目錄。拉到一半不會拉到半套檔。

每月第一份成功的備份會再留一份到 `monthly/YYYYMM/`（同一顆磁碟上用硬連結）。月份用台北時間（`TZ=Asia/Taipei`，映像裡有 zoneinfo），`daily/` 的戳記仍舊是 UTC。

### 1.2 保留

| 目錄 | 留幾份 |
|---|---|
| `daily/` | **14** |
| `monthly/` | **6** |

過期的由備份服務自己刪。

### 1.3 不在自動備份裡（各留一次就好）

這些不是快取，但也不進每日包。磁碟全毀時，每日 rsync **救不回**它們：

| 要另外留的 | 為什麼 |
|---|---|
| `.env` | 裡面有 `SECRET_KEY`。資料庫裡的簽章私鑰與密封憑證要靠同一把才能解開。換過 `SECRET_KEY` 卻沒留新的，還原後登入與派工都會壞 |
| `secrets/`（含最初那把 JWT PEM） | 服務不會刪。第一次啟動匯進資料庫之後，平常簽名不再讀它；仍建議留著 |
| CSP 本地金鑰 volume `anila_anila-csp-local-secrets` | 外部服務憑證的金鑰。不在資料庫那把 `SECRET_KEY` 裡 |
| nginx TLS 私鑰 `infra/nginx/certs/server.key` | 站台憑證。公開的 `server.crt` 可以重來，私鑰不行 |
| 服務憑證 volume | 不必留。CSP 啟動會自己再核發 |

模型權重、Docling 模型、Redis（沒開 AOF，是快取）、code-server 的編輯器設定，都不在每日包裡。資料庫用的是 dump，不是把 `csp-pgdata` 整卷拷走。

---

## 2. 另一台機器來拉（平台這台不用知道對方是誰）

在**另一台**跑。把帳號、主機、路徑換成這台 ANILA 的 SSH 與 repo。內網 repo 若在 `/opt/anila`，路徑就是下面這樣：

```bash
rsync -aH --delete \
  --exclude='.incoming/' \
  --exclude='*.partial' \
  --exclude='*.tmp' \
  '<SSH帳號>@<ANILA主機>:/opt/anila/share/backups/' \
  ./anila-backups/
```

`-H` 保留每月那份硬連結，避免同一份資料佔兩次空間。`--delete` 讓對方也跟著刪掉超過 14／6 的舊檔。平台這台不要設定對方的位址。

想知道最新一輪是哪一份：`cat share/backups/LATEST`。

---

## 3. 還原：一條指令

先停掉會寫這些路徑的服務：`csp`、`ingestion-worker`、`anila-studio`、`router`。資料庫容器要留著（或換成你要還原進去的那一台）。

`pg_restore` 進一個**已經有表**的庫會失敗。演練用下面的丟棄庫；正式換庫時先換一顆空的 data volume，或先在丟棄庫還原成功再把平台指過去。不要對還在服務的 production 庫直接灌。

還原腳本會用 `ANILA_RESTORE_CONTAINER` 檢查目標庫。兩支腳本都先檢查已在本機的 `ANILA_BACKUP_IMAGE`（預設 `anila-pgvector:local`），固定其不可變映像 ID 後才進行寫入；helper 使用 `--pull never`，不會在還原時下載。缺少或無法識別映像時會在寫入前停止，回報 `RESTORE_HELPER_IMAGE_MISSING` 或 `RESTORE_HELPER_IMAGE_INVALID`。先核對離線映像清單及載入結果，不要臨時拉取其他映像。

**丟棄資料庫不等於完整演練隔離。** `restore-all.sh` 還會寫入檔案目錄及三個具名 volume。演練必須同時使用全新資料庫容器、全新 data volume、空白 share 目錄及三個獨享 volume；不可沿用 repo 的 `share/` 或 `anila_*` volume。下例只用新建暫存目錄衍生名稱，先拒絕已存在的容器或 volume。執行前將快照路徑換成已核對的完整快照，並準備已核對的本機 helper。

```bash
# 所有目標皆為這一輪新建；快照來源唯讀使用。
drill_root="$(mktemp -d "${TMPDIR:-/tmp}/anila-restore-drill.XXXXXXXX")"
drill_project="$(basename "$drill_root")"
drill_db="${drill_project}-db"
drill_data="${drill_project}-data"
drill_studio="${drill_project}-studio"
drill_router="${drill_project}-router"
drill_n8n="${drill_project}-n8n"
drill_share="$drill_root/share"
drill_snapshot='/受控備份目錄/daily/YYYYMMDD-HHMMSS'

# 只接受已載入的本機映像，啟動演練庫也固定同一個不可變 ID。
drill_image="$(docker image inspect --format '{{.Id}}' -- anila-pgvector:local)" || exit 1
[[ "$drill_image" =~ ^sha256:[0-9a-f]{64}$ ]] || exit 1
if docker container inspect "$drill_db" >/dev/null 2>&1; then
  printf '演練容器已存在，停止。\n' >&2
  exit 1
fi
for drill_volume in "$drill_data" "$drill_studio" "$drill_router" "$drill_n8n"; do
  if docker volume inspect "$drill_volume" >/dev/null 2>&1; then
    printf '演練 volume 已存在，停止。\n' >&2
    exit 1
  fi
done
mkdir -m 700 "$drill_share"
for drill_volume in "$drill_data" "$drill_studio" "$drill_router" "$drill_n8n"; do
  docker volume create "$drill_volume" || exit 1
done

# 密碼由操作者輸入，不寫入指令文字或文件；不開放主機連接埠。
read -r -s -p '演練資料庫密碼：' drill_password
printf '\n'
[[ -n "$drill_password" ]] || exit 1
POSTGRES_PASSWORD="$drill_password" docker run -d --pull never --network none \
  --name "$drill_db" \
  -e POSTGRES_USER=csp \
  -e POSTGRES_PASSWORD \
  -e POSTGRES_DB=csp \
  -v "$drill_data:/var/lib/postgresql/data" \
  "$drill_image" || exit 1
unset drill_password
for drill_attempt in {1..60}; do
  docker exec "$drill_db" pg_isready -U csp -d csp && break
  sleep 1
done
docker exec "$drill_db" pg_isready -U csp -d csp || exit 1

# 同時指定所有檔案與 volume 目標，不能只改資料庫容器。
ANILA_RESTORE_CONFIRM=yes \
ANILA_BACKUP_IMAGE="$drill_image" \
ANILA_RESTORE_CONTAINER="$drill_db" \
ANILA_SHARE_ROOT="$drill_share" \
ANILA_COMPOSE_PROJECT="$drill_project" \
ANILA_STUDIO_VOLUME="$drill_studio" \
ANILA_ROUTER_VOLUME="$drill_router" \
ANILA_N8N_VOLUME="$drill_n8n" \
bash infra/deployment/scripts/restore-all.sh "$drill_snapshot"
```

最後一行要看到 `RESTORE_ALL_OK`。本例的上傳、附件、靜態檔、CA、快速起步 profile 都解入 `$drill_share`，Studio、路由會話及 n8n 解入本輪獨享 volume。正式還原若省略這些覆寫，腳本仍預設 repo 的 `share/` 與專案 volume；不要把省略目標的指令當成演練。

兩邊都是 `tar -xzf` 蓋上去（volume 用已固定映像 ID 的 `--rm --pull never --network none` 容器解）：同名的檔會被覆蓋，備份之後才新增的檔會留著，不會先清空。快照需先由受權維護人員核對可信來源與完整性；腳本的可讀性檢查不等於可信快照驗證，也不保證資料庫與檔案跨資源原子切換。

只要資料庫、不要動檔案時，使用 `restore-csp-db.sh`，將 `ANILA_RESTORE_DUMP` 指向快照的 `db.dump`，`ANILA_RESTORE_CONTAINER` 指向獨享目標庫，`ANILA_BACKUP_IMAGE` 指向已核對的本機 helper。helper 固定映像 ID，使用 `--pull never --network container:<目標庫>`，保留擁有權並核對還原不變式；不需要另外取得 `pgvector/pgvector:pg16`。

### 3.1 還原後必核

1. 把 `csp_app` 密碼設成 `.env` 的 `CSP_APP_DB_PASSWORD`（在 `psql` 裡 `ALTER ROLE`，不要寫進腳本）：

   ```sql
   ALTER ROLE csp_app LOGIN PASSWORD '...從.env貼上...';
   ```

2. 用的 `SECRET_KEY` 必須是備份當時那一把，否則庫裡的簽章私鑰、密封的外部服務憑證都解不開。
3. 登入（密碼照上一步設回；卡登走的是另一條路）。
4. 打開一則還原前就存在的舊對話，下載裡面的舊附件。
5. 搜尋一份還原前就索引過的文件。
6. 稽核鏈：dump 裡有稽核表，也有檢查點表 `audit_checkpoints`（含 `chain_head`）。拿一份已經交出去的稽核匯出檔上的鏈頭來比對（庫內的可以被改，交出去的那份不行）。

   ```bash
   docker exec <csp 容器> python scripts/verify_audit_chain.py --head <匯出檔上的鏈頭>
   # 不帶 --head 只驗自洽；帶了才對得上庫外錨點
   ```

演練完：

```bash
docker rm -f "$drill_db"
docker volume rm "$drill_data" "$drill_studio" "$drill_router" "$drill_n8n"
# 保留 drill_root 供核對；確認本輪資料不需留存後，再按受控程序清理。
printf '本輪演練目錄：%s\n' "$drill_root"
```

---

## 4. 資料庫還原的已知坑

`restore-all.sh` 的資料庫步驟就是 `restore-csp-db.sh`。這些坑還在：

| 現象 | 原因 | 怎麼辦 |
|---|---|---|
| `role "csp_app" does not exist` | 直接 `pg_restore` 沒先建 role | 用 `restore-csp-db.sh` / `restore-all.sh` |
| 還原後 `/health` 是 200、容器 healthy、沒有人登得進來 | `pg_restore --no-owner` 把擁有權壓平。開機要改 `users` 等表，例外被吞掉 | 不要加 `--no-owner`。重跑 `restore-csp-db.sh` |
| 稽核表歸 `csp_app` | 擁有權被壓平，防竄改失效 | 腳本驗收失敗就不會印 `RESTORE_OK` |
| 忘了給 `csp_app` 密碼 | 還原不含密碼（dump 只建 role） | 還原後在 psql 裡 `ALTER ROLE`，見 §3.1 |
| `extension "vector" does not exist` | 映像不是這張 pgvector | 用 `anila-pgvector:local` |
| 備份很小或狀態是失敗 | dump 中斷 | 看 `LATEST`，用上一份成功的。狀態檔的 `error` 只是代碼，細節在 `docker logs` 的 backup 容器 |

姿態不對時不要把平台指到那個庫。`r1_0027` 之前的舊 dump 要先讓 csp 跑完 alembic、產生稽核檢查點之後再驗。

---

## 5. 跟誰無關

一般使用者沒有新步驟。不要再手動執行 `backup-csp-db.sh`：它現在什麼都不做，印出「改看 compose 的 backup 服務」與這份說明，然後以 1 結束。
