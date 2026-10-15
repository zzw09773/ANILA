# 助手快製：維運手冊

助手快製使用既有 CSP、治理前端、Agent、文件處理與稽核設施。維運入口是治理中心的「助手快製」`/quick-builder`；一般同仁從 ANILA 使用助手。本手冊補充快製平台的設定與還原核對，安裝程序沿用[第一次安裝](../deploy/INSTALL.md)、[更新與回復](../deploy/UPDATE.md)及[備份還原](../runbooks/csp-db-backup-restore.md)。

本文件提供操作與驗收程序，未附本功能的映像 tar、離線安裝、正式還原或弱掃通過證明。出貨包與執行紀錄須依[驗收表](quick-builder-acceptance.md)逐項提供。

## 啟用前核對

1. 確認出貨版本包含快製平台程式與對應資料庫遷移，記下版本、提交識別、遷移 head、映像清單及 SHA256。不能只更新前端而沿用不相容的後端。
2. 沿用既有憑證卡登入、單位、單位主管及平台管理員。測試未登入、已停用使用者與跨單位帳號不可存取未公開助手。
3. 在既有「模型」及「模型角色」管理模型、嵌入角色與授權；所有快製模型呼叫只走平台 proxy。不要在助手、MCP 或文件處理設定另一組模型供應商連線。
4. 在「信任主機」登錄院內必要服務。「外部服務」的 Docling 位址是院內解析服務，不能設定院外位址。未設定或故障時的原生解析與離線 fallback 能力見下節。
5. 由平台管理員從「助手查詢工具」`/quick-builder/mcp` 登錄 MCP 服務並核准唯讀工具與參數快照。第一期只允許查詢，不允許任意 URL、資料庫直連、執行程式碼、寫入工具或重導向。資料庫負責人的範本與驗證方式見[MCP 登錄與開發](quick-builder-mcp.md)。
6. MCP 每次使用真實提問者的短效 JWT；驗證簽發者、對象、有效期限及工具範圍。MCP 自行驗權限並限制資料範圍，不能以建立者、系統使用者或長效共用憑證代查。
7. 核對兩階段發布：主管核准指定不可變版本後才能註冊 Agent；Agent 保持待平台核准，新版獲平台管理員核准前舊版持續服務。建立者不得自審。

## 文件集採管、停用與 Agent 管理

管理員在 `/quick-builder` 展開「管理文件集開放範圍」，逐一確認文件集所屬單位、內容與密等，選擇「本單位」「指定單位」或「全院」，按「儲存文件開放範圍」並確認。這是獨立的文件採管核准；儲存或公開助手都不改變文件集 ACL。未採管來源不可送審或執行，來源每次執行仍會重新檢查。

對應管理 API 為 `PUT /api/quick-builder/collections/{id}/publication`，內容是 `{"scope":"unit","target_department_ids":[]}`；`scope` 可為 `unit`、`public` 或 `departments`，只有 `departments` 填有效的指定單位編號。`unit` 精確限定文件集所屬同一單位；跨單位／全院公開由管理員另核准並受密等限制。`department_id=None` 的個人文件集不可直接採管公開供助手，須先建立有單位歸屬的來源。

已採管庫的寫入限管理員，或仍在精確所屬單位的文件集建立者，不沿用下層單位範圍擴張寫入。公開來源撤回後，非管理員的自訂範本清單應隱藏引用該來源的範本；已儲存助手每次執行仍須重新驗來源。

停用時保存 `disabled_revision`，並停用相關 Agent。恢復必須先另存晚於停用記錄的新版本，再重新主管審核、註冊及平台管理員核准；舊的已核准或待核准候選不可直接重核復活。快製 Agent 的一般修改／刪除入口由管理護欄拒絕；內容變更走新版本，下架走快製「停用助手」，不能手動修改 Agent 或資料庫指標繞過流程。

## Studio 文件產出設定與安全邊界

「文件草稿」設定契約是 `{template, template_id:null, mode:'text'|'studio', title, preset, format:'rtf'|'html'|'pdf'|'docx'}`。`text` 模式只接受 RTF；`studio` 模式只接受 HTML、PDF、DOCX，沿用四種固定 ReportPreset：`deep_tech_review`（深度技術綜述）、`key_summary`（重點摘要）、`teaching_handout`（教學講義）、`external_comms`（對外溝通文件）。不提供任意範本 ID、模板程式或上傳外掛。

