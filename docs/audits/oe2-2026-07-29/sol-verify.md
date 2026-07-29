結論：10 項 CONFIRMED、9 項 PARTIAL、0 項完全 REFUTED。PARTIAL 都是核心缺口存在，但原主張忽略反證或說得過滿。全程唯讀，未修改檔案、未執行 Git 操作。

1. **CONFIRMED** — `rg -ni 'task|任務|狀態機|快照' SYSTEM-MAP.md` 為零；同義詞僅見「派工」於 105/114/138/150/303/396，未定義持久化 Task／狀態機／快照；[SYSTEM-MAP.md:211](/home/c1147259/桌面/ANILA/anila-restart-20260729/ANILA/SYSTEM-MAP.md:211) 確實寫「一張表加幾個索引。不需要 span 樹、parent 關係、trace id。」

2. **PARTIAL** — `rg -ni '降級|降密|雙人|公文'` 為零，但 [SYSTEM-MAP.md:275](/home/c1147259/桌面/ANILA/anila-restart-20260729/ANILA/SYSTEM-MAP.md:275) 明列「改密等」須稽核，77/370 也有近義詞「來文」；雙人申請/核准流程無依據，但「改密完全無 spec basis」過度。

3. **PARTIAL** — `rg -ni 'manifest' SYSTEM-MAP.md` 為零，但 [SYSTEM-MAP.md:114](/home/c1147259/桌面/ANILA/anila-restart-20260729/ANILA/SYSTEM-MAP.md:114) 要求 Router 讀 agent「自我描述」，135 亦要求 agent 註冊；這可支持部分描述性 manifest，但不能支持 `agents.py:116-171` 的完整 trace／classification 契約。

4. **PARTIAL** — `rg -n '註冊|服務' SYSTEM-MAP.md` 顯示註冊只指 agent/model，唯一精確「服務」在 374；但 [SYSTEM-MAP.md:19](/home/c1147259/桌面/ANILA/anila-restart-20260729/ANILA/SYSTEM-MAP.md:19)-35 已要求三個固定站台與一次登入，提供最低限度導覽/SSO 連結依據；另現行 `RegisteredService` 實為 **35 欄**，不是 33 欄。

5. **PARTIAL** — 除 167 與 §11 外，[SYSTEM-MAP.md:49](/home/c1147259/桌面/ANILA/anila-restart-20260729/ANILA/SYSTEM-MAP.md:49) 也明列「Studio 產出」；不過程式模型計數確為主要 artifact 四表 64 欄，加 `source_snapshots`/`citations` 28 欄，共 6 表 92 欄，規格沒有逐欄授權。

6. **PARTIAL** — 全樹搜尋找不到 `usage_service.py` 讀取 `TokenUsage.task_id`，欄位僅在 [token_usage.py:60](/home/c1147259/桌面/ANILA/anila-restart-20260729/ANILA/services/csp/app/models/token_usage.py:60) 定義及寫入；但 grouping 不只 model/user/department，尚有 agent、base model、client（`usage_service.py:531-548,589-598,670-679`）。

7. **CONFIRMED** — 全樹搜尋 `ServiceLaunch|ServiceAuditCallback` 只找到 model、建構寫入及回傳 echo，沒有 query/read consumer；前端 [services.js:28](/home/c1147259/桌面/ANILA/anila-restart-20260729/ANILA/apps/csp-governance-ui/src/api/services.js:28) 呼叫 GET `audit-callbacks`，後端 [services.py:479](/home/c1147259/桌面/ANILA/anila-restart-20260729/ANILA/services/csp/app/api/services.py:479) 只有 POST。

8. **CONFIRMED** — 五欄只出現在 schema、通用 create/update、API serialization（`api/models.py:127-132`）及 UI 表單/標籤；全樹沒有 routing、filter、capability negotiation 或 ownership 邏輯讀取 `protocol/supports_*/owner_department_id`。

9. **CONFIRMED** — `rg 'sync_version_level'` 只找到 [artifacts/service.py:285](/home/c1147259/桌面/ANILA/anila-restart-20260729/ANILA/services/csp/app/modules/artifacts/service.py:285) 的定義與 export；`expires_at` 僅於 137 寫入及 schema echo，無 `ArtifactJob.expires_at` filter、reaper 或定時清理，未實作 [SYSTEM-MAP.md:167](/home/c1147259/桌面/ANILA/anila-restart-20260729/ANILA/SYSTEM-MAP.md:167) 的三日刪除。

10. **CONFIRMED** — [agent.py:94](/home/c1147259/桌面/ANILA/anila-restart-20260729/ANILA/services/csp/app/models/agent.py:94) 欄位 nullable、migration 無非空 default；register/update request 均無此欄，UI/CLI 即使送出也未被 constructor 持久化，seed 亦未設定，因此現有 agent ceiling 不會非空。

