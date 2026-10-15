# 快製助手隔離負載工具

只測本機 `quick_execution.run` 服務層，使用真實版本、Agent 授權、稽核及 QuickRun 持久化；模型固定為非網路合成回呼。這不是完整 HTTP／SSE、登入、模型代理額度或 GPU 壓測。

工作負載固定為 input → model → output；不測 RAG、MCP 工具、代理規劃及文件轉檔的容量，也不經送審／發布 API 建立助手。報告中的 300 助手是預先建立的合成已發布版本，每個都會實際執行。

先從 `/tmp` 執行小樣本，再跑 NF08：

```bash
cd /tmp
python3 -I -S /absolute/repository/tools/loadtest/quick_builder_load.py --profile smoke
python3 -I -S /absolute/repository/tools/loadtest/quick_builder_load.py --profile smoke --inject-failure
python3 -I -S /absolute/repository/tools/loadtest/quick_builder_load.py --profile nf08 --assistants 300 --concurrency 100
```

程式先安裝檔案／網路／程序稽核閘，再載入既有套件。沒有 URL、資料庫連線或登入帳密參數，不啟動服務、不讀 dotenv／金鑰、不使用 Docker。測試 DB 是每次新建的私有 `/tmp` SQLite，執行完刪除；合成使用者沒有可登入的密碼。

父行程僅可啟動一個固定的 Python 子行程；子行程不可連網或另開程序，環境只傳固定白名單。限制為 CPU 90 秒、位址空間 1.5 GiB、RSS 停機門檻 900 MiB、牆鐘 120 秒、單檔 64 MiB。錯誤率以「錯誤／（完成＋錯誤）」計算，取消不列分母，超過 1% 時立即取消剩餘工作（第一筆就錯會停止）。父行程先終止，3 秒未退出則 kill 並 wait 收屍，中斷監控也會回收子行程。所有門檻及停止原因會寫進新建的 `/tmp/quick-builder-load-*/report.json`。`--inject-failure` 僅能搭配 smoke，固定注入一次合成模型失敗，預期 exit 1 並產失敗報告；不接受自訂錯誤內容。錯誤只記型別，不保留例外文字、環境或 traceback，`worker.log` 僅含固定結構狀態。

每位工作者是不同的合成使用者，同單位、一般 user 角色，各有模型授權及三個助手的 Agent 授權。每輪有一個合成模型等待，20 秒有界 barrier 證明實際同時等待數；300 助手／100 使用者各執行三次。報告列請求與模型等待峰值、CPU／RSS、p50／p95／p99、錯誤率、300 個不同助手與 100 個使用者的真實 QuickRun 持久化驗證，以及每位使用者完成數與 Jain 指標。此公平性只描述本合成排程，不代表正式平台代理公平性；100 同時等待也不代表 100 個 CPU 平行模型，也未測混合角色或跨單位容量。

資源限制是單一子行程的主機保護，不是虛擬機／容器隔離或主機資源保留。報告不得用於推論正式資料庫、正式模型或完整 API 的容量。
