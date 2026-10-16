# 助手快製：source-only 離線準備與待交付資產

這份流程產出固定來源選集、可重現 manifest 與 SHA256，可供工程審閱與離線交付前盤點。它**不產生映像、不執行 Docker、不連 Docker daemon、不安裝或下載任何東西**。Source tar 不含完整 repo、執行期依賴、密鑰、資料庫或映像，不能當作可安裝的出貨包。NF-11、正式弱掃、院內整合與斷網安裝驗收仍須各自提供證據。

## 先準備安全的工作目錄

以下命令由工程人員在已知來源 checkout 上執行。Python 3.11+ 即可；不需安裝套件。所有輸入與輸出目錄須為絕對路徑，不能經過符號連結。輸出目錄須預先建立、位於 checkout 之外，且固定產物名稱不能已存在。

```bash
task_repo=/absolute/path/to/ANILA
cd /tmp
python3 -I -S "$task_repo/tools/quick-builder-release/test_release.py"
mkdir /tmp/quick-offline-a /tmp/quick-offline-b
python3 -I -S "$task_repo/tools/quick-builder-release/release.py" build \
  --repo "$task_repo" --out /tmp/quick-offline-a --tar
python3 -I -S "$task_repo/tools/quick-builder-release/release.py" build \
  --repo "$task_repo" --out /tmp/quick-offline-b --tar
cmp /tmp/quick-offline-a/manifest.json /tmp/quick-offline-b/manifest.json
cmp /tmp/quick-offline-a/SHA256SUMS /tmp/quick-offline-b/SHA256SUMS
cmp /tmp/quick-offline-a/source.tar /tmp/quick-offline-b/source.tar
python3 -I -S "$task_repo/tools/quick-builder-release/release.py" verify \
  --bundle /tmp/quick-offline-a
```

相同檔案內容與選項應逐位元一致。程式在產 tar 時重核來源雜湊，若工作目錄正在變動會拒絕；待最後修改穩定後改用新目錄重跑。`--revision` 可附工程人員已核對的完整 40／64 位小寫 commit hash；工具不讀 `.git`、不執行 Git，記為 `revision_claim: {value: ..., verified: false}`，每檔實讀 SHA256 才是這份來源快照的直接依據。不要把目前 HEAD 當作所有未提交內容都已包含於該 commit。

## 讀懂產物與結果

| 產物／欄位 | 可證明的範圍 |
|---|---|
| `manifest.json` | 工具版 1.1.3、schema v1、固定來源路徑與 SHA256、固定資產 slot 的 present／missing、images.tsv 原有宣告 |
| `SHA256SUMS` | manifest 及可選 source.tar 的雜湊；本身不是簽章或來源授權 |
| `source.tar` | 只含 manifest 與 `source/` 下的固定選集；不包含外部資產、密鑰、完整 build context |
| `selection_complete` | 僅表示固定 `SOURCE_FILES` 選集齊全，不是產品、依賴或出貨完成 |
| `asset_files_complete` | 僅表示每個固定 slot 的檔案存在並已雜湊，沒有驗證其內容、授權或可用性 |
| `deployment_ready`、`delivery_ready` | 始終為 false；此工具不能宣告安裝、還原、OCR 或來源合規通過 |
| `observed_image_id`、`identity_verified` | 始終 null／false；映像標籤與 tar SHA256 不偽裝成 image ID／digest 驗證 |

`build` 顯示 prepared、`verify` 顯示 checksums-consistent 或退出 0，只表示上述有限動作成功；missing 清單仍是缺件。校驗一致不等於沒有惡意內容，也不是「沒有密鑰」的內容掃描證明；來源白名單應維持不含敏感資訊，正式交付另需審核與掃描。

## 可選：盤點已由核准管道提供的資產

工具預設完全不搜尋主機資產。只有給 `--assets-root /explicit/staging`，才讀以下固定位置，不遞迴、不跟 symlink、不解包，也不從 images.tsv 任意擴大可讀路徑。資產 root 不可放在 keys／secrets／logs 等禁止路徑下。

