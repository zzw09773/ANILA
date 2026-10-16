# 快製 source-only 離線準備包

`release.py` 1.1.3 使用 Python 3.11+ 標準函式庫，產生固定來源選集的 manifest、SHA256 與可選 source tar。這是工程準備包，**不能直接建置或安裝完整平台**；不是映像交付物、完整 SBOM、來源合規核准或離線安裝通過證明。

操作步驟、固定資產位置、限制及正式交付前待辦，見[離線交付手冊](../../docs/guides/quick-builder-offline-delivery.md)。工具自身不執行 Docker、子程序、網路、下載、安裝或應用匯入。

```bash
cd /tmp
python3 -I -S /absolute/checkout/tools/quick-builder-release/test_release.py
mkdir /tmp/quick-offline-selection
python3 -I -S /absolute/checkout/tools/quick-builder-release/release.py build \
  --repo /absolute/checkout --out /tmp/quick-offline-selection --tar
python3 -I -S /absolute/checkout/tools/quick-builder-release/release.py verify \
  --bundle /tmp/quick-offline-selection
```

`selection_complete=true` 只表示程式內 `SOURCE_FILES` 清單都存在；`deployment_ready` 永遠為 false。`asset_files_complete` 也僅表示固定 slot 的檔案存在並完成雜湊。缺件列 missing，未提供來源 commit 時 revision claim 為 null；有提供也明記 `verified=false`。目前清單涵蓋快製必要產品整合點、依賴宣告、相關文件和本工具，不包含完整 repo 或完整 build context。新增產品檔案時須審查後明確更新白名單。

Guard 在其他匯入前安裝。禁止任何路徑元件中的 `.env*`、`secrets`、`keys`、`credentials`、`logs`、`.git`、`.ssh`、`.aws`、`.npmrc`、`.pem`／`.key`／`.log`／`.p12`／`.pfx`，阻止子程序及網路 audit 事件。所有輸入 root 必須為絕對路徑，逐層使用 directory FD + `O_NOFOLLOW`；檔案不接受 symlink、hardlink、FIFO／裝置檔或超限大小。沒有檔案發現、include 展開或遞迴掃描。

來源每檔上限 16 MiB、總量 64 MiB；外部資產每檔 32 GiB、總量 128 GiB，外部 repo SBOM 16 MiB。圖片／OCR／字型／瀏覽器等封存只當不透明檔案雜湊，工具不解包、不猜測實際版本或 image ID。Tar 只包含選定 source 與 manifest，固定排序、mtime=0、uid/gid=0、mode=0644、空 uname/gname；沒有時刻或主機絕對路徑欄位。

`verify` 不解壓到檔案系統，只接受白名單 regular members 並重算大小、SHA256 和固定 tar 屬性。它也核對原始 images.tsv 宣告；`observed_image_id` 必須為 null、`identity_verified` 必須為 false。校驗一致不代表可信簽章；應以受信任管道保存的 SHA256 比對。

輸出目錄須預先建立，位於來源 root 之外，工具拒絕覆寫固定輸出名稱。退出 0 僅表示準備或一致性檢查完成，即使仍有 missing；任何不安全／被更動／超限輸入退出 2。輸出若中途失敗，可能留下部分檔案，應保留失敗紀錄、改用新目錄重跑，不能把該目錄交付為通過。
