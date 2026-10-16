# 離線檔案備份校驗與還原演練

`recovery.py` 與 `delivery_plan.py` 使用 Python 3.11+ 標準函式庫，適用支援 directory FD、`O_NOFOLLOW` 與 `O_EXCL` 的 Linux/POSIX 環境。沒有新增套件、下載、安裝、應用程式匯入、資料庫連線、Docker 呼叫或網路連線。兩支入口在其他函式庫匯入前安裝 audit guard；測試只先讀取明確指定的同目錄工具程式以載入相同 guard，不執行 site 或 `.pth`。

這是檔案 bytes 的備份／演練工具。正式 DB、Artifact 歸屬／狀態／版本、文件索引、ACL、密等、金鑰與 volume 的一致性仍須依 [維運流程](../../docs/guides/quick-builder-recovery-drill.md) 驗收。

## 檔案清冊

備份來源只能是維護人員已準備好的單一快照根目錄。清冊必須逐筆指定檔案、種類、邏輯 ID、預期大小與預期 SHA256，不自動掃描檔案樹，也不從正式資料庫猜測檔案位置。

```json
{
  "schema": "anila.file-recovery-selection/v1",
  "entries": [
    {
      "category": "artifact",
      "logical_id": "artifact-123-version-2",
      "path": "artifacts/report.pdf",
      "size": 12345,
      "sha256": "填入由一致性快照取得的64位小寫十六進位雜湊"
    }
  ]
}
```

上例是欄位說明，雜湊佔位文字會被拒絕。`category` 只接受 `artifact`、`ingestion`；同一種類的邏輯 ID 不可重複。大小包含零位元組檔案；這只記錄檔案 bytes，不代表文件解析成功。預期 hash 應來自受信任的資料清冊或快照校驗紀錄，不應由未知備份包自行宣告後直接信任。

保護規則：

- 所有輸入根目錄為明確絕對路徑，逐層 directory FD 加 `O_NOFOLLOW` 開啟；檔案只接受 link count 為 1 的一般檔。拒絕 symlink、hardlink、FIFO、裝置檔、路徑穿越及來源／輸出根目錄重疊。
- `.env*`、`secrets`、`keys`、`credentials`、`logs`、`.git`、`.ssh`、`.aws`、`.npmrc` 及 `.pem/.key/.log/.p12/.pfx` 在路徑驗證和 audit guard 兩層拒絕，不因測試假檔或公開憑證放行。
- 路徑各段以文數字開頭，僅含 NFC 文數字、`-_.`；來源最多 12 層、1,024 字元、各段 120 字元。拒絕大小寫折疊後的重複路徑、檔案／目錄前綴衝突及重複邏輯 ID。
- 清冊／manifest 上限 1 MiB；最多 512 檔、單檔 64 MiB、清冊總量 256 MiB。這些是工具安全上限，不是平台文件量或 NF-10 驗收上限。
- 以有界串流核對大小與 hash，讀取前後檢查 inode、大小、時間與 link count，備份和演練結果皆重新讀回校驗。檔案系統時間精度可能無法反映同大小的即時改寫，這些檢查不提供檔案系統快照或無競態保證；來源仍須停寫或採既有一致性快照。相同 bytes 可共用一個 blob，但每個來源檔仍逐一檢查。
- 網路、DNS、process spawn 及 ctypes 載入均被拒絕；工具不執行資料檔、不解壓任何 archive。檔案權限為 `0600`、目錄為 `0700`，不還原來源的可執行位元、owner、ACL 或 xattr。

## 命令

以下路徑是待維護人員準備的演練輸入，並非工具自動建立的正式備份。`--out` 必須不存在；其父目錄必須已存在。`--scratch` 必須是預先建立的空目錄。所有輸出放 `/tmp`，不更改 `HOME` 或 `CODEX_HOME`。

```bash
task_repo=/absolute/path/to/ANILA
cd /tmp
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC TMPDIR=/tmp \
  /usr/bin/python3 -I -S -B "$task_repo/tools/quick-builder-recovery/recovery.py" backup \
  --source-root /tmp/approved-file-snapshot \
  --selection /tmp/approved-file-selection.json \
  --out /tmp/quick-file-backup-new
```

成功輸出含 `manifest_sha256`。將這個值保留在獨立、可信的交接紀錄；後續 `verify`、`rehearse` 必須帶入它。若包和 hash 一起被未受信任者替換，SHA256 無法驗證發布者身分。工具不提供數位簽章或密鑰管理。

```bash
# 將可信交接紀錄中的 hash 填入變數後執行。
task_manifest_sha256=填入受信任的64位小寫十六進位hash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC TMPDIR=/tmp \
  /usr/bin/python3 -I -S -B "$task_repo/tools/quick-builder-recovery/recovery.py" verify \
  --bundle /tmp/quick-file-backup-new --manifest-sha256 "$task_manifest_sha256"

env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC TMPDIR=/tmp \
  /usr/bin/python3 -I -S -B "$task_repo/tools/quick-builder-recovery/recovery.py" rehearse \
  --bundle /tmp/quick-file-backup-new --manifest-sha256 "$task_manifest_sha256" \
  --scratch /tmp/empty-recovery-scratch
```

