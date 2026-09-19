# CSP 調整清單與派工包（2026-09-19）

> 基準：`main @ 9ef47d7a`；工作樹另有 4 個未提交檔（agents registration 文案線），屬現況。
> 來源：6 個唯讀稽核員（Grok×4 / Qwen×2）分面掃描，P0 六條由指揮官逐條複核。
> 擁有者裁決（2026-09-19）：①audit 高風險 fail-closed ②fleet token 先誠實註記、重寫部署腳本時再拆 ③知識庫升密只在 UI 講明 ④SMTP 先標示、真連測試另約 ⑤單位管理員維持小範圍（未來部門改走 Oracle 員編）。
> 規則：唯讀基準不動；一檔一寫者；不 commit；驗收看 file:line 與可跑的測試。

## 裁決後仍要動的包

### A · 模型 create 契約（指揮官）
- 症狀：UI 有 `router_enabled`／`thinking_user_selectable` 勾選（`apps/csp-governance-ui/src/views/ModelsView.vue:438,533`），`ModelCreate` 缺這兩欄（`services/csp/app/schemas/model_registry.py:115`），pydantic 靜默忽略 → 新建永遠 `router_enabled=false`，對話模型選單不收（政策 `services/csp/app/services/router_model_policy.py:70`）。
- 已實測：`ModelCreate(..., router_enabled=True).model_dump()` 不含該鍵。
- 同路徑漏洞：trust-retry（`ModelsView.vue:1303`）只 `modelsStore.create`，不寫 router grants；正常路徑會寫（`:1251`）。
- 修法：兩欄進 `ModelCreate`（`extra="forbid"`）；create 成功後用**回應值**決定 grants；抽一支共用的收尾函式，正常與 retry 同走。
- 附：`infra/compose/platform.yml:101-103` 註解改為「http 由 `ANILA_ALLOW_HTTP_ENDPOINT` 放行，`ANILA_ENV` 現無程式效果」。
- 驗收：schema 欄位斷言測試；`npm run build`；`npm test`。

### D · 測試收集修復（M2）
- `services/csp/tests/test_proxy_task_wiring.py:1`：docstring 之後才 `from __future__` → SyntaxError，整檔不收集（`/v1/chat/completions` task_id 契約）。
- `services/csp/tests/test_conversation_compact.py:13`：dev host 無 alembic → collect ERROR（requirements.txt:17 有釘，host 沒裝）。
- `services/csp/tests/test_triton_grpc_wire.py:745`：import 期開 socket，sandbox `PermissionError` → ERROR（連帶 tensor_contract）。
- 驗收：`python -m pytest --collect-only -q` 0 errors；這 4 檔真的跑（不是變 skip）。

### E · audit fail-closed（M3）
- `services/csp/app/services/audit_service.py:87` fail-soft，caller 丟棄回傳值；核准／停用／金鑰可在無紀錄下 200。
- 修法：高風險 mutation 清單（核准、停用、reactivate、發撤 sk-*、service grant）audit 回 None → 500 + rollback；低風險維持 fail-soft。
- 檔：`audit_service.py`、`api/users.py`（:590,:638,:690）、`api/api_keys.py`、`api/service_access_grants.py`。
- 驗收：一條「monkeypatch 讓 audit 失敗 → 端點 500 且列未變」測試。

### F · 知識庫升密講明白（M1）
- 記憶 purge 只在 `resource_type=="conversation"`（`services/csp/app/modules/policy/service.py:413-417`）；`collections.py:524` 註解卻宣稱 side effect 留著。
- 修法：`CollectionDetailView.vue:31`（升密區塊）與成功訊息（`:470`）加「先前從本庫萃取的長期記憶不受影響」；改掉 collections.py 誤導註解。

### G · 大小寫族（M1）
- `services/ingestion-worker/src/ingestion_worker/settings.py:45` 預設 `nvidia/NV-embed-V2`（csp 常數已小寫，`platform_embedding.py:36`）。
- `services/csp/app/services/memory_service.py:376`、`platform_embedding.py:227` 比對大小寫敏感。
- `services/csp/app/models/platform_setting.py:142` 空 current 回 `True`（假 calibrated）。
- 低：`packages/anila_core/src/anila_core/memory/long_term/embedding.py:40` 舊常數；`services/csp/app/models/model_registry.py` name unique 大小寫敏感（需 migration）。

### H · 健康與告警誠實化（M2）
- `DashboardView.vue:31` KPI hint 說「通過健康檢查」，實為 24h 有請求模型數。
- `AppStatusBar.vue:52` 把 CSP `/health`（liveness，`main.py:674`）講成「系統連線正常」。
- `services/csp/app/services/health_checker.py:312`：degraded 不開也不關告警（紅轉黃仍顯示離線）。
- `services/csp/app/config.py:44-53` SMTP 全組無人讀（`alert_notifier.py:39`）→ 依裁決先加標示。

### I · 權限可見性（M3）
- 裁決 A：單位管理員＝人事與用量；UI 收掉不支援入口（`UsersView.vue:132,391`、`departments.py:202`）。
- 註記：未來部門由 Oracle 員編取得，屆時 grant 繼承（`access_control.py:55` 只比同部門）重評。

### J · ingestion 邊角（M1）
- `packages/anila_core/src/anila_core/ingestion/parser_registry.py:1255`：`w:sdt`／`w:ins` 包住的內容靜默丟失 → skipped 計數寫進文件 metadata。
- `services/ingestion-worker/src/ingestion_worker/handlers.py:1385`：relation 抽取失敗只進 log → 併進 job 訊息。

### K · fleet token 註記（M2）
- `_service_principal.py` 檔頭已有事實描述，依裁決補成「具名已接受風險：三服務不可彼此辨識，重寫部署時再拆」。

### L · 雜項（M4）
- cookie `Path=/`（`services/csp/app/middleware/cookies.py:85`，P2.6 medium）：access/csrf 收斂到 CSP 路徑。
- Shell 停送死鍵（`apps/anila-shell/src/runtime/conversations.js:275`）。
- `trusted_host_service.py:78` env 單向 backfill：文件／註解寫明「撤銷要在 UI」。
- `external_auth_service.py:487` OIDC follow_redirects 與 manifest 不一致。
- `services/anila-studio/app/main.py:91` 關 `/docs`。
- `api/ingestion/search.py:966` 影像檢索錯配空回：至少回應可見。

## 待擁有者（不派工）
1. SMTP 真連測試（等 relay）。
2. Fleet token 實際拆分（等重寫部署腳本）。
3. P2.6 cookie medium 是否併發行包。
