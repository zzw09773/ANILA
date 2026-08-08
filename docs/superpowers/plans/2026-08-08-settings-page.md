# 設定總覽頁 — 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development。
> 規格權威＝`docs/superpowers/specs/2026-08-08-settings-page-design.md`（含 08-08 三項追加裁決）。
> 盤點權威＝env 盤點報告（692 行，**不在 repo**；隨 SDD 工作區存放，路徑見帳本首行注記）。

**Goal:** 管理員在治理中心看到 csp 全部 95 顆設定的真相（值、預設、來源、生效條件），
其中 19 顆改了下一個請求生效、約 30 顆改了重啟生效——而且**畫面說的永遠等於實際發生的**。

**Tech:** FastAPI＋SQLAlchemy（沿用 `platform_settings` 表，**零 migration**）、
Vue 3（governance-ui，node --test）、pytest。

## Global Constraints

- 分支基底 `restart/from-redesign`；工作 worktree `wt-settings-page`（分支 `wt/settings-page`）。
- **本包零 alembic migration**（列缺席＝預設，寫入才建列）。若任何實作者認為需要 schema
  改動：STOP 回報，migration 編號由指揮官分配（下一個是 `r1_0035`），嚴禁自派。
- **絕不對活體 DB 跑 alembic；不碰 compose project `anila-restart`**。
- `platform_settings` 五條不變式全繼承（設計 §4）：讀不快取／值域函數兩端共用一份／
  一次解析拿兩個答案／壞值退回預設且誠實揭露／set 只 flush 不 commit（稽核同交易）。
- **A 類遮蔽發生在後端**；SEC 類永不可編輯；死變數永不上頁。
- 祕密零外洩（PUBLIC repo）；不得在容器內 `env`/`printenv`。
- 卡登旁路防守（Task 7）＝紅線鄰接：**雙票審**（跨家票可用時必用；sol 預計 08-09 09:43 回）。
- 每個 Task 結束 commit（英文）；驗收另派 fresh agent，驗收單必帶：
  「Look for the shape this package exists to eliminate, in the package's own work.」
  本包要消滅的形狀＝**「畫面上看到的設定與實際生效的值不一致」**
  （改了沒生效／沒改卻顯示改了／待生效畫成已生效／祕密上畫面／死變數畫成活的）。
- 常設要求（earned）：驗收自創突變；值域測試要掃**內插值**不是只掃邊界；
  任何「全類別」斷言用完整名單迭代不抽查。

## File Structure（預估，派工前逐錨點驗證——上一計畫三次栽在引用不存在）

| 檔案 | 責任 |
|---|---|
| `services/csp/app/services/settings_registry.py` | **新** — 唯一的設定宣告表（key、類別、型別、值域、預設、env 名、說明） |
| `services/csp/app/models/platform_setting.py` | 泛化 getter/setter（沿用門檻範本形；門檻既有函式不動） |
| `services/csp/app/api/platform_settings.py` | **新** — overview GET＋PUT（admin router） |
| `services/csp/app/config.py` | B 類開機覆蓋載入 hook（`:215` 凍結點之後、服務前） |
| 19 個 C 類消費點 | 逐顆改每請求讀 DB（盤點報告 §2 有讀取點 file:line） |
| `apps/csp-governance-ui/src/views/SettingsOverviewView.vue` | **新** — 四區三態頁 |
| `apps/csp-governance-ui/src/api/platformSettings.js` | **新** — API 模組（照 departments.js 慣例） |
| `infra/compose/platform.yml`＋`.env.example` | 死變數移除（FLUX_*×3、router MODEL、csp 不讀的 3 顆） |
| `services/csp/app/services/startup_security.py` | 卡登旁路 fail-loud 守衛 |

## Tasks（每個派工前展開＋commit；壓縮段落絕不直接派）

1. **設定登錄表＋泛化存取層**：registry 宣告 19 C＋~30 B-edit（名單以盤點報告為準、
   B-edit/B-locked 分界照設計 §3.2）；泛化 `get_setting(db, key)`／`set_setting`；
   每顆值域函式；env→DB→預設回退鏈。測試：登錄表完整性（每顆有值域、預設過自己的值域）、
   值域掃描含內插值。
2. **C 類消費端改造（a：嚴格 3＋數值鈕 7）**：逐顆改讀取點＋round-trip 釘
   （PUT→DB 列→GET→下游參數，下界/內插/上界）。
3. **C 類消費端改造（b：速率逾時 5＋memory 4）**：同上；timeout 下限防呆特別釘
   （值域拒 0——從畫面打掛平台必須不可能）。
4. **B 類開機覆蓋**：config 載入 hook＋模擬 boot 測試工具＋載入失敗誠實路徑
   （env 開機＋警告＋來源欄如實；「靜默假裝成功」突變必紅）。
5. **Overview＋PUT 端點**：全 95 顆 payload（A 後端遮蔽、類別、來源、需重啟旗標、
   待生效值）；PUT 類別閘（非可編輯 key 一律 400 人話）；稽核同交易＋補
   `platform_setting_set` 斷言（帳本舊債）。測試：A 全名單零值外洩、非法 key 全類掃。
6. **前端四區三態頁**：照 DepartmentsView 形＋補初載錯誤 UI；三態並列（生效/待生效/預設）；
   來源欄；不做樂觀更新；detail 原樣呈現；編輯邏輯抽 utils 直測＋唯讀區 regex 護欄
   （照 runtimeConfigReadOnly 樣板）。
7. **死變數清理＋卡登旁路防守**（🔴 後者紅線雙票）：compose/.env.example 移除＋歸檔註記；
   `CARD_DEV_SKIP_NONCE_BINDING` 非 dev-card 模式設值→開機即拒（照 startup_security 既有形）。
8. **文件收尾**：runbook（B 類重啟措辭對齊）、HANDOFF 長期照顧（登錄表是唯一宣告點／
   雙層優先序 DB>env 的除錯指南／TRUSTED_HOSTS 雙源收斂 follow-up）、
   設計文件與 OWNER-QUESTIONS 對齊。
9. **最終全分支審查**（最強模型＋跨家第二票；sol 08-09 起可用）→
   `finishing-a-development-branch`。

## 驗收（另派 fresh agent，不自驗）

| # | 不變式 | 怎麼證明活著 |
|---|---|---|
| 1 | 顯示值＝生效值（C 即時、B 分態） | round-trip 釘＋「待生效畫成已生效」突變必紅 |
| 2 | A 類值永不出後端 | overview 全 A 名單掃描，遮蔽在後端測 |
| 3 | 收得下的＝算得出的 | 值域函式兩端共用，複製一份的突變必紅 |
| 4 | 從畫面打不掛平台 | timeout/retry/ratio 值域下限逐顆釘 |
| 5 | 覆蓋載入失敗不騙人 | env 開機＋警告＋來源如實，三件一組 |
| 6 | 非可編輯 key 的 PUT 必拒 | A/SEC/B-locked 全名單迭代 |
| 7 | 死變數不復活 | compose/頁面雙邊零出現 |
