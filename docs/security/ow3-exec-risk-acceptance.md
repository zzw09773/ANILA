# OW-3 Exec 風險接受說明

> 起草：OW-3 WP-A 實作者。簽署欄留空，由平台擁有者於 P2 掃描前親簽。
> 規格依據：`PLAN.md` OW-3（含三件連帶）、`docs/plans/ow3-message-actions-blueprint.md` §Q3／§8、`SYSTEM-MAP.md` §8。
> 對應程式碼：`services/csp/app/services/message_action_exec.py`（完整 exec 面，單一檔案）。

---

## 1. 決議與範圍

擁有者於 2026-07-29 親身體驗用例後裁定：訊息級自訂按鈕的執行機制採用 **owner 撰寫、同進程 Python（in-process exec）**。本文件即 PLAN:135 要求的書面風險接受說明——沒有它，P2「不得有 OWASP Top 10 高風險項」閘門會擋下此功能。

**接受範圍**

- 僅 **owner** 可建立／更新／刪除 `kind=exec` 動作（`require_owner`）。
- 執行面僅存在於 `message_action_exec.py`；宣告式（`kind=declarative`）不進入此面。

**不接受**

- admin／一般使用者／agent 作者撰寫 exec 動作。
- 以「半沙箱」（subprocess + rlimit + 清 env）包裝後宣稱已隔離——那會誘發錯置信任；升級路徑另見 §9。

---

## 2. 威脅模型

威脅主角是 **特權內部人**（SYSTEM-MAP L280：稽核要防的就是有 admin／owner 權限的人）。

頭條結論：**能撰寫 exec 動作的人 ≡ 能把程式部署到主機的人。**

氣隙內網、單一租戶、一人維運的前提下，owner 本來就能改 compose、掛 volume、進 codeserver 對 docker.sock 操作——exec 表單並未新增「誰能碰到主機」的集合，只是把同一能力開了一條經 DB＋稽核的捷徑。風險是這條捷徑繞過 git、跨家審查與 commit 紀錄。

---

## 3. 能力聲明（逐條，不得美化）

下列文字與 `message_action_exec.py` 模組 docstring／blueprint §Q3 **完整對應**（全文 zh-TW 渲染，非英文 docstring 逐字複製）；美化或省略任何一條即為審查失敗。

Exec 動作在 csp FastAPI 行程內、以容器使用者身分執行，擁有完整標準庫與所有已安裝套件；可讀取 `os.environ`（含 DB URL、`MODEL_GATEWAY_API_KEY`、`CSP_SERVICE_TOKEN`、JWT 金鑰路徑）、讀取檔案含 `secrets/*.pem`（`:ro` 掛載仍可讀）、自行以 app 角色開啟 DB 連線（繞過 API 層範圍控制）、import／變更 `app.*`、發出任意對外網路請求並 **繞過 `url_guard.validate_outbound_url`（SSRF allow-list）**、啟動子行程、對行程做 monkey-patch。**沒有沙箱、沒有 seccomp、沒有獨立直譯器、沒有超出牆鐘逾時與輸出上限以外的資源限制。能撰寫 exec 動作的人，等同於能把程式部署到主機的人。**

反聲明（程式註解與 UI 必須遵守）：

- 逾時 **不能** 終止執行緒（CPython）；逾時 → HTTP 504 + 稽核列，執行緒可能繼續跑。semaphore **限制被放棄的 worker 數量**：卡住的 worker 會永久佔用槽位直到回傳；槽位耗盡後 exec 停用（503）直到 worker 結束或服務重啟——這是刻意的 fail-closed 取捨。
- UI／文件不得寫「沙箱」「已隔離」「已終止」；逾時文案為「逾時，已停止等待（背景可能仍在執行）」。

預設旗標 `ANILA_ENABLE_ACTION_EXEC=False`（fail-closed）。關閉時 exec 動作自 `/visible` 過濾，invoke 回 404（與不存在不可區分）。

---

## 4. 為什麼仍然接受

1. **擁有者驗證過的真實用例**——大量固定化流程（例如第一步載入資料庫數據）用貼一段 Python 最快；部分需求本來就不是 n8n／agent 該做的工程需求。
2. **多數按鈕走宣告式即可**（翻譯／摘要／改寫）——exec 是少數路徑，不是預設。
3. **部署姿態**：氣隙內網、單一租戶、一人維運＝唯一作者；作者集合與「能部署到主機」的集合相同。
4. **連帶控制已入設計**：owner-only 撰寫、完整快照稽核＋匯出、按壓權限另綁（role／部門子樹／使用者）、預設關閉旗標。

---

## 5. 控制措施（可查證，指到程式碼）

