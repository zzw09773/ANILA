# Runbook — CSP 資料庫備份與還原（P3.1）

> **給凌晨兩點、平台掛了、只剩你一個人的時候用。**  
> 腳本：`infra/deployment/scripts/backup-csp-db.sh`、`restore-csp-db.sh`  
> 本樹是 PUBLIC repo：**密碼不進 git、不進檔名、不進 log 行。**

---

## 0. 先別慌：三件事

1. **先確認是不是磁碟滿／容器掛了**，不是一上來就還原。還原會覆蓋目標庫。
2. **永遠不要對還在跑的 production 容器直接 `pg_restore`。** 先起一個替換庫／丟棄庫驗證列數，再切流量。
3. 活體堆疊（本機常見 project 名 `anila-restart`）的 DB 容器類似 `anila-restart-csp-db-1`。  
   **本 runbook 的演練容器名一律用 `anila-ops-…`，不要跟它撞名。**

---

## 1. 平常：排程備份（裝一次就好）

### 1.1 目錄

```bash
sudo mkdir -p /var/backups/anila/{daily,monthly}
sudo chown "$USER":"$USER" /var/backups/anila
```

（路徑可改；改了就讓 cron 帶 `ANILA_BACKUP_DIR`。）

### 1.2 每晚跑一次

```bash
crontab -e
```

加入（依你的 repo 與容器名改路徑）：

```cron
# ANILA CSP DB — 每天 02:15；失敗信寄給自己（若本機 mail 有通）
15 2 * * * ANILA_DB_CONTAINER=anila-restart-csp-db-1 ANILA_BACKUP_DIR=/var/backups/anila /bin/bash /path/to/ANILA/infra/deployment/scripts/backup-csp-db.sh >>/var/backups/anila/backup.log 2>&1
```

- 容器名用 `docker ps | grep csp-db` 對一下；內網正式機可能是 `anila-platform-csp-db-1`。
- **不要**把 `.env` 的密碼寫進 crontab。`pg_dump` 走容器內本機 socket，用 `-U csp` 即可。

### 1.3 保留策略（腳本內建，會自己刪）

| 目錄 | 保留 | 用意 |
|---|---|---|
| `daily/csp-YYYYMMDD-HHMMSS.dump` | **30 天** | 近月誤刪／壞 migration 可回退 |
| `monthly/csp-YYYYMM.dump`（每月 1 號硬連結／複製） | **7 個月** | 稽核帳要留約半年、給長官的月報可追溯 |

單一 artifact：`pg_dump -Fc`（custom + gzip），副檔名 `.dump`。  
檔名**只有**時間戳，沒有密碼、沒有主機祕密。

### 1.4 手動補一份（事故前也建議）

```bash
export ANILA_DB_CONTAINER=anila-restart-csp-db-1   # 改成你的
export ANILA_BACKUP_DIR=/var/backups/anila
bash infra/deployment/scripts/backup-csp-db.sh
```

成功會長這樣：`BACKUP_OK path=... size_bytes=... elapsed_s=...`

---

## 2. 凌晨：還原（先演練庫，再談切回）

### 2.1 選哪一份

```bash
ls -lt /var/backups/anila/daily/csp-*.dump | head
# 需要更久以前：
ls -lt /var/backups/anila/monthly/csp-*.dump
```

記下路徑，例如 `DUMP=/var/backups/anila/daily/csp-20260730-021500.dump`。

### 2.2 起一個丟棄／替換用 Postgres（不要動 production 容器）

```bash
docker volume create anila-ops-restore-data
# 密碼用臨時值即可；正式切回前再改成 .env 的 CSP_DB_PASSWORD
docker run -d --name anila-ops-restore-db \
  -e POSTGRES_USER=csp \
  -e POSTGRES_PASSWORD='暫訂密碼-自己想' \
  -e POSTGRES_DB=csp \
  -v anila-ops-restore-data:/var/lib/postgresql/data \
  -p 127.0.0.1:5544:5432 \
  pgvector/pgvector:pg16

# 等到 ready
until docker exec anila-ops-restore-db pg_isready -U csp -d csp; do sleep 1; done
```