Studio 此路徑僅 render 前一步文字，不再呼叫 LLM 或 RAG。管理員須確認既有 `RegisteredService.slug='studio'` 已啟用、實際 caller 通過 `can_access_service`、網址屬院內且符合既有 trusted URL 規則，以及服務分類上限適用。呼叫使用既有 keyring 簽發的專屬 120 秒 JWT：`type='studio_quick_document'`、`aud='studio-quick-document'`，沿用 Artifact 匯出 gate。此權杖不能用於通用 CSP 或 Studio 登入；不能以系統帳號或助手建立者替代 caller，也不新增另一組模型連線。

服務入口可為直接院內 HTTP／HTTPS origin，或既有同源 `/studio/...` 路徑；完整網址若含 UI 路徑，文件 API 仍以其 origin 為準。同源路徑只由伺服器既有 `SITE_URL` 解析平台 origin，不採用瀏覽器 Host、畫布欄位或外部回覆中的網址。`SITE_URL` 須為完整 origin；預設 localhost 不會繞過院內安全檢查。解析結果仍適用既有信任主機、全體 DNS 回答限 RFC1918／ULA、HTTPS／明確 HTTP 放行政策；不接受帳密、查詢參數、片段、重導向或含糊路徑。

請求固定送往該 origin 的 `/api/quick-documents/render`，兩個平台 nginx 入口的 `quick-documents` API prefix 均轉給既有 Studio backend，不把 `/studio/...` 當成 API 前綴。直接 origin／同源入口與拒絕案例已有離線 mock HTTP 及路由設定檢核；這不代表已部署 nginx 或驗證正式院內 ingress 連線。

QuickRun 的 `execution_context` 是私有執行脈絡，不回傳一般結果或逐步紀錄。產檔後再查有效密等；若升密後不允許匯出，清除產物。每次下載仍重新核對原 task／conversation 的存取與有效密等，刪除、換所有人或升密後不能因持有舊下載網址而放行。

失敗／取消的執行會將本 run 的 Artifact 標為 failed、移除版本連結並保留稽核；最後送出 render 請求前由 callback 重新驗證當下身分、有效密等與服務 ACL。合成 Task／Conversation 升密、Agent 撤權、失敗／取消清理及來源快照升密下載的獨立反例已通過；正式院內服務仍須實接驗證，轉檔成功不能取代授權驗收。

## 文件處理與 Docling 故障

正式共用入口是 `packages/anila-core/src/anila_core/ingestion/parser_registry.py` 的 `ParserRegistry.parse`。Docling 故障後的獨立離線處理使用原生解析器與本機工具，無須另連模型服務；其前提是部署映像內確實備妥經來源審查的程式、系統工具與語料。

| 類型 | 處理路徑 | 限制與故障處置 |
|---|---|---|
| TXT、MD | 既有原生文字解析 | 優先提供 UTF-8；Big5 等舊編碼或不符合可讀性檢查的檔案須另存後重試 |
| 原生 PDF、DOCX | 既有原生解析器 | PDF 核對實體頁碼與表格順序；DOCX 保留標題／表格，不推測 Word 實體頁碼。加密、毀損、空內容須有逐檔失敗原因 |
| DOC、RTF、PPT | `ingestion/office_conversion.py`；本機 LibreOffice 將 DOC／RTF 轉 DOCX、PPT 轉 PPTX，再解析 | 使用獨立暫存設定及 60 秒轉檔上限。缺工具回 `office_converter_missing`；逾時／轉檔失敗回具體類型。轉檔不保證原頁碼或複雜版面完全一致，須抽查原檔 |
| PPTX | `ingestion/presentation.py` | 原生擷取投影片、表格與圖片；需核對投影片順序、群組物件及來源編號，不能視同完整視覺版面還原 |
| 掃描 PDF、圖片 | `ingestion/local_ocr.py`；本機 Tesseract `chi_tra+eng`；圖片介接另見 `ingestion/image_ocr.py` | 獨立於 Docling 的本機 OCR。工具或語料缺失就明確失敗，不把空文字當成功；OCR 只保留文字與來源頁碼，複雜版面與表格須核對原檔 |
| Python、C、C++ | `ingestion/source_code.py` 及 `ingestion/chunking_plugins/structure.py` | Python 使用 AST 保留函式、方法及 decorator 範圍；C／C++ 以詞法處理建構子初始化、運算子、樣板及 lambda 等邊界。巨集、無法判定語法或掃描超限採保守整檔與明確原因；不是完整 C++ AST 或編譯結果 |

表中 `ingestion/` 路徑均相對 `packages/anila-core/src/anila_core/`。文件處理成功後，還須核對檢索、引用與權限，不能只以解析器單元測試判定 DS-01–09 完成。

函式、表格與投影片維持完整結構；超過策略估算預算時有品質提示，超過 16 KiB UTF-8 硬限則明確拒絕，模型實際輸入限制須另行核對。遠端 Docling 若只回整篇 Markdown 與 `page_count`、沒有逐頁標記，無法可靠還原每段實體頁碼，應在來源品質標記與引用核對中呈現限制。

