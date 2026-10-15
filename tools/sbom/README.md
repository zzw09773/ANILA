# Repository 套件 SBOM

`build_repo_sbom.py` 以 Python 3.11+ 標準函式庫讀取程式內固定 `MANIFESTS` 白名單，產生 CycloneDX 1.6 JSON 與範圍摘要。白名單涵蓋 28 份 npm package.json／package-lock.json、Python pyproject.toml／uv.lock／requirements 檔；不遞迴發現檔案、不跟隨 requirements include、不讀取應用設定、不載入應用或已安裝套件程式、不呼叫 installer／resolver／網路。Audit hook 在其他匯入前禁止 `.env*`、`.npmrc`、`secrets` 路徑及 `.pem`／`.key` 讀取。

在 `/tmp` 執行，將 `task_repo` 設為實際 checkout：

```bash
task_repo=/home/c1147259/Desktop/ANILA/anila-restart-20260729/ANILA
cd /tmp
python3 -I -S "$task_repo/tools/sbom/build_repo_sbom.py" \
  --repo "$task_repo" \
  --baseline 816f2b2b053023b9113892e4f7d595d2fc1ba18c \
  --output /tmp/quick-builder-repo-sbom.cdx.json \
  --summary /tmp/quick-builder-repo-sbom-summary.md
python3 -I -S "$task_repo/tools/sbom/test_build_repo_sbom.py"
```

固定基準必須為可解析的 Git commit，無效基準會失敗；每個白名單路徑均讀工作目錄與該 commit 的版本。`baseline-existing`、`added`、`version-or-constraint-changed` 按套件觀測定位比較版本／宣告範圍，移除另列；檔案 SHA256 保留非版本欄位變動證據。此分類不代表授權、作者或來源未變更。

每份來源檔案、定位及 observation 保留獨立 `bom-ref`；`component_count` 是觀測紀錄數，包含同名不同版本及多份宣告／lock，不是去重實裝套件數。宣告範圍不當作精確版本；requirements.txt 即使釘版或帶 hash 仍為 declared。npm lock 記錄巢狀解析及本機 link 邊；uv 依名稱／版本匹配，歧義不猜測；requirements.lock 只有相應 `.in` 提供依據時才區分直接／傳遞。未解析的邊保留 notices，不宣稱完整依賴閉包。Optional、dev 及跨平台項目全保留，不執行環境 marker。

`--installed-root /explicit/path/site-packages` 可重複指定；僅讀匹配 repo Python 套件名稱的 `*.dist-info/METADATA`，略過 symlink metadata，不匯入套件或執行 `.pth`。Installed 為額外觀測、標 `not-compared-installed-observation`，不取代 lock，也不證明建置或出貨。預設 repo SBOM 不讀本機安裝清單。

來源 URL 去除 userinfo、query、fragment；完整 JSON、巢狀 properties、constraint、notices 與摘要均脫敏。機敏資訊若位於 URL 路徑或一般自由文字，無法由 URL 結構辨認；白名單不應存放此類資料。授權名稱、作者及來源均為上游宣告且未逐項複核；`license = {file = ...}` 只記錄未讀取的證據路徑，不偽裝授權內容。國別／控制關係一律 unknown。OpenCC 的目前／移除觀測數與上游詞典來源列在 BOM properties；數字依實讀來源計算，不能證明程式不可達、環境已卸載或映像已移除。

輸出依固定來源排序，不含當下時間；metadata 記錄產生時的 `source-head` 及實讀 manifest SHA256。工作目錄內容可能尚未提交，HEAD 本身不代表所有來源已提交；正式交付在最後提交後重生。serialNumber 由內容雜湊產生，重跑相同輸入與 HEAD 應逐位元一致。此工具不代表完整 OS／映像／瀏覽器／字型／模型／遠端服務 SBOM，也不代表 NF-11 或弱掃通過。

```bash
python3 -I -S "$task_repo/tools/sbom/build_repo_sbom.py" \
  --repo "$task_repo" \
  --output /tmp/quick-builder-repo-sbom-repeat.cdx.json \
  --summary /tmp/quick-builder-repo-sbom-repeat-summary.md
cmp /tmp/quick-builder-repo-sbom.cdx.json /tmp/quick-builder-repo-sbom-repeat.cdx.json
cmp /tmp/quick-builder-repo-sbom-summary.md /tmp/quick-builder-repo-sbom-repeat-summary.md
sha256sum /tmp/quick-builder-repo-sbom.cdx.json /tmp/quick-builder-repo-sbom-summary.md
```

## 官方 schema 離線驗證

建構器只檢查必要結構及參照完整性。`validate_cyclonedx.py` 使用預先存在的 jsonschema 套件及官方 schema 做獨立驗證；不安裝套件，不執行 site／`.pth`，audit hook 禁止網路及保護檔讀取。缺少套件或 schema 時明確失敗，不能把結構檢查稱為完整 schema 驗證。

預先由可信流程取得以下 [CycloneDX 官方 1.6 schema 目錄](https://github.com/CycloneDX/specification/tree/1.6/schema)檔案，存於同一個本機目錄：`bom-1.6.schema.json`、`spdx.schema.json`、`jsf-0.82.schema.json`。驗證器僅使用這三份本機檔案；保留輸出 SHA256 以追溯版本。

```bash
cd /tmp
python3 -I -S "$task_repo/tools/sbom/validate_cyclonedx.py" \
  --bom /tmp/quick-builder-repo-sbom.cdx.json \
  --schema-dir /tmp/quick-builder-cyclonedx-schema \
  --site-packages /home/c1147259/anaconda3/lib/python3.11/site-packages
```

本機既有 jsonschema 4.19.2 驗證 repo 輸出，使用 Draft 7 schema 與 format checks，結果 0 errors；這只代表該份 JSON 通過所列 schema，不能證明內容完整或來源合規。正式交付須針對最後輸入重跑，另與出貨映像、OS、二進位、遠端服務及授權／來源佐證對帳。
