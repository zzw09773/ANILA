# Production Backup 與可拋棄式 Restore Smoke

本 runbook 是 Gate 3 I9 的正式備份執行路徑。執行 SSOT 為
`infra/deployment/backup/production-backup-profile.v1.json`；profile verifier 會把
production Compose 的每個 bind mount 與 named volume 對到明確 disposition。

這套工具只提供備份、驗證、解密到全新可拋棄目標，以及隔離 PostgreSQL restore
smoke。它不提供 live/in-place restore。正式破壞性復原仍是 Gate 6 blocker。

## 安全不變式

- PostgreSQL dump、Redis/n8n/GitLab state、uploads、attachments、artifact blobs、
  vector data 都由來源 stdout 直接串流進 `age`，不在 staging 產生明文檔案。
- JWT/TLS private key、n8n encryption key、GitLab secrets 與 release signing key
  不複製進備份；只記錄外部保管 reference 與 public fingerprint。
- staging 只含 `.age` component、signed envelope 與 signature；本機及 off-host
  發佈都使用同檔案系統 rename，off-host 發佈前逐檔 SHA-256 readback。
- signed envelope 先驗 signature，再驗 encrypted component SHA-256；解密後再驗
  manifest schema、profile SHA-256、logical SHA-256 與 surface mapping。
- 單一 writer lock、writer quiesce、Redis `SAVE`、兩端 retention 與 failure alert
  全部 fail-closed。任何失敗皆回傳非零。
- 所有被 quiesce 的 writer 必須先恢復為 `running`，有 healthcheck 者必須回到
  `healthy`，才會 publish bundle 或送出 success alert；恢復失敗會令備份失敗並告警。
- 任一 `COMPOSE_*` ambient 變數都會被拒絕；compose 命令固定使用 profile 指定的
  `infra/compose/platform.yml`。

## 前置需求

執行主機需要 Python 3.11+、PyYAML、Docker Compose、`age` 與 OpenSSL。備份根目錄、
off-host 目錄、age/signing key 檔案必須位於 repo 之外。Linux 的 off-host 目錄必須是
精確 mountpoint，且必須使用不同 filesystem/device，或使用工具內建 allow-list 的
remote filesystem（NFS/NFS4/CIFS/Ceph/GlusterFS/SSHFS）；只建立同一磁碟上的另一個
目錄會被拒絕。Windows 僅可在非正式 profile 的本機測試以
`ANILA_BACKUP_TEST_ALLOW_WINDOWS_OFFHOST=1` 明確略過；任何 `prod-*` profile 都拒絕。

必要環境變數：

```text
ANILA_BACKUP_AGE_RECIPIENTS_FILE
ANILA_BACKUP_AGE_IDENTITY_FILE
ANILA_BACKUP_SIGNING_KEY_FILE
ANILA_BACKUP_SIGNING_PUBLIC_KEY_FILE
ANILA_BACKUP_OFFHOST_DIR
ANILA_BACKUP_ALERT_HOOK
ANILA_BACKUP_KEEP                         # optional, default from profile

ANILA_BACKUP_JWT_KEY_REFERENCE
ANILA_BACKUP_JWT_PUBLIC_FINGERPRINT
ANILA_BACKUP_TLS_KEY_REFERENCE
ANILA_BACKUP_TLS_PUBLIC_FINGERPRINT
ANILA_BACKUP_GITLAB_CONFIG_REFERENCE
ANILA_BACKUP_GITLAB_CONFIG_FINGERPRINT
ANILA_BACKUP_N8N_KEY_REFERENCE
ANILA_BACKUP_N8N_KEY_FINGERPRINT
ANILA_BACKUP_GITLAB_SECRETS_REFERENCE
ANILA_BACKUP_GITLAB_SECRETS_FINGERPRINT
ANILA_BACKUP_RELEASE_REFERENCE
ANILA_BACKUP_RELEASE_FINGERPRINT
```

