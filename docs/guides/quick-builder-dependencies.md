# 助手快製：依賴與來源交付說明

本文件是依賴盤點與交付檢核入口，不是完整 SBOM，也不代表已符合 NF-11 的來源限制。完整交付清單必須涵蓋既有與新增的直接／間接 npm、Python、系統套件、映像、OCR 語料及隨附資源，並以實際出貨產物為準。

## 本功能的依賴範圍

快製平台採用倉庫既有 Vue 畫布介面、CSP、資料庫、Agent、平台模型 proxy、文件處理及稽核設施。圖形操作以型別化資料及既有前端能力實作；不引入 Dify、Langflow 或可執行使用者程式的底層。

文件集採管由既有 CSP 管理 API 與資料庫權限處理，不因助手儲存或公開改動文件集 ACL。採管庫寫入精確限制單位，範本隨公開來源撤回而隱藏；停用恢復、Agent 管理護欄及 QuickRun 私有執行脈絡均由同一服務處理。文件草稿提供純文字 RTF，以及既有 Studio 四固定 ReportPreset、HTML／PDF／DOCX，已有 12 組離線轉檔工程證據，正式院內串接仍待驗。Studio 路徑只 render，不另叫 LLM 或 RAG，沿用既有 RegisteredService、caller 授權與 Artifact 匯出 gate；專屬 120 秒 JWT 的 type 為 `studio_quick_document`、aud 為 `studio-quick-document`，由既有 keyring 簽發，不能用於通用登入。SSE 可將終端模型的回覆即時送到畫面；流程須只有一個輸出，且直接連接最後的模型或格式步驟，模型亦須支援串流。其他結構與非終端步驟不保證即時 token 串流；來源、文件與最終結果仍須通過完整執行及授權檢查。

不引入替代簡繁套件或系統轉換工具。Runtime manifest 與 core lock 不宣告 OpenCC；依賴比較必須以最後提交重生的 SBOM 為準，包含移除觀測，不能沿用「依賴完全無差異」的結論。這只涵蓋 repo 指定檔案，不代表既有套件、傳遞依賴、正式映像或來源限制已全部核准。

| 範圍 | 倉庫內盤點依據 | 版本判讀與交付要求 |
|---|---|---|
| 治理前端 | `apps/csp-governance-ui/package.json`、`package-lock.json` | manifest 有範圍版本；實際解析版本取 lock 與出貨建置，涵蓋間接依賴與靜態資源 |
| ANILA Shell | `apps/anila-shell/package.json`、`package-lock.json` | 包含既有 React、路由與 Markdown 等；逐項確認來源授權，不因本次沒變更就豁免 |
| ANILA LM | `apps/anilalm/package.json`、`package-lock.json` | 文件上傳／知識庫相關前端仍屬完整平台交付範圍 |
| CSP | `services/csp/requirements.txt`、`requirements-dev.txt`、`infra/docker/csp.Dockerfile` | 需求檔釘版與實際安裝／移除套件需對帳；既有 FastAPI、SQLAlchemy、Alembic、JWT 等均須納入來源審查 |
| 共用文件處理 | `packages/anila-core/pyproject.toml`、`uv.lock` | `[rag]` 既有 PyMuPDF、pymupdf4llm、python-docx、odfpy、striprtf、Pillow；範圍不是出貨實裝版本 |
| 文件 worker | `services/ingestion-worker/pyproject.toml` 及其建置設定 | 沿用 anila-core、Arq、資料庫驅動與 HTTP client；核對映像內實際版本 |
| Studio 文件排版與匯出 | `services/anila-studio/` 既有 ReportPreset、文件 renderer 與 Artifact 匯出設施 | 四固定版型及 HTML／PDF／DOCX 沿用既有套件；本機實轉使用既有 pandoc 3.1.11 CLI 與 Playwright Chromium，無新增套件／下載。須納入完整 Studio 依賴、CLI、瀏覽器及字型來源清單；院內整合與出貨映像仍待驗 |
| 平台映像 | `scripts/release/images.tsv`、`scripts/release/build-release.sh` 及各 Dockerfile | 包含既有服務、OS 套件及基底映像；標籤不等於不可變版本，需映像 digest／ID 與 tar SHA256 |
| 本機舊 Office 轉檔 | `packages/anila-core/src/anila_core/ingestion/office_conversion.py` | 呼叫既有或預先提供的 LibreOffice／soffice；不新增 Python 套件，但系統工具仍需 SBOM、來源授權與離線封裝 |
| 獨立本機 OCR | `packages/anila-core/src/anila_core/ingestion/local_ocr.py`、`image_ocr.py` | 呼叫 Tesseract 與 `chi_tra`、`eng`；執行檔和語料各自記錄版本／來源／授權／校驗值 |
| 院內 Docling | 治理中心「外部服務」及 `ingestion/parser_registry.py` | 屬既有院內服務介接；服務映像、OCR 模型／語料的來源清單另附，不能只盤點 client |
| MCP 開發範本 | `examples/quick-builder-mcp/server.py`、`postgresql-readonly.sql` 及[開發指南](quick-builder-mcp.md) | 重用現有 FastAPI、httpx、python-jose、uvicorn、psycopg2 與 SQLite 標準庫；SQLite 臨時資料／PostgreSQL 替身測試不能代表院內真資料庫驗收 |