映像必須是 **`pgvector/pgvector:pg16`**（跟平台一樣）；純 `postgres:16` 會缺 `vector` extension。

### 2.3 還原

```bash
export ANILA_RESTORE_CONTAINER=anila-ops-restore-db
export ANILA_RESTORE_DUMP="$DUMP"
bash infra/deployment/scripts/restore-csp-db.sh
# 期望最後一行：RESTORE_OK
```

腳本會先 `CREATE ROLE csp_app`（dump 裡有 ACL，缺這個 role 會噴一堆 error）。

### 2.4 核對列數（必做）

還原前若還碰得到舊庫，先記來源列數；沒有就至少確認還原庫非空且合理：

```bash
docker exec anila-ops-restore-db psql -U csp -d csp -c "SELECT count(*) AS audit_logs FROM audit_logs;"
docker exec anila-ops-restore-db psql -U csp -d csp -c "SELECT count(*) AS token_usage FROM token_usage;"
```

兩張表對得上（或與事故前筆記一致）→ 這份備份可用。  
用量表名是 **`token_usage`**，不是 `usage_records`。

### 2.5 若要讓平台 csp 連這個庫

1. 把 `csp_app` 密碼設成 `.env` 的 `CSP_APP_DB_PASSWORD`（**手動**在 `psql` 打 `ALTER ROLE`，不要寫進腳本或 chat）：

   ```sql
   ALTER ROLE csp_app LOGIN PASSWORD '...從.env貼上...';
   ```

2. 視情況把 compose 指到新 volume／新容器，再 `docker compose up -d`（**這一步會 recreate，需清醒時做**；本 runbook 不強迫你現在做）。
3. 設完密碼後，在 shell `history -c` 或避免把含密碼的指令留在 history。

### 2.6 清掉丟棄演練

```bash
docker rm -f anila-ops-restore-db
docker volume rm anila-ops-restore-data
```

---

## 3. 已知坑

| 現象 | 原因 | 怎麼辦 |
|---|---|---|
| `role "csp_app" does not exist` | 直接 `pg_restore` 沒先建 role | 用 `restore-csp-db.sh`，或手動 `CREATE ROLE csp_app LOGIN;` |
| `extension "vector" does not exist` | 映像不是 pgvector | 換 `pgvector/pgvector:pg16` |
| 備份檔很小／TOC 檢查失敗 | dump 中斷 | 別用那份；重跑 backup |
| `docker restart` 後日誌上限沒套到 | logging 選項只在 **create** 時生效 | 要用 `compose up -d` recreate（見 P3.5）；與本備份無關 |
| 磁碟又滿 | 備份目錄也會長大 | 腳本已砍 30 天 daily／7 個月 monthly；確認 cron 真的在跑 |

---

## 4. 一次成功的還原演練紀錄（2026-07-30，本機 wt-ops）

> 對 **丟棄** 容器 `anila-ops-p31-restore-db` 執行；**未**碰 `anila-restart-csp-db-1`。

| 步驟 | 結果 |
|---|---|
| 來源列數 | `audit_logs=408`，`token_usage=10` |
| 備份 | `csp-20260730-204325.dump`，**322 750 bytes（約 316 KiB）**，腳本回報 **elapsed ≈ 0–1 s**（本機開發庫很小） |
| 還原 | `restore-csp-db.sh` → `RESTORE_OK`，`pg_restore exit=0`，約 **1 s** |
| 還原後列數 | `audit_logs=408`，`token_usage=10`（與來源一致） |
| 清理 | 丟棄容器與 volume 已刪；活體庫仍 `running`，列數不變 |

正式機資料量大時，以當日 `BACKUP_OK` 的 `size_bytes`／`elapsed_s` 重新估 cron 窗口；排程預設凌晨 02:15 仍適用。

---

## 5. 跟誰無關

- **一般使用者、開發者日常操作零新增步驟。** 這是維運主機上的 cron＋腳本，不會進產品權限模型、不會多一道登入閘。
- 應用程式碼、API、前端都不依賴這套備份。