包內容為 `manifest.json`、`blobs/<SHA256>` 及最後才寫入的 `COMPLETE.json`。相同選擇與 bytes 產生相同 manifest；工具不包含目前時間、絕對來源路徑或個人環境。備份包之外的檔案不會被發現或帶入還原；blob 也只按 manifest 明列名稱讀取。

演練結果在 `--scratch/rehearsal-<manifest SHA256>/data/<category>/<logical_id>/<原相對路徑>`，逐檔讀回成功後才寫 `REHEARSAL.json`。不修改既有目錄，不覆寫任何檔案。失敗可能留下部分輸出，且沒有成功標記；保留失敗證據後由維護人員另行處理，重試使用新的輸出及空白 scratch。這不是檔案系統快照、斷電耐久性保證或正式還原指令。

`backup/verify/rehearse` 成功 exit 0，拒絕或缺檔 exit 1，CLI 只輸出固定錯誤 type，不回傳原始例外、stderr 或主機路徑。JSON 中 `database_consistency_verified`、`authorization_restored`、`worker_readiness_verified`、`deployment_ready` 固定為 false。

## Patch／來源核定清冊

[delivery_plan.schema.json](delivery_plan.schema.json) 定義清冊格式；[delivery-plan.example.json](delivery-plan.example.json) 是全數未核定的合成範例，revision、變更 hash 也是佔位值，不是本次交付清冊。`delivery_plan.py` 會驗證型別、大小、欄位、固定 slot 及跨欄位關係，不需要 jsonschema，也不連線取得 schema。

```bash
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC TMPDIR=/tmp \
  /usr/bin/python3 -I -S -B "$task_repo/tools/quick-builder-recovery/delivery_plan.py" \
  --plan "$task_repo/tools/quick-builder-recovery/delivery-plan.example.json"
```

- 每筆 source 記錄版本、取得網址、發布者、授權宣告、作者／控制國別宣告、資產 hash 及核定者／證據 hash。未知用 `null`；`pending`、`rejected`、unknown 或缺資料均列在 `sources_missing`。國別只驗兩碼宣告格式，不從網址、專案名稱或作者姓名推定國別，不判定 NF-11 合規。
- 網址只做 HTTPS 結構檢查，拒絕 userinfo、query、fragment、port 與反斜線，不發出任何請求；官方性、取得來源與授權內容仍待人工證據審核。SPDX 表達式僅保存宣告，不驗其有效性或適用性。
- 固定 16 個映像 slot 必須分別對應 `image:<slot>` source，archive hash 若有填入，必須與該 source 的 artifact hash 相同。slot 必須分列 archive hash、實際 image digest、映像 SBOM hash、授權審查 hash，缺漏列在 `images_missing`。source bundle hash 不會填補映像 slot。
- `worker.scope` 固定 `target-worker-image`，digest 必須與 ingestion-worker slot 相同。parser、Tesseract、chi_tra、eng、LibreOffice、Pandoc、fonts、Chromium、無外連政策各有獨立證據 hash，缺漏列在 `worker_checks_missing`。目前 CSP 程序診斷不能填作 worker 映像驗收。
- patch 記錄完整 base/target commit、逐檔 before/after hash（新增／刪除的一側為 null），拒絕不變更、重複、重疊或機密路徑。工具不讀 Git、不產生或套用 diff、不進行部署。
- 驗證只計算清冊 canonical SHA256 並列缺漏，不讀取參照證據。`metadata_complete` 僅表示所列項目的欄位填齊；完整依賴閉包與證據真實性未驗證。`evidence_references_verified`、`source_approval_verified`、`image_identity_verified`、`worker_readiness_verified`、`patch_apply_permitted`、`deployment_ready` 永遠 false，任何填齊的欄位都不構成部署許可。

清冊結構合法且欄位填齊 exit 0；結構合法但缺資料 exit 2；拒絕或讀取失敗 exit 1。缺資料不是通過交付。範例應得到 exit 2、16 個 `images_missing`。

## 合成測試

```bash
cd /tmp
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC TMPDIR=/tmp \
  /usr/bin/python3 -I -S -B "$task_repo/tools/quick-builder-recovery/test_recovery.py"
env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC TMPDIR=/tmp \
  /usr/bin/python3 -I -S -B "$task_repo/tools/quick-builder-recovery/test_delivery_plan.py"
```

測試只使用 `/tmp` 合成 bytes，不建立或讀取保護名稱的假機密檔；拒絕測試在 open 前攔截。輸出包含逐 node outcome、數量及失敗例外類型，不輸出 traceback 或任意設定。測試涵蓋雙種類往返、pin／blob／manifest 竄改、缺檔、不覆寫、連結／FIFO、輸入變動、深度與大小上限、guard、所有映像缺漏、假核定、worker 身分綁定與安全 CLI 錯誤。
