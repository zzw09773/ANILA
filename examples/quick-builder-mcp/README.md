# 本人差勤 MCP 開發範本

此範本使用平台既有的 Python 依賴，不包含模型呼叫或對外服務。`server.py` 提供 Streamable HTTP 的 `/mcp`，只開放 `attendance_my_records`。所有查詢都使用驗簽後的本人 `sub`，不接受 `user_id` 或任意 SQL。

1. 由資料庫負責人準備唯讀資料來源，欄位為 `attendance(anila_user_id, work_date, status)`。`anila_user_id` 必須對應平台使用者 ID，不能假設它等於員工編號；若院內來源用員工編號，請建立經資料管理員核對的身分對照 view。
2. 在治理中心登錄 MCP 服務，取得服務 `id`。第一次服務尚未啟動時，登錄會保留待核准狀態及連線失敗訊息。
3. 以取得的 ID 及固定院內 JWKS 位址啟動範本，TLS 由院內既有反向代理處理，程序預設只監聽 loopback。
4. 在治理中心重新擷取工具，檢查唯讀連線與 SQL 後勾選核准。公開性與單位範圍由管理員設定；資料服務仍須自行限制每列資料。

SQLite 範例：

```bash
python server.py --service-id 1 \
  --jwks-url https://csp.local/.well-known/jwks.json \
  --database sqlite --sqlite-path /srv/attendance/attendance.sqlite
```

SQLite 檔案必須已存在。服務以 URI `mode=ro` 開啟，並啟用 `PRAGMA query_only`；作業系統帳號也應只具檔案讀取權。範本不建立或修改資料表。

PostgreSQL 範例：

```bash
python server.py --service-id 1 \
  --jwks-url https://csp.local/.well-known/jwks.json \
  --database postgresql --postgres-service attendance_readonly
```

`attendance_readonly` 是由院內維運管理的 libpq service 名稱；認證沿用院內憑證或受保護的 pgpass，不能放在命令列、平台登錄網址或日誌。依 [`postgresql-readonly.sql`](postgresql-readonly.sql) 的範例授與必要的 SELECT 權限，服務另以 `readonly=True` 開啟交易並設十秒 statement timeout。若資料來源有租戶或特殊密等限制，應以資料庫 view/RLS 及服務端權限額外限縮。

範本每次呼叫向固定的院內 JWKS 位址取公鑰，不接受重導向或公開 IP。院內 DNS、CA 與服務的執行帳號由維運設定。JWT 必須為平台金鑰圈的 RS256，issuer、服務與工具 audience、期限、金鑰狀態及本人 `sub` 全部通過才查詢；前端提供的欄位無法替換 `sub`。`__catalog__` 憑證只供擷取工具，不能查資料。瀏覽器 Origin 請求一律拒絕。

測試方法及管理 API 見 [完整指南](../../docs/guides/quick-builder-mcp.md)。SQLite 測試使用臨時假資料；PostgreSQL 範例須由資料庫負責人在院內測試庫驗證唯讀權限、本人條件、查詢計畫與 timeout。未在真實院內 MCP、PostgreSQL 或正式差勤資料上執行驗收。
