# ANILA 全平台稽核合成報告

> 產出 2026-07-26。分支 `feat/ux-parity`。
>
> **來源**:兩輪多代理稽核共 17 路。第一輪(重建計畫 + 懷疑論審查 + UX 審查)、第二輪(六視角盤點 + 對抗式驗證 + 完整性審查)、加一路 Open WebUI v0.10.2 功能解剖。
> **驗證強度**:對抗驗證對 33 條做獨立複驗(**28 CONFIRMED / 4 PARTIALLY / 1 推翻**),含對執行中 dev stack Postgres 的唯讀 schema introspection、實跑 pytest(`1830 passed / 4 failed`,coverage 77%)、自寫對比度腳本重算、容器內重現 TypeError。完整性審查另抓出 16 條**六路全漏**的面向。
> **本檔的角色**:唯一的排序權威。各路原始報告在 workflow journal,不再逐條複製。
>
> ⚠ 排序依據是**「平台對使用者/稽核員說的話與事實不符」> 資料遺失 > 上線即爆 > 規模化天花板 > 體驗**。這個順序不是我選的偏好,是完整性審查的 S5 發現迫出來的:此 codebase 的**一級缺陷類別是「文件/UI 的字面與實作相反」**,出現頻率高於任何單一技術債。
>
> **執行計畫**:`platform-remediation-plan-2026-07-26.md`(Wave 0–4、44 個工作包、五份子計畫)。本檔管排序,那份管執行。

---

## 修訂紀錄

**rev.2(2026-07-26,攻堅鏈 ③ 複驗後回寫)** —— 本檔 rev.1 有五處事實錯誤,由 Fable 5 在撰寫執行計畫時以一手證據推翻,已就地修正並保留原文供追溯:

