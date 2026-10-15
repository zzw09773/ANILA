# 助手快製平台 MCP 登錄與開發

第一期只提供經平台管理員確認的唯讀查詢工具。平台不保存資料庫連線帳密，也不代替各單位判斷資料列的使用權限。差勤服務必須以驗簽後的 `sub` 查詢本人資料。

## 管理流程

以既有平台登入進入治理中心。擁有者與平台管理員可擷取、核准及停用服務；登錄服務位址還需既有的端點位址設定權限，擁有者天然具有此權限，管理員由擁有者另外授與。端點顯示沿用相同謂詞，未獲授權的管理員只會看到 `<owner-only>`。單位管理員沿用既有單位樹授權，能使用其管理範圍內的核准服務，不能核准 MCP。

1. 在治理中心「信任主機」登錄院內服務主機。MCP 另要求主機所有解析結果都是 RFC1918 私有 IPv4 或 IPv6 ULA；公開網路、loopback、metadata、link-local 與重導向均拒絕。HTTP 與私有位址的既有平台安全開關仍適用。
2. 登錄服務名稱、用途、完整 MCP 端點及所屬單位。未公開服務僅供同單位、管理該單位的主管及平台管理員使用；公開服務可供所有已核准且有效的本人帳號使用。`system` 帳號不作提問者。
3. 平台擷取 `tools/list`，保存工具說明、輸入 schema、annotations 與 SHA-256 指紋。若首次服務尚未填入平台指派的編號，登錄仍會保留為待核准，畫面提供連線失敗原因。取得編號後配置服務，按「重新擷取工具」。
4. 管理員檢查資料庫唯讀帳號、固定 SQL、參數綁定及工具原始碼，再明確勾選確認為查詢用途的工具。`readOnlyHint` 僅供參考，不會自動核准工具。
5. 助手送審時保存各工具指紋。呼叫時再次讀取帳號、單位授權、服務及工具核准狀態，並向服務取得當下 schema 比對指紋。工具內容變動、版本指紋不同或撤權均拒絕呼叫。
6. 重新擷取會先取消整個服務的核准；擷取失敗也維持待核准。停用會清除全部工具核准。移除的工具保留歷史快照，不能再通過當下工具清單的核准檢查。

服務端工具的內部實作若改變，管理員必須先停用並重新審查。schema 指紋無法證明遠端實作沒有在相同 schema 下改變行為；唯讀資料庫帳號與服務端權限檢查仍是必要邊界。

## HTTP 契約

所有路由前綴都是 `/api/quick-builder/mcp`，沿用既有登入及 cookie CSRF 保護。

| 方法 | 路徑 | 用途 |
|---|---|---|
| GET | 空字串 | 管理員取得服務清單，含工具快照 |
| POST | 空字串 | 登錄服務並嘗試擷取工具 |
| POST | `/{id}/refresh` | 取消核准並重新擷取工具 |
| POST | `/{id}/approve` | 以 `approved_tool_ids` 明確取代唯讀工具清單 |
| POST | `/{id}/disable` | 停用服務及全部工具 |
| GET | `/tools` | 本人目前可用的核准唯讀工具目錄 |

登錄內容：

```json
{"name":"本人差勤","description":"查詢本人差勤紀錄","endpoint_url":"https://attendance.local/mcp","department_id":12,"is_public":false}
```

核准內容：`{"approved_tool_ids":[21,22]}`。至少選一個工具；清空服務的可用工具請使用停用。所有請求拒絕未定義欄位，不能提供 `user_id` 指定操作者。

服務回傳 `id/name/description/endpoint_url/department_id/is_public/status/registered_by_user_id/approved_by_user_id/approved_at/created_at/updated_at/tools`。狀態為 `pending/approved/disabled`。首次擷取失敗另帶 `discovery_error`，其中包含 `error_type/message`。

每個工具包含 `id/service_id/name/description/input_schema/annotations/schema_fingerprint/fingerprint/approved_fingerprint/is_read_only/is_approved`。目錄另附 `service_name`；`fingerprint` 等同 `schema_fingerprint`。端點只在管理員服務清單提供。

## 執行與信任邊界

後端介面：

```python
list_tools(db, user) -> list[dict]
await call_tool(db, user, tool_id, arguments,
                assistant_id=assistant_id,
                expected_fingerprint=approved_version_fingerprint) -> dict
```

`assistant_id`、工具 ID 與核准版本指紋由後端執行器取得；執行器仍需檢查助手本身與使用者的執行權限。MCP 服務層重新檢查資料來源權限。送審與正式執行均應提供 `expected_fingerprint`。

