# 快製助手隔離 HTTP 負載工具

此工具使用真實 TCP／Uvicorn、完整 CSP 路由及既有 middleware，以真密碼登入取得 RS256 JWT，再走授權清單、聊天與對話 API。它與 `quick_builder_load.py` 的直接服務層量測分開保存。

從 `/tmp` 使用既有 Python 執行，先小樣本，再固定故障，最後才跑 100 人／300 助手：

```bash
cd /tmp
python3 -I -S /absolute/repository/tools/loadtest/quick_builder_http_load.py --profile smoke
python3 -I -S /absolute/repository/tools/loadtest/quick_builder_http_load.py --profile smoke --inject-failure
python3 -I -S /absolute/repository/tools/loadtest/quick_builder_http_load.py --profile nf08 --assistants 300 --concurrency 100
```

每次建立新的 `/tmp/quick-builder-http-load-*/report.json`。可指定全新、直接位於 `/tmp`、名稱以 `quick-builder-http-load-` 開始的 `--output-dir`；拒絕既有目錄及 symlink。故障樣本預期 exit 1；一般樣本只有完整負載及持久化驗證通過才 exit 0。正式證據宜在固定 Git SHA、沒有其他測試程序競爭資源時重跑，報告會列工具與共用 helper 的 SHA256。

程式沒有 URL、host、port、DSN、帳號、密碼或 token 參數。父行程只能啟動一個固定 worker，worker 自己只綁一次 `127.0.0.1:0`，取得 OS 分配的埠後，HTTP client 只能連該精確端點。所有其他 loopback／外部／Unix socket 服務連線、DNS 查詢及子程序均被 audit 阻擋。SQLite audit 只准本次新 DB；既有 psycopg／psycopg2／asyncpg／gRPC 原生服務入口也在匯入產品前封鎖，避免 C transport 繞過 Python socket audit。本機執行沙箱若禁止建立 AF_INET socket，需要有授權的執行器允許這次固定工具的本機 TCP；不能改目標為既有或正式服務。

使用者全部是合成、同一單位、一般 user 角色；另有不登入的合成助手擁有者。每人獨立隨機記憶體密碼，沿原始 bcrypt cost 12 雜湊與驗證，透過真 `/api/auth/login` 登入。登入依序完成後，才開始 100 人同時使用的聊天負載；不宣稱 100 人同時登入容量。密碼、JWT、RSA 簽章私鑰只在記憶體，沒有 PEM／key 檔或登入繞過。RSA 僅替換 keyring 的儲存 provider，真簽章、驗章、token type／版本與使用者授權仍執行。測試 DB 只有暫存的密碼雜湊，結束即移除。子行程環境是固定白名單，絕不帶入父行程憑證／URL／代理設定；`-I -S` 與 preimport audit 在產品及第三方套件匯入前生效，不執行 site／`.pth`。

每位使用者有模型授權及三個助手的 Agent grants。每個人會驗證真 `/v1/agents` 只列自己的三個助手、其他助手拒絕呼叫，並驗證無登入與錯密碼均被拒絕。負載固定為 300 個預先建立的合成已發布 input → model → output 助手，每個執行一次；150 個串流、150 個非串流。每次透過真對話 API 建立對話與使用者／預留回覆列，再呼叫 `/v1/chat/completions`，收妥回答後透過真 PUT 寫入回覆並 GET 重載核對；這沿用前端持久化順序，沒有宣稱 completions 自動寫入聊天紀錄。另驗證其他使用者無法讀取對話。

模型替身只放在 `proxy.service.proxy_request`／`proxy_stream` 傳輸邊界，回傳固定合成 OpenAI JSON 或合法 SSE delta／finish／DONE。原 `internal_llm.complete_chat`、`quick_model_stream.stream_chat`、quota 預檢、模型 snapshot、串流 parser、reasoning redaction、terminal 檢查與快製執行授權照常執行。quota 表是空的，未量測有限額度／Redis 重建；被替換代理內部的 admission、SSRF／DNS、模型憑證傳遞與用量結算也未量測。不能將結果稱為完整模型代理或正式模型容量。

模型使用 20 秒有界 barrier，每輪必須真的有 100 個模型等待才放行，三輪各 100 助手。報告列真 HTTP 與模型等待峰值、登入與 setup 獨立時間／CPU、工作流程與聊天延遲、SSE 首個 content delta 的 TTFT、錯誤率、每人完成數與 Jain 指標，以及 DB 中不同使用者／助手／QuickRun／對話／訊息數。100 同時等待不等於 100 個 CPU 平行模型；Jain 只描述本合成排程。

固定資源上限：CPU 90 秒、AS 1.5 GiB、RSS 停機門檻 900 MiB、wall 120 秒、單檔 64 MiB、最多 100 人／1,000 助手；拒絕超上限參數。HTTP request／response／header 上限分別 64 KiB／256 KiB／32 KiB，拒絕 redirect、壓縮與不明長度回覆。512 個 FD 是為同時 client／server sockets 保留；thread stack 512 KiB、glibc arena 最多 2，避免測試用 thread 虛擬位址無界增長。

錯誤率分母是 completed＋errors，取消不列分母，超過 1% 立即停止新工作、取消 client、abort barrier、關閉 server／listener；父行程最多給 5 秒清理，再 terminate，3 秒仍未退出則 kill 並 wait 收屍。RSS／wall 超限立即終止。報告記 child_reaped、temporary_database_removed、listener_closed、server_task_done 及終局剩餘 running runs。錯誤只記固定 code／type 及程式檔名行號，不輸出例外文字、HTTP body、帳密、JWT、環境或 traceback；子行程 stdout／stderr 丟棄。

一個額外有界 observer thread 監看 event loop；停頓超過 2 秒時，最多保存 3 份 `stall-stacks.json` 位置診斷，每份最多 48 個 thread／32 個 frame，僅含程式檔 basename、函式名稱與行號，不含 source、locals、例外內容或 request 資料。正常結束會停止 observer。若 worker 被強制回收，`report.json` 只保留最後固定 progress counters／錯誤 stage，無法證實的 listener／run 終局不會宣稱成功。

工具專用 lifespan 只排除正式 migrations、帳號／永久憑證 provisioning 及外部背景服務，沒有更改正式 app 安全設定。未測 TLS／nginx、瀏覽器 cookie／CSRF 流程、正式 PostgreSQL／GPU、混合角色／跨單位、RAG／MCP／文件轉檔，也未經送審／發布 API 建立助手。單子行程 RLIMIT／watchdog 是主機保護，並非 VM／容器隔離或資源保留；三輪短負載不是持續容量認證。

工具回歸（既有受保護檔案 guard，明列 async plugin）：

```bash
cd /tmp
python3 -I -S /absolute/evidence/scripts/quick_builder_guarded_pytest.py --service csp -- -p pytest_asyncio.plugin /absolute/repository/services/csp/tests/test_quick_builder_http_load_tool.py -q --tb=short --disable-warnings
```