每個 fingerprint 必須是 `sha256:` 加 64 位小寫 hex。reference 應是不可含空白的
KMS/HSM/secret-manager/release locator，不可放 private key 內容或 private key 路徑。

alert hook 必須是可執行的 real file。它從 stdin 接收單一 canonical JSON：
`schema_version`、`status`、`backup_id`、`detail`、`timestamp`。hook 回傳非零會令
作業失敗；profile/tool/mount/key 等 preflight 失敗也會走 failure event。若 alert
設定本身缺失或 hook 失敗，工具會在 stderr 輸出固定格式的 alert error，並維持非零。

## 備份

先做 profile/Compose coverage 驗證：

```bash
python3 infra/deployment/scripts/verify-production-backup-profile.py
```

執行備份（`BACKUP_DIR` 由既有 `anila-ops.sh` 設定）：

```bash
bash infra/deployment/scripts/anila-ops.sh backup
```

或直接指定 repo 外目錄：

```bash
python3 infra/deployment/scripts/production-backup.py \
  backup --backup-root /srv/anila-backups
```

成功時 stdout 回傳 published bundle 目錄；alert 必須是 success。bundle 至少含：

```text
backup-YYYYMMDDTHHMMSSZ-xxxxxxxx/
  envelope.json
  envelope.sig
  manifest.age
  external-references.age
  <surface>.age
```

若程序遭強制終止，`.production-backup.lock` 會保留以避免自動重入。只有確認沒有
backup process、staging 不再寫入，並完成事件記錄後，才可人工移除 stale lock；
不得把自動刪 lock 寫進排程。

## 驗證與可拋棄式復原

驗證 bundle，不寫出 component 明文：

```bash
python3 infra/deployment/scripts/production-backup.py verify \
  /srv/anila-backups/backup-YYYYMMDDTHHMMSSZ-xxxxxxxx
```

解密到「目前不存在、位於 repo 與 live `ANILA_STATE_DIR` 之外」的新目錄：

```bash
bash infra/deployment/scripts/anila-ops.sh restore \
  /srv/anila-backups/backup-YYYYMMDDTHHMMSSZ-xxxxxxxx \
  /srv/anila-restore-drills/drill-20260714 prepare
```

完整 pre-restore smoke 會先做相同驗證/prepare，再用 manifest 鎖定的 PostgreSQL image
建立 `--network none` disposable container，執行 `pg_restore`，檢查：

- `document_chunks` 到 `ingestion_documents` 無 orphan。
- active vector generation 的 leaf chunks 有 embedding 且標為 active；generation 的
  status、document/collection scope、fingerprint、dimension 與 chunk_count 一致。
- ingestion、conversation attachment 與 immutable artifact blob 的 DB reference 都能在
  還原 surface 找到實體檔案。

```bash
bash infra/deployment/scripts/anila-ops.sh restore \
  /srv/anila-backups/backup-YYYYMMDDTHHMMSSZ-xxxxxxxx \
  /srv/anila-restore-drills/drill-20260714 smoke
```

成功後目標目錄會有 `RESTORE_PREPARED.json`、`manifest.json` 與
`RESTORE_SMOKE.json`。保留這三份檔案、bundle ID、source commit、執行時間與 alert
事件作為 Gate evidence；不要把解密後的 drill 目錄當成正式服務資料目錄。

## 排程與演練

- scheduler 必須以非重疊模式執行，並監控 exit code 與 alert hook。
- 每次備份都要求 off-host readback；不可把「copy 命令成功」當成證據。
- 至少依 Gate/營運頻率挑一份最新 bundle 跑 smoke；每次都用全新 target。
- `prepare`/`smoke` 拒絕既存 target，因此重跑前要換新名稱。舊 drill 目錄應按核准的
  classified-data disposal 流程清除，不由本工具自動刪除。
- 任何需要覆寫 live PostgreSQL、volume、bind mount、TLS/JWT key 或 ingress state 的
  步驟都不得由本 runbook 執行，必須等 Gate 6 destructive DR drill 核准。
