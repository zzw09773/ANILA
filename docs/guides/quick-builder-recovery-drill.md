# 快製助手離線交接與檔案還原演練

新增的 [recovery.py](../../tools/quick-builder-recovery/README.md) 可將管理者顯式清冊中的 Artifact 與 ingestion 檔案製成帶大小／SHA256 的檔案包，再還原到新的空白演練目錄。這補上檔案 bytes 與清冊的比對證據；完整助手、版本、歸屬、密等、ACL、索引與稽核鏈仍依原平台流程驗證。

## 一致性與正式還原邊界

1. 正式備份／還原沿用 [CSP DB 備份還原 runbook](../runbooks/csp-db-backup-restore.md)。需由有權維護人員安排停寫或一致性快照，保留 DB dump、版本、Artifact／ingestion 對應及稽核資料。本工具不連資料庫、不停止服務，也不驗證跨 DB／volume 的同一時間點。
2. 從同一份受控快照準備檔案根目錄與清冊。每筆邏輯 ID 應能對回該快照內的 Artifact version 或 ingestion source，大小／hash 由可信紀錄提供。工具不掃描正式磁碟、不匯出帳號／權限，也不從檔名推定 owner。
3. 以不存在的 `--out` 建包，另存成功回覆的 manifest pin；用獨立受信任管道交接 pin。驗證包 hash 與每個 blob 後，在新的空白 scratch 執行 `rehearse`，保留 `REHEARSAL.json`。所有還原檔都不可執行，也不保留原 owner、ACL、xattr 或可執行位元。
4. 在獲准的隔離環境完成正式 DB／volume 還原後，再以真實使用者驗證助手、版本、文件內容、Artifact 歸屬、撤權／升密與下載限制，以及 ingestion 任務／向量索引的對應。檔案演練成功不能取代這一步；本次離線工具測試沒有執行它。
5. 私鑰、密碼與執行環境另依院內機密備援流程處理。本工具連公開 `.pem` 也拒絕，不可拿來取代原 runbook 的全部 PKI／設定／DB 備份。不得把正式秘密放入清冊、來源包或演練目錄。

任何缺檔、大小／hash 不符、危險檔案型別或可觀測到的讀取期間 metadata 變動都使演練失敗。檔案系統時間精度可能看不到同大小的即時改寫，工具不是一致性快照；來源停寫／快照與可信預期 hash 仍是必要前提。保留失敗證據及不含機密的錯誤 type，重試採新目錄；不自動清理、修補、移動原始檔或覆寫正式 volume。

## 離線修補交接

