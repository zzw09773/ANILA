# Runbook — 從畫面改設定（治理中心「平台設定」）

> 對象：平台管理員（admin 以上）。頁面管理的是 `settings_registry.py` 的 `SETTINGS`：`len(SETTINGS)` 目前是 21，以那個 tuple 為準，不要抄 21 這個數字。存檔後下一個請求就會讀到新值，不需要重啟。

## 在哪裡

治理中心 → 側邊欄 **「平台設定」**（路徑 `/platform-settings`）。讀寫都由
admin 權限保護。

頁面依用途分三段（`settingsView.js` 的 `SECTION_DEFS`：模型與檢索／帳號／對話行為），每一列都會顯示：

- `effective`：目前實際生效的值
- `stored`：資料庫裡管理員最後存下的字串
- `source`：`db`、`env` 或 `default`

解析順序固定是 `platform_settings → env → 程式預設`（`resolve_setting` →
`resolve_setting_without_db`）。DB 那一列壞掉或超出值域時先記警告，再往下一層
（env，再程式預設）。每次消費都重新查詢，不使用開機覆蓋、啟動快照或行程內快取。

## 修改與查證

1. 修改一列並儲存。後端會依該列的型別和值域驗證；不合法的值會以 400
   拒收，不會留下半套資料。
2. 回應與重新整理頁面，確認 `effective`、`stored`、`source` 都符合預期。
3. 直接送出下一個相同功能的請求；不需要 `docker restart` 或
   `docker compose up`。每一列旁的「生效時機」照 `settingsView.js` 的
   `applyWhenLabel` 顯示：Router 三份 prompt 約 30 秒、JWT 輪替與閒置停用看
   下一次排程、其餘 `auth.*` 在下次簽發權杖時、其餘都是下一個請求。

   ⚠ 兩個例外：Router 的 TTL 是「有請求進來、且距上次讀取超過 `ANILA_ROUTER_PROMPTS_TTL`
   （預設 30 秒）」才重讀（`router_server.refresh_router_prompts`），沒有背景計時器，
   零流量時會延到下一則請求；而 `limits.router_round_cap`、`limits.router_model_call_budget`
   兩顆 key 不帶 `router.prompt.` 前綴，`applyWhenLabel` 會標成「下一個請求生效」，
   實際跟三份 prompt 同一個 TTL。

兩顆 token 時效設定有一個刻意的語意：修改只影響之後簽發的新 access token
與 refresh token。已簽發 token 的 `exp` 已嵌入 token，本身不會因設定頁修改而
突然失效，也不會被延長成永不過期。

## 值域與來源

值域與 env 對應只在
[`settings_registry.py`](../../services/csp/app/services/settings_registry.py)
宣告，避免 runbook 另抄一份後漂移。若 `source=env`，表示資料庫沒有可用列；
若資料庫列壞掉或超出值域，解析器會記錄警告並退回下一層。

部署事實、秘密、程式常數、其他服務專用環境變數不在這個頁面。要改那些值，
請依部署文件修改 `.env`、compose 或 `secrets/`，再用對應部署流程 recreate
服務；不要期待平台設定頁能改動它們。

## 常見問題

- `stored` 有值但 `effective` 沒跟著變：先看 `source`；通常是 stored 值無法
  解析或不在宣告值域內，重存合法值即可。
- 新值未出現在下一個請求：確認該功能讀的是 `SETTINGS` 裡的某一顆；Router 的 prompt 與兩顆 `limits.router_*` 要等 router 下一次拉（有請求進來且超過 TTL）。其他服務（ingestion-worker、anila-studio、asr）有自己的設定來源，不讀這張表。
- fresh DB 首頁沒有入口連結是預期行為：請由 `/platform-links` 手動建立。

## 這一頁不管的事

- 不提供 B 類、SEC 類或秘密欄位，也沒有「重啟後生效」或 boot override banner。
- 不做瀏覽器以外的服務重啟；compose／secret 變更仍須依部署文件用 recreate
  套用。
- 不替其他服務的 env 提供預設值；每一個仍被服務讀取的 env 都必須由其部署
  provider 明確提供。