本機 OCR 需要 `tesseract` 可執行檔及 `chi_tra`、`eng` 語料；本機舊 Office 轉檔需要 `libreoffice` 或 `soffice`。可在目標執行環境只讀檢查：

```bash
command -v libreoffice
libreoffice --version
command -v tesseract
tesseract --version
tesseract --list-langs
```

缺少其中一項時，依來源審核與離線出貨程序補齊，或恢復院內 Docling 後重試。開發主機存在工具不代表 ingestion-worker 映像已有工具；主機的版本也不能當成映像 SBOM 的版本。繁體中文 OCR 的真實語料品質、舊 Office 的實際轉檔與完整 fallback 串接都須另附測試紀錄。

本機 PDF OCR 程式目前設定最多 500 頁、整份 180 秒及單頁辨識最多 45 秒；這些是程式防護上限，並非 NF-10 的承諾容量或效能結果。大文件先分檔，記錄錯誤類型再評估資源。OCR 使用 CPU、記憶體及暫存空間；不部署本機語言模型或嵌入模型，正式資源建議須以試辦語料量測。

## 離線工具預檢

1. 以平台管理員身分開啟「助手快製」，展開「離線文件工具檢查」。一般建立者與單位主管沒有此診斷權限。
2. 按「檢查離線工具」。檢查期間可繼續編輯草稿；結果不會變更助手或文件權限。
3. 分別核對解析套件、LibreOffice，以及 Tesseract 與 `chi_tra`／`eng` 語料。缺少執行檔與缺少語料是不同原因；逾時或回覆異常也會保留錯誤類型。
4. 在「文件格式預檢」查看各格式的必要工具。模組資料存在或工具能回覆預檢，仍須用代表文件確認解析品質。

`GET /api/quick-builder/readiness` 僅接受平台管理員身分，不接受路徑、指令、網址或其他參數。工具探測每項最多 2 秒、輸出最多 16 KiB，同一 CSP 程序共享 5 秒快取。結果只描述回應請求的 CSP 程序；背景 ingestion worker、遠端 Docling 與正式映像須各自核對，不能由此推定已就緒。此功能不安裝工具、不下載語料，也不調整系統權限。

函式、表格、投影片或程式區塊的單一完整結構超過 16 KiB UTF-8 時，會以 `E_CHUNK_TOO_LARGE` 停止處理，不截斷內容。請拆小單一結構或分檔上傳。策略估算預算被超過但仍在硬限內時，片段保留品質提示；16 KiB 是平台保護，不能視為嵌入模型 token 容量的保證。C/C++ 以靜態詞法辨識；巨集、無法確認的語法與掃描上限會標示保守品質及原因，需核對原檔行號。

## 安裝與更新

本節指令摘自既有正式流程，供維護人員在排定的部署作業使用；此文件沒有執行這些指令。快製平台沒有另外一套 Docker 部署或新增獨立服務的安裝步驟。

1. 出貨端在乾淨檢出以 `bash scripts/release/build-release.sh` 建立平台出貨包。記下提交、版本及壓縮檔 SHA256，交付每張映像的 tar、清單與校驗值；只在有授權的出貨環境執行建置。
2. 內網主機依[第一次安裝](../deploy/INSTALL.md)核對 SHA256、建立 `/opt/anila`、解開包並執行預檢。內網只載入出貨映像，不連網拉套件或映像。
3. 既有站更新前貼生效公告，確認備份與回復空間，再用既有更新入口：

   ```bash
   sudo bash /opt/anila/anila-update.sh <新包.tar.gz>
   ```

4. 更新器依既有安裝記錄與鎖執行，停寫入、備份、載入已核對映像、跑遷移及內部健康檢查後才開入口。不要從原始碼工作樹操作正式 compose 專案，也不要以 `restart` 取代需要重建的更新。
5. 核對 CSP `/health` 的 JSON、登入頁與 `/quick-builder`。`/api/health` 不是 CSP 健康端點；不要把任意路徑回 `200 text/html` 當作 API 正常。
6. 以測試單位確認版本清單、待審版本、已發布助手與文件仍在，再完成下方更新後核對。健康檢查成功不代表功能或資料驗收通過。

HTTPS 網址應保留實際埠號；使用 8443 時，不可省略埠號而誤連到另一個服務。部署位置與 port forwarding 沿用院內已核准方式，需驗證代理後的登入、串流及服務位址設定；不得將測試機 IP 寫死在助手。

## 備份、回復與還原