| 控制 | 程式碼錨點 | 保證邊界 |
|---|---|---|
| 撰寫僅 owner | `api/message_actions.py` → `require_owner`；`auth_service.require_owner` | admin POST/PUT/DELETE → 403「需要 owner 權限」 |
| 預設關閉 | `config.py` `ANILA_ENABLE_ACTION_EXEC=False`；`list_visible`／`resolve_for_invoke` 過濾 | 旗標關 → 404，無 oracle |
| 單一檔案 exec 面 | `services/message_action_exec.py` 全檔 | 升級／抽離只需換這一個 seam |
| 存檔時靜態驗證、永不執行 | `validate_source()`：`ast.parse` + 頂層 `FunctionDef run` + `compile`；**不** `exec` | 語法錯／無 run → 400；模組層 side-effect 於存檔時不觸發 |
| 牆鐘逾時 | `asyncio.wait_for(..., timeout=ANILA_ACTION_EXEC_TIMEOUT_SECONDS)` | 只停止等待；**不能殺執行緒** |
| 併發上限 | `asyncio.Semaphore(ANILA_ACTION_EXEC_MAX_CONCURRENCY)`；逾時不釋放槽位，完工 callback 才釋放；取得逾時 → 503 | 限制被放棄的 worker；卡住者永久佔槽；槽滿 → exec 停用至完工或重啟 |
| 輸出截斷 | `ANILA_ACTION_OUTPUT_MAX_CHARS` | 截斷後 `truncated: true` |
| 綁定 fail-closed | `message_action_service._user_visible_action_ids`；零綁定 → 無人可見；admin **不**繞過；owner 全見 | PLAN 驗證欄「指定使用者看得到」 |
| ≥密 擋外流 | invoke 呼叫 `outbound_action_allowed(level)`；否 → 403 | 與 SPA `!classified` 同一條線（OE-4） |

---

## 6. 稽核與複查

七種稽核動作（同一本 `audit_logs`）：

| action | 語意 |
|---|---|
| `message_action_create` | 建立快照（含完整 body、choices、sha、version、bindings）；fail-closed |
| `message_action_update` | 更新快照（含 previous＋new sha）；fail-closed |
| `message_action_delete` | 刪除前最終狀態快照；fail-closed |
| `message_action_bindings_replace` | 綁定 before／after；fail-closed |
| `message_action_invoke` | **write-ahead**：執行前先 commit；失敗 → 500、什麼都不跑 |
| `message_action_exec_result` | 執行後結果（duration、truncated、error_type、traceback）；**fail-soft** |
| `message_action_audit_export` | 匯出本身是管理動作（SYSTEM-MAP L275） |

監管鏈：列上 `body_sha256` 與每一次稽核列的 sha 對照——靜默改 DB 可被偵測。匯出：`GET /api/message-actions/audit/export` → `application/x-ndjson`，**僅 owner**。

複查節奏：**每月一次**，以及**每一次撰寫變更之後**。sha 不符＝資安事件。

保留期限＝平台半年（平台尚無 purge job；非 OW-3 發明範圍）。

---

## 7. 已知殘餘風險（必須寫）

(a) **稽核非 append-only**，直到 PLAN 2.7（G2）落地——目前最多是篡改可察覺（tamper-evident），不是防篡改（tamper-proof）。特權內部人若能直接改 `audit_logs` 列，快照完整性不成立。

(b) **逾時後執行緒可能繼續跑**（CPython 無法從外部殺 thread）；semaphore 限制被放棄的 worker：卡住的 worker 永久佔用槽位直到回傳，槽位耗盡後新 exec → 503（fail-closed），直到完工或服務重啟。

(c) **速率／併發限制是每進程的**，多 worker／多副本不共享；叢集級限流不在範圍。

(d) **宣告式稽核只記錄伺服器渲染後的 prompt 的 sha／長度**，不記錄 body 原文於 invoke 列；惡意客戶端仍可另送別的 prompt——但那等同「使用者自己打字」，本就允許且已計費。

(e) **exec 對外連線繞過 SSRF allow-list**（`url_guard.validate_outbound_url`）；這是能力聲明的一部分，不是遺漏。

---

## 8. P2 掃描對照表

| 掃描項 | 對應程式碼 | 判定 | 依據 |
|---|---|---|---|
| A03 Injection / CWE-94（Code Injection） | `message_action_exec._execute_sync` → `exec(code, …)` | **接受**（本文件） | owner-only + 預設關 + 簽署本文件後才開旗標；等同部署權 |
| A01 Broken Access Control | `require_owner`；bindings 聯集；admin 不繞過可見性 | **已緩解** | 按壓權與撰寫權分離；零綁定 fail-closed |
| A09 Security Logging and Monitoring Failures | 七種 audit action；write-ahead invoke；owner-only export | **已緩解；殘餘至 2.7** | 快照＋sha；append-only 待 PLAN 2.7 |
| A05 Security Misconfiguration | `ANILA_ENABLE_ACTION_EXEC` 預設 `False` | **已緩解** | 旗標關＝攻擊面不可達（P0.2 旗標分域先例） |

---

## 9. 失效條件與回退

出現下列任一情況，本接受說明自動失效，必須重審：

1. 第二位作者被授權撰寫 exec（含把 `require_owner` 放寬到 admin）。
2. 非 owner 的任何角色可建立／更新 exec body。
3. csp 改為多 worker／多副本部署，且未重做進程外隔離與叢集限流。
4. 資安中心要求「真正隔離」或禁止同進程動態程式碼。

**回退步驟（不摧毀宣告式）**

1. 設 `ANILA_ENABLE_ACTION_EXEC=0` 後 `docker compose up -d`（非 `docker restart`）——exec 立即從 `/visible` 消失、invoke 404；宣告式繼續可用。
2. 將既有 exec 工作流改接到 agent（MLSteam 天然隔離）或 n8n（PLAN 機制表）。
3. 保留稽核匯出作為歷史證據。

單一檔案 `message_action_exec.py` 即升級／抽離 seam——未來若改 subprocess＋獨立直譯器，只換這一個模組。

---

## 10. 簽署

| 欄位 | 內容 |
|---|---|
| 擁有者姓名 | |
| 簽署日期 | |
| 簽署時 commit SHA | |
| 當時生效中的 exec 動作 `body_sha256` 清單 | |

（以上欄位由擁有者親填；起草者不得代簽。）