沿用 [source-only 交付工具](../../tools/quick-builder-release/README.md) 製作原有來源選擇包，依 [SBOM 工具](../../tools/sbom/README.md) 產生宣告／lock 的 repo SBOM。這兩者都不等於正式映像包，也不證明來源核定。新增的 [patch 清冊 schema 與驗證器](../../tools/quick-builder-recovery/README.md#patch來源核定清冊) 以固定欄位記錄待審資訊，不修改原 release.py。

維護流程依序為：

1. 指定完整 base／target commit，記錄逐檔增刪改前後 hash、來源包 hash、回歸結果與回復審查證據。由可信流程核對目前安裝版本與 base 相符；本工具不執行 Git 或套用 patch。
2. 將每個已宣告或新增依賴、二進位、模型、字型與 schema 的精確來源／版本／授權／發布者／作者及控制國別填入來源清冊，附取得資產 hash 與院內核定證據。未知維持 `null/pending`，不得因鎖檔可解析、HTTPS URL 或某國網域就標成合規。新增官方資產仍須先經來源審查；本工具沒有下載任何資產。
3. 每個實際映像另外記錄 archive hash、實際 digest、映像／OS SBOM 及授權核定證據。固定 16 個 slot 不會被 source tar 填補。映像來源仍待提供或核准時，保留 `images_missing`，不得用來源包宣稱 NF-03 完成。
4. 在獲准的測試環境離線載入實際映像，對目標 ingestion-worker digest 保留 parser、OCR／語料、Office、字型、瀏覽器及無外連驗證證據。測試更新前後的備份還原與回復路徑，保留真實 HTTP／權限／資料完整性回歸。這些操作此輪均未執行。
5. 完成院內弱掃、源碼檢測與來源核定，確認高風險修正、修補負責人、保固／修補時效後，再由既有受控流程發布。清冊驗證器不認證證據內容，不授權 patch apply，所有 deployment／identity／approval verified 旗標固定 false。

## Worker readiness 與運算資源待驗表

現有管理 API 的 readiness 只代表呼叫當下 CSP process 的本機 metadata／固定工具探測，不代表 worker、遠端 Docling 或正式容器。目標映像仍需獨立保留以下證據：

| 證據 ID | 目標 worker 應提供的資料 | 目前離線工具能驗證的範圍 |
| --- | --- | --- |
| parser-packages | 固定 image digest 對應的已安裝 parser 與 SBOM；代表格式合成輸入 | 檢查證據 hash 是否填入，無實裝／解析執行 |
| tesseract / chi_tra / eng | 確切執行檔／語料版本、來源授權、hash，繁中與英文真 OCR 輸出 | 分別列缺漏，不以本機安裝或 marker-only 輸出代替 |
| libreoffice / pandoc | 版本、來源授權、hash、轉檔故障與超時／暫存清理結果 | 檢查參照是否填入，不執行安裝或轉檔 |
| fonts / chromium | 字型授權、瀏覽器版本／hash、離線渲染輸出 | 檢查參照是否填入，不下載或啟動瀏覽器 |
| offline-egress-policy | 目標映像與網路政策下，安裝／更新／執行均無院外連線的證據 | 工具自身拒絕網路不代表平台網路驗收 |

NF-07 的文件處理／OCR 資源建議及 NF-10 的處理量仍需以目標 worker 真實語料量測 CPU、記憶體峰值、並行數、單檔大小與耗時，區分純文字 PDF、掃描 PDF、Office／圖片等工作負載。此工具的 64 MiB 單檔／256 MiB 選擇總量是安全上限，不能當作平台效能數據。本次只做小型合成檔案演練，未重跑 100 人負載或使用真模型。

## 需求證據對照

| 需求 | 本次新增的離線工程證據 | 尚未完成的驗收 |
| --- | --- | --- |
| NF-01 | 工具 guard 拒絕 DNS／網路／spawn；清冊與演練均為離線 | 整套平台安裝／更新／執行的目標環境無外連驗收 |
| NF-03 | 16 個映像 slot 與 hash／digest／SBOM／核定缺漏檢查 | 實際映像 tar、映像身分核實及離線載入 |
| NF-05 | Artifact／ingestion 顯式清冊、SHA 校驗、新空目錄還原與失敗證據 | DB／volume 一致性、助手／版本／文件／ACL／稽核鏈真還原 |
| NF-06 | worker 證據與 CSP 本機 readiness 明確分開 | 目標 worker／遠端 Docling／正式映像 readiness |
| NF-07 | OCR／Office 等資產與資源量測項目清單 | 目標 worker CPU／記憶體等實測建議 |
| NF-08 | 來源與回歸報告 hash 可記入 patch 清冊 | 本工具不新增併發效能證明，沿用另行驗證的負載報告 |
| NF-10 | 有界工具上限、超額拒絕合成測試 | 文件總量／單檔上限／處理速度由院方確認及實測 |
| NF-11 | 每項來源／版本／授權／作者與控制國別／核定證據必填，未知列缺漏 | 逐項來源與控制關係核定、完整映像／OS／資產 SBOM |
| NF-14 | 不讀秘密、不執行資料檔、新檔 0600／目錄 0700 | 容器非管理者、實際映像／設定匯出／日誌機密檢測 |
| NF-15 | 工具合成負向測試與有界輸入檢查 | 正式弱掃、源碼檢測與高風險修正驗收 |
| NF-16 | 固定 revision／變更 hash／來源核定／回復審查的可驗證清冊 | 安全公告追蹤、獲准離線修補包及修補時效承諾 |

上述表格刻意保留正式映像、來源核定、弱掃與院內驗證的缺口；合成清冊填齊、檔案 hash 一致或演練成功都不能將這些缺口改成完成。
