# OE-2 Spec-Conformance Audit — Domain C: POLICY / CLASSIFICATION

> ⚠ 2026-08-17 盤點：本檔為【歷史紀錄】——稽核報告,保留當時的判定與依據,不代表現況。現行狀態與執行順序見 `PLAN.md`。

- **Spec authority**: `/home/c1147259/桌面/ANILA/anila-restart-20260729/ANILA/SYSTEM-MAP.md` (399 lines), read in full.
  Line 4: 「**這份取代先前所有規劃文件的權威地位。** 與其他文件衝突時以本檔為準。」
- **Code root**: `/home/c1147259/桌面/ANILA/anila-restart-20260729/ANILA/services/csp/`
- **Method**: per-construct. Every Q1 verdict is backed by a grep over the actual spec text (terms logged), never by the code's own docstrings.
- **Read-only**: no repo file was modified.

## Grep log (Q1 search terms run against SYSTEM-MAP.md)

`降級` `降密` `解密` `改密` `升密` `密等` `分級` `機密` `營業秘密` `列管` `浮水印` `稽核`
`append-only` `防竄改` `核准` `權責` `審批` `雙人` `公文` `簽呈` `盤點` `上限` `ceiling`
`閂鎖` `latch` `政策` `policy` `裁決` `trace`

Zero hits: `降級` `降密` `審批` `雙人` `公文` `簽呈` `盤點` `ceiling` `閂鎖` `latch` `政策` `policy` `裁決` `權責`(only L371, unrelated).

## Canonical spec quotes used below

| Tag | Line | Verbatim |
|---|---|---|
| S-ops | 13–15 | 「**最重要的約束:這個系統由一個人維運。** 偶爾有一位幫忙。/ 任何需要專人照顧的機制(守衛、儀式、多步驟部署)都是負債,/ 除非它換來的東西大於「一個人要維護它」的成本。」 |
| S-mem | 189 | 「⚠ **對話中途升密 → 之前萃取的記憶要撤回** → 每則記憶要記得來源對話,才找得到」 |
| S-trace | 211 | 「→ 一張表加幾個索引。**不需要 span 樹、parent 關係、trace id。**」 |
| S-record | 227 | 「密等在**專案啟動時就標好了**,平台主要是**記錄**它。」 |
| S-levels | 228 | 「實際會碰到:**無機密 / 營業秘密 / 密 / 機密**。」 |
| S-lines | 241–242 | 「- 可以做 = 密等 ≤ **營業秘密** / - 要落稽核 = 密等 ≥ **營業秘密**」 |
| S-oldgate | 244 | 「(現行程式碼是統一判準 `> 無機密` 全部管制,所以營業秘密現在不能分享 —— 要改。)」 |
| S-mark | 267 | 「- collection 與 agent 上有一個**列管標記**,存取它的人與動作全部記錄」 |
| S-honest | 268 | 「- **UI 上的字必須誠實** —— 不可以寫「已加密」,要寫「列管」或「受控存取」」 |
| S-auditwhat | 275 | 「\| 記什麼 \| 讀取受控文件/對話、上傳、刪除、**改密等**、匯出、列印、分享、登入登出、呼叫模型/agent、管理動作 \|」 |
| S-auditwho | 277 | 「\| 誰查 \| admin 在後台查;**要能匯出給稽核單位**;未來可能上資安中心 SOC \|」 |
| S-tamper | 278–280 | 「\| 防竄改 \| **要** —— 明確是為了防止 admin 權限的人偷偷做假 \|」 / 「> ⚠ **append-only 的稽核帳因此是必要的,不是過度設計。** 威脅模型包含特權內部人。」 |
| S-wipe | 323 | 「\| 現有資料 \| 有真實對話,**沒有知識庫,全部可刪** → 資料庫可以砍掉重來 \|」 |
| S-a02 | 347 | 「\| 「加密模式」宣稱加密但沒加密 \| A02 加密失效 \|」 |

---

# 1. Construct table

Legend — **Action**: `keep+re-cite` / `keep+debt` / `converge`.

## CAT-A — spec-required (keep, re-cite SYSTEM-MAP)