日常備份沿用 compose 的 `backup` 服務與[既有還原手冊](../runbooks/csp-db-backup-restore.md)：資料庫 dump 加上原檔、附件與其他持久資料。資料庫應涵蓋助手、不可變版本、審核、執行紀錄、文件採管範圍、`disabled_revision` 及稽核；原檔與解析結果的實際 volume／路徑亦須納入核對。不能只備份流程 JSON。

更新失敗或需退版時，依既有[手動回復](../deploy/UPDATE.md#手動回復)流程：

```bash
sudo bash /opt/anila/anila-update.sh rollback
```

回復會先說明備份後新增資料的損失範圍並要求輸入目標版本。資料庫 schema 回復與資料還原應一起演練，不可只換映像。日常 `restore-all.sh` 的檔案覆蓋行為與更新器的快照交換不同；依既有手冊在隔離還原環境確認，不對仍在服務的正式庫直接灌入 dump。

`r1_0076` schema downgrade 會停用本功能相關 Agent，保留 Agent 本身與其他平台資料，並移除快製專用資料表；它不是保留快製版本歷史的應用程式回復。正式退版前必須保存相容的完整備份；重新升級 schema 不等於還原被移除資料。重升級後註冊同名快製 Agent 時以尾碼避開既有名稱，不修改其他 Agent；此行為須列入遷移與回復演練。

密封憑證與簽章金鑰的還原另依既有受控備份程序處理，使用備份時相容的密封金鑰。不要將金鑰值加入助手、操作紀錄、文件、設定匯出或問題回報。

還原及更新後由維護人員逐項核對：

1. 登入、單位與主管權限正確，未公開助手仍不可跨單位查得。
2. 抽查助手數、版本數、版本內容雜湊、待審版本及已發布版本指標。
3. 抽查原檔可下載、舊文件可檢索、來源位置正確；工具核准快照及來源狀態一致。
4. 由一般同仁在 ANILA 使用原已發布版本；尚待新版核准時仍為舊版。
5. 驗證撤權、停用、過期 JWT 及取消 MCP 核准仍被拒絕。
6. 核對稽核與庫外鏈頭、備份狀態及既有 `operations.log`；保留本次還原紀錄。

## 異常排除與例行維護

| 現象 | 核對項目 |
|---|---|
| 清單空白／沒有工具 | 登入與單位、來源權限、工具登錄及核准；跨單位看不到未公開資源是預期行為 |
| 模型不可用／沒有正文 | 模型健康、金鑰、授權、分類上限與額度；在既有模型管理處修正，不改成直連 |
| 文件入庫失敗 | 逐檔錯誤類型、Docling 健康、本機轉檔／OCR 工具與語料、加密／損毀／大小限制 |
| 工具核准後仍不能執行 | 真實 caller JWT、MCP audience、工具 fingerprint、唯讀標示、信任主機及單位權限 |
| 新版已送審但內容未切換 | 核對主管核准、註冊與平台 Agent 核准三個狀態，勿手動改發布指標 |
| 來源曾經可用，現在被拒絕 | 每次執行重檢來源；撤權或停用後不能沿用舊審核結果 |
| 文件可上傳但不能送審／執行 | 確認有單位歸屬及管理員的獨立採管記錄，不能靠公開助手擴張文件 ACL |
| 停用後仍可執行 | 立即查核 Agent 停用與 published version 判定，保留請求識別與時間，列為阻擋上架缺陷 |
| Agent 修改／刪除被拒 | 快製 Agent 管理護欄；回到快製另存新版本或停用 |

## 輸出能力的驗收限制

FR-06 的完整模組與固定 Studio 範本已有工程實作；四固定版型乘 HTML／PDF／DOCX 共 12 組離線轉檔成功。工程樣本的 PDF 均為 1 頁並以 `pdftotext` 核對繁中內容，DOCX 以 ZIP／XML 核對內容及無院外圖片引用。這是本機實際 renderer 證據，不代表正式院內串接或任意文件版面均通過。

1440／1280 真瀏覽器已驗 Studio 設定儲存、缺服務時 `DOCUMENT_RENDERER_NOT_CONFIGURED`，以及切回 RTF 下載 200／跨單位 404；未將此證據認定為瀏覽器實際 Studio DOCX 下載通過。SSE 仍為模型步驟完成後分段送結果，尚非即時 token stream，NF-09 屬部分符合。正式映像尚未製作，離線交付仍須以實際出貨物驗證。

管理員定期查詢與匯出既有稽核，涵蓋建立、修改、送審、核准、註冊、發布、執行與停用。保存期限依院內規定核定，備份保留份數不能代替稽核保存政策。離線修補包仍需完整 SBOM、漏洞判定、回歸測試與回復方案；保固期及修補時效尚待院方確認。
