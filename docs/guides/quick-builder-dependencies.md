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

## 有限路徑盤點與合規阻礙

快製路徑的有限盤點核對 12 份已知 npm／Python manifest 與 lock，對固定基準沒有依賴差異；另讀取本機已安裝 distribution metadata 及靜態 import／呼叫位置，區分直接使用、共用啟動匯入、條件執行與只在宣告中存在的元件。這份範圍不含完整傳遞依賴閉包、遠端 Docling 映像／模型／語料、所有 OS 套件、字型或實際出貨映像，不能當作完整 SBOM。既有元件來源背景仍有未決事項，NF-11 尚未通過。

OpenCC 在快製流程有實際依賴，不能因不是新增套件就排除：

- 串流模型步驟由 `quick_execution` 進 `quick_model_stream`，再匯入 `reasoning_gate` 的 `anila_core.text.leaked_thought`；Python 會先執行文字子套件 `__init__.py`，其 `zh_normalizer` 匯入 `opencc.OpenCC`。
- ANILA 儲存助手回覆經 `conversation_service.append_message`、`zh_normalize_service.prepare_message_content`，當正規化設定啟用時，實際呼叫 `normalize_report`、`normalize_zh_tw` 與 `OpenCC('s2twp').convert`。此設定程式預設開啟，部署當下狀態另核對，不能以關閉設定當成套件已從映像移除。
- Studio 固定文件排版本身直接走 `quick_document_render → report_renderer`，沒有 OpenCC 轉換呼叫；但既有 `app.main → api.reports → report_runner → studio_text_normalizer` 的共用啟動匯入仍帶入 OpenCC。

OpenCC Python 的[官方套件定義](https://github.com/yichen0831/opencc-python/blob/master/setup.py)列版本 0.1.7、開發者與 Apache 授權；[官方 NOTICE](https://github.com/yichen0831/opencc-python/blob/master/NOTICE.txt)明列使用 BYVoid/OpenCC 詞典及設定，不能只審 Python 包裝而漏掉資料來源。這些第一手資料可核對專案與授權自述，尚不足以認定國別／控制關係或通過院內來源審查。

限定本機快製、CSP、worker、core 與 Studio 程式搜尋未發現 jieba／Paddle 的 import 或呼叫。中文關鍵字採標準函式庫的字元組合；院內遠端 Docling 所使用的 OCR 引擎仍需另附 SBOM，不能由本機沒有這些 import 推定遠端也沒有。

不新增 Python 套件的替代方案仍須設計與品質驗證：可評估解除文字子套件的非必要啟動匯入、保留原文並使用院方自行核定的固定詞彙對照。這不等價於完整簡繁轉換，且只改 import 不會移除映像裡的套件及詞典。ICU／uconv 也僅屬候選：本機沒有 uconv CLI，只有 libicu70；引入 CLI 或新的系統執行依賴前，須先核定來源、授權、離線封裝及與 s2twp 的台灣用語差異，不能直接宣稱可替代或已符合 NF-11。

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
