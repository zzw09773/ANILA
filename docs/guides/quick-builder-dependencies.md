# 助手快製：依賴與來源交付說明

本文件是依賴盤點與交付檢核入口，不是完整 SBOM，也不代表已符合 NF-11 的來源限制。完整交付清單必須涵蓋既有與新增的直接／間接 npm、Python、系統套件、映像、OCR 語料及隨附資源，並以實際出貨產物為準。

## 本功能的依賴範圍

快製平台採用倉庫既有 Vue 畫布介面、CSP、資料庫、Agent、平台模型 proxy、文件處理及稽核設施。圖形操作以型別化資料及既有前端能力實作；不引入 Dify、Langflow 或可執行使用者程式的底層。

文件集採管由既有 CSP 管理 API 與資料庫權限處理，不因助手儲存或公開改動文件集 ACL。採管庫寫入精確限制單位，範本隨公開來源撤回而隱藏；停用恢復、Agent 管理護欄及 QuickRun 私有執行脈絡均由同一服務處理。文件草稿提供純文字 RTF，以及既有 Studio 四固定 ReportPreset、HTML／PDF／DOCX，已有 12 組離線轉檔工程證據，正式院內串接仍待驗。Studio 路徑只 render，不另叫 LLM 或 RAG，沿用既有 RegisteredService、caller 授權與 Artifact 匯出 gate；專屬 120 秒 JWT 的 type 為 `studio_quick_document`、aud 為 `studio-quick-document`，由既有 keyring 簽發，不能用於通用登入。SSE 仍是模型完成後分段送結果，未提供即時模型 token stream。

對照基準 `816f2b2b053023b9113892e4f7d595d2fc1ba18c` 時，所核對的治理前端／Shell／ANILA LM manifest 與 lock、CSP requirements、anila-core manifest／lock、ingestion-worker manifest 沒有本功能新增 npm／Python 套件的差異。這只說明指定檔案的變更範圍，不代表既有套件與完整傳遞依賴已全體複核，也不能替代實際映像的 SBOM。交付前須重新比對最後提交。

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

倉庫現有 requirements 包含 `opencc-python-reimplemented==0.1.7`；其他現有套件也可能有待核對的維護者或來源背景。本文件不對其國別、控制關係或合規作推定。NF-11 必須對完整清單逐項提供可查證來源並由院方審認；有疑義者列為未決，不能僅憑名稱、網站網域、上傳平台或某位貢獻者認定產地。

## 系統工具與語料的狀態

開發主機的 LibreOffice 已有 DOC／PPT／RTF 工程樣本實轉證據，不能當作 ingestion-worker 容器內的能力或版本。Tesseract 及 `chi_tra` 的缺失測試或替身測試，只能證明錯誤處理；沒有真實繁中 OCR 品質通過證據。正式映像尚未製作；未提供工具、語料與映像清單前，不能宣稱獨立 OCR 已可離線交付。

LibreOffice、Tesseract、`chi_tra`、`eng` 各需獨立列出上游專案、實際取得位置、套件發行者、精確版本／提交、授權、下載或封存 SHA256、來源背景審查及修補責任。系統套件的版本要含發行版修訂；語料沒有版本標籤時用提交與雜湊識別，不猜測版本。離線環境不得執行時自動下載工具、字型或語料。

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