| # | Construct | file:line | Doc ref in source | Q1 evidence | Q2 cost | Action |
|---|---|---|---|---|---|---|
| A1 | `ClassificationLevel` as an **ordered** type (`rank`, `__lt__`, `total_ordering`) | `app/schemas/contracts/classification.py:35-53,90-93` | doc 08 §1 | S-lines L241–242 requires `≤ 營業秘密` / `≥ 營業秘密` comparisons ⇒ an ordered level type is required. S-levels L228 gives an ordered set. | 低;單一型別 | keep+re-cite（**成員集合本身走 OE-3**） |
| A2 | `from_storage` / `to_storage` round-trip with **fail-closed unknown value** | `classification.py:74-87` | doc 08 §1 | S-lines L241–242: gates are level-driven; an unparseable level must not silently pass a gate. Fail-closed = project security posture. | 低 | keep+re-cite |
| A3 | `max_of()` 單向閂鎖 helper | `classification.py:55-64` | doc 08 §2 | S-mem L189「對話中途**升密**」— a conversation's level rises in-flight and must stick; propagation must never silently lower it. | 低（純函式） | keep+re-cite |
| A4 | `apply_classification()` — 升級寫事件 + 更新資源等級 | `app/modules/policy/service.py:273-333` | doc 08 §2/§5 | S-mem L189 + S-record L227「平台主要是**記錄**它」+ S-auditwhat L275「**改密等**」必落稽核。 | 中：是全系統唯一寫入分類的路徑（5 個呼叫端） | keep+re-cite |
| A5 | `effective_level()` 讀當前等級（未知資源 fail-closed） | `service.py:336-341` | doc 08 §2 | S-lines L241–242：閘門要先讀得到等級。 | 低 | keep+re-cite |
| A6 | `ClassificationEvent` 表本身（resource/previous/new/reason/actor/created_at） | `app/models/classification.py:52-80`；`migrations/.../r1_0003:120-145` | doc 08 §6 | S-auditwhat L275「改密等」必記；S-tamper L278–280 append-only 帳為必要。 | 中：append-only 表 + 索引 | keep+re-cite（**但見 GAP-1**） |
| A7 | `conversations.classification_level` / `messages.classification_level` | `r1_0003:68-74,235-245`（`_FULL_COLUMN_TABLES`） | doc 08 §5 | S-mem L189；S-auditwhat L275「讀取受控文件/**對話**」。 | 低（NOT NULL + server_default） | keep+re-cite |
| A8 | `ingestion_collections.classification_level` | `r1_0003:68-74,235-245` | doc 08 §5 | S-mark L267「**collection** 與 agent 上有一個列管標記」。 | 低 | keep+re-cite |
| A9 | `agents.default_classification_level` | `r1_0003:249-258` | doc 08 §3 bridge | S-mark L267「collection 與 **agent** 上有一個列管標記」。 | 低 | keep+re-cite |
| A10 | `ingestion_documents.classification_level` | `r1_0003:68-74,235-245` | doc 08 §5 | S-auditwhat L275「讀取**受控文件**」— 要判斷是否受控就要有等級。 | 低 | keep+re-cite |
| A11 | `evaluate_classification_ceiling()` 純函式 | `service.py:152-164` | doc 08 §10 | 無直接條文（searched: ceiling, 上限 → L220 是額度上限,非分類）。**基礎 = OE-1 既決「classification_ceiling confirmed KEEP」**。 | 低 | keep+re-cite（引 OE-1 裁決,非 SYSTEM-MAP） |
| A12 | access_control **Step 6**：ceiling 綁所有 tier,admin bypass 不解除 | `app/services/access_control.py:76-99`（尤其 :97-99 的順序） | doc 07 §12 | 同 A11（OE-1）＋ **專案紅線**：admin 特權不得繞過分類閘門（S-tamper L278「防止 admin 權限的人偷偷做假」）。 | 低 | keep+re-cite |
| A13 | 分類異動落 **audit log**（`log_audit_event` 於降級路徑） | `service.py:415-423,524-533,538-553` | doc 08 §12 | S-auditwhat L275「改密等」。 | 低 | keep+re-cite（**行為保留,承載它的流程走 C1**） |
| A14 | `require_admin` on `GET /api/policy-decisions` / `/api/classification/inventory` | `app/modules/policy/router.py:92`；`app/api/classification_inventory.py:186` | doc 03 §11 | S-auditwho L277「admin 在後台查」。 | 低 | keep+re-cite |

## CAT-B — no spec basis, no ongoing cost (keep, mark debt)

| # | Construct | file:line | Doc ref | Q1 evidence | Q2 cost | Action |
|---|---|---|---|---|---|---|
| B1 | `ClassificationEvent.inherited_from_resource_type` / `_resource_id` | `models/classification.py:77-78`；`r1_0003:129-134` | doc 08 §6 | no basis（searched: 繼承, 來源對話, 記憶 → L189 要求「每則**記憶**要記得來源對話」,那是 memory 表的欄位,不是分類事件的） | 零：**全 codebase 從無寫入**（grep 確認),永遠 NULL | keep+debt |
| B2 | `DeclassificationStatus.CANCELLED` | `contracts/classification.py:123` | doc 08 §8 | no basis（searched: 降級, 取消） | 零：無任何碼路徑產生此值 | keep+debt（隨 C1 一併移除） |
| B3 | `DeclassificationRequest.resulting_resource_id`（降密副本） | `models/classification.py:129`；`r1_0003:174-176` | doc 08 §9 | no basis | 零：in-place 生效,永遠 NULL | keep+debt（隨 C1） |
| B4 | `ClassificationAuthorityAssignment.department_id` | `models/classification.py:156-159` | doc 08 §7.3 | no basis（searched: 權責 → 僅 L371「單位管理員的權責規範 \| 規劃中」= 待議） | 低但非零：對 `departments.id` 的 FK,P1.1 剛把 departments 改成樹,任何 department 重整都要考慮這張表 | keep+debt（隨 C2） |
| B5 | access_control **Step 7/8**（project membership / launch policy）MVP no-op | `access_control.py:23-27,107` | doc 07 §12 | no basis（SYSTEM-MAP 無 project membership 概念） | 零：只有 docstring 與一行註解,無執行碼 | keep+debt（改寫 docstring 時順手刪） |
| B6 | `document_chunks` 四欄分類欄位 | `r1_0003:68-74,235-245` | doc 08 §5 | no basis（chunk 非 SYSTEM-MAP 概念） | 零：ORM 未建模,寫入路徑「後續 slice」= 從未接線 | keep+debt |
| B7 | `task_runs` 三欄分類尾欄 | `r1_0003:76,246-247` | doc 08 §5 | no basis | 零：無讀取者 | keep+debt |
| B8 | `PolicyDecision.classification_level` | `models/policy_decision.py:74-75` | doc 03 §5 | no basis | 零但**是缺陷**:`record_decision()` 從不寫它 → 永遠 `無機密`;`PolicyDecisionOut.classification_level` 卻是必填非選（`contracts/policy.py:68`） | keep+debt（隨 C3 移除） |
| B9 | `classification_source` 自由字串（10 張表） | `service.py:323,459`；`models/*.py` | doc 08 §5 | no basis | 低:有寫無讀（grep:零查詢者）,但值是自由字串,已出現 5 種寫法（`propagation`/`content_detection`/`legacy_backfill`/`artifact_inheritance`/`declassification_approved`) | keep+debt |
| B10 | `app/modules/policy/__init__.py` 的 doc 02/doc 08 出處敘述與公開面清單 | `__init__.py:1-54` | doc 02 §1、doc 08 | 檔案本身無行為構件,只有 re-export + docstring | 零 | keep+re-cite（docstring 要改引 SYSTEM-MAP §8;`__all__` 隨 C1 縮） |
| B11 | `can_access_link` / `accessible_links_for` legacy 別名 | `access_control.py:158-162` | (無 doc 引用) | 非本域構件（Service Registry 域） | 零 | 不處理（跨域） |

## CAT-C — no spec basis, ongoing cost, doc-only justification (converge candidates)

### C1 — 降級申請治理流程（申請單 + 雙人原則 + 紙本代錄 + 4 個端點）

- **file:line**：
  - `app/modules/policy/service.py:363-429`（`create_declassification_request`）
  - `service.py:432-462`（`_apply_approved_declassification`,唯一繞過閂鎖的內部路徑）
  - `service.py:465-598`（`decide_declassification`,含雙人原則、權責 fail-closed、紙本三欄）
  - `app/modules/policy/router.py:112-291`（`POST ""` / `GET ""` / `POST /{id}/approve` / `POST /{id}/reject`）
  - `app/schemas/contracts/classification.py:113-137,149-206`（3 個 enum + 4 個契約型別）
  - `app/models/classification.py:83-131`（`declassification_requests` 18 欄）
  - `migrations/versions/r1_0003_five_level_classification.py:148-201`
- **Doc ref**：doc 08 §7 變體 A、§8、§9、§12;doc 09 §11;ADR-0005
- **Q1**：**no basis found**（searched: `降級` 0 hits、`降密` 0、`解密` 僅 L265「現階段不做真的加解密」、`核准` 全部 4 處都是**帳號**核准 L63/L73/L92/L95、`權責` 僅 L371「單位管理員的權責規範｜規劃中」、`審批` 0、`雙人` 0、`公文` 0、`簽呈` 0）。
  最接近的條文是 S-auditwhat L275「**改密等**」——它要求**改密等要落稽核**,不要求申請/批核流程。
  反向條文:S-record L227「密等在**專案啟動時就標好了**,平台主要是**記錄**它。」
- **Q2 成本（高)**：
  - 這是系統中**唯一**能把等級降下來的路徑。誤標一則對話為機密之後,修正需要:第二個人 + 該人先被授予「機密審批權責」(見 C2)+ 走 4 個 HTTP 端點。
  - `decide_declassification` 的雙人原則明寫「**系統內無例外**」(`service.py:504`),與 S-ops L13「這個系統由**一個人**維運。偶爾有一位幫忙」直接衝突 —— 幫手不在的日子,分類錯誤不可修正。
  - 18 欄的申請單表 + 3 個封閉 enum + 4 個契約型別要長期維護。
- **Q3**:是,doc 08 §7 變體 A 是唯一存留理由。
- **Category**:**CAT-C**
- **Recommended action**:**converge** —— 保留「分類等級可被 admin 修正」+「改密等落稽核」(A13 的行為),收斂為**單一 admin 動作 + audit event**;移除申請單表、雙人儀式、紙本代錄三欄、4 個端點。
- ⚠ **EXEC CHECK**:派工說明把「governed declassification (applicant ≠ approver)」列為 hard spec requirement。我在 SYSTEM-MAP 全文找不到任何降級/批核條文(上列 grep 結果為證)。若擁有者確認雙人降級是**口頭**需求而未入 SYSTEM-MAP,則此條轉 CAT-A 並應補進 SYSTEM-MAP §8;在補進之前,依「SYSTEM-MAP 是唯一權威」的規則,它就是 CAT-C。**這一條請擁有者裁決,不要由執行者自行合併。**

### C2 — 「機密審批權責」指派表 + 雙人控制的授予/確認/撤銷端點

- **file:line**:
  - `app/models/classification.py:134-171`（表）
  - `app/modules/policy/service.py:344-360`（`has_declassification_authority`）
  - `app/modules/policy/router.py:294-442`（`GET` / `POST` owner-only / `POST /{id}/confirm` 第二人 / `DELETE` soft-revoke）
  - `app/schemas/contracts/classification.py:209-240`
  - `migrations/versions/r1_0003_five_level_classification.py:204-232`
- **Doc ref**:doc 08 §7 第 2–3 點、§12
- **Q1**:**no basis found**（searched: 權責、審批、信任錨、指派 → L63/L74 的「指派」全指 agent/模型權限;L371 明示單位管理員權責規範「規劃中」= 尚未有需求）
- **Q2 成本（高)**：一張**必須有人維護的名冊**;授予要 owner + 另一名 admin 確認（`router.py:391-395` 明擋登錄人自我確認）+ 公文文號。一人維運下,bootstrap 這張表本身就需要兩個帳號。這正是 S-ops L14「需要專人照顧的機制(守衛、**儀式**、多步驟部署)都是負債」。
- **Q3**:是。
- **Category**:**CAT-C**
- **Recommended action**:**converge**（與 C1 同一包）。若 C1 被裁決保留,C2 至少應塌縮為「admin role 即權責」,移除授予/確認/撤銷三端點與整張表。

### C3 — `PolicyDecision` 第二套治理帳（表 + 九值 action enum + 查詢端點）

- **file:line**:
  - `app/models/policy_decision.py:44-76`（表 + 2 索引）
  - `app/schemas/contracts/policy.py:20-71`（`PolicyAction` 九值、`PolicyDecisionVerdict` 三值、`PolicyActorType`、`PolicyDecisionOut`）
  - `app/modules/policy/service.py:86-149`（`record_decision`,含 deny 必附 reason、立即 commit）
  - `app/modules/policy/router.py:76-109`（`GET /api/policy-decisions` + 5 種過濾 + 分頁）
  - 寫入端（跨域,收斂時要一起改）:`app/services/proxy/ceiling.py:104,126`、`app/services/proxy/task_link.py:160`、`app/api/services.py:384,442`、`app/api/artifacts.py:374`、`app/modules/policy/router.py:233`
- **Doc ref**:doc 03 §5、doc 03 Done Criteria 4、doc 03 §11
- **Q1**:**no basis found**（searched: 政策 0、policy 0、裁決 0）。**間接檢查稽核段**:S-auditwhat L275 要求「呼叫模型/agent、管理動作」落稽核 —— 但 SYSTEM-MAP 只認**一本**帳:S-auditwho L277「admin 在後台查;要能匯出給稽核單位」、S-tamper L278–280「防竄改…**append-only 的稽核帳**」(單數)。
  ⇒ **記錄這些動作是規格要求;把它們記到 `audit_log` 之外的第二張表、配第二個 admin 查詢面、且不受防竄改保護,不是。**
- **Q2 成本（高)**：
  - 每個新的閘門都要記得同時寫 `audit_log` 與 `policy_decisions`,漏一邊就有帳對不上。
  - 九值封閉 enum:每加一種被管制的動作就要改 enum + 契約 + 測試。
  - 兩個 admin 查詢面（`/api/audit-logs` 與 `/api/policy-decisions`）—— S-auditwho 只要求一個。
  - `ceiling.py:20-25` 已為量太大而發明「legacy allow 不落列」的第二套抑制規則,治理帳自此不完整。
- **Q3**:是,doc 03 §5 是唯一理由。
- **Category**:**CAT-C**
- **Recommended action**:**converge** —— 把 `record_decision` 的內容折進規格要求的單一 append-only 稽核帳（L275–280）,移除 `policy_decisions` 表、九值 enum 與 `GET /api/policy-decisions`。**需與 audit 域協同**（`audit_log` 不在本域檔案集內)。

### C4 — 機敏分類盤點端點 `/api/classification/inventory`（含 CSV 匯出）

- **file:line**:`app/api/classification_inventory.py:1-202`（整檔;`_RESOURCES` 8 型別表 :88-104、`_row_for` :107-157、`_to_csv` :167-180、端點 :183-202）
- **Doc ref**:doc 08 §15「Classification Inventory Before Cutover(✅ 已拍板 v0.2)」
- **Q1**:**no basis found**（searched: 盤點 0 hits、切換、cutover、報表 → L313「要給長官的**月報表**」是用量報表,不是分類盤點）。
  **反向條文（決定性）**:S-wipe L323「現有資料｜有真實對話,**沒有知識庫,全部可刪** → 資料庫可以砍掉重來」。本端點存在的唯一目的是「切到五級分類**前**驗證既有資料 backfill 一致」——那個 cutover 已經因為資料庫要砍掉重來而不存在。
- **Q2 成本（中）**：硬編碼 8 個資源型別 × 5 個等級欄位（`:57`、`:88-104`),OE-3 改等級集合必改此檔;CSV 表頭也是硬編碼中文欄位;`inconsistent` 檢查綁死舊 boolean 語意（見 C5）。
- **Q3**:是。
- **Category**:**CAT-C**
- **Recommended action**:**converge**（整檔刪除,連同 `app/api/router.py:35,82` 的掛載與 `tests/test_classification_inventory.py`）。若擁有者仍想要一份「哪些資源被列管」的營運視圖,那是新需求,應以 S-mark L267 為據重新定義,不要沿用 cutover 盤點的形狀。

### C5 — 舊 boolean latch 鏡射（`classified` / `classification_inherited` compatibility read model）

- **file:line**:`app/modules/policy/service.py:214-232`（`_mirror_legacy_boolean`）、`service.py:325-330`（`memory_inherited` 旗標鏡射）、`service.py:461`;backfill `r1_0003:260-300`
- **Doc ref**:doc 08 §3 / §15 Step 3
- **Q1**:**no basis found**（searched: 相容, compatibility, 舊欄位 → 無）。反向:S-wipe L323 資料庫可砍掉重來 ⇒ 沒有需要 bridge 的舊資料;S-a02 L347「「加密模式」宣稱加密但沒加密｜**A02 加密失效**」+ S-honest L268「不可以寫「已加密」」⇒ 這條鏡射鏈的另一端 `agents.requires_encryption` 是 SYSTEM-MAP 點名的**待修缺陷**,不是要維持相容的目標。
- **Q2 成本（中高)**：每次 latch 都要同步兩種表示（等級 + boolean),兩者不一致就是 bug;C4 的整個 `inconsistent` 欄位存在的理由就是量測這條鏡射有沒有壞掉。鏡射規則 `classified = level >= 機密`（`service.py:225`）同時是 OE-3 的爆點之一。
- **Q3**:是。
- **Category**:**CAT-C**
- **Recommended action**:**converge** —— 刪 `_mirror_legacy_boolean`,以 `classification_level` 為唯一事實來源。⚠ **跨域依賴**:`conversations.classified` 仍被 `app/api/public_share.py:58-67`、`app/api/conversations.py:292,316-317`、`app/services/conversation_service.py:304-390` 讀取（分享閘門與讀取稽核就掛在它上面）。這些是 OE-4 要動的同一批位置 → **建議 C5 與 OE-4 合併成同一個工作包**,一次把閘門從 boolean 換成等級門檻。

### C6 — `ClassificationEvent.trace_id` 與每次 latch 的額外 Task 查詢

- **file:line**:`app/models/classification.py:79`;`app/modules/policy/service.py:254-258`（`db.query(Task.trace_id)...scalar()`);`r1_0003:135`
- **Doc ref**:doc 08 §6
- **Q1**:**no basis found**;且有**反向條文** S-trace L211「**不需要 span 樹、parent 關係、trace id。**」
  ⚠ 誠實標註:L211 的上下文是 §7 用量與配額,不是 §8 分類。條文不直接管分類事件,但它是 SYSTEM-MAP 對 trace id 這個概念唯一的表態,而且是否定的。
- **Q2 成本（低–中）**:每次分類升級多打一次 DB SELECT;欄位無任何讀取者（grep 確認)。
- **Q3**:是。
- **Category**:**CAT-C**（低優先）
- **Recommended action**:**converge**（刪欄位 + 刪 `service.py:254-258` 的查詢）。

### C7 — `classification_event_id` 反向 FK（掛在 10 張表上）

- **file:line**:`r1_0003:99-115`（`_add_common_tail_columns` 對 8 表)、`r1_0007:86,171`（artifacts/export_records）;寫入 `service.py:324,460`
- **Doc ref**:doc 08 §5
- **Q1**:**no basis found**
- **Q2 成本（中）**:10 個 FK 約束指向 `classification_events`,永久綁死該表不可刪、不可重建;**零讀取者**（grep:只有寫入與一行測試斷言 `tests/test_classification_upgrade.py:154`)。事件歷史本來就可用 `(resource_type, resource_id)` 索引查（`models/classification.py:56-62`),這個反向指標是冗餘。
- **Q3**:是。
- **Category**:**CAT-C**（低優先;動它要一支 migration 掃 10 張表,成本不對稱 → 可排在 OE-3 的 migration 一起做）
- **Recommended action**:**converge**（併入 OE-3 的 migration 批次）

### C8 — append-only「命名警察」測試（字串比對守衛）

- **file:line**:`tests/test_policy_module.py:167-203`（`MUTATOR_MARKERS = ("update","delete","modify","purge","revoke","overwrite","edit","remove")`,對三個模組的所有公開名稱做子字串比對）
- **Doc ref**:doc 03 append-only 公開面
- **Q1**:**no basis found**。S-tamper L278–280 要求的是**稽核帳** append-only + 防竄改,不是「模組公開函式名不得含 revoke 等字」。
- **Q2 成本（中）**:這是純儀式守衛（S-ops L14 的定義）。實證:撤銷權責的端點被迫命名為 `deactivate_classification_authority`（`router.py:415`)而非 `revoke_...`,純粹為了閃過字串比對 —— 測試在指揮命名,而它擋不住任何真的改寫（`decide_declassification` 就在改 `DeclassificationRequest` 的列,名字裡沒有禁字所以通過)。
- **Q3**:是。
- **Category**:**CAT-C**
- **Recommended action**:**converge**（刪除 `TestAppendOnlySurface` 三個測試;真正的 append-only 保證應由 DB 層 grant/trigger 提供,那屬 audit 域）

---

# 2. GAPS —— 規格要求但碼**沒做**（不是過度設計,是缺口）

> 這幾條是「wrong converge verdict cuts a genuine requirement」的反面:規格有、碼沒有。列在此以免收斂時被一起清掉。

| # | 缺口 | 條文 | 現況 |
|---|---|---|---|
| GAP-1 | **升級分類不落 audit log** | S-auditwhat L275「**改密等**」 | `apply_classification`（`service.py:273-333`)只寫 `classification_events`,**完全不呼叫 `log_audit_event`**。只有降級路徑（`service.py:415,524,538`）進 audit。⇒ 「對話升密」這個最常發生的改密等動作,不出現在 admin 查得到、可匯出給稽核單位的那本帳（S-auditwho L277）。**收斂 C3 時要一併把升級接進單一稽核帳。** |
| GAP-2 | **列管資源的存取未全記錄** | S-mark L267「collection 與 agent 上有一個列管標記,**存取它的人與動作全部記錄**」 | 目前只有 conversation 有 `log_classified_access`(`app/services/conversation_service.py:352-368`,且綁在舊 boolean 上）。collection / agent / document 的讀取沒有對應的稽核寫入。 |
| GAP-3 | **`PolicyDecisionOut.classification_level` 必填但來源永不寫** | — | `contracts/policy.py:68` 宣告為必填非選;`record_decision` 從不設值,靠 DB `server_default="無機密"` 兜。與 P1.4 記下的 `UserResponse.updated_at` 同型風險:外部工具/migration 灌入 NULL 就 500。隨 C3 移除即消。 |
| GAP-4 | **`requires_encryption` 是 SYSTEM-MAP 點名的缺陷,卻是本域 backfill 的來源** | S-a02 L347、S-honest L268 | `r1_0003:293-300` 用 `agents.requires_encryption` backfill `default_classification_level`;`classification_inventory.py:98` 用它做一致性檢查。修 A02 時這兩處要同步。（欄位本身在 agents 域） |

---

# 3. OE-3 impact surface —— 等級集合從 5 級改 4 級會動到的每一處

> 現況 5 級 `無機密/營業秘密/機密/極機密/絕對機密`;SYSTEM-MAP L228 為 4 級 `無機密/營業秘密/密/機密`。
> ⚠ 最大陷阱:**「機密」在兩套集合裡的序位不同**（5 級 rank=2 中段;4 級是**最高級**）。所有寫死 `>= 機密` 的比較語意會反轉。

### 3.1 Enum 定義與衍生
| file:line | 內容 |
|---|---|
| `app/schemas/contracts/classification.py:39-43` | 五個成員的字面值（**唯一事實來源**） |
| `app/schemas/contracts/classification.py:90-93` | `_RANKS` 由宣告順序推導（跟著自動變,不需改） |
| `app/schemas/contracts/classification.py:66-72` | `from_legacy_classified` → `CONFIDENTIAL`(「機密」語意反轉的第一個爆點） |
| `app/modules/policy/__init__.py:5` | docstring 列舉五級（文字） |
| `app/schemas/contracts/classification.py:6` | docstring 列舉五級 + rank 0–4（文字） |

### 3.2 寫死 `機密` 的比較（**語意反轉風險最高**）
| file:line | 內容 |
|---|---|
| `app/modules/policy/service.py:225` | `now_classified = level >= ClassificationLevel.CONFIDENTIAL`（舊 boolean 鏡射門檻） |
| `app/api/classification_inventory.py:60-64` | `_BELOW_CONFIDENTIAL`（低於「機密」的集合） |
| `app/api/proxy.py:73,87,94,117,119,790` | 「floored at 機密」的 latch 語意與註解 |
| `app/api/conversations.py:125` | 註解:`classified = classification_level >= 機密` |
| `app/api/public_share.py:67` | `"（機密對話）"` 字面字串 |

### 3.3 `無機密` 字面值 default / server_default（改字面值就全中）
`app/models/conversation.py:47`、`app/models/message.py:31`、`app/models/task.py:94,157`、
`app/models/ingestion.py:94,170`、`app/models/source_snapshot.py:71,115`、
`app/models/artifact.py:97,145,239`、`app/models/agent.py:109`、
`app/models/policy_decision.py:75`、`app/models/trace_span.py:82`、
`app/models/service_launch.py:58`、`app/api/conversations.py:127`

### 3.4 `UNCLASSIFIED` 比較
`app/modules/tasks/service.py:123`、`app/api/classification_inventory.py:114`、
`app/api/artifacts.py:284`、`app/api/services.py:66,360`、
`app/api/proxy.py:149,796`、`app/services/proxy/ceiling.py:59,66,67`、
`app/schemas/contracts/tasks.py:120`、`app/api/agents/health.py:418`、`app/api/proxy.py:77`

### 3.5 Migration / seeded data
| file:line | 內容 |
|---|---|
| `migrations/versions/r1_0003_five_level_classification.py:63-64` | `_UNCLASSIFIED = "無機密"` / `_CONFIDENTIAL = "機密"` |
| `r1_0003:242,257` | `server_default=_UNCLASSIFIED`（8 表 + agents) |
| `r1_0003:262-300` | 四段 backfill SQL,`'機密'` / `'無機密'` 字面內嵌 |
| `r1_0007_artifact_contract.py:83-86,168-171` | artifacts / export_records 的同一組欄位 |
| **新 migration 必需** | 改等級集合是**資料遷移**（既有列的 `極機密`/`絕對機密` 要重新映射),不是只改 enum。⚠ 但 S-wipe L323 允許砍庫重來 → 可評估直接改 `r1_0003` 而非加新 migration（需擁有者裁決,牽動 P0.4 的「從零到 head」驗證) |
| Column 寬度 | 全部 `String(20)`,4 級的「密」更短,無需調整 |

### 3.6 測試（會直接紅的）
| file:line | 內容 |
|---|---|
| `tests/test_contract_classification.py:15` | `ORDERED_VALUES = ["無機密","營業秘密","機密","極機密","絕對機密"]` |
| `tests/test_contract_classification.py:19-20` | `test_five_levels_exact_values` |
| `tests/test_contract_classification.py:31-32` | `test_rank_matches_doc08_numbering` → `[0,1,2,3,4]` |
| **`tests/test_contract_classification.py:96`** | ⚠ **直接與規格衝突**:`test_unknown_storage_value_rejected` 把 `"密"` 列為**必須被拒絕**的非法值 —— 而 S-levels L228 的 4 級集合**包含「密」**。 |
| `tests/test_contract_classification.py:42,52-55,65,69` | 各處 `極機密` / `絕對機密` 字面 |
| `tests/test_contract_classification.py:83-86` | legacy true → `機密` |
| `tests/test_classification_upgrade.py:95-118` | doc 08 enum 逐字斷言（reason 7 值 / status 5 值 / via 2 值） |
| `tests/test_classification_upgrade.py:120-129` | backfill 映射 → `CONFIDENTIAL` |
| `tests/test_classification_upgrade.py:192-227,383,504` | `極機密` / `絕對機密` / `營業秘密` 字面斷言 |
| `tests/test_classification_inventory.py:36-53,90-99` | `機密` / `極機密` 分佈斷言 |
| `tests/test_policy_module.py:42,210-231` | `LEVELS` 由 enum 推導(自動跟隨);`test_truth_table` 5×6 參數化會自動縮成 4×5 |
| `tests/test_declassification_api.py`（**不在本域檔案集,但同受影響**) | 大量等級字面 |
| `tests/test_artifact_contract.py:237-240`、`tests/test_task_trace_schema.py` | 等級字面 |

---

# 4. OE-4 impact surface —— 統一門檻 `> 無機密` 改成兩條線

> 目標:`可以做 = 密等 ≤ 營業秘密`(S-lines L241)、`要落稽核 = 密等 ≥ 營業秘密`(L242)。
> **關鍵發現:五個外流面(複製/匯出/分享/列印)在後端實際上不是用等級判的,而是用舊 boolean `conversations.classified` 判的**,而該 boolean 由 `level >= 機密` 鏡射而來（`service.py:225`)。所以 OE-4 的真正切點是 C5 的鏡射鏈,不是某個 `> 無機密` 常數。

### 4.1 本域檔案集內
| file:line | 現行判準 | OE-4 後 |
|---|---|---|
| `app/modules/policy/service.py:225` | `classified = level >= CONFIDENTIAL` | 需拆成兩條:外流允許 `≤ 營業秘密`、落稽核 `≥ 營業秘密` |
| `app/services/access_control.py:76-84` | `_classification_ok`:`context_level <= ceiling`（ceiling 語意,**不是**外流門檻） | 不受 OE-4 影響（屬 OE-1 keep),但改等級集合後門檻值要重算 |
| `app/api/classification_inventory.py:60-64` | `_BELOW_CONFIDENTIAL` | 若 C4 收斂則整檔消失 |

### 4.2 域外（收斂時必須同步,不屬本次審計範圍）
| file:line | 內容 |
|---|---|
| `app/api/public_share.py:55-67` | **分享閘門**:`classified = bool(conv.classified)` → 機密對話不外露內容與標題。OE-4 後應為「等級 > 營業秘密 才擋」 |
| `app/api/conversations.py:292` | `if not c.classified:` 分支 |
| `app/api/conversations.py:316-317` | **讀取稽核觸發**:`if conv.classified: log_classified_access(...)` → OE-4 後應為 `等級 >= 營業秘密` 才落稽核（L242） |
| `app/services/conversation_service.py:304-350` | `classify_conversation`,把 boolean 與等級一起寫 |
| `app/services/conversation_service.py:352-368` | `log_classified_access` |
| `app/services/conversation_service.py:384` | `if conv.classified:` |
| `app/api/proxy.py:149` | `if conv_level <= UNCLASSIFIED: return`（傳播抑制,`> 無機密` 的典型寫法) |
| `app/api/proxy.py:796` | `if agent_level > ClassificationLevel.UNCLASSIFIED:`（同上) |
| `app/api/proxy.py:79,775-792,853,880,937-948,1038,1073` | `requires_encryption` 驅動的 wire meta / `is_encrypted` 旗標鏈 |
| `app/api/agents/credentials.py:35-60` | 「啟用/停用 agent 加密模式」—— S-honest L268 要求改字為「列管」 |
| `apps/csp-governance-ui/src/api/agents.js:24` | `setAgentEncryption` 前端 API（**UI 用字**同受 L268 約束) |
| **未找到** | 前端沒有任何 copy / print / contextmenu 的分級閘門（grep `apps/` 無命中）—— 與 S-防弊 L248-256「採用乙案」一致(不做假的禁止複製),但**全頁浮水印(L254)與每次讀取落稽核(L255)目前也都沒實作** → 這是 OE-4 的新工作,不只是改門檻 |

---

# 5. 測試專節 —— 本域測試鎖住了哪些「規格上不存在」的構件

| 測試 file:line | 鎖住的 spec-absent 構件 | 收斂時的後果 |
|---|---|---|
| `tests/test_policy_module.py:171-184` `test_package_public_surface_exact` | 把 `__all__` 精確鎖成 8 個名字,**包含 C1/C2 的 `create_declassification_request` / `decide_declassification` / `has_declassification_authority`** | 收斂 C1/C2 的第一刻就紅;必須同步改 |
| `tests/test_policy_module.py:186-197` `test_no_mutator_exposed_anywhere` | C8 的命名警察（8 個禁用子字串) | 任何 `revoke_*` / `update_*` / `delete_*` 命名都被擋;收斂時建議直接刪 |
| `tests/test_policy_module.py:199-203` | `update_decision` / `delete_decision` 必須不存在 | 同上 |
| `tests/test_policy_module.py:70-161`（`TestRecordDecision` 全 11 項） | C3 的 `PolicyDecision` 語意（九值 enum、deny 必附 reason、actor_id 非數字落 metadata、立即 commit) | 收斂 C3 時整組移除 |
| `tests/test_policy_module.py:237-339`（`TestPolicyDecisionsApi` 全 8 項） | C3 的 `GET /api/policy-decisions` 端點與 5 種過濾 | 同上 |
| `tests/test_classification_upgrade.py:93-129` `TestDoc08EnumsVerbatim` | doc 08 的三個 enum **逐字順序**（reason 7 值、status 5 值、via 2 值）—— 全部無 SYSTEM-MAP 依據 | 這是把已被取代的文件當成測試 oracle;收斂 C1 時全刪 |
| `tests/test_classification_upgrade.py:338-567` `TestDeclassification`（13 項） | C1/C2 的完整流程:僅 Admin 可申請、申請人≠核准人、無權責停留 pending + `supervisor_missing` audit、紙本必附文號/官職姓名、恰好降一次、撤銷生效 | C1 的行為規格全在這裡;若擁有者裁決 C1 保留,這組是唯一的行為文件 |
| `tests/test_classification_upgrade.py:136-158,211-243` | C5 的舊 boolean 鏡射雙向一致（`classified`、`classified_at`、`classification_inherited`) | 收斂 C5 時同步改 |
| `tests/test_classification_upgrade.py:245-261` | C6 的 `trace_id` 掛載 | 收斂 C6 時刪 |
| `tests/test_classification_upgrade.py:573-597` `TestMigrationChainSlice3a` | 非本域構件(alembic 單一 head + `r1_` 命名空間);**寫得很好**——不釘死特定 head id | 保留 |
| `tests/test_classification_inventory.py:1-139`（全檔 3 項） | C4 的盤點端點:8 個資源型別的精確集合(`:107-111`)、`inconsistent` 一致性語意、CSV BOM | 隨 C4 整檔刪除 |
| `tests/test_contract_classification.py:96` | ⚠ 把 SYSTEM-MAP 的合法等級「密」鎖成**必須被拒絕**的值 | **與規格直接衝突**,OE-3 必改（已列入 §3.6） |
| `tests/test_contract_classification.py:14-99`（其餘） | A1/A2/A3 的排序、max_of、round-trip —— **有規格依據,是好測試** | 保留,只換等級集合 |
| （域外）`tests/test_declassification_api.py` | C1/C2 的 HTTP 層(447-539 行有 `ClassificationEvent` 查詢) | **不在本域檔案集,但收斂 C1/C2 必須一起處理**——列此以免遺漏 |

---

# 6. 檔案集覆蓋確認（無可稽核者必須明列）

| 檔案 | 狀態 |
|---|---|
| `app/modules/policy/service.py` | 已審:A3–A5、A11、A13、C1、C5、C6 |
| `app/modules/policy/router.py` | 已審:A14、C1（:112-291)、C2（:294-442)、C3（:76-109) |
| `app/modules/policy/__init__.py` | **無行為構件** —— 純 re-export + docstring。唯一可稽核項 = B10（docstring 仍引 doc 02/doc 08,`__all__` 隨 C1 縮) |
| `app/schemas/contracts/classification.py` | 已審:A1–A3、B2、C1（:113-137,149-206)、C2（:209-240) |
| `app/schemas/contracts/policy.py` | 已審:C3 全檔;GAP-3 |
| `app/models/classification.py` | 已審:A6、B1、B3、B4、C1（:83-131)、C2（:134-171)、C6 |
| `app/models/policy_decision.py` | 已審:C3 全表;B8 |
| `app/api/classification_inventory.py` | 已審:C4 全檔 |
| `app/services/access_control.py` | 已審:A12（Step 6)、B5（Step 7/8)、B11。**Steps 1–5（is_active / admin bypass / required_roles / is_public / grant)屬 Service Registry 域,未審** |
| `tests/test_policy_module.py` | 已審（§5) |
| `tests/test_contract_classification.py` | 已審（§5) |
| `tests/test_classification_upgrade.py` | 已審（§5) |
| `tests/test_classification_inventory.py` | 已審（§5) |
| `migrations/versions/r1_0003_five_level_classification.py` | 已審:A6–A10、B6、B7、C1、C2、C7、OE-3 §3.5 |

**無可稽核內容的檔案**:僅 `app/modules/policy/__init__.py` 接近此類（只有 re-export;已於 B10 列出,不是靜默略過）。其餘 13 個檔案皆有列表構件。

---

# 7. 計數

| 類別 | 數量 | 構件 |
|---|---|---|
| **CAT-A** | 14 | A1–A14 |
| **CAT-B** | 11 | B1–B11 |
| **CAT-C** | 8 | C1–C8 |
| GAP（規格有、碼沒有） | 4 | GAP-1 – GAP-4 |

## CAT-C 收斂優先序（依「一人維運」成本排序）

1. **C1 + C2**（同一包）—— 降級申請 + 權責名冊。⚠ 需擁有者裁決（見 C1 的 EXEC CHECK）。
2. **C3** —— `PolicyDecision` 第二本帳,折進單一稽核帳（需 audit 域協同,並同時補 GAP-1）。
3. **C4** —— 盤點端點整檔刪（前提 S-wipe L323 已成立,風險最低、收益即時)。
4. **C5 + OE-4**（同一包）—— 舊 boolean 鏡射 → 等級門檻兩條線。
5. **C8** —— 命名警察測試（純刪,零風險)。
6. **C6 / C7** —— trace_id 與反向 FK,併入 OE-3 的 migration 批次。