| 固定相對位置 | 待核對資產 |
|---|---|
| `images/{pgvector,pgbouncer,csp-credential-dirs,csp,redis,ingestion-worker,router,nginx,pptx-renderer,hr-lookup,anila-studio,anilalm,anila-ui,codeserver,n8n,asr-gateway}.tar.gz` | scripts/release/images.tsv 所列 archive slots；宣告版本原樣保留，尚未證明實際映像 ID／digest |
| `ocr/tesseract.tar.gz` | 既有 Tesseract 執行環境及來源／版本證據待核對 |
| `ocr/chi_tra.traineddata`、`ocr/eng.traineddata` | 繁中／英文 OCR 語料；須另附實際來源、版本或提交、授權與品質證據 |
| `office/libreoffice.tar.gz`、`document/pandoc.tar.gz` | 離線轉檔工具與系統依賴；不以主機有 CLI 推定目標映像可用 |
| `fonts/fonts.tar.gz`、`browser/chromium.tar.gz` | Studio／文件渲染需要的核准字型與瀏覽器資產 |
| `sbom/repo.cdx.json`、`sbom/images.cdx.json` | 實際選集與映像／OS／傳遞依賴 SBOM；此工具不替代官方 schema 或完整性驗證 |
| `licenses/third-party-notices.txt` | 可在院內保存閱讀的授權／來源聲明；存在不代表已審認 |

```bash
python3 -I -S "$task_repo/tools/quick-builder-release/release.py" build \
  --repo "$task_repo" --out /tmp/quick-offline-assets \
  --assets-root /explicit/staging --tar
python3 -I -S "$task_repo/tools/quick-builder-release/release.py" verify \
  --bundle /tmp/quick-offline-assets --assets-root /explicit/staging
```

先建立新的 `/tmp/quick-offline-assets` 目錄。沒有對應檔案時記 missing；不會自製空 tar、虛構版本或將 optional slot 當作已交付。每個資產上限 32 GiB、總量 128 GiB；來源每檔 16 MiB、總量 64 MiB。上限若不適用，須先審查調整程式固定限制，沒有無界覆寫旗標。

若已有 repository SBOM，可額外指定 `--repo-sbom /explicit/existing.cdx.json`，只接受有界、無 symlink 的 CycloneDX JSON；它覆蓋 repo SBOM slot 的輸入，不生成或下載 SBOM。這只驗證 `bomFormat` 及校驗值，不冒稱官方 schema、來源或依賴完整性已通過。驗證時提供同一 `--repo-sbom` 路徑以重算；如也有其他 present 資產，同時提供其 `--assets-root`。

只給 `verify --bundle` 時，核對的是封包內選集與 manifest；外部資產不在 source.tar 裡，因此沒有重讀它們。輸出 `present_asset_checksums_verified=false` 會保留這個區別。帶入資產 root／SBOM 後才逐項檢查 manifest 中 present 的外部檔案；missing 仍不變成 present。Manifest-only 模式不加 `--tar`，驗證時必須提供 `--repo` 以核對來源。

## 實際離線出貨仍待完成

1. 固定最後提交與來源 SHA256，重產相符的 repo SBOM、source 選集與資產清單；本工具輸出不可取代完整建置來源。
2. 由另行核准的建置／匯出程序提供所有真實映像與不可變 ID／digest、映像 tar、OS／實裝套件與傳遞依賴 SBOM。這份手冊不執行該程序。
3. 補齊 OCR、Office／Pandoc、瀏覽器、字型、遠端 Docling／模型／MCP 服務的來源、授權、版本、雜湊與修補責任；由院方審認 NF-11。
4. 在無網路目標環境驗證安裝、升版、備份還原、權限撤回、真繁中 OCR、舊格式轉檔與文件匯出；保留實際結果，不以替身或主機樣本取代。
5. 以受信任管道比對交付 SHA256，另附弱掃、源碼檢測、高風險複測與回復演練，再依[驗收表](quick-builder-acceptance.md)判定。

依賴來源與已知缺口見[依賴說明](quick-builder-dependencies.md)；日常維運與權限撤回見[操作手冊](quick-builder-operations.md)。

## 固定選集與未打包項目的對照

本版以固定基準 `816f2b2b053023b9113892e4f7d595d2fc1ba18c` 做工程時的檔名對照，選集包含 Shell／治理 UI、CSP／Studio／ASR／worker／core 的已知快製及 OpenCC 修改接點、依賴宣告與操作文件。未選入的已變更測試／工程佐證檔逐一列在 `omitted_changed_paths`，只列路徑與原因，不讀其內容。這是一次固定對照，不是工具執行時重新檢查 Git，也不是完整功能檔案自動證明；`coverage_inventory_verified=false` 保留這個限制。最後若仍有新增或移動檔案，須人工重核並更新固定清單後再產包。