11. **CONFIRMED** — [health_checker.py:73](/home/c1147259/桌面/ANILA/anila-restart-20260729/ANILA/services/csp/app/services/health_checker.py:73) 呼叫 `validate_outbound_url(base_url)` 未傳 `endpoint_kind='agent'`；`url_guard.py:310` 預設 generic，故只開 `ANILA_ALLOW_HTTP_AGENT_ENDPOINT=1` 仍會拒絕 HTTP agent。

12. **PARTIAL** — [policy/service.py:273](/home/c1147259/桌面/ANILA/anila-restart-20260729/ANILA/services/csp/app/modules/policy/service.py:273)-333 的 `apply_classification` 本身只寫 `ClassificationEvent`、不寫 `audit_logs`；但 conversation wrapper 在 `conversation_service.py:327-341` 另呼叫 `log_audit_event`，所以不是所有升密路徑都漏記。

13. **CONFIRMED** — [audit_log.py:7](/home/c1147259/桌面/ANILA/anila-restart-20260729/ANILA/services/csp/app/models/audit_log.py:7)-22 無 hash chain、append-only constraint 或防修改 trigger；`users.py:745-748` 甚至會 UPDATE 既有 `AuditLog.actor_user_id`，與 [SYSTEM-MAP.md:278](/home/c1147259/桌面/ANILA/anila-restart-20260729/ANILA/SYSTEM-MAP.md:278)-280 不符。

14. **PARTIAL** — [artifacts.py:436](/home/c1147259/桌面/ANILA/anila-restart-20260729/ANILA/services/csp/app/api/artifacts.py:436)-453 的 GET 確實未寫 audit；但回傳的是 artifact/version metadata、`storage_ref`、`file_refs` 與 citation map，並非直接串流檔案內容，因此「classified artifact content」說得過滿。

15. **PARTIAL** — [ceiling.py:124](/home/c1147259/桌面/ANILA/anila-restart-20260729/ANILA/services/csp/app/modules/policy/ceiling.py:124)-135 的 task-less allow 確實不寫 `PolicyDecision`；但全域並非如此，例如 `api/services.py:441-456` 與 `api/artifacts.py:374-389` 可在無 task link 時仍寫 allow decision。

16. **CONFIRMED** — [auto_seed.py:253](/home/c1147259/桌面/ANILA/anila-restart-20260729/ANILA/services/csp/app/services/auto_seed.py:253)-388 直接建構/更新 endpoint，未呼叫 `_enforce_endpoint_url` 或 `validate_outbound_url`，並於每次啟動覆寫既有 model/agent endpoint；368-391 預設 agent approved，430-446 自動設定 user `is_approved=True`。

17. **CONFIRMED** — `rg '/v1/models'` 只找到 health probe、部署檢查及各模型服務自身 listing；[api/models.py](/home/c1147259/桌面/ANILA/anila-restart-20260729/ANILA/services/csp/app/api/models.py) 只有 CRUD/health/test/primary routes，沒有抓取 upstream listing 後 bulk upsert 的路徑。

18. **CONFIRMED** — [agent.py:46](/home/c1147259/桌面/ANILA/anila-restart-20260729/ANILA/services/csp/app/models/agent.py:46)-51 是單一 nullable FK，request 亦只有 singular `collection_id`，search scope 直接做單值相等比較；沒有 agent-to-collections 關聯表，違反 [SYSTEM-MAP.md:140](/home/c1147259/桌面/ANILA/anila-restart-20260729/ANILA/SYSTEM-MAP.md:140)。

19. **PARTIAL** — 後端 [message.py:10](/home/c1147259/桌面/ANILA/anila-restart-20260729/ANILA/services/csp/app/models/message.py:10)-43 確實無 parent/branch 欄，編輯會刪除後續訊息；但前端 `app.jsx:1438-1499,1607-1647` 以 client-side `revisions[].tail` 實作分支切換，僅未持久化、重載後消失。

| Claim | Verdict |
|---:|---|
| 1 | CONFIRMED |
| 2 | PARTIAL |
| 3 | PARTIAL |
| 4 | PARTIAL |
| 5 | PARTIAL |
| 6 | PARTIAL |
| 7 | CONFIRMED |
| 8 | CONFIRMED |
| 9 | CONFIRMED |
| 10 | CONFIRMED |
| 11 | CONFIRMED |
| 12 | PARTIAL |
| 13 | CONFIRMED |
| 14 | PARTIAL |
| 15 | PARTIAL |
| 16 | CONFIRMED |
| 17 | CONFIRMED |
| 18 | CONFIRMED |
| 19 | PARTIAL |