回覆固定為 `{"tool_id":21,"fingerprint":"…","trust":"untrusted_external_data","data":{…}}`，`data` 只保留 `content/structuredContent/isError`。執行器將整份回覆編碼為工具資料，放入 `tool` 或 `user` 角色；不得將回覆合併為系統訊息、工具定義、身分或流程設定。巢狀欄位即使叫 `role/system/sub/tools` 仍只是資料。平台不解析結果內的 URL，也不下載 resource 或圖片。

輸入參數上限 32 KiB；單次 HTTP 回覆上限 256 KiB；最多 100 個工具。單次 HTTP 逾時 20 秒，完整初始化、工具清單比對及查詢的總時限 30 秒。SSE 支援同次 POST 的回覆事件，只接收對應 request ID 的結果，不執行服務送來的 sampling、elicitation 或其他 request。連線不使用程序代理設定，也不跟隨重導向。

工具 schema 支援明確的基本 `type`、`properties/required/additionalProperties`、`items`、`enum/const`、字串與陣列長度及數值上下限，以及 `title/description/default/examples/$schema` 描述欄。根節點必須是 object，巢狀最多八層。不解析 `$ref`，不接受程式碼、條件、外部 schema 或其他擴充。服務仍須驗證所有輸入。

## 身分憑證

MCP 每次操作取得平台金鑰圈簽發的一分鐘 RS256 JWT，不需要另一組金鑰或平台設定。登錄與擷取使用操作管理員本人，實際資料查詢使用當下提問者本人，從不改成助手建立者或服務帳號。

必要 claims：`iss=anila-csp`、`aud=urn:anila:quick-mcp:{service_id}:{tool_name}`、真實平台使用者 ID 的字串 `sub`、`service_id/tool/assistant_id`、`type=quick_mcp`、`scope=mcp:read`、`iat/nbf/exp/jti`。另含當下 `username/department_id/tv`。`aud` 必須是單一精確字串，不能接受同時授與其他服務的清單。

工具擷取使用保留工具名稱 `__catalog__`、`assistant_id=0`，不可執行資料查詢。資料查詢使用真實工具名稱與正整數助手編號。服務必須驗證簽章、`kid`、issuer、audience、期限及所有必要 claims，不能只解碼 JWT；不能接受普通 access token。平台 JWKS 為 `/.well-known/jwks.json`，只接受 active 以及符合 `anila_iat_not_after` 的 retiring 金鑰，拒絕 next 或 retired。

範本每次驗證重新取得院內 JWKS，連線失敗即拒絕查詢。自訂服務若快取 JWKS，緊急金鑰撤銷可能有最長一分鐘的既有 MCP 權杖殘餘時間；如需即時撤銷，需接上院內撤銷機制。帳號與服務撤權由平台在發出每次呼叫前重新檢查，已送出的查詢無法追回。

## 開發範本與測試

範本位於 [`examples/quick-builder-mcp`](../../examples/quick-builder-mcp/README.md)。它提供固定的 `attendance_my_records` 工具，SQLite 使用 `mode=ro` 加 `PRAGMA query_only`，PostgreSQL 使用資料庫最小權限角色加 readonly transaction。SQL 固定在服務端，`sub` 與月份使用綁定參數。請求不存在可覆蓋本人身分的欄位。

沒有新增套件，重用 CSP 現有 FastAPI、httpx、python-jose、uvicorn、psycopg2；SQLite 使用 Python 標準庫。離線環境使用院內已核對的 wheelhouse，不執行線上安裝或 MCP Inspector 下載。

從暫存工作目錄執行測試，避免讀取工作樹的 `.env`：

```bash
cd /tmp
PYTHONPATH=/path/to/ANILA/services/csp:/path/to/ANILA/packages/anila-core/src \
  python -m pytest /path/to/ANILA/services/csp/tests/test_quick_mcp.py -q
```

測試使用合成 RSA 金鑰與 mock HTTP，實際 SQLite 臨時檔驗證本人隔離；PostgreSQL 測試以 mock 驗證唯讀交易與 SQL 參數，不代表實際院內 PostgreSQL 或各單位 MCP 已經驗收。正式驗收仍需院內測試帳號、MCP 端點、信任主機及唯讀資料庫，由兩位不同單位同仁確認只能取得各自授權資料，並逐一測試停用、撤權、逾時及服務工具變更。

傳輸相容性採 [MCP Streamable HTTP 2025-03-26](https://modelcontextprotocol.io/specification/2025-03-26/basic/transports) 的 JSON/SSE POST 與 [tools/list、tools/call](https://modelcontextprotocol.io/specification/2025-03-26/server/tools)。不接受舊 HTTP+SSE endpoint redirect、stdio、任意新程序或遠端執行環境。