| # | rev.1 原文 | 修正 |
|---|---|---|
| 1 | 「governance 與 anilalm 的改動不觸發任何 workflow」(§6、§7.2) | **推翻**。`gate1-ci.yml` 無 `paths:` 過濾,三個前端 job 都在。真缺口是 anilalm 無 `test` script |
| 2 | 「40 個 `_pg` 測試」(§6) | 那是 pytest **skip 數**。實際 12 檔 / 34 個 test function |
| 3 | `execution_grants` 列入授權表(§0、T2-1) | **它不是資料表**,是 runtime minted 契約。收斂對象是 6 張持久 DAC 表 |
| 4 | 「549 個 inline style」(§1) | 那只是 shell。合計 **966**(shell 549 + anilalm 417) |
| 5 | 「127 檔 / 1412 test function」(§7.2) | 本分支現為 **130 檔**(PR #50 新增 3 檔測試);function 數不變 |

> 第 1 條的成因值得記下來:它源自第一輪懷疑論審查的 C4,我**未複驗即採用**。這正是本檔 §5 S3 所描述的模式(接手一個未經查證的主張並繼續傳播),也是我在派工時要求「降級任何發現須附複驗證據」的原因——那條紀律反過來抓到了我自己。

---

## 0. 給決策者的五句話

1. **不做大型重建。** 四個痛點裡只有一個是架構性的;而重建會砸掉唯一沒有後端備援的資產(分類分級的使用者端執法,約 400 行、6 檔、14 處條件式,100% 在前端),同時保住不需要保的(後端護城河對前端重建完全免疫)。前端有 **0 支 E2E**,無護欄重建等於裸奔。
2. **真正該修的第一類不是功能缺口,是平台在說謊。** 已驗證的字面不實至少 **7 處**,包括「加密模式」什麼都沒加密、runbook 說備份含私鑰而實作明確不含、治理帳時間錯 8 小時、串流稽核恆 success、營業秘密可自由匯出且零紀錄。在涉密驗收會議上這類問題的殺傷力高於效能或測試覆蓋率。
3. **專案卡住的結構原因是「驗證器先於被驗證物」。** 五個高品質、有測試的驗證器,驗的東西全都還沒建(Gate 5 素材無產生程序、6 個 SLO 指標零 producer、production restore 路徑不存在、RTO/RPO 值只存在於未產生的簽章 profile 需求裡、備份 cron 必定失敗)。這不是任何單一 bug,是工程能量壓倒性投在「證明它合規的機器」上。
4. **能力天花板比想像低一個數量級。** RAG 熱路徑是 N+1 clearance 查詢(200 份文件的知識庫 ≈ 單一 turn 1000+ 次同步 DB round-trip),跑在單一 uvicorn worker 的 event loop 上;連線池 30 對執行緒池 40、RAG 每請求佔 3 條 → **RAG 併發天花板約 10**。「數百人」目前是零證據的宣稱。
5. **從 Open WebUI 該抄的是資訊架構,不是架構。** 投報率最高的單一項是**一張多型 `access_grant` 表**取代 ANILA 現有的 6 張持久 DAC 授權表(不含 MAC 層的 clearance,見 T2-1)——它讓「這個人到底能看到什麼」幾乎免費得到,而那正是涉密系統最該有、目前完全沒有的一張表。同時有五個實作姿態必須明確拒絕。

---

## 1. 大型重建的裁決

### 不做。理由三條,都有現場證據。

**① 痛點的根因是「介面根本不存在」,不是架構錯。**
`clearance` / `compartment` / `need-to-know` / `collection grant` 在 `apps/` 三個前端的 grep 命中數是 **0**。後端有 10 個 `require_admin` 端點在那裡。這是「新增畫面 + 補 GET 端點」的工作,重建不會解決,只會延後三到六個月。

**② 重建會砸掉唯一沒有備援的資產。**

| 資產 | 對前端重建的暴露 |
|---|---|
| 後端護城河(Gate 2/5、clearance MAC、卡登 CRL、稽核帳、18 張表的分類 CHECK) | **完全免疫**,1412 個 test function 釘住 |
| **分類分級的使用者端執法**(浮水印、複製鎖、匯出鎖、`convScope.js` 的 hydrate 閘門) | **100% 在前端,零後端備援** |

`runtime/convScope.js` 那條「conversation 沒 hydrate 就一則訊息都不渲染」的安全閘門,絕對不能在第二個 app 重寫一次。

**③ 前端有 0 支 E2E,且 `e2e/README.md` 宣稱有 RBAC 與注入拒絕覆蓋——那個檔案不存在。**
這比「沒有 E2E」更糟:任何人讀 README 會以為已有端到端覆蓋。

### 但有一件事必須先拍板,而且它不是重建

**樣式策略。** shell 與 anilalm 是 **零 `@media`**——不是漏做,是 inline style **語法上無法寫 media query**(`style={{` 共 **966** 處:shell 549 + anilalm 417)。唯一有響應式的 app(governance,25 條 `@media`)恰好是唯一沒用 inline style 的。

這一項不定,「無響應式」永遠修不了,而且**任何前端投資都會做兩次**。這是重建與不重建之間真正的分歧點,不是要不要重寫 UI。

---

## 2. Tier 0 — 平台說謊(最高優先)

> 共同特徵:**失敗是靜默的或誤導性的**,使用者/稽核員拿到的字面與事實相反。這一類必須先修,因為它們損害的是「平台說的話能不能信」,而那是涉密系統唯一不能失去的東西。

### T0-1 · 上傳文件後模型只看到檔名,並照著檔名編造答案 · CRITICAL · 止血 S / 真修 M

**CONFIRMED**(對抗驗證獨立複驗三處後端零命中)

- [app.jsx:203-220](apps/anila-shell/src/app.jsx#L203-L220) `buildUserContent()`:非圖片附件只組成 `[附件]\n- 檔名`
- 後端也不補:`grep attachment services/csp/app/api/proxy.py` → **0**;`grep -rln attachment services/csp/app/services/` → 只有 `attachment_service.py` 自己;`router_server.py` 對 `attachment` → **0**

使用者拖一份 `人事管理規章.pdf` 問「幫我摘要重點」,模型收到的字面只有檔名,**然後照著檔名編一份摘要**。UI 上有檔名 chip、有「上傳中…」、有成功狀態,全部看起來正常。

**為什麼排第一**:這是 NotebookLM 式平台的招牌情境,失敗模式是**靜默幻覺**。使用者不會發現被騙,只會在某次對照原文時發現「這系統會編」——那一刻可信度就沒了,且會口耳相傳。在會放營業秘密的環境裡,一份幻覺出來的規章摘要是實質風險。

而解析能力**其實有**(`anila_core/ingestion/parser_registry.py:627-628`、`docling_parser.py:46-47` 支援 pdf/docx/pptx/xlsx/html/md)——只是聊天路徑沒接上。

**加重**:[chat.jsx:1233](apps/anila-shell/src/chat.jsx#L1233) ≥10MB 圖片被靜默降級成同一條路徑(`file.size < 10*1024*1024` 才 `readAsDataURL`)。使用者拍的 12MB 掃描檔上傳成功、chip 顯示圖示、模型說「我看不到圖片」。**零警告**。

**止血(數小時)**:非圖片附件在 composer 明確擋掉並導向「我的知識庫」。**真修**:CSP 端呼叫既有 parser 抽文字注入(需處理 token 上限與密等繼承)。

---

### T0-2 · 「加密模式」什麼都沒加密 · CRITICAL · S(改措辭)/ L(真做)

**我自己複驗**:全 repo(services+infra+docs)`pgcrypto|LUKS|dm-crypt|TDE|encrypt.*at.rest` → **0 命中**。

- [ceiling.py:5](services/csp/app/services/proxy/ceiling.py#L5) 自承:「現況機制(`requires_encryption` + 單向 conversation latch)**從不 deny 出向**」
- [user_memory.py:134](services/csp/app/models/user_memory.py#L134) docstring 自承:`is_encrypted` 只是**分類傳播旗標**,「caller is responsible for latching」
- [credentials.py:55](services/csp/app/api/agents/credentials.py#L55) 稽核訊息稱它為「**加密模式**」
- [app.jsx:3184](apps/anila-shell/src/app.jsx#L3184) 對使用者說「**加密模式**由 agent 設定…使用者無法手動切換」

極機密對話內文、`document_chunks` 全文、`attachments` 檔案 bytes **全部明文**躺在 PG data volume 與 `share/uploads` bind mount 上。現況是「**備份加密、線上明文**」(備份確實有 age 加密)。

使用者與稽核員都會把那句話讀成「我的機密對話有加密」。**在涉密驗收會議上這是不實陳述風險,不只是技術缺口。**

**動作**:先改措辭(零功能改動,消除不實陳述);at-rest 加密是獨立的 L,由資安權責人決定是否需要。

---

### T0-3 · 營業秘密在「內容外流」維度等同無機密 · CRITICAL · S

**只有把三路合起來才看得到**(完整性審查 S3 + user-journey + original-sins)。全部是同一顆 legacy boolean:

| 位置 | 效果(營業秘密 = `classified:false`) |
|---|---|
| [conversations.py:316](services/csp/app/api/conversations.py#L316) | **讀取不寫稽核**(全 repo 唯一 `log_classified_access` 呼叫點) |
| [conversations.py:292](services/csp/app/api/conversations.py#L292) | 搜尋吐訊息內文摘要 |
| [chat.jsx:490](apps/anila-shell/src/chat.jsx#L490) `canCopy = !classified` | **可複製內文** |
| [chat.jsx:2184](apps/anila-shell/src/chat.jsx#L2184) `onExportConv && !c.classified` | **可匯出,且檔案零標示零稽核** |
| [app.jsx:2521-2528](apps/anila-shell/src/app.jsx#L2521-L2528) | 可建分享連結 |
| 列印 | `@media print` / `window.print` 全 repo **0 命中** → Ctrl+P 另存 PDF 完整帶走 |

mirror 規則是 [policy/service.py:262](services/csp/app/modules/policy/service.py#L262) `classified = level >= 機密`,而五級是 `無機密 < 營業秘密 < 機密 < 極機密 < 絕對機密`。

**完整失效鏈**:營業秘密對話 → 可匯出 → 檔案零密等標示 → 零 policy 檢查 → 零 `export_records` → 落在使用者桌面/USB。而平台為 artifact 蓋了一整套 `ExportRecord` + `PolicyDecision` 的匯出管制機器([artifact.py:288-300](services/csp/app/models/artifact.py#L288-L300),docstring 明寫「只在 classification policy 核可後落列」),**流量最大的路徑完全不經過它**。

**修法幾乎免費**——`classification_level` 早就在 API 回應裡([conversations.py:127](services/csp/app/api/conversations.py#L127)),前端已經拿到:
1. 五個 gate 全改吃 `classification_level`(照 `is_publicly_shareable` 的 fail-closed 寫法,[conversation_service.py:376-389](services/csp/app/services/conversation_service.py#L376-L389))
2. 匯出檔案加密等頁首 + 匯出者 + 時間 + 來源系統
3. `ExportRecord.artifact_id` 改 nullable,聊天匯出也落列
4. 加 print stylesheet(浮水印在列印時保留)

⚠ **這個模式會再犯**,除非同時做:建立「legacy `classified` 的最後一個引用點清單」並加 lint。這是 original-sins 診斷的主導反模式(見 §5)。

---

### T0-4 · 治理帳的法律證據時間錯 8 小時 · CRITICAL · S(邊界)/ M(欄位)

**完整性審查 L1,實庫實測**:`timestamp with time zone` **93** 欄 vs `timestamp without time zone` **93** 欄。naive 那 93 欄涵蓋**整套五級分類治理帳**:

```
classification_events.created_at              ← 分類異動 append-only ledger
policy_decisions.created_at
declassification_requests.created_at/.decided_at   ← 降密雙人核准
classification_authority_assignments.*             ← 需公文文號的權責指派
export_records.created_at
{conversations,messages,artifacts,document_chunks}.classification_latched_at
```

pydantic 序列化實測:naive 出去是 `"2026-07-25T10:00:00"`(**無時區標記**),aware 是 `...Z`。前端 `TZ=Asia/Taipei node` 實測:naive → 早 **8 小時**。

**致命組合**:[ClassificationInventoryView.vue:121](apps/csp-governance-ui/src/views/ClassificationInventoryView.vue#L121) 對 naive 資料套 `timeZone:'Asia/Taipei'` ——Date 已被誤解析成本地時間,再指定時區等於**零修正**。而同一個治理後台混用**四種**日期格式(`en-GB` 8 處、`zh-TW`+timeZone 2 處、無 locale 1 處、`zh-TW` 無 timeZone 2 處)。

**同一畫面上有些時間對、有些錯 8 小時,而且看不出來是哪些。**

`classification_events` 與 `declassification_requests` 是降密雙人核准的法律證據鏈;時間錯 8 小時且無時區標記,稽核複查時無法主張紀錄時序。

**動作**:① API 邊界統一 TZ 正規化(全 `schemas/` 目前只有一處做,`source_snapshot_adapter.py:34`);② 一份共用 formatter;③ 93 個 naive 欄收進 migration(與 §6 的 schema drift gate 一起做)。

**連帶**:[api_key_service.py:70](services/csp/app/services/api_key_service.py#L70) 比較 naive `expires_at` 與 aware `now()` → **任何設了到期日的 API key,每次驗證都 500**。對抗驗證已在容器內重現 `TypeError`。根因是 `r1_0017_gate2_pg_atomicity.py:85` 用 `sa.DateTime()`(naive)而同表其他欄在 `0001` 就是 `timezone=True`。`validate_api_key` **0 個測試**。

---

### T0-5 · 備份能跑但還不了,而文件說的和實作相反 · CRITICAL · M–L

**CONFIRMED**(對抗驗證逐條實地核對,四條全成立)

| 條目 | 事實 |
|---|---|
| `--full` 直接 fatal | [anila-ops.sh:582](infra/deployment/scripts/anila-ops.sh#L582) `[ "$#" -eq 0 ] \|\| fatal`,而 guide `:180` 的週日 cron 就是 `backup --full` → **每週日 100% 失敗** |
| 20 個必要 env 一個都沒設 | `production_backup.py:141-145` 硬性要求;`.env.example` 與 `intranet-deploy.sh` 零覆蓋;`cmd_backup` 是 `exec python3` 不 source 任何檔案 → cron 空環境第一行就死 |
| **文件與實作相反** | guide `:183` 寫「**備份含 `.env` 與 JWT 私鑰**」;profile 對 JWT/TLS key 用 `key_management_reference`(只存外部指標與公鑰指紋),且 `grep "\.env"` 該 profile → **0 命中**(`.env` 根本不是 surface) |
| restore 指令形式已不存在 | 現行簽章 `restore <signed-bundle-dir> <new-disposable-target> [prepare\|smoke]`;guide `:195` 教的是單一參數形式 → 撞 `usage:` fatal;且 `*) fatal "正式 destructive restore 留在 Gate 6"` |
| `age` 沒搬進內網 | runbook 硬依賴,離線工具包不含它,`grep "command -v age"` 在 `infra/` → 0 |

兩條 cron 都 `>> /var/log/anila-backup.log 2>&1` → **沒人會發現**。

**一句話**:能備份、不能還原、而且還原演練的證據只到 disposable target 的 smoke。**這是「相信自己有 DR,實際上沒有」的最壞組合。**

---

### T0-6 · 排錯時拿到的證據會誤導你 · HIGH · M

**PARTIALLY**(對抗驗證收窄了範圍,主結論成立)

- [inference_audit.py:208](services/csp/app/services/inference_audit.py#L208):`stream=True` 時在**推論發生之前**就寫一列 `status="success"`;`:250-251` 看到 `acceptance_recorded=True` 直接 `return` → `proxy.py:2092-2335` 一長串 outcome 寫入在串流路徑上**全是 no-op**
- **收窄**:acceptance **之前**的拒絕仍會寫正確的 denied 列。精確版是「**acceptance 之後**的 denied/error(上游 5xx、串流中途 clearance 拒絕、breaker、stream deadline)在串流路徑上不留痕」
- docstring `:14-16` 自承 "outcome fidelity is the documented tradeoff"

**加重三條**:
- 治理稽核 [audit_logs.py:44](services/csp/app/api/audit_logs.py#L44) 只有 `limit ≤ 500`,**無 offset、無 cursor、無日期範圍、無匯出** → 「上週三誰改了那個模型設定」查不到。而隔壁 `admin_inference_audit.py:216-260` 的串流 CSV 匯出寫得極好——同一張表,只有 inference 那一半拿到正確待遇。
- `audit_logs` **66% 是 deprecation telemetry**(實庫:`service_token_legacy_env_used` 3269 / 4941)→ 真實稽核訊號被稀釋成 1/3。而這個數字本身說明 **cutover 沒發生**:service client 機制已建好卻沒接上,內部服務間認證實務上仍是靜態 env 共享密鑰。
- `audit_logs.detail` 的 `ILIKE` 搜尋**無 trgm index**,而 `messages.content` 與 `conversations.title` 都特意加了。**給聊天搜尋做了、給合規稽核搜尋沒做**,而 `audit_logs` 在 dev stack 已是最大表。

**還有**:`ANILA_AUDIT_STRICT` 預設 `False`,且該旗標**不在 formal posture 契約內**(`startup_security.py` 的兩份 posture 清單都沒有)→ 涉密正式部署預設「稽核寫不進去也照跑」。而 `audit_service.log_audit_event` **永遠 fail-soft、無 strict 開關**(對比 `inference_audit.py:180-183` 有)。

---

## 3. Tier 1 — 上線即爆 / 資料遺失

### T1-1 · 編輯訊息:三重缺口 · CRITICAL · M

**我自己複驗**([conversation_service.py:172-209](services/csp/app/services/conversation_service.py#L172-L209)):

```python
msg.content = content
db.query(Message).filter(
    Message.conversation_id == conv.id,
    Message.created_at > msg.created_at,
).delete(synchronize_session=False)   # 硬刪,不是軟刪
```

| | 狀況 |
|---|---|
| 資料 | **實刪 DB 列**,無軟刪、無版本、不可復原 |
| 稽核 | 這條路徑**沒有 `log_audit_event`**(同檔 `classify_conversation` 有 → 不是專案慣例) |
| 分級 | **零 classification 檢查** → 機密對話的訊息可經此永久銷毀 |
| 保留 | 零 `legal_hold` 檢查 |

根因是 [message.py:11-40](services/csp/app/models/message.py#L11-L40) **沒有 `parent_id`,`messages` 是扁平 list**。因為沒有樹,「編輯」只能實作成截斷。

**在涉密環境的意義比「使用者資料遺失」更重:這是一條無紀錄的證據銷毀路徑。** 平台花大功夫做 latch 不可降級、`legal_hold`、`erase_due_at`,而這裡有一個 PUT 端點繞過全部。

**加重**:前端那一半也壞了——[app.jsx:1422-1425](apps/anila-shell/src/app.jsx#L1422-L1425) 編輯後 re-run 的 payload 是 `messages: [{role:"user", content: trimmed}]`,**只有一句**。同檔 `sendMessage`(`:1669`)、`continue`(`:1837`)、`regenerate`(`:1913`)都用 `buildMessageHistory(...)`,**編輯路徑是唯一沒用的**。後端明確不補(`proxy.py:1866` 註解:「history is already in the messages array」)。附件也一併消失。

使用者在第 8 輪修一個錯字,模型當作全新第一句回答。**他不會說「編輯功能有 bug」,他會說「這個 AI 很笨、記不住我們在講什麼」。**

**動作**:① `messages` 加 `parent_id` + 索引,編輯/重生改成長出兄弟節點(這同時解鎖多模型並排、Arena、A/B 比較——現在一個都做不了);② 編輯路徑補 audit + classification + legal_hold 檢查;③ 前端改用 `buildMessageHistory`。⚠ 補樹的同時要一併決定舊分支的保留政策,每個分支都可能帶分級。

---

### T1-2 · 串流失敗 → 該輪全滅,且覆蓋已生成文字 · HIGH · M

**CONFIRMED**。持久化順序錯了:[app.jsx:1744-1756](apps/anila-shell/src/app.jsx#L1744-L1756) 兩則 `apiAppendMessage` 都在 `await streamWithAbort` **之後**;`:1777-1782` catch 用 `text:` 而非 append → **取代**已累積文字。

兩件事同時發生:(a) 已串出的半篇回答被錯誤字串取代;(b) 兩則訊息都沒進 DB,重整後這一輪徹底不存在。而 `sessionStorage` 草稿在 `submit()` 時就 `removeItem` 了。

**按 Stop 是安全的**(`sse.js:147-149` 吞 AbortError → 不進 catch → 半篇會持久化)——這點做對了,壞的只有真失敗路徑。

**加重**:錯誤訊息把後端 JSON 原文貼給使用者。`sse.js:119-124` `await response.text()` 沒有 `JSON.parse` → 使用者看到 `請求失敗:{"detail":"模型 'xxx' 未註冊"}`。而那些 detail 是給開發者看的內部語彙(service token、Router scope、TaskRun)。錯誤橫幅只有一個 ✕,**沒有重試按鈕**;27 個 `setRuntimeError` call site 全渲染成同一條 12px mono 紅字。

---

### T1-3 · 一次批次匯入凍結全平台 · CRITICAL · S–M

**CONFIRMED**(對抗驗證逐項複驗)。[documents.py:556](services/csp/app/api/ingestion/documents.py#L556) `async def upload_zip`,import 清單**無 `asyncio`**,per-member 迴圈(`:618-700`)內**零 `await`**:

`zf.read`(zlib)→ `validate_content` → `sha256` → `_persist_blob` → `with open(...,"wb") as f: f.write()`(同步磁碟寫)→ `db.query`

上限 `_ZIP_MAX_TOTAL_BYTES = 1GB`。單 uvicorn worker + 800MB 匯入 = **全平台 SSE 與 API 停擺數十秒到數分鐘**。而這是產品文件鼓勵的正常操作。

修法現成:`app/services/` 已有 8 處 `asyncio.to_thread` 前例。

---

### T1-4 · RAG 併發天花板約 10 · CRITICAL · S + M

三條相乘:

**① N+1 clearance 查詢**([retrieval_service.py:399-436](services/csp/app/services/retrieval_service.py#L399-L436)):`requested_document_ids is None`(= 預設聊天)時撈 collection 內全部 doc id,再**逐一**呼 `resolve_and_evaluate_data_access`,該函式內含 5 個 `db.query`。200 份文件 ≈ **單一 turn 1000+ 次同步 DB round-trip**。
**批次版就在同一支檔案 `clearance/service.py:804`,生產呼叫者只有 `api/traces.py:173`。修法幾乎免費。**

**② 連線池對不上執行緒池**([database.py:26-33](services/csp/app/database.py#L26-L33)):`pool_size=10, max_overflow=20` = 上限 30,**無 `pool_timeout`/`pool_recycle`**,寫死無 env。Starlette 對 199 個 sync endpoint 用 anyio 預設 **40** tokens。

**③ RAG 每請求佔 3 條連線**:`retrieval_service.py:232-233` 另開兩個 session,而 `:252 await proxy_request(...)`(embedding,timeout 30s)**在同一個 try 內** → 兩條連線被 pin 整個 round-trip。加請求自己那條 = 3。

加上 SSE:`Depends(get_db)` 的 session 到 response body 送完才釋放,`PROXY_STREAM_MAX_SECONDS=300` → 每個進行中的串流 pin 1 條達 5 分鐘。

**第 11 個 RAG 請求阻塞 30 秒後拋 `TimeoutError`,而沒有 exception handler → 使用者拿到裸 500。**

**④ 而且這不是理論問題**:[proxy/service.py:1836-1846](services/csp/app/services/proxy/service.py#L1836-L1846) 註解自承「Doing that on the MainThread **freezes every concurrent coroutine** … closes the **deadlock ring observed in production**」——已用 `asyncio.to_thread` 補一處,**同樣的 pattern 還有 33 處沒補**。

**⑤ 資料面無 request body 上限**:全 backend 只有一處 content-length 檢查(`users.py:541` ui_settings 256KB)。`/v1/chat/completions`、`/v1/embeddings`、`/v1/agents/.../answer`、`/v1/images/generations` 全部裸 `await request.json()`。nginx `client_max_body_size 100M`。實測 100MB JSON 的 `json.loads` ≈ 0.5s event-loop 停頓。而 `platform.yml` 對任何服務**都沒有 `mem_limit`/`deploy.resources`** → 單一容器 OOM 拖垮整台 air-gapped 主機。

**⑥ 零負載測試**:全 repo 零 k6/locust/vegeta/wrk。**修完①②③你也不知道修夠了沒。**

---

### T1-5 · 前端零錯誤邊界 → 白畫面 · CRITICAL · **S**

**CONFIRMED**。`grep 'ErrorBoundary|componentDidCatch|getDerivedStateFromError'` 於 `apps/anila-shell/src/` → **0**;`csp-governance-ui/src/main.js` 全 10 行無 `app.config.errorHandler`。

涵蓋**聊天主流程**(數百名使用者的日常)與**治理後台**(管理員唯一入口)。任何 render 期 throw = 白畫面、零訊息、零復原路徑。

**這是全案唯一「CRITICAL 但成本 S」的項目,投報率沒有第二名。**

順手修 anilalm 那個唯一存在的 error boundary 的三個問題:`ErrorBoundary.tsx:63,81` 把 stack trace 與 componentStack 印給使用者看(涉密平台洩漏內部路徑);`:87` 的 `localStorage.clear()` **會清掉另外兩個 app 的資料**(三個 SPA 同源,會一併清掉 shell 的 `anila-folders` 與 governance 的 `anila.theme`)。

---

### T1-6 · 串流每 token 重繪整條對話 · CRITICAL · M

**CONFIRMED**。三個事實疊起來:`MessageBubble` 與 `MarkdownView` **零 `React.memo`**(`chat.jsx` 全檔 0 次);串流累積是 `(prev[convId]||[]).map(...)` → **換整條陣列 identity**;`MessageBubble` 收 3 個 inline closure(就算加了 memo 也不生效)。

40 則訊息的對話,**每個 token 就是 40 次完整 markdown pipeline 解析**(react-markdown + remark-gfm + remark-math + rehype-katex + rehype-highlight)。同時零虛擬化。

**修法**:memo 化 + inline closure 改 `useCallback` + 串流累積改成「只換該則訊息的 identity」。這是拆 `ChatRuntime` 的第一刀,M 成本、可獨立驗證。

---

### T1-7 · Gate 5 治理素材:新環境根本起不來 · CRITICAL · L

**PARTIALLY**(事實全對,**框架錯,而這個區分會決定排錯工**)

事實:`main.py:271-275` 素材缺失 → `RuntimeError` crash-loop;`/ready` 回 **503** → `platform.yml:315` healthcheck 打的正是 `/ready` → 容器永不 healthy → `deploy` 一定超時;`governance_required_for_settings()` 對所有 `prod-*` 與 `trial-military` 回 True(`GATE5_MODEL_GOVERNANCE_ENABLED=false` 救不了);`.env.example:31` 寫 `ENABLED=false` 而 `platform.yml:121` 預設 `:-true`(姿態不一致會誤導 operator);`grep GATE5_MATERIAL_DIR docs/` → 只有 1 筆表格,**零產生程序**。

**框架修正**:「repo 沒有產生工具」**不是疏漏,是刻意的**——[infra/policy/gate5/README.md:150-154](infra/policy/gate5/README.md#L150-L154) 明文只附 disabled template 與 synthetic generator,**不附任何 production approval**。正式 profile 需要簽章 profile、artifact/deployment digest、法務裁決、GPU topology、fresh health、**四類 approver**,設計上就不能由 repo 腳本生成(否則簽章毫無意義)。

→ **真正的缺口是「沒有 operator runbook 說明誰簽、四個檔案怎麼組裝、放哪裡」,不是「缺少 generator」。**
照原框架排工,會有人去寫一支「產生 production profile 的腳本」,而那恰好是 README 明文禁止的事。嚴重度不變,修法完全不同。

**同源**:Gate 5 下「加一個模型」不是 UI 動作,是離線重簽(provider binding 必須逐欄相符,含 `transport_target` 整個 dict 與 `transport_target_sha256`)。**這條就是「沒辦法從 https 端點取得所有模型」痛點的真解所在——先讓註冊能通,自動 discovery 才有意義。**

---

### T1-8 · alerts schema drift(而病根影響面遠大於 alerts) · CRITICAL · M

**CONFIRMED**(兩路獨立實庫驗證,結果一致)

```
pg_indexes   WHERE tablename='alerts' → alerts_pkey      （僅此一個）
pg_constraint WHERE conrelid='alerts' → alerts_pkey | p  （FK = 0）
```

而 `models/alert.py:11` 宣告 `fingerprint ... unique=True, index=True`、`:23` 宣告 FK。碰過 alerts 的 migration **只有 `0001`**(建 7 欄),12 欄由 `startup_migrations.py:209-227` 的 `ADD COLUMN IF NOT EXISTS` 補上、**無 index/UNIQUE/FK**。

`alert_service.py:22-25` 是 read-then-write → **併發下重複告警 DB 攔不住**;缺 index → `health_checker` 每 60 秒對每個 model/agent 呼叫它 → **永久性的每分鐘全表掃**。ORM diff 腳本全庫掃出 **17 條缺失**(另含 `users.department_id`、`attachments.message_id`)。

**病根是同一個**(完整性審查 S2):`startup_migrations.py` 是 alembic 外的**第二套 schema 機制**,覆蓋 8 張表、coverage 21%,docstring 自承「The 0001 alembic baseline does not match the current SQLAlchemy models」。四個後果:

1. `alembic upgrade head` 到乾淨 DB **得不到可用 schema** → **DR 還原是壞的**(與 T0-5 相乘)
2. `alerts.fingerprint` 缺 UNIQUE
3. **93 個 naive timestamp 欄**(= T0-4)
4. `ingestion_collections.created_by` = NOT NULL + ON DELETE SET NULL → **硬刪使用者必定 IntegrityError**(離職即撞,見 T2-5)

而測試用 SQLite + `Base.metadata.create_all`(`conftest.py:76`,不跑 alembic)→ **驗證迴路結構上看不見以上任何一條**。

**一個 30 行的 ORM-metadata vs 真 PG diff 腳本進 CI,一次抓掉四類問題。這是全案 ROI 最高的單一動作。**

---

## 4. Tier 2 — 規模化天花板

### T2-1 · 「這個人到底能看到什麼」系統內無法回答 · CRITICAL · M

**CONFIRMED**。`grep clearance|compartment|need-to-know` 於三個前端 → **0 命中**。後端有 10 個 `require_admin` 端點,但:
- `collection-access-grants` 與 `required-compartments` **連 GET 都沒有**(只有 POST/DELETE)
- `ClearanceGrantOut`([clearance.py:55-67](services/csp/app/schemas/contracts/clearance.py#L55-L67))是扁平的,不含 compartments / collection grants

→ **涉密部署要讓任何人讀任何機密知識庫,只能 curl。** 而「這個人能看到哪些資料」在系統內無法回答,只剩直連 DB(`CLAUDE.md §6` 明令禁止,且需 superuser 繞 RLS)。

**這是涉密系統最該有的一張表,現在完全不存在。**

**這裡是 Open WebUI 唯一該直接抄的資料模型**:一張多型 `access_grant` 表 `(resource_type, resource_id, principal_type, principal_id, permission)`,取代 ANILA 現有的 **6 張持久 DAC 表**(`user_model_permissions`、`api_key_model_permissions`、`user_agent_permissions`、`api_key_agent_permissions`、`service_access_grants`、`collection_access_grants`)。
⚠ **2026-07-26 複驗修正兩處**:① `execution_grants` **不是資料表**(migration 零建表、無 `__tablename__`),它是 runtime minted 的短效傳輸契約,**不併入**;② `clearance_grants` / `clearance_grant_compartments` 屬 **MAC 層,永不併入**——分級與 compartment 必須留在 grant 之外當獨立 AND 條件。
收斂後授權引擎變成**無狀態純函式**,於是 **Preview Access 幾乎免費得到**(Open WebUI 的做法:把 `user_id` 傳空字串來模擬「只屬於這個群組的虛擬使用者」,重用同一個原語,零額外邏輯)。

⚠ **必須拒絕的姿態**:Open WebUI 的權限合併運算子**只有 `or`**,全系統無交集/差集語意。**ANILA 的分級與 compartment 檢查必須留在 grant 之外當獨立的 AND 條件**,不能折進聯集。

---

### T2-2 · 知識庫無法分享 → 逼人用 admin 當 workaround · CRITICAL · M

**CONFIRMED**,而決定性證據是該函式**自己的 docstring**:

> Future Sprint may add a `collection_access_grants` table for sharing across users; this helper is the single point that needs to grow when that lands.

**表已經 land 了**(`models/clearance.py:164-171`),**helper 沒長**。而資料面已經認它(`clearance/service.py:23,336-372` 有完整 grant 解析)。

管理面([collections.py:52-80](services/csp/app/api/ingestion/collections.py#L52-L80))只認 `is_admin_tier` 或 `created_by`;`:194-198` 非 admin 傳 `owned_only=false` 直接 403。

→ **被授權的同事能檢索、卻看不到這個知識庫存在、看不到文件清單、不能上傳。** 唯一 workaround 是給 admin,而 admin 是「看光全平台所有密等」的角色。**這是功能缺口造成的安全洞。**

---

### T2-3 · 營運全盲 · CRITICAL–HIGH · M

**CONFIRMED**(三條分別複驗)

| | 事實 |
|---|---|
| metrics | `grep /metrics` 於 csp/studio/router `main.py` → **0**;`platform.yml` **無 prometheus/grafana/victoriametrics 任何服務** |
| log 輪替 | `grep -c "logging:" platform.yml` → **0**(Docker 預設 json-file 無上限)。全 repo + 全 docs `daemon.json\|logrotate\|max-size` → **0 筆**。**air-gapped 主機會在上線 N 個月後被填滿** |
| 告警 | `alert_service.py` 全檔零 smtp/notify/send;唯一生產者是 `health_checker` → **DB 滿、queue 積壓、憑證到期、備份失敗、暴力登入皆不產生告警** |
| 已算好卻沒人用 | `api/alerts.py:56` 的 `/summary` 已含 `high_count`,**22 個 governance view 沒有一個放在首頁**;`AlertsView.vue:118` 只在 `onMounted` 抓一次,**連輪詢都沒有** |
| 做好了零入口 | ingestion-worker 已輸出 Prometheus 格式的 queue/dead-letter/heartbeat 指標,stack 內無人抓、nginx 無路由 |
| CSP log | 寫在容器內 `/app/logs/csp.log`,`platform.yml` **沒掛出來** → 每次 `up -d` recreate 就消失,而 runbook 要求套設定一律 `up -d` |
| 無 request id | 全 repo **0 處** `X-Request-ID`。使用者說「剛才失敗了」時無法對應到任何 log 行 |
| 服務健康總覽 | **不存在**。22 個 view 沒有一個顯示 router/studio/worker/db/redis/nginx 狀態 |

**管理者的唯一真實工具是 SSH 進去跑 `anila-ops.sh health`——而 [anila-ops.sh:210-219](infra/deployment/scripts/anila-ops.sh#L210-L219) 的 profile case 只接受 `prod-intranet-card`**,`prod-military-passwd` 與 `trial-military` 的 health/backup/restore/cert-renew/model-ca/gateway-key/prune/status/logs **8 個呼叫點全部失能**。

---

### T2-4 · 無配額、無並行上限 · CRITICAL · S–M

**CONFIRMED**,而框架是對的:`0006_drop_quota_rate_limit.py` 整表刪除,理由「on-prem local model → no quota」。**這在 2026-04 的單人/小規模是合理的設計決策,不是 bug。它變成缺口的前提是「數百人共用一組 GPU」。**

`grep quota|rate_limit` 於 `services/csp/app`(排除 SSE budget)→ **0 命中**。唯一防線是 nginx **per-IP** `100r/s` —— 那是 DDoS guard,不是公平性,而且卡登/SSO 場景多人共用 NAT IP,per-IP 限流**既會誤傷又擋不住單人濫用**。

計量已經齊全(`token_usage` 有 user/department/model/agent/client + CSV 匯出),**只缺 enforcement**。一個人開 50 個並行長 context 請求就能讓全院排隊。

---

### T2-5 · 人員異動:知識庫擁有權無法移轉,硬刪必定失敗 · HIGH · S

**完整性審查 L4,實庫實測**。[ingestion.py:130-132](services/csp/app/models/ingestion.py#L130-L132) 註解自承「rely on **app-layer reassign** if a user is offboarded」——**那個 app-layer reassign 不存在**:`CollectionUpdate` 只有 name/description/chunking_config/status/classification_level,**無 `created_by`**;`collections.py` 只有 POST/PATCH/DELETE,無 transfer。

且 FK 語意自相矛盾(實測 `created_by` = **NOT NULL + ON DELETE SET NULL**)→ `DELETE /api/users/{id}/permanent` 對擁有任何知識庫的人**必定 IntegrityError**。`users.py:437-450` 為 `agents.owner_user_id` 做了 pre-flight(還附了理由註解),**沒有為 `ingestion_collections` 做**。

而 `_require_collection_access` 只認 `created_by` 或 admin → **人一停用/離職,該知識庫永久變成 admin-only**(放大 T2-2)。

做對的部分:`clearance/service.py:210,666` 有查 `subject.is_active`,停用確實切斷 clearance。所以問題不是權限殘留,是**資產孤兒化**。

---

### T2-6 · 什麼都要找管理員 · HIGH · M(改密碼單獨 S)

- **無自助改密碼**:後端有 `PUT /api/auth/password`,UI 只存在於治理中心(`AppHeader.vue:36`);shell 的帳號 tab 只印 username + role;`shellNav.jsx:28` `GOVERNANCE_ROLES` 不含 `user` → 一般使用者無從得知治理中心存在。**對 `prod-military-passwd`(純帳密)這是每天的管理員雜務。**
- **無忘記密碼**:`LoginView.vue` 全檔沒有;重設只有 admin 端點。
- **Settings 是一頁唯讀公告**:「一般」tab 零輸入元件,payload 只有 `{model, messages}`——`grep temperature` 於 shell → **0**。使用者無法自訂系統提示、無法調任何參數、不能存自己的 prompt。「我想要它固定用公文體回我」= 開單找管理員。
- **零 help / 零使用手冊 / 零 FAQ / 零 onboarding**:`docs/guides/` 只有 developer-guide;唯一空狀態引導是**叫 LLM 即時產生一張「介紹 ANILA 能做什麼」的卡片**——在 air-gapped、GPU 受限環境下,新使用者的第一次互動要嘛慢、要嘛品質不穩、要嘛 gateway 掛掉就失敗。而有 `anila-changelog-seen` / `anila-dismissed-banners` 兩個 localStorage 旗標,**就是沒有 `first-login`** → 基礎設施已在,只是沒用。
- **密碼強度政策只綁自助註冊**:`password_strength` validator 只掛 `RegisterRequest`,`UserCreate`/`PasswordChangeRequest`/`AdminResetPassword` 皆無 → 對 admin 建帳號是唯一路徑的軍方 profile,**政策形同不存在**。無帳號鎖定/失敗節流(`grep failed_login|lockout` → 0)。

**這是規模化的天花板。** 數百人 × 每人每月一件小事 = 管理員被埋。而「連密碼都不能自己改」會讓使用者從第一天就把平台歸類為「別人的系統」。

---

### T2-7 · 找不回自己的東西 · HIGH · S 各項

- **搜尋回任意 30 筆**:[conversations.py:273-282](services/csp/app/api/conversations.py#L273-L282) subquery 是 `.distinct().limit(limit)` **無 `order_by`**,排序在**外層**對已截斷子集做。用高頻詞搜三個月前的對話 → 宣稱只有 30 筆且很可能不含目標 → 使用者結論是「找不到」,實際資料在庫裡。修法:把 `order_by` 移進 subquery。
- **產出 30 天靜默失效**:`RETENTION_ARTIFACT_ACTIVE_DAYS=30` + `ALLOW_ARCHIVED_DOWNLOADS=False` 預設,下載回 410。`ArtifactVersionHistory.tsx` 有正確的 `downloadUnavailableReason()`(做對了),但**使用者日常會去的 `OutputsPage.tsx` 完全不顯示 `lifecycleState`**(grep 零命中),只在點下去失敗後丟一句三合一猜謎:「下載失敗 — 權限、保存期限或檔案完整性檢查未通過。」
- **分享連結建得出來但一定 404**:讀取端有擋 `ENABLE_PUBLIC_SHARE`,**建立端從未讀該旗標**。card 部署姿態就是 `false`。使用者按分享 → 成功 → 傳給同事 → 404。
  > **對抗驗證裁決了一個矛盾**:original-sins 說「這條已修」是**假陰性**(它只查了讀取端)。若照它排工,會把一個真實缺陷關掉。
- **附件重載後消失且從來不能下載**:`appendMessage()` 的 body **沒有 attachment 欄位**;`uploadAttachment` 能帶 `message_id` 但呼叫只給 `conversationId`(送出前還沒有 message id)→ `Message.attachments` relationship 永遠是空的。就算 chip 還在也是個死 `<div>`——後端有 `GET /api/attachments/{reference_id}`,前端零引用。
- **對話清單無分頁**:`conversation_service.py:111-120` 是 `.all()`,API 層也沒有分頁參數 → **契約層就沒有分頁可以接**。前端全量 `.map()` 無虛擬化。
- **`ui_settings` 是 last-write-wins 整包取代**:共享 PKI 卡工作站 + 兩個分頁 → 一邊新增的資料夾/標籤被另一邊蓋掉。而它同時在 auth hot path 上被**每請求重讀**(`_load_user_from_payload` 撈全欄位、無 `load_only`,還帶 `department` 的 `lazy="joined"`)。

---

### T2-8 · 只要回答帶引用,全部 markdown 失效 · HIGH · **S**

**CONFIRMED**。[chat.jsx:530-538](apps/anila-shell/src/chat.jsx#L530-L538) 三元式:有 citations 就走 `renderTextWithCitations`,而 [trust.jsx:57-72](apps/anila-shell/src/trust.jsx#L57-L72) 只做 `/\[(\d+)\]/g` 切割 + 純文字 Fragment。

於是有引用的回答**失去**:表格、程式碼高亮與複製、KaTeX、Mermaid、清單縮排、標題層級。使用者看到滿螢幕 `| 項目 | 金額 |` 與 `## 標題` 的原始符號。

**諷刺的是引用出現的時機正是知識庫問答,也就是這個平台最重要的使用情境。RAG 回答的品質呈現反而最差。**

命中率 100%、可見度 100%、修復成本最低。**這是最划算的一條。**

---

### T2-9 · 深色模式 focus ring 失效 + token 對比不合格 · CRITICAL · S

**PARTIALLY**(結構成立,**數字錯**)

結構 CONFIRMED:`index.html:74-93` 的 `:root[data-theme="dark"]` 覆寫段涵蓋 bg/fg/border/accent-soft,**唯獨沒有 `--accent`**;而 `:119` 全站 focus ring 是 `outline: 2px solid var(--accent) !important` → **深色模式下鍵盤使用者看不到自己在哪裡**(違反 WCAG 2.4.7/2.4.11)。

**數字修正**(對抗驗證自寫 oklch→linear sRGB→WCAG 腳本重算,發現原值恆為正確值的 ÷1.347,七個全部同一係數 = 系統性換算錯誤):

| accent | 原報告 | 重算 | 3:1 |
|---|---|---|---|
| official `#2b4c7e`(預設) | 1.67 | **2.25** | ✗ |
| teal `#0b7285` | 2.58 | **3.48** | **✓** |
| slate `#334155` | 1.39 | **1.87** | ✗ |
| moss `#4a6444` | 2.19 | **2.96** | ✗(邊緣) |
| clay `#a05a2c` | 2.74 | **3.69** | **✓** |
| indigo `#3949a1` | 1.81 | **2.44** | ✗ |
| crimson `#9c2a3b` | 1.92 | **2.60** | ✗ |

→ **5/7 未達標,不是「全滅」。** 缺口本身(預設 official 2.25 + focus ring 失效)依然是真的。
⚠ 而 D2 的三個 token 對比值(`fg-subtle 3.41` / `warn 2.55` / `success 4.16`)**未經重算**——鑑於 D1 有 1.347 倍的系統性偏差,這三個值應該重算後才進 CI 門檻。

**加重**:`--anila-color-warn` 若真是 2.55:1,而 [chat.jsx:2095](apps/anila-shell/src/chat.jsx#L2095) 正是用 `--warn` 表達「因引用過往加密記憶而升級的密等」——**一個安全語意訊號用了可能看不見的顏色**。

**還有**:主題/accent **選了不會記住**(`resolveInitialTweaks` 只讀 `window.ANILA_TWEAKS`,`persistUiSettings` 只送 `{folders, convMeta}`)。而那兩顆按鈕在 header 上**完全未 gate**,一般使用者可切、切了重載即復原。**這是「有 UI 但功能是假的」,比沒有這個開關更糟。**

---

### T2-10 · 後端會發事件,前端把它丟掉——UI 元件已寫好 513 行 · HIGH · S

**CONFIRMED**。`grep "from ./agentic"` → **0**。

- 後端會發:`router_server.py:3809-3823` 的 `_AGENT_PASSTHROUGH_EVENTS` 含 `interrupt_requested`/`todos_updated`;`tools/ask_user.py:91-96` 的 `ask_user` 工具就是回 `InterruptItem` 觸發它
- SSE 層有接口:`sse.js:75-82` 的 `onInterrupt`/`onResumed`/`onTodos`,`:355` 還有完整的 `streamSessionAnswer()` resume 實作
- UI 元件已寫好:`agentic.jsx` 513 行(`PausedBadge`/`InterruptCard`/`TodoChecklist`/`FollowUpChips`)+ `toolExecution.jsx` 338 行,**兩者 production 零引用**
- `app.jsx` 的 `streamWithAbort` callbacks 只掛 `onText`/`onFinishReason`/`onTrace`/`onMeta`/`onReasoning`

→ 任何 agent 使用 `ask_user`,串流會靜靜結束、留下空泡泡,agent 在後端無限等答案,使用者完全沒有出路。

⚠ 該路自承未確認「目前線上哪個 agent 實際掛了 `ask_user_tool`」→ 觸發機率未知。若無 agent 用它,嚴重度降為「已投入成本閒置」;但**修復成本極低而風險是死路**。

---

### T2-11 · 其他規模化缺口(不逐條展開,附錨點)

| 項 | 錨點 | 嚴重度·成本 |
|---|---|---|
| **混合檢索是半成品**:`keyword_search()` 寫好了、GIN 索引已建,**全 repo 唯一呼叫者是測試**。且即使接上也無效——用 `plainto_tsquery('simple')` 不切 CJK,**全 repo 無任何中文分詞器** | `pgvector_store.py:1080-1130`;`0014:336` | HIGH · S+M(須綁一起做) |
| **零 rerank** | 全 repo 零 `rerank`/`cross-encoder`;`retrieval_service.py:626-628` 只 `sorted()[:top_k]` | HIGH · M |
| **「NV-Embed-V2 是 Matryoshka」是無依據的載重假設**,複製到 6 處,repo 內零 benchmark。整個 RAG 品質壓在一句沒人查證過的話上 | `embedding.py:1-6,36,46`;`0014:74`;`0015:13` | HIGH · **S**(一次量測就能定案) |
| **使用者的讚/倒讚進 DB 就沒人看**:`rating` 欄位有寫入,治理後台 `grep rating` → 0,無彙總 API、無 export | `message.py:27-28` | HIGH · **S** |
| **對話/訊息/trace_span 零保留政策** → 機密級聊天內容永久留存 | `retention_reaper.py` 對 `conversation`/`message`/`trace_span` 全部 0 命中 | HIGH · M |
| **「append-only」只是註解**:實庫實測 `audit_logs`/`policy_decisions`/`classification_events` 三表**零 trigger**,`csp_app` role 有 `DELETE,UPDATE,TRUNCATE`。來源 `0014:129` 的 `GRANT ALL` | `r1_0001:174` 等三處自稱 append-only | HIGH · **S** |
| **附件在分類與 retention 體系之外**:`attachments` 表**無 `classification_level`**(全庫 18 張表有);`get_attachment` 的 `is_admin_tier` 直通與 `search.py:719` 明文「Admin/owner never bypass」矛盾;`retention_reaper` 完全不碰 `ATTACHMENT_STORAGE_PATH` → **機密對話刪了,附件 bytes 還在** | `attachment_service.py:119-134` | HIGH · S+M |
| **`POST /api/attachments` 不驗擁有權** → 跨使用者內容注入 + filename metadata 洩漏(下載端有擋,不是讀取洩漏)。該授權分支 coverage **0%** | `attachments.py:36-46` → `attachment_service.py:57-113` | HIGH · **S** |
| **877 個 HTTPException 只有 3 個結構化**,無 error code、無 exception handler、無 `X-Request-ID`。治理 UI 有 **104 處 / 22 檔**直接插值 `data.detail` → dict/array detail 渲染成 `[object Object]`。登入流程的待核准分支靠**比對中文子字串**(`LoginView.vue:419`) | `proxy/service.py:178-187` 是好範本 | HIGH · M |
| **anilalm 12,805 行零測試**、governance 16,457 行只有 2 個 util 測試、**全案零 linter**、零 E2E | `apps/anilalm/package.json` 無 `test` script | CRITICAL · M |
| **anilalm 完全沒有密等標示元件**(`grep atermark|Badge` → 空),而它正是機密文件與 Studio 產出所在的 app。shell 的 `trust.jsx:543-575` 可直接移植 | | HIGH · M |
| **Studio 產出的 artifact 本體無密等標記、無 AI 生成標示**,`classification_level` 只以 opaque `str \| None` 流過且**預設 None**(與 contracts 宣告的 fail-closed 相反) | `studio_render.py` / `pptx-renderer/server.js` 對 classification 零提及 | HIGH · M |
| **427 行 ASR 檔 byte-identical 複製兩份**,註解自承「改一邊要同步另一邊」。而 repo **沒有 JS workspace**(三個 app 各有獨立 `package-lock.json`) | `diff` 無輸出 | HIGH · S |
| **零 SBOM / 零 CVE 監控 / 零 license 清冊**;`download-intranet-toolkit.sh` 的 `pip download` **無版本無 hash 無 constraints**、image 用 mutable tag 無 digest,而這包會**直接產出生產模型權重** | `:35-55,77` | HIGH · S–M |
| **本國法規零引用**:資通安全管理法/個資法/國家機密保護法/營業秘密法/檔案法 全 repo **0 命中**。治理文件是完整的 ISO 42001 套件(做得認真)但那是國際標準。而五級分類把**兩套法律體系併進同一個 enum**,依據沒有寫下來 | `docs/governance/` 12 份 | HIGH · M |
| **零教育訓練材料**:`docs/guides/` 無終端使用者手冊;`training-records/` 目錄不存在(ISO 42001 自己標紅了);沒有任何文件教管理員怎麼開通 clearance | | HIGH · M |
| **無回滾、無維護模式、升級不會自動先備份**,而 migration 是開機自動跑的 | `deploy-prod.sh:1203-1218` 無 rollback | HIGH · M |
| **ISO 42001 追溯欄位是死欄位**:migration 建了 8 欄(`model_card_url`/`weights_sha256`/`intended_use`/`vv_status`…),**ORM/API/UI 全無**,grep 零命中 | `0035_iso_42001_traceability.py:60-105` | HIGH · **S** |
| **安全銷毀**:`retention_reaper` 一律 `os.unlink`,無覆寫/shred/crypto-erase。合併 T0-2(無 at-rest 加密)→ 極機密內容 unlink 後仍可還原。平台退役程序:零 | `:281,784,899` | MEDIUM · M |

---

## 5. 結構性病灶(不是 bug,是制度)

> 這一節不排工,但它解釋了為什麼上面每一條都是「同一個 repo 裡正確答案已經寫過一次,但沒有回頭覆蓋舊區」。**不處理這四條,清完的債會長回來。**

### S1 · 驗證器先於被驗證物 —— 專案卡住的結構原因

| 驗證器(已完成、品質高、有測試) | 它要驗的東西 | 狀態 |
|---|---|---|
| `model_governance_runtime.bootstrap()`(`main.py:271-275` 硬性 gate) | Gate 5 四個簽章素材 | **無產生程序、無 runbook** |
| `slo_window_verifier.py`(1300+ 行,嚴格 7 天觀測窗 + cadence tolerance) | 6 個 SLO 指標 | **六個全部零 producer**(實測:只出現在 profile schema 與測試檔) |
| `test_restore_drill_verifier.py` | production restore | **restore 路徑不存在** |
| `fault_drill_verifier.py:552`(`"exceeds P0 RTO"`) | RTO/RPO | **值只存在於未產生的 signed profile 需求裡** |
| `production_backup.py` + profile(29 個 surface,寫得完整) | 備份 | **cron 必定失敗、`.env`/私鑰不在包裡** |

**工程能量壓倒性投在「證明它合規的機器」,而被證明的那個東西還沒建。** 這不是六路任何一路的結論,是疊起來才顯形的。

⚠ 這同時修正了一條排序:missing-capabilities 說「無 SLO 定義」是**錯的**——定義存在且相當嚴謹(6 個欄位 + 上下界 + 簽章 profile 契約 + 7 天觀測窗),**缺的是「值」與「producer」**。建議從 S 成本的「定義 SLO」改成 M 成本的「補 6 個 metric producer + 在 signed profile 填閾值」。

### S2 · 沒有人被指派

`roles-responsibilities.md` 的 RACI 表 11 個活動、7 個角色,**全部以職稱代稱,零具名**(ISO 42001 Clause 5.3 要求 assigned responsibilities);`training-records/` 不存在;跨單位技術介面(`.12` gateway / MLSteam / CSPKI CA)**零 owner**;`.env.example` 27 個必填變數沒有一個標註誰簽發。

**→ 這解釋了 S1**:沒有人被指派去產生 Gate 5 素材 / 定義 SLO 值 / 簽 RTO/RPO,所以驗證器只能一直空轉。

而 `.12` 是**另一個團隊**維運的機器,ANILA 的 Gate 5 provider binding 必須與它的 `transport_target` 逐欄相符 → **`.12` 端任何 endpoint 變更都會讓簽章 profile 對不上 → 模型被拒 → 平台無法推論**(且 formal profile 下是 crash-loop)。而 `docs/` 找不到任何與 `.12` 的介面協議、變更通知機制、對口窗口、SLA。**平台最脆弱的耦合點沒有 owner。**

### S3 · 主導反模式:加一層正確實作 → 保留舊 read model → 逐點修

重複了**八次**:`classified` boolean 與 `classification_level`、`platform_links` 與 `registered_service`、`CSP_SERVICE_TOKEN` 與 per-agent credential、`is_legacy` service client、`legacy_runtime_call`、`proxy_service` facade 與 `proxy/` package、100k 與 600k KDF、SQLite 與 Postgres。

**每一次的工程決策本身都是對的**(相容遷移是專業做法),但**沒有一次附帶退場條件、期限,和「最後一個引用點」的清單**。

T0-3 就是這個模式的直接產物:public share 的 boundary 被認出來修好了(還留了警告 docstring),隔壁的 audit gate / 複製 / 匯出 / 分享四個 boundary 沒有——**因為沒人有那張清單**。

**債是散文,不是標記**:真 `TODO`/`FIXME` 只有 **2 個**,但 `legacy` 出現 **559 次、橫跨 147 檔**。團隊的自我認知很誠實(多處 docstring 直接認罪),但**認罪寫在 docstring 裡,沒有任何工具能追蹤它**。沒有 issue、沒有期限、沒有 owner。這是為什麼「這個修好了嗎」在每個 boundary 都得重新查一次。

### S4 · 所有嚴謹度都投在「執行期強制」,零投在「輸入正確性」與「輸出標示」

```
輸入端  → 分類自我宣告、無 reviewer、無內容偵測、無抽查      ← 幾乎不存在
執行期  → clearance/compartment/need-to-know/RLS/CHECK/latch  ← ★★★ 極完整,優於多數商用平台
輸出端  → artifact 無密等標記、無 AI 標示、聊天匯出無標示無稽核、
          anilalm 無浮水印、無列印控制                        ← 幾乎不存在
```

**輸入端的實測**:文件密等**完全繼承知識庫**(`documents.py:351` `_locked_collection_classification`),無 per-document 宣告、無 reviewer(`grep approve|審核|核准|reviewer` 於 `documents.py` → **0**)。而**任何人都能建知識庫**(`collections.py:110-115`)→ **把機密文件丟進自己建的「無機密」知識庫,平台標成無機密,對所有無機密 clearance 的人開放**。降密要兩個人加一份公文文號,誤標低密則零成本、零偵測、零紀錄。全域無內容式偵測(`pii_detect|presidio|auto_classify` → 0)。

**而涉密事故從來不發生在中間層——它發生在「有人標錯了」和「有人把檔案帶走了」。** 這是六路都在測中間層造成的集體盲區。

---

## 6. 讓問題被抓出來,而不是被讀出來 —— 三個 CI gate

> 這三個 gate 到位之後,§2–§4 會變成「被抓出來」而不是「被人讀出來」,而「一邊嚴謹一邊漏」的形態才會停止再生。**先建 gate,再清債。**

**Gate ①:ORM metadata vs 真 Postgres 的 drift 檢查(30 行腳本,已有人寫過)**
一次抓掉四類問題:`alerts` 的 UNIQUE/index/FK 缺失(17 條)、93 個 naive timestamp 欄、`created_by` 的 NOT NULL + SET NULL 矛盾、`startup_migrations.py` 那套 alembic 外機制的全部漂移。順手把 `startup_migrations.py` 折回 alembic,讓 `alembic upgrade head` 自足(DR 前提)。

**Gate ②:至少一條走真 PG 的 smoke path**
**12 檔 / 34 個 `_pg` test function 已經寫好了**,只差 CI 起一個 PG。(⚠ 原文寫「40 個」是把 pytest 的 **skip 數**當成測試數,2026-07-26 複驗修正。)目前測試用 SQLite + `create_all` 不跑 alembic → 三重盲點:migration 漂移測不到、生產缺的 index 在測試裡存在、18 張表的分類 CHECK 只在 PG 有(model 沒宣告 `CheckConstraint`)→ 寫入非法 level 的路徑**測試綠、生產 IntegrityError 500**。

**Gate ③:tokens 對比自動驗證 + ESLint(含 `jsx-a11y`) + bundle 預算**
`packages/tokens/scripts/verify.mjs` **已存在且已接 `npm test`**,擴充成「任何低於 4.5:1(文字)/3:1(UI 元件)的組合 fail CI」——這是唯一能讓 T2-9 不復發的機制,而且純 node 腳本、air-gapped 友善。
`jsx-a11y` 一啟用就自動抓 heading 缺失 / `aria-current` / 對比這一整類。全案**零 linter**是所有前端債能無聲累積的根因。
Vite 的 500 kB 警告要 **fail build**(現在 1,001 kB 只是印出來沒人管;shell 與 anilalm **零 `React.lazy`**,而 governance 24 條 route 全 lazy——又一次「最好的實作不在共用層」)。

**⚠ 三個 CI 事實必須先修,否則 gate 是假的:**
- ~~`apps/csp-governance-ui` 與 `apps/anilalm` 的改動**不觸發任何 workflow**~~ → **本條已推翻(2026-07-26 複驗)**。`gate1-ci.yml:3-11` 無 `paths:` 過濾,對 `main`/`prod-intranet-card` 的 PR 與 push 一律觸發,且 `frontend-governance`(:513)、`frontend-shell`(:533)、`frontend-anilalm`(:553)三個 job 都存在。**真缺口是:`apps/anilalm` 沒有 `test` script,CI 只跑 `typecheck` + 兩次 `build`。** 原主張來自第一輪懷疑論審查 C4,我未複驗即採用——這正是本檔 §5 S3 警告的模式。
- `prod-military-passwd` 與 `trial-military` **完全沒有 CI**(不在 `branches` 白名單。⚠ 依 2026-07-24 範圍決策,此條刻意不修,列欠帳)
- `spanTree.test.jsx:15-21` 的 `try/catch {}` 讓同檔 **13 個「通過」的測試在空轉**;shell 測試永遠只在 node 20 跑 → **CI 結構上永遠看不到這件事**,所以 8 個 localStorage 源碼檔(含 T2-9 那個「主題不持久化」bug 的成因)的驗證缺口是**永久性的**

---

## 7. 被推翻的主張(避免重工與錯誤排序)

> 這一節與清債同等重要。過時的主張會同時造成「重工修已修的東西」與「錯誤的嚴重度排序」。

### 7.1 `CLAUDE.md` §5.1 列為「最嚴重一條」的 codeserver 阻斷項 **已修**

原主張:codeserver 以 RW 掛 repo root、遮蔽清單只有兩條、無 `profiles:`(預設隨 stack 啟動)。

現行 `platform.yml:807-822`:有 `profiles: ["developer-tools"]`、只掛隔離 workspace `../../share/codeserver-sandbox`(非 repo root)、`working_dir` 在 workspace 內、networks 只有 `codeserver-tools`。nginx 兩個平台 server block 都改成 `return 404`。

**同樣過時的還有兩條**:`anila-ops.sh` 的 `BACKUP_DIR` 已改為 `${ANILA_BACKUP_DIR:-$ANILA_STATE_DIR/backups}` 且經 `assert_outside_repo` 強制在 repo 外;「營業秘密對話可建立未登入分享連結」→ `is_publicly_shareable` 已 fail-closed 到只允許 `無機密`。

**仍成立**:`n8n` 與 `gitlab` 確實**無 `profiles:`** → 仍預設隨 stack 啟動,`AGENTS.md §3.3`「交付規格要求移除」的關切未解。

**→ `CLAUDE.md` §5.1 需要重寫。** 它目前把一個**已修的 CRITICAL** 掛在「No-Go」首位,而 original-sins 的 legacy SQLite 威脅模型是建立在這條之上的(該條因此從「首次上線的帳號注入路徑」降級為「無存在理由的 legacy 自動路徑 + 錯誤的 docstring」,純刪除即可)。

### 7.2 第一輪計畫的關鍵數字錯了

| 主張 | 實測 |
|---|---|
| 「373 支測試 vs 0」 | 373 是 repo 全域檔數;`services/csp/tests` 是 **127 檔 / 1412 個 test function** |
| 「CI 會變紅」 | ⚠ **此列本身已被推翻**(見 §6):CI 確實會跑那兩個 app 的 job。真缺口是 anilalm 無 `test` script |
| 「`trial-military` 刪 8 個開發者視圖檔」 | 實際 **33 檔 / 3836 行刪除**,含後端 `config.py`/`proxy.py`/`database.py`,還刪掉兩支後端測試 |
| 「沒有 Python 測試讀 `apps/`」 | `test_gate2_retrieval_governance.py:10-11` **直接字串比對前端檔案**,是 Gate 2 合規證據 → 前端重構會弄紅 Gate 2,屬治理變更需重新核准 |
| 「分類門檻是絕對機密」 | `classified` = **level ≥ 機密**,而唯一的 API 只能建立「機密」→ 照原文實作會造成**淨退化** |

### 7.3 六路之間的矛盾,已裁決

| 爭點 | 裁決 |
|---|---|
| 分享連結是否已修 | **user-journey 對,original-sins 假陰性**(只查讀取端)。`create_share` 從未讀 `ENABLE_PUBLIC_SHARE` |
| anilalm 有沒有 token 檔 | **frontend 對**。`theme/tokens.ts` 存在(1,866 bytes,`accent: '#6361E0'`)→ 不是「接上共用 token」,是**要先拆掉一套既有的、預設深色、紫色 accent 的競爭系統**,成本不同級 |
| SLO 有沒有定義 | **missing-cap 錯**。定義存在且嚴謹,缺的是值與 producer(見 S1) |
| Python 測試能不能跑 | **backend-quality 自己補裝依賴跑成了**(1830/4/40, 77%),而另兩路都說跑不了。→ original-sins 建議「由有環境的人補跑」的工作**已被同批次另一個 agent 做完**,兩份都不知道。⚠ 那 4 個失敗的分類**只有一份證據來源、無交叉驗證** |
| `spanTree.test.jsx` 在 CI 是紅還是綠 | **可以定論:永遠是綠的**。shell 測試只在 node 20 跑。所以那個驗證缺口是**永久性**的,不是暫時的——比兩路各自的描述都更嚴重 |
| anilalm vs shell 的 sealed RAG | **兩者都對(不同 app)**,但合起來的結論是兩路都沒說出來的:**CSP 的 per-request RAG scoping 只有 1/2 的聊天介面接上**,而 shell 是使用者的日常入口 |

### 7.4 明確做得好、不要動的

CI 其實相當完整(gate1 有 postgres service、逐一餵 DSN、還有 `check_gate1_test_governance.py` 在治理 skip 標記);Full-Trace 已接線;Redis 已持久化;RAG prompt 的 injection 防護**優於 Open WebUI**(來源轉義、明寫「來源是不可信資料」、零命中誠實規則);SSRF guard 25 處且 call-time 也跑(TOCTOU 防護);23 個無 auth dependency 的 endpoint 逐一查證全部刻意;`r1_*` migration 系列品質很高(79 支**全部有 `downgrade()`**,無空殼);樂觀更新回滾有細緻測試;`classifyRetryQueue.js` 檔頭記錄了「過去用 `.catch(() => {})` 吞掉失敗」的教訓;arena 級的自我修復(孤兒 `selectedConvId` 自癒、focus 重抓 agent、classify retry queue)。

**結構性判讀**:團隊做得到,問題是**這種嚴謹只施加在安全相關路徑上**——一般 UX 路徑(附件、編輯、錯誤呈現、主題持久化)沒有同等待遇。

---

## 8. 排程建議

> 不是承諾,是排序。**Wave 0 先建 gate,因為沒有 gate 的清債會長回來。**

### Wave 0 · 護欄(約 1 週,全部 S)
1. Gate ①ORM/PG drift 腳本進 CI
2. Gate ②CI 起一個 PG,接上已寫好的 12 檔 / 34 個 `_pg` test function
3. Gate ③ESLint(含 `jsx-a11y`)+ tokens 對比驗證 + bundle 預算 fail build
4. 修三個 CI 事實(governance/anilalm 觸發、軍方分支 CI、拿掉 `spanTree.test.jsx:16-20` 的 try/catch + `.nvmrc`)
5. **T1-5 前端錯誤邊界**(CRITICAL/S,投報率第一)
6. 改寫 `CLAUDE.md` §5.1(移除已修的主張,避免錯誤排序)

### Wave 1 · 停止說謊(約 1–2 週)
1. **T0-3** 五個 gate 改吃 `classification_level` + 匯出加密等頁首 + `ExportRecord` 記聊天匯出 + print stylesheet(**S,資料已在前端**)
2. **T0-1** 附件止血:非圖片明確擋掉並導向知識庫(**數小時**)
3. **T0-2** 「加密模式」改措辭(**S,零功能改動**)
4. **T0-4** API 邊界 TZ 正規化 + 共用 formatter(**S**);93 個欄位收進 migration 跟 Gate ① 一起
5. **T0-6** 治理稽核補日期範圍/分頁/匯出(照隔壁 `admin_inference_audit.py` 抄)+ `log_audit_event` 加 strict + `ANILA_AUDIT_STRICT` 進 formal posture
6. **T0-5** 備份:修 `--full`、20 個 env 進 `.env.example`、搬 `age`、**改正 runbook 那三處與實作相反的敘述**
7. 建立「legacy `classified` 最後引用點清單」並加 lint(讓 S3 反模式停止再生)

### Wave 2 · 停止流血(約 2–3 週)
1. **T1-4** RAG 熱路徑改用已寫好的 batch API(**S,幾乎免費**)+ 池參數走 env + `embed_query` 的兩個 session 在 await 前關掉
2. **T1-3** `upload_zip` 包 `asyncio.to_thread`(照 8 處前例)
3. **T1-1** `messages` 加 `parent_id`;編輯路徑補 audit + classification + legal_hold;前端改用 `buildMessageHistory`
4. **T1-2** 持久化順序改成「先寫 user 訊息、串流中 checkpoint、失敗用 append 不用取代」+ 錯誤訊息 `JSON.parse` + 加重試鈕
5. **T2-8** 引用與 markdown 共存(**S,最划算**)
6. **T1-8** `startup_migrations.py` 折回 alembic
7. 補負載測試(k6 三條 profile:純聊天 / RAG 聊天 / 上傳+索引),把 p95 與飽和點寫進 Gate 6 acceptance ——**沒有這個,前六項修完你也不知道修夠了沒**

### Wave 3 · 能上線(約 4–6 週)
`access_grant` 表收斂 + Preview Access 視圖(T2-1)、知識庫分享 helper(T2-2)、metrics + log 輪替 + 告警通知(T2-3)、配額與並行上限(T2-4)、離職 transfer + 硬刪 pre-flight(T2-5)、自助改密碼 + help 入口 + onboarding(T2-6)、搜尋/產出/分享三條找回路徑(T2-7)、Gate 5 operator runbook(T1-7)

### Wave 4 · 制度(並行,非工程)
指派 S1 那五個驗證器各自的「產生器 owner + 期限」,或明確凍結該 Gate;RACI 具名;`.12` / MLSteam / CSPKI 三個跨單位介面各指定對口與變更通知機制;補本國法規對映表;建 `training-records/` 與終端使用者手冊。

**⚠ 樣式策略必須在 Wave 2 之前拍板**(見 §1 末),否則所有前端投資會做兩次。

---

## 9. 誠實聲明

1. **DB introspection 全部來自執行中的 dev stack**(`anila-platform-dev-csp-db-1`,alembic head `r1_0034`,僅唯讀 SELECT,未動任何容器)。與 `.15` 走同一條 migration chain,**但不是生產本身**。`alerts` 缺 UNIQUE、93 個 naive 欄、`service_token_legacy_env_used` 3269 筆這類觀察**須在 `.15` 另跑同一組查詢確認**(我預期 schema 類相同,因為缺失來源是 migration chain 本身;runtime 類則必須另核)。
2. **未實跑部署、未動任何 `anila-platform-*` 容器。**
3. **pytest 只有一路跑成**(Python 3.10 主機,生產容器是 3.11),那 4 個失敗的分類**無交叉驗證**;第 4 個(`test_refresh_reuse_cross_sid`)無定論。
4. **未開瀏覽器**。T2-9 的對比值是腳本算的,不是量螢幕;深色底取 `index.html:75` 的 `oklch(0.16 0.004 270)`。**D2 的三個 token 值未重算**,鑑於 D1 有 1.347 倍系統性偏差,進 CI 門檻前必須重算。
5. **`.15` 的實際 runtime 姿態看不到**(是否已手動加 log rotation / TZ / 監控 / Gate 5 素材 / `age`)。所有部署類判定都是 repo 內姿態。
6. **法規細節未逐條核對法源條文**。我驗證的是「repo 內零引用」這個可重現事實與由此推導的缺口;具體條號、通報時限、資安責任等級判定,以及中科院是否受資通安全管理法規範、內部系統是否適用無障礙規範——**這些是組織事實,不是程式碼事實**,需法務/資安權責人確認。
7. **T2-10 的 `ask_user` 實際觸發機率未確認**;**T0-1 的 attachment 幻覺未實機重現**(程式路徑三處零命中已確認);**列印浮水印是否消失是推論**,未開瀏覽器實測。
8. **`myCSPPlatform/`、`scraps/`、`runtime_logic/`、`cht/` 四個目錄未審查**(不在六路範圍)。若其中有進入交付包的內容,供應鏈與授權結論可能需要擴大。
9. **Open WebUI 的分析全程引用其程式碼 0 行**,一律以 pseudo-schema 與散文描述結構——因其授權條款禁止在超過 50 名使用者時移除品牌,方針是**只學設計、不引入實作、不部署其本體**。
10. **workflow 的第 9 個 agent(合成排序 backlog)因 API 529 失敗**,本檔的排序與合成是我自己做的,並非該 agent 的產出。§2 兩條頭條(T0-3 的匯出/複製 gate、T0-2 的加密命名)以及 T1-1 的編輯硬刪路徑,我逐行複驗過。