## 來源限制與文字處理契約

來源／控制關係未知的元件不能標成合規或准用。OpenCC Python 的[官方套件定義](https://github.com/yichen0831/opencc-python/blob/master/setup.py)列版本 0.1.7、作者 Yichen Huang 與 Apache 授權；[官方 NOTICE](https://github.com/yichen0831/opencc-python/blob/master/NOTICE.txt)明列使用 BYVoid/OpenCC 詞典及設定。這能核對專案及授權自述，不能證明國別／控制關係符合院方限制，因此產品不載入或呼叫 OpenCC，也不複製其詞典或改用另一個未核定簡繁庫。

| 路徑 | 實際能力與限制 |
|---|---|
| CSP／core 保存與共用文字工具 | `normalize_zh_tw`／`normalize_report` 僅套用倉庫原有 `domain_terms` 固定表；未列入的字元保留原文。None、空值及變更字數契約保留。`intl.zh_normalize` 只控制這個固定用語處理，不能啟用完整簡繁轉換。 |
| Studio 投影片、報告、資訊圖表、資料表 | 使用服務原有的 citation／LaTeX 清理及固定台灣用語表；數字、ID、檔名與來源欄位依既有型別／欄位規則保留。Studio 不匯入 anila_core 或外部轉換詞典；未列字元不轉換，不等價於 s2tw／s2twp。心智圖保持引用／LaTeX 清理，不提供字形轉換。 |
| 建議追問 | 能力固定不可用，`FOLLOW_UPS_UNAVAILABLE`，畫面為「追問建議目前未提供」。API 保留身分、擁有者及訊息狀態檢查；生成、parser、快取回放均回空建議，不因使用者或管理設定開啟。GET／寫入回覆不外送舊 `follow_ups`／`follow_ups_generated`，資料庫歷史保留供稽核。 |
| ASR | `ASR_OPENCC_MODE=off` 保留辨識原文。所有非 off 值明確回報 `ASR_TEXT_CONVERSION_UNAVAILABLE`，須將部署設定遷移為 off；不默默轉換或載入替代套件。 |

這些固定詞表僅代表可追溯的倉庫既有規則，不能由「在 repo 內」推定所有資料的來源國別已核定。除明列詞彙外，不承諾任意簡體轉繁體、完整台灣字形或簡體偵測；人名、引文及未列字元以原文為準。正式交付仍須核對詞表、其餘套件及所有隨附資源的來源證據；任何未決來源不得列成 NF-11 通過。

限定本機快製、CSP、worker、core 與 Studio 程式搜尋未發現 jieba／Paddle 的 import 或呼叫。中文關鍵字採標準函式庫的字元組合；院內遠端 Docling 所使用的 OCR 引擎仍需另附 SBOM，不能由本機沒有這些 import 推定遠端也沒有。

產品原始碼與宣告不包含 OpenCC 執行依賴，不代表既有主機或舊映像已卸載套件。正式離線產物須另核對 distribution、詞典、設定檔及完整映像清單。ICU／uconv 也不是已啟用的替代方案；不因存在其他系統函式庫就推定可用或准用。沒有新套件不能替代來源審認。

## Repository manifest／lock SBOM

[產生工具與重跑說明](../../tools/sbom/README.md)採 Python 3.11 標準函式庫及固定 manifest／lock 白名單，輸出 CycloneDX 1.6 JSON；不載入應用程式、不解析 include、不安裝套件、不讀取密鑰或 `.npmrc`。比較基準為 `816f2b2b053023b9113892e4f7d595d2fc1ba18c`。觀測數、宣告／鎖檔數及新增／版本變動／移除數量以最後提交重生的 JSON 摘要為準。數量按來源檔案與套件定位保留，包含同名不同版本、不同宣告及 lock 位置，並非去重後的實體安裝套件數。

`declared` 表示 manifest 宣告；精確釘版或含 hash 的 requirements.txt 仍不等於 lock。`locked` 表示鎖檔記錄；direct／transitive 僅在鎖檔與明確輸入可判定時標示，無法判定者為 unknown。可選的 `--installed-root` 只讀指定既有 distribution metadata，產生獨立 installed 觀測，不能覆蓋 lock、證明實際出貨或當作此次新增。版本範圍不偽裝精確版本；授權檔路徑不偽裝已讀取授權內容。來源、授權及開發者為宣告資料，國別／控制關係未證實者標 unknown 並保留未決，不當作准用。URL 的 userinfo、query 及 fragment 在 JSON 與摘要各欄位移除。

官方 [CycloneDX 1.6 schema](https://github.com/CycloneDX/specification/blob/1.6/schema/bom-1.6.schema.json)及其 SPDX／JSF 相依 schema 可預先存於離線環境；獨立驗證器使用既有 jsonschema，禁止網路及保護檔讀取。建構器本身只做結構檢核；每份交付 JSON 須另執行官方 schema、format 與參照檢查。Schema 合法不等於依賴完整、授權核准或 NF-11 通過。

此 repo SBOM 保留所有宣告的 optional／dev／平台條件項目，不執行 resolver 或安裝環境 marker；部分 Python 宣告缺少傳遞鎖檔，未解決或有歧義的依賴邊列於 notices。白名單外的元件、OS、映像、二進位、瀏覽器、字型、模型、OCR 語料、遠端 Docling 與院內 MCP 服務另行盤點；實際出貨仍須與映像及安裝清單對帳。NF-11 尚未通過。

## 系統工具與語料的狀態

開發主機的 LibreOffice 已有 DOC／PPT／RTF 工程樣本實轉證據，不能當作 ingestion-worker 容器內的能力或版本。Tesseract 及 `chi_tra` 的缺失測試或替身測試，只能證明錯誤處理；沒有真實繁中 OCR 品質通過證據。正式映像尚未製作；未提供工具、語料與映像清單前，不能宣稱獨立 OCR 已可離線交付。

LibreOffice、Tesseract、`chi_tra`、`eng` 各需獨立列出上游專案、實際取得位置、套件發行者、精確版本／提交、授權、下載或封存 SHA256、來源背景審查及修補責任。系統套件的版本要含發行版修訂；語料沒有版本標籤時用提交與雜湊識別，不猜測版本。離線環境不得執行時自動下載工具、字型或語料。

### 核心解析元件的來源佐證

核對日期為 2026-10-16。下表分開記錄 lock、本機 metadata 及上游參考版本；這些都不能取代正式映像的實裝清單。八項元件的完整來源／控制背景仍未核定，未知不視為准用。網站內容與參考 tag 只證明所列上游自述，正式離線交付須另保存相符原始碼、授權全文、NOTICE、二進位／語料 SHA256 及背景審認結果。

| 元件 | 版本與本機觀測 | 上游授權／專案依據 | 待補證據 |
|---|---|---|---|
| LibreOffice | 本機 OS 套件 `1:7.3.7-0ubuntu0.22.04.13`；上游參考 `libreoffice-7.3.7.2`，Ubuntu 修訂尚未完整對帳 | [官方授權說明](https://www.libreoffice.org/licenses/)列 MPL-2.0 主體與其他隨附授權；[TDF imprint](https://www.libreoffice.org/imprint/)可證基金會受德國法管轄 | 基金會管轄地不等於全部程式／依賴的來源國別；出貨映像、Ubuntu 修訂來源與隨附授權仍需核對 |
| Tesseract | 固定 PATH 未找到執行檔，`dpkg` 顯示 `tesseract-ocr` 未安裝；尚未選定出貨版本 | [官方專案](https://github.com/tesseract-ocr/tesseract)及 [Apache-2.0 授權](https://raw.githubusercontent.com/tesseract-ocr/tesseract/main/LICENSE) | 精確版本、binary/source、系統依賴、來源背景與離線執行證據 |
| `chi_tra` 語料 | 標準 4.00／5 路徑均缺；尚未選定 family、commit 或 SHA256 | [官方 best 說明](https://raw.githubusercontent.com/tesseract-ocr/tessdata_best/main/README.md)及 [standard 資料血緣](https://raw.githubusercontent.com/tesseract-ocr/tessdata/main/README.md) | 語料與原訓練資料／字型／詞典來源、權利及控制背景；繁體中文不是產地證據 |
| `eng` 語料 | 同上，固定路徑缺件，無已核定版本 | [官方版本與語言資料表](https://tesseract-ocr.github.io/tessdoc/Data-Files-in-different-versions.html) | 不能由相同檔名或英文語言推定來源；須與實際選定的語料 repo／commit／SHA256 對帳 |
| PyMuPDF | `uv.lock` 為 `1.27.2.2`；本機 metadata 觀測為 `1.26.7` | [固定 tag 專案說明](https://raw.githubusercontent.com/pymupdf/PyMuPDF/1.27.2.2/README.md)及 [AGPLv3](https://raw.githubusercontent.com/pymupdf/PyMuPDF/1.27.2.2/COPYING)，另有商業授權選項 | lock 與本機 metadata 不一致；出貨版本待對帳，授權路徑與控制背景未核定 |
| PyMuPDF4LLM | `uv.lock` 為 `1.27.2.2`；本機 metadata 觀測為 `0.2.9` | [固定 tag 專案說明](https://raw.githubusercontent.com/pymupdf/PyMuPDF4LLM/1.27.2.2/README.md)及 [AGPLv3 授權](https://raw.githubusercontent.com/pymupdf/PyMuPDF4LLM/1.27.2.2/LICENSE) | 版本對帳、完整權利／控制證據及實際隨包聲明 |
| python-docx | lock／本機 metadata 均為 `1.2.0`；參考 tag `v1.2.0` | [MIT／Steve Canny 授權檔](https://raw.githubusercontent.com/python-openxml/python-docx/v1.2.0/LICENSE)及 [固定 tag metadata](https://raw.githubusercontent.com/python-openxml/python-docx/v1.2.0/pyproject.toml) | 作者自述不能證明國籍／控制背景；`lxml` 等傳遞依賴另核 |
| 院內 Docling | 實際遠端版本與 image digest 未知；上游參考 `v2.60.0`／`3467b0a0357eb72fd5ab3b45ef03c07305e8bf8e` | [參考版本 MIT 授權](https://raw.githubusercontent.com/docling-project/docling/v2.60.0/LICENSE)列 IBM copyright；[目前 main MIT 授權](https://raw.githubusercontent.com/docling-project/docling/main/LICENSE)列 Docling Contributors | 不能將參考版本當作院內版本，亦不能由 IBM 名稱或研究團隊所在地推定整套國別；遠端 OCR／模型／語料與映像清單未齊 |

本機數位 PDF 路徑使用 `pymupdf4llm`／`fitz`，DOCX 使用 `python-docx`，PPTX 使用標準函式庫 ZIP／XML。這個限定路徑沒有使用 `pypdf` 或 `python-pptx`，不能據此推定遠端服務也沒有。Docling 的[參考版本宣告](https://raw.githubusercontent.com/docling-project/docling/v2.60.0/pyproject.toml)包含 `python-pptx` 與 RapidOCR；實際遠端部署須提供自己的完整依賴與模型清單，且不得採用未符合來源限制的元件或模型。

Docling [官方文件](https://docling-project.github.io/docling/)記載 IBM Research Zurich 團隊起始及 LF AI & Data hosting。這些資訊不等同於完整法律控制關係，也無法證明未知遠端映像的版本或授權。Client 程式註解不能作為來源核准文件。

## 完整 SBOM 每筆欄位

| 欄位 | 內容 |
|---|---|
| 元件識別 | 套件／映像／語料名稱、類型、直接或傳遞依賴、所屬服務 |
| 精確版本 | 實際出貨版本、套件發行版修訂、映像 digest 或上游提交 |
| 來源 | 上游專案與原始碼網址、實際取得位置、發行者／維護者及可查證來源資料 |
| 授權 | SPDX 或實際授權檔、修改及院內使用條件、需隨附的聲明 |
| 校驗 | 封存或套件 SHA256、映像 ID／digest、所屬出貨清單 |
| 合規判定 | NF-11 背景審查證據、審查人／日期、通過或待釐清理由 |
| 安全維護 | 已知漏洞、修正版判定、公告來源、修補／回歸責任及離線更新安排 |

使用 SPDX／CycloneDX 或院方核准格式交付完整 SBOM；本文件的依據表不能代替機器可讀清單。授權與來源佐證須可在院內保存閱讀，不只提供需院外連線的 URL。

## 出貨前核對

1. 以最後提交的 manifest、lock、實際安裝清單及每張映像 OS 清單對帳，確認建置工具與執行期套件的範圍；保留產生工具版本與命令紀錄。
2. 對全部元件及離線 fallback 系統工具／語料補齊來源與授權，未決項不得標成通過。來源背景審查與弱點掃描分開記錄。
3. 逐層檢查映像、原始碼與設定匯出不含密鑰；正式弱掃與源碼檢測另附完整報告與高風險修正複測結果。
4. 核對映像 tar、`images.tsv`、manifest、SHA256 與原始碼提交，並在無網路目標環境實做安裝、OCR、舊格式轉檔與故障切換。
5. 交付離線修補包的重建、回歸及回復程序，核定保固與修補時效。各項完成證據填入[驗收表](quick-builder-acceptance.md)。

## Pandoc 既有工具的來源佐證

本機既有 CLI 回報 Pandoc 3.1.11；相同版本的[官方套件描述](https://raw.githubusercontent.com/jgm/pandoc/3.1.11/pandoc.cabal)記載 `GPL-2.0-or-later`，作者與維護者為 John MacFarlane。[作者履歷](https://johnmacfarlane.net/macfarlane-cv.html)列任職美國加州大學柏克萊分校；這是主要維護者所屬地的佐證，不能推定全體貢獻者國籍、資本背景或本機二進位的供應鏈。正式出貨仍須取得可追溯的來源、授權及二進位校驗證明；本機 `/home/c1147259/bin/pandoc` 非 dpkg 列管，不能因 12 份轉檔成功就判為完整 SBOM 已核准。

文件輸出重用 Studio 的固定 HTML 範本，以標準函式庫呼叫既有 Pandoc CLI：固定 `--sandbox`、HTML→DOCX、私有暫存目錄、無 shell／外掛／filter、45 秒子程序上限。沒有安裝或下載 Pandoc 或 Python wrapper；正式環境缺工具時明確失敗。
