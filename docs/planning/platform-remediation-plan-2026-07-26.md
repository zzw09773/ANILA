# ANILA 平台補救執行計畫(2026-07-26)

> **本檔角色**:把 `platform-audit-synthesis-2026-07-26.md`(排序權威)轉成可直接排程的執行計畫。稽核發現的細節不在此重述;本檔只寫「怎麼做、誰做、做完怎麼證明、什麼卡什麼」。
> **產出者**:攻堅鏈 ③(Fable 5,純規劃崗,零實作)。分支 `feat/ux-parity`,2026-07-26。
> **證據標記**:標「◎複驗」= 本檔撰寫時另行以 grep/讀碼/git 一手驗證;標「稽核源」= 沿用合成報告/對抗驗證(07)/完整性審查(08)的已驗證結論,未重複驗證。錨點行號除註明外皆為 `feat/ux-parity` 座標(見 §2.1 錨點漂移警告)。

---

## 修訂紀錄

**rev.1(2026-07-26)** —— 攻堅鏈 ③(Fable 5)初版:41 個工作包 + 五份子計畫,並以一手證據推翻合成報告五處事實(§1 F1–F8),已回寫合成報告 rev.2。

**rev.2(2026-07-26,Opus 5 缺漏審查後整合)** —— 對 rev.1 做獨立缺漏審查(逐條核對完整性審查 L1–L16、合成 T2-11 十八列、S1–S7、依賴圖環路、驗收條件是否機械可檢查、驗收是否偷偷全落在 admin 面)。**rev.1 通過絕大多數檢查**——依賴圖無環、驗收幾乎全為「指令 + 期望輸出」、Wave 3 離開條件要求複測 W1-0 前測且完成率不得低於 baseline(這條直接解掉第一輪「驗收全在 admin 面」之弊,做得比合成報告好)。以下 10 條為補入項:

| # | 缺漏 | 類型 | 補在哪 |
|---|---|---|---|
| 1 | **W0-1 驗收③ 在結構上做不到**:ORM 與 PG 兩邊都是 naive,ORM↔PG diff 永遠不報那 93 欄;C1 §a 又把該輸出當權威 | **方法論級** | W0-1 改法 (b) 政策斷言 + C1 §a 改用 `information_schema` + W2-10 改法③ ORM 同步 |
| 2 | **分類正確性的輸入端完全不見**(完整性審查 L3):S4 只做了輸出端 | **實質缺漏(CRITICAL)** | 新增 **W2-11** |
| 3 | 服務健康總覽視圖零覆蓋(admin-journey D1) | 缺漏 | W3-3 ⑦ |
| 4 | 結構化錯誤信封 / API 契約零覆蓋(含 `LoginView` 靠中文子字串比對、104 處 `[object Object]`、37% 無 `response_model`) | 缺漏 | 新增 **W2-12** |
| 5 | `ENABLE_MEMORY=false` 時記憶 tab 說謊——與「加密模式」同一缺陷家族,rev.1 只修了一半 | 缺漏 | W1-3 擴充 |
| 6 | `data-governance.md` 的個資法權利宣稱不可執行(更正權說 admin 人工處理,而 admin 連讀都不行) | 缺漏 | W4-4 擴充 |
| 7 | SBOM / CVE 監控 / 第三方 license 清冊 / NOTICE 全缺(L5+L10) | 缺漏 | 新增 **W3-12j2** |
| 8 | 資料來源版權與 provenance(L11) | 缺漏 | 新增 **W4-7** |
| 9 | `e2e/README.md` 的不實宣稱只在 §6.2 提到「順手」,無驗收條件 → 無 owner 會漏 | 漏 owner | W0-6 驗收⑤ |
| 10 | 兩個排序註記:Gate 5 runbook 缺「若提前立新環境即升阻斷項」的觸發條件;W1-1④ 與①②③⑤⑥ 節奏不同會互卡 | 排序 | W3-8 依賴欄、W1-1 改法④ |

**rev.2 後規模**:44 個工作包(Wave 0×8、Wave 1×10、Wave 2×12、Wave 3×12+13 子包、Wave 4×7)+ 五份子計畫。

**rev.3(2026-07-26,user 拍板六個決策點)** —— D1–D6 全數拍板,計畫由「待決」轉為「可排程」:

| 決策 | 結果 | 對計畫的影響 |
|---|---|---|
| D1 | **PR #50 merge** | Wave 1 前端工作包解鎖(錨點沿用 `feat/ux-parity`,省 0.5 人日重定位)。⚠ §2.1 四條機械收斂條件仍須先綠 |
| D2 | **PR #51 merge 路線核可** | ⚠ 核可的是**路線**,不是「現在就 merge」——M1–M5 未達成前不得 merge,M3 blocked-by W0-2 |
| D3 | **甲案:CSS Modules + design tokens** | C4 定案。**追加配套**:W0-3 的 ESLint 必須同時加「禁新增 inline style」規則,否則 966 處只會變 967 |
| D4 | **儲存層與呈現層皆 UTC+8** | C1 §b/§c/§f、W2-10 改法與驗收全數改為 `AT TIME ZONE 'Asia/Taipei'`。⚠ 新增三項執行前置與一條 csp-db TZ 鐵則(見 C1 §c) |
| D5 | **access_grant 雙軌影子讀** | C2 §d 定案,Phase 0→1→2,先 `collection_access_grants` |
| D6 | **軍方相關先不做** | §6.1 六條降級清單生效 |

> **D4 的處理方式記錄**:實作者(Opus 5)曾以「兩條寫入路徑(Python 211 處 + DB `server_default`,session TZ=`Etc/UTC`)皆為 UTC」提出反對意見,user 重申後照決定執行。反對意見的內容與證據完整保留在 C1 §c,**並要求寫進 migration 檔頭**——目的不是留存異議,是讓未來的稽核複查能自行判斷這 8 小時平移的來由,而不是只看到一個沒有脈絡的 `AT TIME ZONE 'Asia/Taipei'`。

---

## -1. 授權與範圍變更(2026-07-26,user 明示)

> **「不要管什麼資料權責啥的,那些不是我該負責,我要做的就是開發出一個完整的 SaaS 系統,所以什麼書面簽核那些不用管,做就對了,我授權。」**

據此調整,**本節優先於下文任何相衝突的敘述**:

| 原本的設計 | 調整後 |
|---|---|
| W2-10 需「資料權責人書面簽核」才可動 93 欄遷移 | **取消該閘門**。改以工程手段承擔:① 對稱 `downgrade()` 保證可逆;② 遷移前對受影響表做獨立快照;③ 判讀依據與後果寫進 migration 檔頭(見 C1 §c)。遷移在 dev / 乾淨 PG 上驗證,不對 `.15` 生產執行 |
| **Wave 4 整批**(RACI 具名、法規對映表、跨單位介面對口、training-records) | **跳過,不產出**。這些是組織治理而非工程,且模型自行填法規條號等於造假 |
| W1-0 五位院內使用者 UX 前測 | **降級為選配**。改用可自動化的替代驗收(a11y 掃描 + 關鍵路徑測試),不阻擋 Wave 1 離開 |
| W3-8 Gate 5 operator runbook | **保留但降級**。仍寫程序文件(對開發有用),但不再當阻斷項 |

**不因授權而改變的三條**(這些不是簽核問題,是工程正確性問題):

1. **PR #51 的兩處授權繞過要修,不是跳過** —— M1–M5 照做,因為那是「程式碼是壞的」。
2. **不對 `.15` 生產跑破壞性操作**,不動 running `anila-platform-*` 容器(user dev 環境)。
3. **不產出內容不實的文件**(例如編造法規條號、或宣稱測試覆蓋了實際沒測的東西)—— 這正是本 codebase 的一級缺陷類別(§5 S5),不能由我再製造一份。

---

## 0. 六個決策點(**全部已於 2026-07-26 拍板**)

| # | 決策 | 何時必須決 | 依據 |
|---|---|---|---|
| ~~D1~~ | ~~PR #50 merge 或 close~~ → **已決:merge**(2026-07-26 user 拍板) | — | §2.1 四條機械收斂條件仍須先綠才可執行 merge;錨點沿用 `feat/ux-parity` 座標 |
| ~~D2~~ | ~~PR #51 的 merge 條件核可~~ → **已決:merge 路線核可**(2026-07-26) | — | ⚠ **核可的是路線,不是「現在就 merge」**:M1–M5(§2.2)未達成前不得 merge,否則會把已知 CRITICAL(legal-hold TOCTOU)送上 main。M3 blocked-by W0-2 |
| ~~D3~~ | ~~樣式策略三選一~~ → **已決:甲案 CSS Modules + design tokens**(2026-07-26) | — | 子計畫 C4;`packages/tokens` 沿用;需在 W0-3 的 ESLint 加「禁新增 inline style」規則,否則 966 處只會變 967 |
| ~~D4~~ | ~~93 個 naive timestamp 的既有值判讀~~ → **已決:兩層皆 UTC+8**(2026-07-26,user 經一次反對意見後重申) | — | 呈現層 UTC+8 = W1-4④;儲存層用 `AT TIME ZONE 'Asia/Taipei'`。⚠ **W2-10 執行前須齊備三項前置**(生產受影響筆數報表、資料權責人書面簽核、獨立快照+對稱 downgrade),見 C1 §c。⛔ 連帶鐵則:**csp-db 容器 TZ 在該批欄轉完前必須維持 UTC** |
| ~~D5~~ | ~~access_grant 收斂路線~~ → **已決:雙軌影子讀**(2026-07-26,user 授權照建議執行) | — | 子計畫 C2 §d:Phase 0 唯讀收斂 → Phase 1 雙寫+影子讀 → 連續 14 天零不一致才切讀;每張表獨立走,先 `collection_access_grants` |
| ~~D6~~ | ~~軍方相關條目降級清單確認~~ → **已決(2026-07-26,user 拍板:軍方相關先不做)** | — | §6.1 六條降級清單生效;每 Wave 收尾記欠帳 |

另有 Wave 4 的 owner 指派(§4 Wave 4)屬制度面,不是工程排程,但 S1「驗證器空轉」的解只有這一條路。

### 人力編組代號(每包「派工」欄用此表;依 2026-07-24 編組表 v3)

| 代號 | 實體 | 用途 |
|---|---|---|
| 實作A | `cursor-grok-4.5-high` + 隔離 worktree | 主力實作 |
| 實作B | `gpt-5.6-luna×max`(Codex) | 第二實作線、批次套用 |
| 偵察 | `composer-2.5`(`--mode plan` 鎖唯讀) | 清單盤點、call-site 掃描;**禁寫、禁驗收** |
| 驗收甲 | `claude-opus-5-thinking-high`(cursor-agent,唯讀) | 驗收 GPT/Cursor 家產出 |
| 驗收乙 | `gpt-5.6-sol×xhigh`(Codex,唯讀) | 驗收 Claude 家產出(本計畫文件類) |

規則:驗收一律反家;`Sonnet`/`terra`/`Composer` 禁入驗收崗;**批次工作先由強模型做對一個案例、萃取成「逐步指令+已完成範例」再交批次**(無範例的批次派工必失敗);每包驗收條件跑完才可宣稱完成(宣稱前依全域路由表讀 `20-judgment.md`);commit/push 只在 user 要求時。

---

## 1. 對合成報告的修正(本檔複驗所得;依「降級須附複驗證據」原則逐條列證)

| # | 合成報告原文 | 複驗結果 | 證據 | 對計畫的影響 |
|---|---|---|---|---|
| F1 | 「`apps/csp-governance-ui` 與 `apps/anilalm` 的改動**不觸發任何 workflow**」(§6/§7.2) | **不成立**。`gate1-ci.yml` 無 `paths:` 過濾,對 `main`/`prod-intranet-card` 的 push 與 PR 一律觸發,且含 `frontend-governance`(npm test+build)、`frontend-shell`(npm test+build)、`frontend-anilalm`(typecheck+2×build)三個 job | ◎複驗 `gate1-ci.yml:3-11,514-583` 全文讀過 | 真缺口重框定為:(a) anilalm **無 test script**,CI 只 typecheck+build(03.md:70、04.md:253 原文即如此,是合成時走樣);(b) 軍方分支不在 branches 白名單 → 無 CI;(c) shell 測試只跑 node 20。→ W0-6 按此三條排工 |
| F2 | `execution_grants` 列入「8+ 張分資源授權表」 | **它不是資料表**。`ExecutionGrant` 是 `anila_contracts` 的短效 minted 傳輸契約,只存在 `schemas/execution_grant.py`;全部 migration 零建表 | ◎複驗 `grep -rln execution_grants migrations/versions/` → 0;`schemas/execution_grant.py:1-75` | 子計畫 C2 的收斂對象排除它(收斂 6 張持久表,見 C2) |
| F3 | 「549 個 inline style」 | 549 = **僅 shell**;anilalm 另有 417,合計 966 | ◎複驗 `grep -o "style={{" -r` shell=549、anilalm=417;governance `@media`=25 | 子計畫 C4 的遷移成本用 966 估,不用 549 |
| F4 | 「40 個 `_pg` 測試已寫好」 | `tests/*_pg*.py` 共 **12 檔 / 34 個 test function** | ◎複驗 ls+grep 計數 | W0-2 驗收數字用 34 |
| F5 | 「127 檔 / 1412 test function」 | 本分支現為 **130 檔 / 1412 function**(PR #50 新增 3 檔測試) | ◎複驗 ls+grep | 無影響,記錄備查 |
| F6 | (工作指示內)「#51 還有約 62 個 `with_for_update` 沒加 `populate_existing`」 | `fix/stream-session-pool` 分支現為 **119 處 `with_for_update` / 74 處 `populate_existing`**(進度已推進;精確配對數需腳本判定,佔位計數不可當驗收) | ◎複驗 `git grep -c` 於該分支 | §2.2 M1 直接要求機械配對腳本,不採任何手數 |
| F7 | 「修法現成:`app/services/` 已有 8 處 `asyncio.to_thread` 前例」 | 全 `app/` 現為 **17 處** | ◎複驗 grep 計數 | W2-7 批次範圍以偵察掃描為準 |
| F8 | timestamp 既有值語意未定 | 寫入端 **210 處 `datetime.now(timezone.utc)`、1 處 `datetime.utcnow()`(`api/service_clients.py:214`)、0 處裸 `datetime.now()`** → naive 欄存的是 UTC 牆鐘。**Opus 5 追加**:第二條寫入路徑 DB `server_default CURRENT_TIMESTAMP`(涵蓋治理帳四張表)在 session TZ=`Etc/UTC` 下**也是 UTC** | ◎複驗 grep 三式 + 實庫 `information_schema` / `SHOW TimeZone` | ⚠ **D4 已於 2026-07-26 由 user 拍板採 `Asia/Taipei`**,本列的碼證改作「反對意見的證據紀錄」保留,並依 C1 §c 寫入 migration 檔頭 |

**已修、不排工**(沿用 07.md NOT-SUPPORTED 段,W0-8 負責把 `CLAUDE.md` §5.1 改寫):codeserver RW 掛 repo root(現行 `platform.yml:807-822` 有 profiles+隔離 workspace)、`BACKUP_DIR` 在 repo 內(現行 `assert_outside_repo` 強制)、營業秘密分享連結的**分類面**(`is_publicly_shareable` 已 fail-closed 至僅無機密)。⚠ 分享連結的**旗標面**(`create_share` 不讀 `ENABLE_PUBLIC_SHARE`)仍是真缺陷,排在 W3-7c——兩者別混。

---

## 2. 兩個開啟中 PR 的收斂路徑

### 2.1 PR #50 `feat/ux-parity`(UI/UX 五功能;+5,717/−291,34 檔)◎複驗

**衝突面**:Wave 1/2 前端工作包(W1-1、W1-2、W2-4、W2-5、W2-9)全部改 `chat.jsx`/`app.jsx`,與本 PR 同檔重疊。且**全部稽核錨點取自本分支**:例 `canCopy` 在本分支 `chat.jsx:490`、在 `main` 是 `:475`(檔案 2,278 vs 2,048 行)◎複驗。

**收斂條件(機械)**:
1. `cd apps/anila-shell && npm test && npm run build` 綠(node 20 與 22 各一次)。
2. `cd apps/anilalm && npm run typecheck && npm run build` 綠。
3. 既有五崗位審查鏈(Opus5 實作 → sol → Fable5)的未結 findings 清零(以審查記錄為準)。
4. rebase 到最新 `main` 後 CI(gate0+gate1)全綠。

**建議**:在 **Wave 0 期間**裁決。**merge 優於 close**——理由:(a) 錨點經濟:所有後續前端工作包可直接沿用稽核座標;(b) 本 PR 含訊息版本切換 UI(`chat.jsx:727-758`,「此回覆有 N 個版本」◎複驗),是子計畫 C3(`parent_id`)前端對接的落點;close 則 C3 前端要重做。若 user 決定 close,W1-1/W1-2/W2-4/W2-5/W2-9 的錨點須由偵察在 `main` 上重定位(0.5 人日,composer-2.5 唯讀),再開工。

### 2.2 PR #51 `fix/stream-session-pool`(`expire_on_commit=False` 根治連線池)

**現況**◎複驗:分支上 `database.py:89` 設 `expire_on_commit=False`;119 處 `with_for_update`、74 處 `populate_existing`。**已知 CRITICAL**(專案記憶 + 工作指示):read-then-act 站點若無 `populate_existing()` 會讀 identity map 舊值 → legal-hold TOCTOU 被擊穿;其中兩處為真授權繞過;且全部驗證跑在 SQLite(`FOR UPDATE` 為 no-op)= 未驗。

**merge 前置條件(全部機械,缺一不可)**:

| # | 條件 | 驗收指令/證據 |
|---|---|---|
| M1 | **配對稽核腳本**:新增 `infra/ci/check_lock_read_freshness.py`(AST 層判定每個 `with_for_update()` 呼叫鏈是否含 `populate_existing()`;允許逐站 `# lock-fresh-waiver: <理由>` 註記豁免,豁免理由限「物件本 transaction 內建立」或「唯寫不讀」兩類) | 腳本對 PR51 分支 exit 0;對「移除任一 populate_existing」的 seed 變更 exit 1;豁免清單進 PR 描述供驗收逐條抽查 |
| M2 | **兩處授權繞過修復** + 各附先紅後綠測試 | pytest 指定測試檔,舊碼紅、新碼綠(PR 描述附兩次執行輸出) |
| M3 | **真 PG 的跨 session TOCTOU 回歸測試**:retention reaper legal-hold 案例改寫為雙 session PG 測試(參考 `test_retention_lifecycle.py:232` 的偽陰性教訓——同 session 版本兩種旗標都會過,不算證據) | 在 W0-2 建好的 CI PG job 中執行;`expire_on_commit=True` 舊語意下亦須通過(證明不是靠旗標僥倖) |
| M4 | `database.py` docstring 保持「`expire_on_commit=False` 是主角、`close()` 不是」的因果敘述(防止未來回拔旗標引爆全平台凍結) | `grep -n "expire_on_commit=False.*is the fix\|deadlock guard" services/csp/app/database.py` ≥1 |
| M5 | 反家驗收:M1–M3 產出若出自 Codex/Cursor 家 → 驗收甲;若含 Claude 家改動 → 加驗收乙 | 驗收記錄 |

**依賴與時序**:M3 blocked-by W0-2。**#51 應在 Wave 2 開工前 merge**——W2-3(conversation_service)、W2-1(retrieval_service)都會撞它的 populate_existing 批次;先 merge 再開工,免得兩邊 rebase 地獄。若 M1–M5 在 Wave 1 結束時仍未達成,Wave 2 的 W2-1/W2-3 改為「在 #51 分支上開工」並凍結 main 上同檔改動(次佳解,需 user 核可)。

**與新工作的衝突面**:M1 的腳本天然成為 C5(legacy ledger)的第一個「機制化債務追蹤」範例——同一 ratchet 思路,共用 CI 接線。

---

## 3. Wave 結構與閘門條件

> 對合成報告 §8 的修改:① 把「D2 三 token 重算」從隱含事項提為 Wave 0 明確工作(W0-4),因為它是 Gate ③ 門檻的前置;② 在 Wave 0 離開條件加入 PR #50 裁決、Wave 2 進入條件加入 PR #51 收斂與 C4 拍板(合成報告只說「Wave 2 之前」,此處落成機械條件);③ T0-4 的 93 欄 migration 移到 W2-6(startup_migrations 折回 alembic)**之後**(W2-10),避免對同一批表做兩輪 schema 手術;④ 負載測試(合成 Wave 2 第 7 項)保留,但明定它是 Wave 2 的**離開條件**而非工作之一。其餘 Wave 劃分沿用。

| Wave | 主題 | 概估 | 進入條件 | 離開條件(機械) |
|---|---|---|---|---|
| **0** | 護欄 | 1–1.5 週 | 無(立即) | E0-1:`check_orm_pg_drift.py` 於 CI 綠且 seed-drift 測試證明會咬人;E0-2:CI PG job 執行 34 個 `_pg` test function 全綠;E0-3:三 app `npm run lint` 於 CI 執行(jsx-a11y/vue-a11y 啟用);E0-4:`verify.mjs` 含 WCAG 門檻且 D1 七值與 07.md 重算表一致、D2 三值重算入庫;E0-5:bundle 預算 fail-build 證據;E0-6:anilalm `npm test` 於 CI 執行、spanTree try/catch 移除、shell node matrix 含 22;E0-7:三 app error boundary 驗收過;E0-8:`CLAUDE.md` §5.1 改寫完成;E0-9:**PR #50 已裁決** |
| **1** | 停止說謊 | 1.5–2 週 | E0-1…E0-9 全數成立 | W1-1…W1-9 驗收全過;**C4 樣式決策文件產出且 user 已三選一(D3)**;C1 判讀已簽(D4);N-7 ledger 上線且 `classified` 引用數封頂 |
| **2** | 停止流血 | **3–4 週**(rev.2:+W2-11 輸入端分級 3 日、+W2-12 錯誤信封 3 日) | Wave 1 離開 + **PR #51 已 merge(或 user 核可次佳解)** | W2 各包(含 rev.2 新增的 W2-11、W2-12)驗收過;**k6 三條 profile 的 p95/飽和點數字已寫入 `docs/planning/load-baseline.md`**(數字本身不設及格線,存在即可離開;及格線是 Gate 6 的事) |
| **3** | 能上線 | 4–6 週 | Wave 2 離開 | W3 各包驗收過;複測 W1-0 的 UX 前測(同 5 任務,完成率不得低於 baseline) |
| **4** | 制度 | 與 1–3 平行 | 無 | 五驗證器 owner+期限清單、RACI 具名、三個跨單位介面對口、法規對映表、training-records 目錄與使用者手冊初版——五樣以「檔案存在於 docs/ 且含具名欄位」驗收 |

**跨分支傳播**(每個 Wave 收尾統一做,遵 `AGENTS.md` §3):core 改動 main 先落,再 cherry-pick `prod-intranet-card`;`.env.example` 姿態語意重推導、禁 apply 舊 diff;`prod-military-passwd`/`trial-military` **不傳播**(範圍決策,§6.1),但每個 Wave 收尾在 `docs/planning/branch-sync-ledger.md` 記一行「軍方分支欠账」,防日後以為漏了。

**Gate 2 治理紅線**:凡工作包觸碰 `apps/anilalm/src/workspace/WSChat.tsx`、`apps/anilalm/src/api/chat.ts`、`services/csp/app/api/proxy.py`、`services/csp/app/services/retrieval_service.py`(= `infra/policy/tests/test_gate2_retrieval_governance.py:10-20` 字串比對的四個檔 ◎複驗)→ 該包標 **[G2]**,必須保住被斷言的字串或連同治理測試一起送治理重核。下文已逐包標記。

---

## 4. 工作包

> 九欄:ID/錨點/根因/改法/工作量(人日,註明含測試與否)/依賴/驗收(機械)/派工(實作+驗收,反家)/風險。
> 錨點原則上抄自合成報告與 07/08;凡本檔複驗發現偏差者已在 §1 修正。

### Wave 0 — 護欄

#### W0-1(=合成 Gate ①)ORM ↔ 真 PG drift 檢查進 CI
- **錨點**:`services/csp/app/services/startup_migrations.py`(alembic 外第二套機制,覆蓋 8 表)、`services/csp/tests/conftest.py:76`(SQLite `create_all`)、`services/csp/app/models/alert.py:11,23`
- **根因**:三方漂移(ORM/alembic/真 DB)且測試迴路結構上看不見(08.md S2)。
- **改法**:新增 `infra/ci/check_orm_pg_drift.py`,**兩種檢查分開實作**:
  - **(a) drift 檢查**:CI 內 `alembic upgrade head` + `run_startup_migrations()` 到乾淨 PG → 用 SQLAlchemy inspector 對 `Base.metadata` diff(缺欄/缺 index/缺 UNIQUE/缺 FK/型別不符);輸出 JSON;非白名單項 → exit 1。
  - **(b) 政策斷言(⚠ Opus 5 缺漏審查補入)**:「所有 `DateTime` 欄必須宣告 `timezone=True`」——這**不是** drift,不能靠 ORM↔PG diff 抓到。**理由**:`classification_events.created_at` 的 ORM 是 `Column(DateTime, …)`(naive)、PG 是 `timestamp without time zone`(naive),**兩邊相符** → diff 永遠不報。全 models 是 **naive 104 vs aware 53**,病根是「ORM 與 DB 一起錯」,不是漂移。存量進 baseline ratchet,由 W2-10/C1 逐批清。
  - 白名單 = `infra/ci/orm_drift_baseline.json`,逐條掛對應工作包 ID,**只准縮不准增**(ratchet)。
- **工作量**:1.5(腳本即測試;+0.5 為政策斷言)
- **依賴**:無。blocks W2-6、W2-10、C1。
- **驗收**:① CI job 綠;② seed 測試:從 baseline 移除「`alerts.fingerprint` UNIQUE 缺失」條目後重跑 → exit 1;③ baseline 的 **drift 段**含 `alerts` 17 條缺失類(以腳本輸出為準,若數字與稽核不同,以腳本為權威並回寫本計畫);④ baseline 的 **政策段**含全部 naive `DateTime` 宣告(數量以腳本為準,預期約 104);⑤ seed 測試:把任一欄改回 naive → 政策段超額 → exit 1。
  > ⚠ **原驗收③ 曾寫「baseline 含 93 筆 timestamp 型別不符」,那是做不到的**——見改法 (b)。93 是 **PG 側** naive 欄數(來源 = `information_schema`),與 ORM 宣告數(104)是兩個不同的量,且兩者相符時 drift 為零。
- **派工**:實作A;驗收甲。
- **風險**:白名單過寬 = 假 gate → 驗收逐條檢查 baseline 是否都掛了工作包 ID;SQLite 變體誤報 → 比對一律用 PG dialect。

#### W0-2(=合成 Gate ②)CI 真 PG 測試路徑
- **錨點**:`conftest.py:76`;12 檔 `tests/*_pg*.py` / 34 test functions ◎複驗
- **根因**:SQLite 下 `FOR UPDATE` no-op、18 表 CHECK 不存在、migration 不跑。
- **改法**:gate1-ci 增 job:PG service → `alembic upgrade head` → `python -m pytest tests/*_pg*.py -q`(以 `ANILA_TEST_PG_DSN`/`TEST_POSTGRES_URL` 餵入,比照既有 `test_gate2_auth_races_pg.py` job 的寫法)。不動 conftest 主迴路(那是 W2-6 之後的事)。
- **工作量**:1
- **依賴**:無。blocks PR#51 M3、W2-3、W2-6、W1-1③、W1-5。
- **驗收**:CI log `-q` 尾行顯示 34 passed(或 34−skip,skip 需逐條理由);workflow 檔 `grep "alembic upgrade head"` ≥1。
- **派工**:實作A;驗收甲。
- **風險**:個別 `_pg` 測試依賴本機殘留狀態 → 先在 worktree 對空 PG 驗過再上。

#### W0-3(=合成 Gate ③a)ESLint + a11y 三 app
- **錨點**:04.md:166(全 repo 零 a11y 工具);三 app package.json 無 lint script(稽核源)
- **根因**:全案零 linter,前端債無聲累積(合成 §6)。
- **改法**:shell/anilalm 加 ESLint flat config + `eslint-plugin-jsx-a11y`;governance 加 `eslint-plugin-vuejs-accessibility`。存量違規以 baseline 檔凍結(計數 ratchet,掛 C5 機制);新增違規 fail CI。air-gapped 注意:新 devDependencies 進 lock 檔,離線 bundle 需重打包(記入 bundle 重打包待辦,見 W1-6 風險欄)。
- **工作量**:2
- **依賴**:無。blocks 之後所有前端包的「lint 綠」隱含驗收。
- **驗收**:三 app `npm run lint` exit 0;seed 違規(`<img>` 無 alt)exit 非 0;CI job 存在且綠;baseline 計數寫入 C5 ledger。
- **派工**:實作B;驗收甲。
- **風險**:baseline 凍結後無人清 → C5 ratchet 每 Wave 收尾檢查一次。

#### W0-4(=合成 Gate ③b + 陷阱 9)tokens 對比門檻 + D1/D2 重算
- **錨點**:`packages/tokens/scripts/verify.mjs`(已接 `npm test` ◎複驗);`apps/anila-shell/index.html:74-93,119`;07.md D1 重算表(1.347 倍系統性偏差)
- **根因**:對比值靠手算且已錯一輪;D2 三值(`fg-subtle 3.41`/`warn 2.55`/`success 4.16`)未重算,不可直接進門檻。
- **改法**:把 oklch→linear sRGB→WCAG 相對亮度演算法寫進 `verify.mjs`(純 node,air-gapped 友善);**先重算** D1 七值 + D2 三值,重算結果落檔;再把「文字 <4.5:1、UI 元件 <3:1 → fail」設為門檻,現存未達標值進 baseline(修色排 W3-10,不在此包)。
- **工作量**:1
- **依賴**:無。blocks W3-10。
- **驗收**:① `node packages/tokens/scripts/verify.mjs` 對 seed 壞值 exit 非 0;② D1 七值輸出與 07.md 重算表逐項相符(±0.01;golden:official 2.25/teal 3.48/slate 1.87/moss 2.96/clay 3.69/indigo 2.44/crimson 2.60);③ D2 三值的重算數字寫入 repo(檔案存在即驗)。
- **派工**:實作A;驗收甲(逐值比對 golden)。
- **風險**:演算法本身寫錯 → golden 七值就是防呆;若與 07 值系統性偏差,先裁演算法再進門檻。

#### W0-5(=合成 Gate ③c)bundle 預算 fail-build
- **錨點**:04.md:183(1,001 kB 警告被無視;`vite.config.js` 無 manualChunks)
- **根因**:Vite 警告不 fail build,CI 攔不到。
- **改法**:shell/anilalm build 後置 size assert(腳本讀 `dist/assets/*.js` 總量與最大 chunk,超出即 exit 1);預算 = 現值凍結(縮減屬 W3 的 React.lazy 工作,另計)。
- **工作量**:0.5
- **依賴**:無。
- **驗收**:門檻調低 1 kB 重跑 → build fail;現行 build 綠;CI 證據。
- **派工**:實作B;驗收甲。
- **風險**:低。

#### W0-6(重框定,§1 F1)CI 三事實修正
- **錨點**:`gate1-ci.yml:3-11`(branches 白名單)、`:554-583`(anilalm 無 test)◎複驗;`spanTree.test.jsx:14-18`(beforeEach try/catch 吞 localStorage 失敗)◎複驗;08.md C5(shell 測試永遠只跑 node 20)
- **根因**:anilalm 零測試設施;測試環境失敗被靜默吞掉;node 版本單一化讓缺口永久不可見。
- **改法**:① anilalm 加 vitest + `test` script,首發 3 支冒煙測試(ErrorBoundary 不輸出 stack 字串、OutputsPage 基本 render、`theme/tokens.ts` 匯出形狀),gate1 job 補 `npm test`;② spanTree beforeEach 移除 try/catch,改直接 `setItem` 並斷言讀回(環境壞 → 紅,不再空轉);③ shell 測試 job 改 node matrix `[20, 22]` + repo 根加 `.nvmrc`;④ 軍方分支 CI:**不做**(§6.1),記欠帳。
- **工作量**:1.5
- **依賴**:無。
- **驗收**:① `grep -n "catch" apps/anila-shell/src/__tests__/spanTree.test.jsx | head -3` 在 beforeEach 區 0 命中;② gate1-ci.yml `frontend-shell` job 的 `node-version` 為 matrix 且含 22,CI 兩腿皆綠;③ anilalm `npm test` 於 CI 執行且綠;④ `.nvmrc` 存在;⑤ **⚠(Opus 5 缺漏審查補入)刪除 `apps/anila-shell/e2e/README.md`**(它宣稱 `functions.spec.js` 覆蓋 RBAC / verb whitelist injection rejection / ownership 403,而該檔不存在、repo 無 playwright 依賴——這是 S5 類的對讀者不實陳述,比誠實地說「沒有 E2E」危險):`test -e apps/anila-shell/e2e/README.md` 回非 0,或該檔內容改為「本目錄目前無 E2E;補齊屬 Gate 4+」且 `grep -c "functions.spec.js" ` =0。原 §6.2 只說「併入 W0-6 順手」而未給驗收 → 無 owner 會漏。
- **派工**:實作B;驗收甲。
- **風險**:移除 try/catch 後 node 20 也紅 = 那 13 個測試一直在空轉的實錘 → 修測試本體,禁止加回 catch。

#### W0-7(=T1-5)前端錯誤邊界(CRITICAL·S,投報第一)
- **錨點**:`apps/anila-shell/src`(ErrorBoundary grep=0)、`apps/csp-governance-ui/src/main.js`(10 行無 errorHandler)、anilalm `ErrorBoundary.tsx:63,81,87`(印 stack;`localStorage.clear()` 清跨 app)
- **根因**:render throw = 白畫面零出路;唯一 boundary 反而洩漏內部路徑並誤傷同源另兩 app 的 localStorage。
- **改法**:① shell:top-level class boundary 包 `<App/>`,另在訊息列表層包細粒度 boundary(單則訊息炸不掀全頁);fallback = 繁中訊息 + 重新載入鈕 + 短錯誤代碼(代碼→console 對應,不顯示 stack);② governance:`app.config.errorHandler` + 全域 fallback banner;③ anilalm:移除 stack/componentStack 渲染;`localStorage.clear()` 改為只刪 `anilalm-` 前綴 key。
- **工作量**:1(含三 app throw 注入測試)
- **依賴**:無。
- **驗收**:① `grep -rn "getDerivedStateFromError" apps/anila-shell/src | wc -l` ≥1;② `grep -n "errorHandler" apps/csp-governance-ui/src/main.js` ≥1;③ `grep -rn "localStorage.clear()" apps/anilalm/src | wc -l` =0;④ 新測試:子元件 throw → fallback 出現且輸出不含 `at `(stack 特徵)——三 app 各一支,CI 綠。
- **派工**:實作A;驗收甲。
- **風險**:boundary 吞掉開發期的真錯 → fallback 一律 `console.error` 全文 + 待 W3-3 request-id 後接上報。

#### W0-8 改寫 `CLAUDE.md` §5.1
- **錨點**:`CLAUDE.md` §5.1;現行 `platform.yml:807-822`;07.md NOT-SUPPORTED 段
- **根因**:過時的安全主張造成錯誤排序與重工(已修的 CRITICAL 掛在 No-Go 首位)。
- **改法**:重寫 §5.1:移除 codeserver RW/BACKUP_DIR/分享連結(分類面)三條已修主張;保留並更新「n8n/gitlab 無 `profiles:`」(掛 N-6);**分享連結精確措辭**:「分類面已 fail-closed、`create_share` 未讀 `ENABLE_PUBLIC_SHARE` 旗標仍待修(W3-7c)」;附驗證指令與快照日期。No-Go 判定本身不動(資料門檻仍依 roadmap §6.1)。
- **工作量**:0.5(文件)
- **依賴**:無。
- **驗收**:`grep -c "read-write 掛 repo root" CLAUDE.md` =0;新 §5.1 含 `platform.yml:807` 級的現行行號引用;n8n/gitlab 條目存在。
- **派工**:Claude 家撰寫(本鏈或 Opus);**驗收乙(sol)**。
- **風險**:把「分享連結」寫成全修好會關掉 W3-7c 真缺陷 → 驗收乙逐句比對 07.md C1 裁決。

---

### Wave 1 — 停止說謊

#### W1-0(=N-5)UX 零訓練前測(在 Wave 1 前端改動落地**前**執行)
- **錨點**:第一輪 UX 審查(驗收條件全是 admin 面之弊)
- **根因**:無基線就無法區分「變直觀」與「搬位置」。
- **改法**:5 名院內使用者 × 5 任務(找回上月對話/匯出一份對話/上傳文件並提問/更改密碼/找到快捷鍵面板),記錄完成率與首次點擊正確率;Wave 3 收尾複測同題。
- **工作量**:1/輪(人工;腳本由偵察草擬)
- **依賴**:無;必須先於 W1-1/W1-2 上線。
- **驗收**:`docs/planning/ux-baseline-2026-07.md` 存在,含 5×2 數字表。
- **派工**:user 協調受測者;任務腳本 composer-2.5(唯讀產出);無需反家(非程式)。
- **風險**:樣本小 → 只作方向指標;文件標明不可當 KPI。

#### W1-1(=T0-3,併 N-3/N-4)外流面五 gate 改吃 `classification_level`
- **錨點**:`services/csp/app/api/conversations.py:316,292`;`chat.jsx:490`(main:475)、`chat.jsx:2184`;`app.jsx:2521-2528`;範本 `conversation_service.py:376-389`;`models/artifact.py:288-300`;`modules/policy/service.py:262`
- **根因**:legacy `classified` boolean(=level≥機密,**非絕對機密**——五級 `無機密<營業秘密<機密<極機密<絕對機密`)被當授權輸入 → 營業秘密在複製/匯出/分享/列印/稽核五個外流面等同無機密。
- **改法**(六件一組,不可拆散上線):
  1. audit/搜尋 gate:`if conv.classified` → 以 `classification_level != 無機密` 判定(fail-closed:未知值視同受控,照 `is_publicly_shareable` 寫法)。
  2. 前端三 gate(canCopy/匯出/分享)改吃 API 已回傳的 `classification_level`(`conversations.py:127`)。
  3. 匯出檔頁首:`密等 + 匯出者 + 時間 + 來源系統`(`app.jsx` `exportConversation`)。
  4. `ExportRecord.artifact_id` 改 nullable(alembic migration)+ 新後端端點 `POST /api/conversations/{id}/export-record`;前端先落列成功才產檔,失敗則擋。
     > ⚠ **節奏解耦(Opus 5 缺漏審查補入)**:六件雖「不可拆散上線」,但④是 migration + 新端點,與①②③⑤⑥的前端文案不同節奏。**把④做成 feature flag(`ANILA_EXPORT_RECORD_REQUIRED`,預設 off)**:flag off 時①②③⑤⑥可先上(禁令已收緊、檔案已有密等頁首,只是尚未落稽核列);flag on 為 Wave 1 離開條件。否則④一卡,五件已完成的收緊工作會被綁住不能上。
  5. print stylesheet:`@media print` 保留浮水印與密等頁首;`classification_level ≥ 機密` 時 print 遮蔽內文。
  6. **N-3 禁令姿態統一**:被擋動作一律 render disabled + tooltip(依據 + 替代路徑),不再整條消失;**N-4 收緊文案**:「為何昨天能匯出今天不行」進 tooltip 與 changelog(W1-9),否則使用者的因應是截圖/手機拍屏,淨資安效果為負。
- **工作量**:3(前後端 + migration + 測試)
- **依賴**:blocked-by E0-9(PR#50 裁決)、W0-2(真 PG 測 audit);blocks W1-9;與 N-7 ledger 對扣(五處引用清掉後 ratchet 下修)。
- **驗收**:① `grep -n "canCopy = !classified" apps/anila-shell/src/chat.jsx` =0;② `grep -n "if conv.classified" services/csp/app/api/conversations.py` =0;③ pytest(真 PG):讀取營業秘密對話 → `audit_logs` 落列(先紅後綠);④ pytest:匯出營業秘密 → `export_records` 落列且回應含密等頁首字樣;⑤ `grep -rc "@media print" apps/anila-shell/src apps/anila-shell/index.html` 合計 ≥1;⑥ 前端測試:營業秘密對話的匯出鈕為 disabled 且 tooltip 含替代路徑文字。
- **派工**:後端實作B、前端實作A(各自 worktree,同 PR 收斂);驗收甲。
- **風險**:**淨退化陷阱**——門檻寫成「絕對機密」即重演第一輪錯誤,驗收 ③④ 用營業秘密案例釘死;audit 列量上升(營業秘密讀取開始記帳)→ 與 W1-5 的稀釋治理一起觀察;匯出先落列的網路失敗路徑要 fail-closed(斷線=不放行)並給重試文案。

#### W1-2(=T0-1 止血)非圖片附件明確擋下
- **錨點**:`app.jsx:203-220`(`buildUserContent` 只組檔名);`chat.jsx:1233`(≥10MB 圖片靜默降級)
- **根因**:聊天路徑沒接 parser,模型只收到檔名 → 靜默幻覺;UI 全程顯示成功。
- **改法**:composer 攔非圖片附件:不上傳、顯示導引卡(「請改用『我的知識庫』上傳後對其提問」+ 入口連結);≥10MB 圖片:顯式錯誤 toast,不建 chip。真修(parser 注入)= W3-9。
- **工作量**:0.5(含前端測試)
- **依賴**:E0-9。
- **驗收**:前端測試:拖入 `.pdf` → 導引卡出現且送出 payload 無 `[附件]` 段;`grep -n "\[附件\]" apps/anila-shell/src/app.jsx` 僅存於圖片成功路徑或 =0。
- **派工**:實作A;驗收甲。
- **風險**:功能感受倒退 → 導引文案必附替代路徑;W3-9 上線後回收此擋(在 W3-9 驗收裡列明)。

#### W1-3(=T0-2,**擴充**)使用者面字面誠實化(不只「加密模式」)
- **錨點**:`app.jsx:3184`(「加密模式」);`api/agents/credentials.py:55`;`user_memory.py:134`;`ceiling.py:5`;**⚠(Opus 5 缺漏審查補入)`app.jsx` MemoryTab 空狀態文案**(`ENABLE_MEMORY=False` 預設關、card 分支明確 false,而文案說「和 ANILA 多聊聊…平台會自動學習」——功能沒開,這句是假的;`config.py:38`、`memory_service.py:1130` 關閉時回空 200);命令面板還有「開啟設定 → 記憶」捷徑主動把人帶過去(`app.jsx:743-747`)
- **根因**:S5 一級缺陷類別——UI 字面與實作相反。① 全 repo 零 at-rest 加密(`pgcrypto|LUKS|dm-crypt|TDE` grep=0)卻稱「加密模式」;② 記憶功能關閉時 UI 承諾一個永遠不會來的東西。**兩者是同一類缺陷,同一包修完才不會只修一半。**
- **改法**:① UI/稽核訊息/docs 的「加密模式」一律改為「密等鎖定(latch)模式」類正確措辭;`requires_encryption` 欄位名不動(改名是 schema 事務,掛 C5 ledger 退場條件);at-rest 加密另立決策件交資安權責人(§6.2);② **記憶 tab 依 capability 分流**:CSP 出 `/api/capabilities`(或沿用既有 config 端點)吐 `enable_memory`,關閉時 tab 顯示「本部署未啟用記憶功能」並**移除命令面板該捷徑**,不再顯示「會自動學習」;③ 順手把 `ENABLE_PUBLIC_SHARE` 也放進同一個 capabilities 回應(W3-7c 的前端隱藏鈕要用,避免兩次做)。
- **工作量**:1(原 0.5 + 記憶分流與 capabilities 端點 0.5)
- **依賴**:無。**blocks W3-7c 的前端半邊**(共用 capabilities 端點)。
- **驗收**:① `grep -rn "加密模式" apps/ services/csp/app | wc -l` =0(允許 docstring 內歷史對照註記,驗收時逐條看);② 前端測試:`enable_memory=false` 時記憶 tab 不含「自動學習」字樣且命令面板無該捷徑;`=true` 時行為與現況一致;③ `GET /api/capabilities` 回應含 `enable_memory` 與 `enable_public_share`(pytest);④ changelog 條目存在(W1-9)。
- **派工**:實作B;驗收甲。
- **風險**:對外簡報/教材若已用「加密」字眼需同步(記 W4-5);capabilities 端點不得洩漏非必要部署資訊 → 只回布林旗標白名單,驗收逐欄看。

#### W1-4(=T0-4 邊界)API 邊界 TZ 正規化 + 共用 formatter + api_key 500
- **錨點**:`schemas/` 僅 `contracts/source_snapshot_adapter.py:34` 做正規化;`api_key_service.py:70`(naive/aware TypeError,容器內已重現,稽核源);`migrations/versions/r1_0017_gate2_pg_atomicity.py:85`(病根);`ClassificationInventoryView.vue:121`;08.md L1 的 13 處前端日期呼叫點清單;`api/service_clients.py:214`(唯一 `utcnow` ◎複驗)
- **根因**:序列化無邊界正規化,naive 值出去無時區標記,前端各自為政(四種格式),治理帳顯示錯 8 小時;`expires_at` naive 對 aware `now()` 比較 → 設到期日的 API key 每次驗證 500(**0 測試**)。
- **改法**:① 共用 pydantic base(或 `field_serializer` mixin):所有 `datetime` 出邊界一律 aware UTC(naive 視同 UTC 補 tzinfo)——這是讀取端防禦,不動庫;② `api_key_service.py:70` 比較前 `_as_utc()` + 補 `validate_api_key` 過期案例測試;③ `service_clients.py:214` 改 `now(timezone.utc)`;④ 前端共用 `formatDateTime()`(zh-TW + `timeZone:'Asia/Taipei'`),governance 13 處呼叫點換用;anilalm/shell 各放同款 util(無 JS workspace,暫容忍複製,掛 T2-11 ASR 同病一起收)。
- **工作量**:2(含測試)
- **依賴**:無;blocks W2-10/C1(先邊界後欄位)。
- **驗收**:① pytest(真 PG):任一 naive 欄(如 `classification_events.created_at`)序列化輸出以 `Z` 或 `+00:00` 結尾(先紅後綠);② pytest:過期 API key → 401 非 500(先紅後綠);③ `grep -rn "toLocaleString\|toLocaleDateString" apps/csp-governance-ui/src | wc -l` =0;④ `grep -rn "datetime.utcnow()" services/csp/app | wc -l` =0。
- **派工**:後端實作B;前端批次照**降級萃取**:實作A 先改 1 個 view 當範例 → 偵察列出全部呼叫點 → 實作B 批次;驗收甲。
- **風險**:全域 serializer 改變 API 字面,前端既有 `new Date(...)` 解析點需抽查(偵察掃描 `new Date(` 呼叫點清單附在 PR);與生產庫無關(不動資料)。

#### W1-5(=T0-6)稽核可用性與 fidelity
- **錨點**:`api/audit_logs.py:44`(僅 limit≤500);範本 `admin_inference_audit.py:216-260`;`inference_audit.py:208,250-251`(串流 acceptance 後 outcome no-op);`audit_service.log_audit_event`(恆 fail-soft);`startup_security.py` 兩份 posture 清單無 `ANILA_AUDIT_STRICT`;`audit_logs.detail` 無 trgm index(而 `messages.content` 有,稽核源)
- **根因**:治理稽核查詢面殘缺;稽核寫入失敗靜默;串流路徑 acceptance 後的 denied/error 不留痕。
- **改法**:① audit_logs API 加 `created_at` 範圍 + cursor 分頁 + CSV 串流匯出(照隔壁範本抄);② `log_audit_event` 接 `ANILA_AUDIT_STRICT`(比照 `inference_audit.py:180-183`);③ `ANILA_AUDIT_STRICT` 納入 formal posture 契約;④ alembic migration:`audit_logs.detail` gin_trgm_ops(CONCURRENTLY,autocommit block);⑤ 串流 outcome:acceptance 後的 denied/error **補寫第二列 outcome row**(不 UPDATE 原列,保 append-only 語意);⑥ `service_token_legacy_env_used` 遙測降頻(每 client 每小時 1 列 + 每日彙總),cutover 本體掛 C5 ledger(`CSP_SERVICE_TOKEN`→per-agent credential 對)。
- **工作量**:3(含測試)
- **依賴**:W0-2(真 PG 驗 index 與分頁排序)。
- **驗收**:① pytest(真 PG):date-range+cursor 分頁穩定排序、跨頁不重不漏;② CSV endpoint 回 `text/csv` streaming;③ posture 測試:formal profile + `ANILA_AUDIT_STRICT=0` → 啟動拒絕(先紅後綠);④ CI PG job `SELECT indexname FROM pg_indexes WHERE tablename='audit_logs'` 含新 trgm index;⑤ pytest:串流 acceptance 後注入上游 5xx → audit 表存在 error/denied 第二列。
- **派工**:實作B;驗收甲。
- **風險**:⑤ 動推論主路徑的稽核旁路,寫壞影響串流 → 以 flag 漸進、預設先開在 dev;若時程壓縮,⑤ 可獨立延到 Wave 2 但必須在 audit docstring 寫明現況限制(不可無聲延期)。

#### W1-6(=T0-5)備份修到「能還原」與「文件不再說反話」
- **錨點**:`infra/deployment/scripts/anila-ops.sh:582,588-604`;`docs/runbooks/intranet-zero-to-prod-guide.md:180,182,183,194,195` ◎複驗(cron `backup --full`);`infra/deployment/backup/production_backup.py:140-144`(`_required_env` 硬性檢查)◎複驗;`age` 未進離線工具包(稽核源)
- **根因**:驗證器先於被驗證物(S1)——備份 profile 完整,但 cron 必炸、env 零覆蓋、runbook 三處與實作相反、restore 只到 disposable smoke。
- **改法**:① `anila-ops.sh backup` 接受 `--full`(或 runbook 改指令,擇一,以實作為準);② 盤點 `production_backup.py` 全部 `_required_env` 名單(偵察產清單),逐一補進 `.env.example` 與 `intranet-deploy.sh`(⚠ 分支姿態語意重推導,禁 apply diff);cron 範本改為 source env 檔後執行;③ `age` 進 `download-intranet-toolkit.sh` 與離線包 manifest;④ runbook 修三處反話:備份不含 `.env`/JWT 私鑰(是 `key_management_reference`)、restore 是 `restore <bundle> <target> [prepare|smoke]` 雙參數、備份目錄在 repo 外;⑤ cron 失敗可見:backup 腳本結尾寫 heartbeat 檔 + `anila-ops.sh health` 檢查 heartbeat 年齡(>25h 告警)。**正式 destructive restore 仍留 Gate 6,不在本包**(照 `anila-ops.sh:604` 設計)。
- **工作量**:3(含 shell 測試;runbook 修訂)
- **依賴**:無。與 W4-1(restore 演練 owner)銜接。
- **驗收**:① `bash anila-ops.sh backup --full` 於測試 stack exit 0(或 runbook 已無 `--full` 字樣,`grep -n "backup --full" docs/runbooks/` =0);② `grep` 比對 `_required_env` 清單與 `.env.example` 逐一命中(腳本化比對,exit 0);③ `grep -n "備份.*\.env\|JWT 私鑰" docs/runbooks/intranet-zero-to-prod-guide.md` 顯示新敘述;④ 離線工具包 manifest 含 `age`;⑤ 對測試 stack 完整走一次 `backup → restore prepare → restore smoke`,輸出存檔。
- **派工**:實作A;驗收甲。
- **風險**:動 `.env.example` 三分支姿態 → 只動 main 與 prod-intranet-card,語意重推導(鐵則);⑤ 只證明到 smoke,**不得**在任何文件宣稱「DR 已驗證」——那是 Gate 6 的簽核詞。

#### W1-7(=N-7,S3 機制第一發)legacy `classified` 最後引用點清單 + lint
- **錨點**:本檔複驗基線:`.classified` 後端非測試 12 處、shell 47 處 ◎複驗;07.md #10(修法在隔壁沒被套用的診斷)
- **根因**:S3 反模式——新 read model 落地後舊 boolean 無退場條件、無清單,每個 boundary 都要重查一次。
- **改法**:見子計畫 C5。首發:`infra/ci/legacy_ledger.json`(pattern/計數上限/owner/退場條件/期限)+ `check_legacy_ledger.py` 進 CI;`classified` 對(上限 = W1-1 落地後的實測殘量)為第一條;同檔登錄其餘七對(C5 §3)。
- **工作量**:1
- **依賴**:與 W1-1 同 Wave(W1-1 清五處後 ratchet 下修)。
- **驗收**:CI job 存在;seed 測試:新增一處 `conv.classified` 引用 → CI fail;ledger 檔八對俱全且每對有 owner 欄(可先填職稱,W4-2 具名)。
- **派工**:實作A;驗收甲。
- **風險**:上限設太鬆 = 無效 → 上限一律 = 當下實測值,只准降。

#### W1-8(=N-6)n8n / gitlab 補 `profiles:`
- **錨點**:`platform.yml:842,903`(07.md 複驗:兩服務無 `profiles:`,預設隨 stack 啟動;`AGENTS.md` §3.3 交付規格要求移除的關切未解)
- **根因**:唯一仍成立的 §5.1 阻斷項。
- **改法**:兩服務加 `profiles: ["developer-tools"]`(比照 codeserver);main + prod-intranet-card 落地;runbook 若有引用需同步。
- **工作量**:0.5
- **依賴**:無。
- **驗收**:`docker compose -f infra/compose/platform.yml config --format json | jq` 顯示兩服務含 profiles;預設 `up` 的服務清單不含 n8n/gitlab(compose config 驗證,不動 running 容器)。
- **派工**:實作B;驗收甲。
- **風險**:若 `.15` 現場有人在用 n8n → 上線前知會(W4-3 對口);**不動本機 running `anila-platform-*` 容器**(鐵則),只改 repo 姿態。

#### W1-9(=N-1)changelog 解凍 + 發佈紀律
- **錨點**:`apps/anila-shell/src/changelog.jsx:9`(`CHANGELOG_VERSION = "2026-06-12"` ◎複驗);`app.jsx:609,2571`(seen 機制活著 ◎複驗);第一輪 UX 審查(其後已上線三批使用者可見功能含 13 快捷鍵+2 觸發字元;`Cmd+/` 面板要先知道 `Cmd+/` 才找得到)
- **根因**:機制在、內容凍結;快捷鍵可發現性自我指涉。
- **改法**:① 補三批遺漏條目 + W1-1/W1-2/W1-3 的變更文案,bump `CHANGELOG_VERSION`;② composer 空狀態 placeholder 加一行「按 Cmd+/ 檢視快捷鍵」;③ 發佈紀律:PR 模板加 checklist「使用者可見變更已進 changelog?」。
- **工作量**:0.5
- **依賴**:W1-1/W1-2/W1-3 文案定稿。
- **驗收**:`grep -n "CHANGELOG_VERSION" changelog.jsx` 顯示新日期;條目數 ≥ 舊 + 4;`grep -rn "Cmd+/\|⌘/" apps/anila-shell/src` 於 composer placeholder 命中 ≥1;PR 模板檔含該 checklist 行。
- **派工**:實作A;驗收甲。
- **風險**:低。

#### W1-10(=N-2)first-run 導引 + 最小使用者說明
- **錨點**:`anila-changelog-seen`/`anila-dismissed-banners` 已在(◎複驗)、無 `first-login` 旗標;T2-6(零 help/手冊/FAQ);空狀態靠 LLM 即時生成(air-gapped 下慢/不穩)
- **根因**:基礎設施已在,沒人接 first-run;零靜態文件。
- **改法**:① `anila-first-run` localStorage 旗標 + 首登靜態導引卡(三步:選 agent、上傳知識庫、提問;純靜態,不呼叫 LLM);② `docs/guides/user-guide.md` 初版(操作 + 密等行為解釋:latch、複製/匯出限制的「為什麼」);③ header 加「說明」入口指向該文件(nginx 靜態路由或前端內嵌)。深度 onboarding 與自助功能仍在 W3-6。
- **工作量**:2
- **依賴**:無;文案與 W1-1 的 N-4 一致化。
- **驗收**:`grep -rn "anila-first-run" apps/anila-shell/src | wc -l` ≥1;新前端測試:首登渲染導引卡、二登不渲染;`docs/guides/user-guide.md` 存在且含「複製與匯出限制」一節;header 說明入口可點(前端測試)。
- **派工**:實作A(UI)+ Claude 家(文件)→ 驗收甲(UI)/驗收乙(文件)。
- **風險**:文件與實作再度說反話 → 文件內每個宣稱附對應 UI 路徑,驗收乙抽查三條與實機一致。

---

### Wave 2 — 停止流血

#### W2-1(=T1-4 ①②③⑤)RAG 熱路徑與連線池 **[G2]**
- **錨點**:`retrieval_service.py:399-436`(N+1)、`:230-233,252`(兩 session pin 過 embedding await);批次版 `modules/clearance/service.py:804`(生產呼叫者僅 `api/traces.py:173`);`database.py:26-33`(pool 30 寫死、無 timeout/recycle);`users.py:541`(全 backend 唯一 body 上限);nginx `client_max_body_size 100M`
- **根因**:逐 doc 呼叫 5-query 的 clearance 解析;池參數與 anyio 40 tokens 不匹配;RAG 每請求 pin 3 條連線;裸 `request.json()` 無上限。
- **改法**:① 熱路徑改呼 `resolve_and_evaluate_data_access_batch`(批次版已在,swap call site;**保住 Gate 2 測試斷言的字串**);② `pool_size/max_overflow/pool_timeout/pool_recycle` 走 env(預設值不變,加 timeout 30s/recycle 1800s);③ `embed_query` 的兩個 session 在 `await proxy_request` 前 close(或縮小 try 範圍);④ proxy/embeddings/agents/answer/images 四端點加 content-length 上限(env,預設 10MB)+ 413 回應;`platform.yml` 各服務加 `mem_limit`(值進 `.env.example`,姿態重推導)。
- **工作量**:3(含測試)
- **依賴**:blocked-by PR#51 merge(同檔 populate_existing);blocks W2-8(負載測試要在修後量測)。
- **驗收**:① pytest:mock clearance 呼叫計數——200 docs 的檢索 clearance 解析呼叫次數 ≤ 2(先紅後綠);② `python -c "from app.config import settings; ..."` 顯示四個池參數可由 env 覆寫;③ 單元測試:`embed_query` 期間活躍 session 數 =1(instrument);④ 11MB JSON 打 `/v1/chat/completions` → 413;⑤ `infra/policy/tests/test_gate2_retrieval_governance.py` 綠(**[G2] 若字串斷言變動 → 送治理重核**)。
- **派工**:實作B;驗收甲。
- **風險**:批次版與逐一版的授權語意若有邊角差異 → 先寫「兩版對同一 fixture 輸出一致」的等價測試再 swap;mem_limit 設太低會 OOM 自傷 → 首版只設 csp/studio,值由 dev stack 實測 RSS + 50%。

#### W2-2(=T1-3)`upload_zip` 出 event loop
- **錨點**:`api/ingestion/documents.py:556-700`(async def 內零 await 的 per-member 迴圈;`_ZIP_MAX_TOTAL_BYTES=1GB`)
- **根因**:1GB 同步 zlib+sha256+磁碟寫在 event loop 上 → 全平台 SSE/API 停擺。
- **改法**:per-member 工作(讀+驗+hash+persist)包 `asyncio.to_thread`(照 repo 內 17 處前例 ◎複驗);db 寫入留主執行緒批次化。
- **工作量**:1(含測試)
- **依賴**:無。
- **驗收**:pytest:上傳期間以 asyncio task 探針證明 event loop 每 100ms 內仍可調度(先紅後綠;紅 = 現況探針逾時);功能回歸:多檔 zip 匯入結果與現況一致。
- **派工**:實作A;驗收甲。
- **風險**:to_thread 內用到 Session(非 thread-safe)→ 改法明確隔離「純 CPU/IO 段」與「DB 段」。

#### W2-3(=T1-1,子計畫 C3 執行)`messages.parent_id` + 編輯語意 + 三重缺口
- **錨點**:`conversation_service.py:172-209`(硬刪截斷、零 audit/classification/legal_hold);`models/message.py:11-43`(無 parent_id ◎複驗);`app.jsx:1422-1425`(編輯 payload 只送一句)vs `buildMessageHistory`(`:1669,:1837,:1913`);版本切換 UI `chat.jsx:727-758` ◎複驗
- **根因**:訊息是扁平 list,「編輯」只能截斷;且該路徑繞過平台自己的稽核/分級/保全機器 → 無紀錄證據銷毀路徑。
- **改法**:依子計畫 C3(migration、回填、語意、保留政策、前端對接)。編輯路徑同步補:`log_audit_event`、classification 檢查(機密以上編輯需與 classify 同權限姿態)、`legal_hold` 拒絕;前端編輯 re-run 改走 `buildMessageHistory`(沿 active path)。
- **工作量**:5(含 migration + 前後端 + 真 PG 測試)
- **依賴**:blocked-by PR#51 merge、W0-2、C3 計畫核可;blocks 多模型並排/Arena(backlog)。
- **驗收**:① alembic 對乾淨 PG upgrade+downgrade 成功(CI);② pytest(真 PG):編輯第 3 則 → 舊分支仍在庫、新 sibling 建立、audit 落列;legal_hold 對話編輯 → 4xx;③ 前端測試:編輯後 re-run payload 含完整 active-path 歷史;④ 版本導覽 N/M 與 sibling 數一致(前端測試);⑤ `grep -n "\.delete(synchronize_session=False)" services/csp/app/services/conversation_service.py` 於編輯路徑 =0。
- **派工**:實作A(後端+migration)、實作B(前端對接);驗收甲;因觸分級語意,追加驗收乙對「分級/保留」測試設計做第二眼。
- **風險**:回填錯誤把樹接錯 → 回填腳本先在 dev stack 快照庫演練並抽 100 對話驗鏈;舊分支保留讓對話量單調成長 → C3 §e 保留政策同步落地,不得先上樹後補政策。

#### W2-4(=T1-2)串流持久化順序 + 錯誤呈現
- **錨點**:`app.jsx:1744-1756`(兩則 append 都在 stream 之後)、`:1777-1782`(catch 用 `text:` 覆蓋);`runtime/sse.js:119-124`(raw JSON 貼給使用者)、`:147-149`(AbortError 路徑正確,**不動**);27 個 `setRuntimeError` 同質紅字
- **根因**:持久化在串流成功之後才發生;失敗路徑覆蓋已生成文字。
- **改法**:① user 訊息在 stream 開始**前**持久化;② assistant 半成品週期性 checkpoint(節流 upsert)或至少 catch 時 append 保留累積文字 + `error` metadata;③ `sse.js` 錯誤 body `JSON.parse` 取 `detail` 並映射為使用者語彙(對照表:模型未註冊/逾時/上游中斷…),原文進 console;④ 錯誤橫幅加「重試」鈕(重送同一 user 訊息)。
- **工作量**:2(含前端測試)
- **依賴**:E0-9;與 W2-9 同檔,建議同一實作者串行。
- **驗收**:① 前端測試:mid-stream 注入網路錯 → 已累積文字仍在 DOM 且重整後(mock 持久層)user+partial 存在;② 測試:AbortError 路徑行為與現況 bit-for-bit 一致(守住做對的部分);③ `grep -n "await response.text()" apps/anila-shell/src/runtime/sse.js` 後續有 JSON.parse 分支;④ 重試鈕測試:點擊 → 重發 payload 等於原 payload。
- **派工**:實作A;驗收甲。
- **風險**:checkpoint 寫入頻率過高打 DB → 節流 ≥2s 且僅 delta;錯誤語彙映射漏項 → 未知碼 fallback「發生錯誤(代碼)+ 重試」。

#### W2-5(=T2-8)引用與 markdown 共存
- **錨點**:`chat.jsx:530-538`(三元式二選一);`trust.jsx:57-72`(`renderTextWithCitations` 純文字切割)
- **根因**:有 citations 即繞過 `MarkdownView` → RAG 回答(平台招牌情境)失去表格/代碼/KaTeX/Mermaid。
- **改法**:引用標記改為 markdown pipeline 的後處理(rehype 層把 `[n]` 文本節點替換為引用元件,或 remark plugin),`MarkdownView` 恆走。
- **工作量**:1(含測試)
- **依賴**:E0-9;與 W2-9 同檔串行。
- **驗收**:前端測試:含 `[1]` 與表格/代碼塊的回答 → 同時渲染 `<table>`/highlight 與引用元件(先紅後綠);既有引用互動(hover/點擊)回歸綠。
- **派工**:實作A;驗收甲。
- **風險**:rehype 替換撞 code block 內的 `[1]` 字面 → 規則排除 code/pre 節點(測試釘住)。

#### W2-6(=T1-8 病根)`startup_migrations.py` 折回 alembic
- **錨點**:`services/csp/app/services/startup_migrations.py`(8 表、21% 覆蓋、`:246-248` 啟動 ALTER TYPE;docstring 自承 baseline 不符);`alert_service.py:22-25`(read-then-write 去重)
- **根因**:S2 第二套 schema 機制 → `alembic upgrade head` 不自足,DR 還原壞。
- **改法**:① 新 alembic migration 把 startup DDL 全量收編(含 `alerts` 12 欄 + UNIQUE(fingerprint) + index + FK、`users.department_id` index、`attachments.message_id` index 等 17 條);② `alert_service` 改 `INSERT ... ON CONFLICT (fingerprint) DO UPDATE`(去重靠 DB);③ `startup_migrations.run_startup_migrations()` 降級為「檢查 + 拒啟動」(schema 不符 → 明確報錯,不再自癒);legacy SQLite 自動匯入路徑(`:320-338` 無條件探測)**整段刪除**(07.md #13:威脅前提已死,純刪除即可,docstring 說 opt-in 與實作不符)。
- **工作量**:3(含真 PG 測試)
- **依賴**:blocked-by W0-1(baseline 清單就是收編清單)、W0-2。blocks W2-10。
- **驗收**:① 乾淨 PG `alembic upgrade head` 後 `check_orm_pg_drift.py` 輸出 0 條非白名單(timestamp 型別除外,那是 W2-10);② `alembic downgrade -1` 成功;③ pytest(真 PG):併發兩路同 fingerprint 告警 → 庫內 1 列(先紅後綠);④ `grep -n "LEGACY_SQLITE_DEFAULTS" services/csp/app/services/startup_migrations.py` =0;⑤ 啟動於 schema 落後的庫 → 拒啟動且訊息指向 alembic(整合測試)。
- **派工**:實作A;驗收甲。
- **風險**:對 `.15` 既有庫,新 migration 必須冪等(`IF NOT EXISTS` 姿態)因為該庫欄位已被 startup DDL 補過 → migration 寫成「補齊缺失」而非「新建」;`alerts_fingerprint` 若現庫有重複列,UNIQUE 建立會炸 → migration 先去重(保留最新)再建約束,並在 upgrade 註解寫明。

#### W2-7(=T1-4 ④)event-loop 同步阻塞批次收斂
- **錨點**:`services/proxy/service.py:1836-1846`(自承 production deadlock ring,已修 1 處);「同 pattern 33 處」(稽核源;實際清單以偵察掃描為準)
- **根因**:sync DB/IO 在 async def 內直呼。
- **改法**:**降級萃取**:實作A 修 1 處代表案例(含測試)→ 萃取「判定規則 + 改寫模板 + 已完成範例」→ 偵察產全清單 → 實作B 批次;每處保留原語意,只包 to_thread。
- **工作量**:2(範例 0.5 + 批次 1.5)
- **依賴**:PR#51 merge(同檔面)。
- **驗收**:偵察清單(檔:行)進 PR 描述;批次後複掃 → 殘量 =0 或逐條豁免註記;pytest 全套綠;`asyncio.to_thread` 計數 ≥ 清單數(grep)。
- **派工**:範例實作A → 批次實作B;驗收甲(抽 20% 逐處比對語意不變)。
- **風險**:盲目包 to_thread 讓 Session 跨執行緒 → 模板明定「session 取得與使用不得跨 thread 邊界」;這正是無範例批次必失敗的型。

#### W2-8(=N-9)負載測試三 profile(Wave 2 離開條件)
- **錨點**:全 repo 零 k6/locust/vegeta/wrk(稽核源);T1-4⑥「修完你也不知道修夠了沒」
- **根因**:零量測 → 「數百人」是零證據宣稱。
- **改法**:k6 三 profile(純聊天 SSE / RAG 聊天 / 上傳+索引),對**臨時拉起的隔離 stack**(`compose.dev.yaml` 另一 project 名)跑,**禁打 `anila-platform-*` 與 `.15`**(鐵則);記錄 p95 與飽和點(RAG 併發天花板實測值,對照修前理論值 10)。
- **工作量**:2
- **依賴**:blocked-by W2-1、W2-2(量修後的);腳本可提前寫。
- **驗收**:`docs/planning/load-baseline.md` 存在,含三 profile 的 p95/錯誤率/飽和併發數與 stack 規格;k6 腳本入 repo `infra/loadtest/`。
- **派工**:實作B;驗收甲(重跑一次抽樣核對數字量級)。
- **風險**:本機 GPU/模型與 `.15` 不同 → 文件明標「相對值供回歸比較,非生產容量承諾」;數字不設及格線(那是 Gate 6)。

#### W2-9(=T1-6)串流渲染 memo 化
- **錨點**:`chat.jsx` 全檔 `React.memo` 0 次;串流累積 `(prev[convId]||[]).map(...)` 換整條 identity(`app.jsx:1448-1460`);`MessageBubble` 收 3 個 inline closure
- **根因**:每 token 40 則訊息 × 完整 markdown pipeline 重解析。
- **改法**:`MessageBubble`/`MarkdownView` 包 memo;inline closure 改 `useCallback`;串流累積只換該則訊息 identity(其餘元素引用不變)。這是拆 `ChatRuntime` 的第一刀,不做虛擬化(另列 backlog)。
- **工作量**:2(含效能測試)
- **依賴**:E0-9;與 W2-4/W2-5 同檔,同一實作者串行,順序:W2-4 → W2-5 → W2-9。
- **驗收**:前端測試:以 render 計數器證明串流 append 時非活躍訊息的 `MessageBubble` render 次數 =0(先紅後綠);`npm run build` 綠;手動冒煙:40 則對話串流不卡頓(記錄於 PR)。
- **派工**:實作A;驗收甲。
- **風險**:memo 比較函式漏 prop → 引用穩定性測試釘 3 個 callback;與 PR#50 的引用分組功能交互 → 回歸跑該 PR 帶入的測試。

#### W2-10(=T0-4 欄位面,子計畫 C1 批次 1)治理帳 timestamp 欄轉 timestamptz
- **錨點**:93 naive 欄(實庫 introspection,稽核源);病根範例 `r1_0017:85`;寫入端語意 ◎複驗(§1 F8)
- **改法**:依子計畫 C1:① 批次 1 = 治理帳 + api_keys(小表、法律證據優先);② **`USING col AT TIME ZONE 'Asia/Taipei'`**(D4 已決,2026-07-26);③ **同一 PR 內把該批欄的 ORM 宣告一併改為 `DateTime(timezone=True)`,並從 W0-1 政策段 baseline 下修對應筆數**——只改 PG 不改 ORM 會讓剛做完的正確工作反而觸發 drift 告警;④ migration 檔頭寫入 C1 §c 要求的判讀脈絡段;⑤ 批次 2(大表 messages/document_chunks 等)另窗。
- **工作量**:3.5(批次 1,含真 PG 測試、ORM 同步與演練)
- **依賴**:blocked-by W1-4(邊界先行)、W2-6(單一 schema 機制)、**C1 §c 三項執行前置**(生產受影響筆數報表 / 資料權責人書面簽核 / 獨立快照)。
- **驗收**:見 C1 §g;**追加**:① migration 檔頭含判讀脈絡段(grep 關鍵句);② 簽核文件與 migration 同 PR;③ `downgrade` 用對稱 `AT TIME ZONE 'Asia/Taipei'` 還原並經 upgrade→downgrade→upgrade 冪等測試。
- **派工**:實作A;驗收甲 + 驗收乙(法律證據語意第二眼)。
- **風險**:見 C1 §f(鎖時間、rewrite、downgrade)。**⛔ 追加鐵則**:本包完成前 **csp-db 容器 TZ 必須維持 UTC**——`classification_events`/`declassification_requests`/`classification_authority_assignments`/`policy_decisions` 的 `created_at` 皆有 `server_default CURRENT_TIMESTAMP`,DB session TZ 一改就會讓新舊值在同一欄混兩種語意且無標記可分(見 C1 §c 追加證據)。此條同時寫進 `.env.example` 與 compose 註解。

#### W2-11(=完整性審查 L3;**Opus 5 缺漏審查新增**)分類正確性的輸入端 · CRITICAL
> **為何新增**:原計畫把 S4 的**輸出端**做得很完整(W1-1 外流五 gate、W3-12g anilalm 浮水印、W3-12h Studio 標示),但**輸入端一條都沒有**——`reviewer` / `per-document` / `自我宣告` 三個關鍵詞在原計畫命中數皆為 0。而「涉密事故從來不發生在中間層,它發生在『有人標錯了』和『有人把檔案帶走了』」——只修後者等於做一半。

- **錨點**(◎Opus 5 複驗):`services/csp/app/api/ingestion/documents.py:351` `classification_level = _locked_collection_classification(db, collection_id)`(純繼承,無 per-document 宣告);同檔 `grep "approve|approval|審核|核准|reviewer|待審"` → **0**;`api/ingestion/collections.py:110-115` `create_collection` 只需 `get_current_user`(**任何人都能建知識庫**);`grep "pii_detect|presidio|auto_classify|敏感詞"` 全域 → 0;`docs/governance/data-governance.md:83` 自承「PII detection(待完整實作)」;`classification_inventory.py` 是**一次性 cutover attestation 報表**,不是持續正確性稽核
- **根因**:整套五級分類機器(18 張表 CHECK 全覆蓋、clearance/compartment/need-to-know、雙人降密+公文文號)建立在一個**自我宣告且無人複核的輸入**上。**失效鏈**:任何人建一個「無機密」知識庫 → 丟進機密文件 → 平台標成無機密 → 對所有無機密 clearance 的人開放。**零成本、零偵測、零紀錄。** 而降密要兩個人加一份公文文號 —— **不對稱到了荒謬的程度**。
- **改法**(最小可行版,不做內容式偵測):
  1. **per-document 密等宣告**:`POST /documents` 增 `classification_level` 選填欄;**預設繼承 collection,但可上調**(只准上調,不准下調——沿用單向 latch 不變式)。
  2. **高於 collection 時走既有 latch 升級**:文件宣告高於所屬 collection → 觸發 collection latch 升級(呼既有 `apply_classification` 路徑,寫 `ClassificationEvent`),而非拒絕上傳。
  3. **上傳者確認閘**:UI 上傳流程加一步「本文件密等 = X(繼承自知識庫);若實際更高請在此上調」,附「誤標低密的後果」一行文案。**這是最便宜的正確性槓桿**——把自我宣告從隱式變顯式。
  4. **抽查機制**:`classification_inventory` 增一支持續性報表(隨機取樣 N 份/週,列出「文件標題 + 現行密等 + 上傳者 + 所屬 collection」供權責人複核),複核結果寫 audit。
  5. **建立知識庫的密等上限**:`create_collection` 若宣告密等 > 建立者 clearance → 拒絕(現況未檢查,屬同一輸入端缺口)。
  6. 內容式偵測(PII / 敏感詞 / auto-classify)**不在本包**,列 §6.2 決策件(需模型與詞庫,air-gapped 供應鏈成本另計)。
- **工作量**:3(後端 + migration 選填欄 + UI 一步 + 報表;含測試)
- **依賴**:blocked-by W0-2(真 PG 測 latch);與 W1-1 共用「fail-closed 分類判定」的寫法,建議同一實作者;blocks 無。
- **驗收**:① pytest(真 PG):上傳宣告「機密」到「無機密」collection → collection latch 升級為機密且寫 `ClassificationEvent`(先紅後綠);② pytest:宣告低於 collection → 400/422(不准下調);③ pytest:建立密等超過自身 clearance 的 collection → 403(先紅後綠);④ 抽查報表端點回傳可複核欄位且複核動作寫 audit(pytest);⑤ 前端測試:上傳流程含密等確認步驟且顯示繼承來源。
- **派工**:後端實作B、UI 實作A;驗收甲 **+ 驗收乙**(分級不變式屬安全紅線,需第二眼)。
- **風險**:**上調觸發 collection latch = 不可逆**(單向 latch)→ UI 必須在確認步驟明示「這會把整個知識庫升級為 X 且無法降回」,並要求二次確認;誤上調的救濟只有雙人降密流程,文案要指出這條路;抽查報表本身含文件標題 → 端點需 admin + 該 collection 可見性雙重 gate,別造出新的洩漏面。

#### W2-12(=後端 #4 + backend §1;**Opus 5 缺漏審查新增**)結構化錯誤信封與 API 契約 · HIGH
> **為何新增**:原計畫零命中 `error envelope` / `object Object` / `response_model` / `OpenAPI`。而 W1-1/W1-2/W2-4 **全都在新增使用者面錯誤路徑**——現在不定信封,這批工作會把問題放大而不是收斂。`X-Request-ID` 雖在 W3-3⑤,但那只解「找得到 log」,不解「前端拿到的東西能不能用」。

- **錨點**:全 backend **877 個 `HTTPException`,`detail` 為 dict 的只有 3 處**(`services/proxy/service.py:180,582`、`api/proxy.py:326`,`{"code","message"}` 是好範本);全 repo **零 `add_exception_handler`** → `RequestValidationError` 的 422(array)與 handler 的 `{"detail": str}` 是兩種 shape;`apps/csp-governance-ui` **104 處 / 22 檔**直接插值 `e.response?.data?.detail`(例 `KnowledgeCollectionsView.vue:173`、`ApiKeysView.vue:221`)→ dict/array 渲染成 `[object Object]`;`apps/anilalm/src/api/client.ts:129-141` 處理 string/array 但**不處理 dict**(退化成 `"503 Request failed"`,訊息丟失);**`LoginView.vue:419` 的待核准分支靠比對後端中文子字串**(`detail.includes('等待核准')`)→ 後端改一個字,登入 UX 靜默壞掉;`app/main.py:486-511` `/openapi.json` 與 `/docs` 被 `require_admin` 鎖且 `ENABLE_API_DOCS` 預設 false,**repo 內無 CSP 的 OpenAPI 匯出腳本**(只有 studio 有);**243 個 endpoint 中 91 個(37%)無 `response_model`**
- **根因**:無 API 契約層。**已造成過生產事故**——`startup_migrations.py:245-247` 註解:「Pydantic ResponseValidationError was firing on GET /api/audit-logs because PG returned ints」。
- **改法**(範圍刻意收窄,不做全面 refactor):
  1. **註冊兩個 exception handler**:`HTTPException` 與 `RequestValidationError` → 統一信封 `{"error": {"code", "message", "details"?, "request_id"}}`(`request_id` 接 W3-3⑤);**保留 `detail` 欄位一個 release 供舊前端過渡**(雙寫,避免一次切)。
  2. **error code 列舉**:先只為「前端需要分流」的路徑定 code(登入待核准/未核准、模型未註冊、clearance 不足、配額超限、分級禁令、逾時)——約 15 個,不求覆蓋 877 個。
  3. **`LoginView.vue:419` 改吃 code**,不再比對中文子字串。
  4. **三個前端各一個共用 `extractError(e)` helper**(governance 104 處逐步收斂,先改共用 helper + 高頻 10 處,其餘掛 C5 ledger ratchet)。
  5. **CSP OpenAPI 匯出腳本**(照 studio 的 `export-openapi.py`)+ CI 檢查產出物與程式碼同步;`response_model` 覆蓋率**只設 ratchet 不設目標**(baseline = 現值 63%,只准升)。
- **工作量**:3(後端 1.5 + 前端 1 + 契約腳本 0.5;含測試)
- **依賴**:建議排 **Wave 2 末**(W1-1/W1-2/W2-4 的新錯誤路徑落地後,一次收斂比邊做邊改省);`request_id` 欄位可先留空,W3-3 後補實。
- **驗收**:① pytest:任一 `HTTPException` 與任一 422 的回應 JSON 皆符合信封 schema(schema 測試);② pytest:回應同時含 `error.code` 與 legacy `detail`(過渡雙寫);③ 前端測試:後端回 dict detail 時 UI **不出現** `[object Object]`(先紅後綠);④ 前端測試:待核准帳號登入 → 走 code 分流,且把後端中文訊息改字後測試仍綠(這條就是防再犯);⑤ `scripts/export-openapi.py` 產出物入 repo,CI 比對不同步即 fail;⑥ `response_model` 覆蓋率 ratchet baseline 檔存在。
- **派工**:後端實作B、前端實作A;驗收甲。
- **風險**:改信封是**跨切面 breaking change** → 過渡期雙寫 + 契約測試釘住舊形狀,一個 release 後才移除 `detail`;15 個 code 若定得太細會失控 → 只為「前端有分流需求」的路徑定,其餘走 generic;governance 104 處不在本包清完(掛 ledger)要在 PR 描述明寫殘量,不得宣稱已收斂。

---

### Wave 3 — 能上線

#### W3-1(=T2-1,子計畫 C2 Phase 0+1)「這個人能看到什麼」可回答
- **錨點**:三前端 `clearance|compartment|need-to-know` grep=0;`collection-access-grants`/`required-compartments` 無 GET(僅 POST/DELETE);`ClearanceGrantOut`(`schemas/contracts/clearance.py:55-67`)扁平
- **根因**:授權散在 6 張持久表 + clearance 兩表,無讀取面收斂 → 涉密開通只能 curl,權限視圖不存在。
- **改法**:依子計畫 C2:Phase 0(本包)= 補齊全部授權 GET + `effective_access` 唯讀彙總視圖 + Preview Access 端點與 governance UI 頁(輸入 user → 輸出可見資源清單與依據鏈);Phase 1(雙軌新表)獨立排程,不與本包綁定。
- **工作量**:3(Phase 0,含測試與 UI)
- **依賴**:無硬依賴;C2 核可(D5)只 gate Phase 1。
- **驗收**:① 每張授權表都有對應 GET(路由清單 grep 覆核);② Preview Access API:對 fixture 使用者回傳 = 手工 SQL 對照結果(pytest,真 PG);③ governance UI 新頁 e2e 冒煙(人工記錄);④ 視圖為唯讀(無任何寫路徑,程式碼審查項)。
- **派工**:後端實作B、UI 實作A;驗收甲。
- **風險**:視圖與真實執法邏輯不同步 → 視圖必須呼叫**同一個**授權函式(clearance/service.py 的 resolve 系),禁止平行重寫判斷式;這也是 C2 Phase 1 的等價基準。

#### W3-2(=T2-2)知識庫分享 helper 補完
- **錨點**:`api/ingestion/collections.py:52-80`(`_require_collection_access` 只認 admin/created_by;docstring 自承 single point that needs to grow)、`:194-198`;表已在 `models/clearance.py:164-171`;資料面已認(`clearance/service.py:336-372`)
- **根因**:表 land 了、helper 沒長 → 被授權者看不到庫存在,逼 admin workaround。
- **改法**:`_require_collection_access` 併入 `CollectionAccessGrant` 解析(讀權);列表端點對有 grant 者回傳;上傳/管理權限維持 owner/admin(分享 ≠ 共管,首版唯讀分享);UI:collection 卡顯示「已授權」標記。
- **工作量**:2(含測試)
- **依賴**:W3-1(GET 面先在);與 T2-5 transfer 相鄰但獨立。
- **驗收**:pytest(真 PG):user B 獲 grant → 列表可見、文件清單可讀、上傳 403(先紅後綠);無 grant 者 404/403 不變;clearance 不足者即使有 grant 仍拒(AND 語意,釘住)。
- **派工**:實作B;驗收甲。
- **風險**:把 grant 做成 OR 蓋過 clearance = 最壞退化 → 驗收最後一條就是防這個。

#### W3-3(=T2-3)營運可觀測最小集
- **錨點**:csp/studio/router `main.py` 零 `/metrics`;`platform.yml` 零 `logging:`;`alert_service.py` 零通知;`/summary` 已有 `high_count` 沒人擺首頁;ingestion-worker 已輸出 Prometheus 格式無人抓;CSP log 在容器內未掛出;零 `X-Request-ID`
- **根因**:S1 的營運面——資料在或半在,入口與輪替全缺。
- **改法**:① 三服務加 `/metrics`(prometheus_client;先 request count/latency/pool 使用率/SSE 併發 + 6 個 SLO producer 的第一批:`auth_error_rate`、`queue_age_seconds`、`stuck_job_count`——與 S1 對齊);② `platform.yml` 全服務 `logging: {driver: json-file, options: {max-size, max-file}}`;③ CSP log 目錄 bind mount 出來;④ alert 通知:air-gapped 下首版 = governance 首頁掛 `/summary` 卡 + AlertsView 輪詢(30s)+ `anila-ops.sh health` 讀 alerts API;SMTP 之類外送通道列決策件(內網有無 mail relay 是組織事實);⑤ `X-Request-ID` middleware(入站生成、回應頭帶出、log 記錄、前端錯誤 UI 顯示);⑥ prometheus 服務本體**不進本包**(是否進 stack 屬部署決策,列 backlog);⑦ **⚠(Opus 5 缺漏審查補入)服務健康總覽視圖**——admin-journey D1「22 個 governance view 沒有一個顯示 router / anila-studio / ingestion-worker / csp-db / redis / nginx / pptx-renderer 狀態」原計畫零覆蓋(`健康總覽|服務健康` 命中 0)。CSP 新增 `GET /api/admin/health/overview` 彙總各服務(既有 `health_checker` 已有 model/agent 五態,補基礎服務探測)+ governance 首頁一張總覽卡(綠/黃/紅 + 最後檢查時間)。**這是管理者目前唯一真實工具是 SSH 跑 `anila-ops.sh health` 的直接解**。
- **工作量**:6(原 5 + 健康總覽 1;含測試)
- **依賴**:無。
- **驗收**:① `curl :8000/metrics` 三服務 200 且含指定 metric 名(CI 整合測試);② `docker compose config` 顯示每服務有 logging options;③ 對 stack 發一請求 → 回應頭有 `X-Request-ID` 且 log 檔同 id 可 grep 到;④ governance 首頁含 alert 摘要卡(前端測試);⑤ SLO 三指標在 `slo_window_verifier.py` 的 schema 下可被讀取(單元測試)。
- **派工**:實作B(後端)、實作A(UI);驗收甲。
- **風險**:metrics 端點洩內部資訊 → 掛 admin auth 或僅內網 network 可達(nginx 不路由);log rotation 姿態進 `.env.example` 三分支語意重推導。

#### W3-4(=T2-4)配額與並行上限
- **錨點**:`0006_drop_quota_rate_limit.py`(**當時合理的設計決策**,前提已變——不是修 bug,是加能力);`token_usage` 計量已齊;nginx per-IP 100r/s 在共用 NAT 下既誤傷又擋不住單人
- **改法**:① per-user 並行串流上限(in-app 計數,env `ANILA_MAX_CONCURRENT_STREAMS_PER_USER`,預設 3,超限 429 + 繁中文案);② per-user/day token 軟配額(讀 `token_usage` 聚合,超限僅警示 banner,**不硬擋**——首版避免誤傷);硬擋與 department 配額列 Phase 2 決策件。
- **工作量**:3(含測試)
- **依賴**:W2-1(池參數理順後才談公平性)。
- **驗收**:pytest:同 user 第 4 條並行串流 → 429 且文案含「並行上限」;不同 user 不互相影響;軟配額超標 → 回應 meta 帶 warning 旗標(前端 banner 測試)。
- **派工**:實作B;驗收甲。
- **風險**:上限誤傷長對話重度使用者 → 429 文案給出路(等待/聯繫 admin);計數器要在串流終止(含 abort/error)時必然釋放——用 finally,測試釘。

#### W3-5(=T2-5)人員異動:transfer + 硬刪 pre-flight
- **錨點**:`models/ingestion.py:130-132`(自承 rely on app-layer reassign,而它不存在);`CollectionUpdate` 無 `created_by`;`users.py:437-450`(agents 有 pre-flight、collections 沒有);FK = NOT NULL + ON DELETE SET NULL(實庫,稽核源)
- **改法**:① `POST /api/ingestion/collections/{id}/transfer`(admin 或 owner;audit 落列);② `DELETE /api/users/{id}/permanent` 加 `ingestion_collections` pre-flight(比照 agents 寫法,回 409 + 指引先 transfer);③ FK 矛盾修正併入 W0-1 baseline → 由 C1/W2-6 系 migration 改為 NOT NULL + ON DELETE RESTRICT(語意:必須先 transfer)。
- **工作量**:1.5(含測試)
- **依賴**:migration 面 blocked-by W2-6。
- **驗收**:pytest(真 PG):擁有 collection 的 user 硬刪 → 409(先紅:現況 IntegrityError 500);transfer 後硬刪 → 成功;transfer 寫 audit(斷言列存在)。
- **派工**:實作A;驗收甲。
- **風險**:transfer 授權過寬 → 僅 admin + 現任 owner;分級不變(latch 不因 owner 變動)。

#### W3-6(=T2-6)自助能力最小集
- **錨點**:後端 `PUT /api/auth/password` 已在、shell 無 UI(`shellNav.jsx:28` GOVERNANCE_ROLES 不含 user);`password_strength` 只掛 `RegisterRequest`;`failed_login|lockout` grep=0;Settings 一般 tab 零輸入元件
- **改法**:① shell 帳號 tab 加改密碼表單(呼既有端點);② `password_strength` validator 掛上 `UserCreate`/`PasswordChangeRequest`/`AdminResetPassword`;③ 登入失敗節流(per-user 計數 + 指數退避,存 DB;鎖定門檻 env);④ 個人系統提示詞(per-user `ui_settings.system_prompt`,proxy 注入;涉密審視:提示詞本身按對話分級 latch 傳播)。忘記密碼自助**不做**(air-gapped 無 mail;維持 admin 重設 + 文件化流程,寫進 user-guide)。
- **工作量**:3(含測試)
- **依賴**:無。
- **驗收**:① 前端測試:帳號 tab 有表單、錯誤密碼被 validator 擋;② pytest:弱密碼過 `AdminResetPassword` → 422(先紅後綠);③ pytest:連續 N 次失敗登入 → 429/423 且退避時間遞增;④ 系統提示詞:設定後 proxy payload 含之(pytest)。
- **派工**:實作B;驗收甲。
- **風險**:鎖定機制被拿來 DoS 他人帳號 → 鎖定僅針對「該帳號+該來源」組合並附 admin 解鎖;軍方純帳密 profile 獲益最大但不傳播(§6.1 記欠帳)。

#### W3-7(=T2-7)找回自己的東西(六個獨立小包)
| 子包 | 錨點 | 改法 | 量 | 驗收(機械) | 派工 |
|---|---|---|---|---|---|
| a 搜尋排序 | `conversations.py:273-282`(subquery limit 無 order_by) | `order_by(updated_at.desc())` 移進 subquery | 0.5 | pytest(真 PG):50 筆命中取 30 → 回傳必含最新者(先紅後綠) | 實作B/驗收甲 |
| b 產出生命週期可見 | `OutputsPage.tsx`(lifecycle grep=0);`ArtifactVersionHistory.tsx` 有正確範本 | 卡片顯示 `lifecycleState` 與到期日;三合一錯誤拆分 | 1 | 前端測試:archived artifact 卡片含「已封存」字樣;`grep -n "lifecycleState" OutputsPage.tsx` ≥1 | 實作A/驗收甲 |
| c 分享建立端 gate | `conversation_service.py:427-454`(從未讀 `ENABLE_PUBLIC_SHARE`) | `create_share` 開頭讀旗標,off → 403 + 文案;前端按鈕隨 config 隱藏 | 0.5 | pytest:旗標 off 建分享 → 403(先紅後綠);07.md C1 裁決引用於 PR | 實作B/驗收甲 |
| d 附件持久化+下載 | `appendMessage` body 無 attachment 欄;`uploadAttachment` 未帶 message_id;`GET /api/attachments/{reference_id}` 前端零引用 | append 回傳 message_id 後補綁(PATCH 或 upload 帶 late-bind);chip 接下載端點 | 2 | pytest:訊息重載後 `attachments` relationship 非空(先紅後綠);前端測試:chip 點擊觸發下載 URL | 實作A/驗收甲 |
| e 對話清單分頁 | `conversation_service.py:111-120` `.all()`;API 無分頁參數 | API 加 cursor 分頁(向後相容:無參數=舊行為);前端滾動載入 | 2 | pytest:cursor 兩頁不重不漏;舊 client 無參數呼叫回傳形狀不變 | 實作B/驗收甲 |
| f `ui_settings` 併發合併 | `users.py:513-551` 整包 last-write-wins;auth hot path 每請求重讀 | 改 per-key merge(folders/convMeta 分 key 寫入)+ `_load_user_from_payload` 加 `load_only`(排除 ui_settings 大欄) | 2 | pytest:兩 session 各改不同 key → 兩者皆存(先紅後綠);auth 路徑 SQL 不含 ui_settings 欄(echo 斷言) | 實作B/驗收甲 |
- **依賴**:d 與 W2-3 同檔區(message 模型),排在 W2-3 之後;其餘無。
- **風險**:e 的向後相容形狀要釘契約測試;f 動 auth hot path,回歸跑全 auth 測試組。

#### W3-8(=T1-7 重框定)Gate 5 operator runbook(**不是** generator)
- **錨點**:`infra/policy/gate5/README.md:144-159` ◎複驗(只附 disabled template 與 synthetic generator,**不附任何 production approval**;四類 approver);`main.py:271-275` crash-loop;`.env.example:31` vs `platform.yml:121` 姿態不一致;`GATE5_MATERIAL_DIR` 在 docs 僅 1 筆
- **根因**:缺的是「誰簽、四個檔案怎麼組裝、放哪裡」的人類程序文件;寫產生腳本恰是 README 明文禁止的事(陷阱 3)。
- **改法**:① `docs/runbooks/gate5-operator-runbook.md`:四素材(inventory/signed profile/trust store/observed deployment facts)的組裝流程、簽核角色對應(接 W4-2 RACI 具名)、放置路徑與 `:ro` 掛載對應、驗證指令(`check_model_governance.py` 用法)、常見 fail-closed 訊息對照表;② 修 `.env.example:31` 與 `platform.yml:121` 的預設姿態不一致(以「formal 必須顯式啟用」為準,姿態語意重推導);③ 「加模型 = 離線重簽」流程寫明(provider binding 逐欄相符含 `transport_target_sha256`)——這是「https 端點取得所有模型」痛點的真解入口。
- **工作量**:2(文件 + 姿態修正)
- **依賴**:W4-2(具名)可後補;不 block。
  > ⚠ **條件性提前(Opus 5 缺漏審查補入)**:本包排在 Wave 3,但 Gate 5 的失敗樣態是「**新環境根本起不來**」(`main.py:271-275` crash-loop、`/ready` 503 → healthcheck 永不 healthy → `deploy` 超時,且錯誤只在容器 log)。**若在 Wave 3 之前需要在 `.15` 立新 formal 環境、換 profile、或重建 stack,本包立即升為該時點的阻斷項,必須提前。** 原計畫未標此觸發條件。
- **驗收**:runbook 存在且含四素材各自的「來源、簽核人欄位、放置路徑、驗證指令」四欄;`grep "GATE5_MODEL_GOVERNANCE_ENABLED" .env.example infra/compose/platform.yml` 兩處姿態一致;**repo 內不得新增任何 production profile 產生腳本**(驗收檢查 diff 無新增 executable 於 infra/policy/gate5/)。
- **派工**:Claude 家撰寫;**驗收乙(sol)** + user 過目(涉組織程序)。
- **風險**:runbook 又說反話(S5)→ 驗收乙逐指令實跑 dry-run 部分。

#### W3-9(=T0-1 真修)聊天附件接上 parser **[G2]**
- **錨點**:`anila_core/ingestion/parser_registry.py:627-628`、`docling_parser.py:46-47`(能力已在);`api/proxy.py`(attachment grep=0)
- **改法**:CSP 在 proxy 入口對訊息綁定的附件呼既有 parser 抽文字,注入 user content(帶來源標記);token 上限:截斷 + 明示「已截斷」;**密等繼承**:附件內容併入對話 → 對話 latch 至附件所屬層級(fail-closed);超過大小/格式白名單 → 明確錯誤。W1-2 的前端擋改為放行白名單格式。
- **工作量**:3(含測試)**[G2]**(觸 `proxy.py`,保住治理測試斷言字串)
- **依賴**:W1-2(止血在前)、W2-1(別把 parser 同步工作放上 event loop——用 to_thread,引 W2-7 模板)。
- **驗收**:pytest:上傳 txt/pdf fixture 提問 → 模型收到的 payload 含抽出文字(mock 上游斷言);超限檔 → 4xx 有文案;`test_gate2_retrieval_governance.py` 綠;前端 W1-2 導引卡對白名單格式不再出現(測試)。
- **派工**:實作A;驗收甲。
- **風險**:parser 對惡意檔案的攻擊面 → 沿用 ingestion 既有 `validate_content` 路徑,不另起爐灶;大檔 CPU → to_thread + 大小上限。

#### W3-10(=T2-9)深色 accent / focus ring / 主題持久化
- **錨點**:`index.html:74-93`(dark 覆寫缺 `--accent`)、`:119`(focus ring 用 `var(--accent)`);07.md 重算表(5/7 未達 3:1;official 預設 2.25);`resolveInitialTweaks` 只讀 `window.ANILA_TWEAKS`、`persistUiSettings` 只送 `{folders, convMeta}`
- **改法**:① dark 段補 `--accent` 覆寫(七色各給 dark 變體,以 W0-4 重算腳本驗到 ≥3:1);② focus ring 改用獨立 `--focus-ring` token(dark/light 各自達標);③ 主題/accent 持久化:併入 `ui_settings`(搭 W3-7f 的 per-key 寫入);④ `--warn` 若 W0-4 重算未達標 → 換色(它承載密等升級語意,`chat.jsx:2095`)。
- **工作量**:1.5(含測試)
- **依賴**:blocked-by W0-4(重算值)、W3-7f(per-key 設定)。
- **驗收**:`node packages/tokens/scripts/verify.mjs` 全綠(含新 dark accent 與 focus-ring 對);前端測試:切主題 → reload 後仍生效(mock 持久層);鍵盤 Tab 於 dark 模式 focus 可見(axe 或手動記錄)。
- **派工**:實作A;驗收甲。
- **風險**:換 accent 動品牌觀感 → 提供 before/after 截圖給 user 過目再合。

#### W3-11(=T2-10)接上 `ask_user` 互動事件
- **錨點**:`router_server.py:3809-3823`(`_AGENT_PASSTHROUGH_EVENTS`);`sse.js:75-82,355`(接口與 resume 已在);`agentic.jsx` 513 行 + `toolExecution.jsx` 338 行零引用;`app.jsx` streamWithAbort 未掛 onInterrupt/onTodos
- **改法**:先由偵察確認「線上是否有 agent 掛 `ask_user_tool`」(0.25 日,唯讀);無論結果,接線成本低而風險是死路:`app.jsx` 掛 `onInterrupt`/`onResumed`/`onTodos` → 渲染 `InterruptCard`/`TodoChecklist`,回答走 `streamSessionAnswer()`。
- **工作量**:2(含測試)
- **依賴**:E0-9;W2-4/W2-9 之後(同檔)。
- **驗收**:前端測試:mock SSE 發 `interrupt_requested` → InterruptCard 渲染、提交後 resume 呼叫發出;`grep -rn "from \"./agentic\"" apps/anila-shell/src/app.jsx` ≥1。
- **派工**:實作A;驗收甲。
- **風險**:resume 流程與後端狀態機不一致 → 先以 dev stack 手動全鏈跑通一次記錄於 PR。

#### W3-12(=T2-11 選集)規模化零星缺口
> 每列九欄壓縮呈現:錨點/根因見合成 §4 表,此處只列改法、量、依賴、驗收、派工、風險。未列入者見 §6 不做清單。

| 子包 | 條目 | 改法 | 量 | 依賴 | 驗收(機械) | 派工 | 風險 |
|---|---|---|---|---|---|---|---|
| a | append-only 只是註解(`0014:129` GRANT ALL;三稽核表零 trigger) | migration:REVOKE `UPDATE,DELETE,TRUNCATE` on `audit_logs`/`policy_decisions`/`classification_events` from `csp_app` + BEFORE UPDATE/DELETE 拒絕 trigger | 1 | W2-6 | CI PG:`role_table_grants` 查無三表的 UPDATE/DELETE;pytest:UPDATE 稽核列 → 拒絕(先紅後綠) | 實作A/驗收甲 | 既有程式若有合法 UPDATE(如 W1-5 補列改為 INSERT 型即無)→ 偵察先掃寫入點 |
| b | NV-Embed Matryoshka 假設零依據(複製 6 處) | 一次量測:對現有 eval fixture 以全維 vs 截斷維跑 recall 對比,結論寫進 `embedding.py` 檔頭與 docs | 0.5 | 無 | benchmark 數字檔存在;6 處註解更新指向它 | 實作B/驗收甲 | 結果若否定假設 → 開 re-embedding 決策件(不自動改) |
| c | 讚/倒讚無人看(`message.py:27-28`) | governance 加彙總 API(`GET /api/admin/ratings/summary`:by model/agent/day)+ view 一頁 + CSV | 1 | 無 | pytest:彙總數字 = fixture 手算;UI 冒煙 | 實作B/驗收甲 | 低 |
| d | 對話/訊息/trace_span 零保留政策 | `retention_reaper` 增三資源類(政策值 env,預設**不啟用**——保留期是治理決策,交 user 簽);legal_hold/latch 全鏈尊重 | 2 | W2-3(訊息樹的分支保留語意)、C3 §e | pytest(真 PG):到期對話被 reap、legal_hold 者不動(先紅後綠);預設 off 姿態測試 | 實作A/驗收甲+乙 | 誤刪不可逆 → 兩段式(先標記後刪)+ dry-run 模式 |
| e | 附件在分類/retention 體系外(`attachments` 無 `classification_level`;reaper 不碰 storage;admin 直通矛盾) | migration 加欄(繼承對話 latch);reaper 納入附件檔案;`get_attachment` 的 admin bypass 對齊 `search.py:719` 姿態 | 2 | W2-6、d | drift 腳本:attachments 有分類欄;pytest:對話 reap → 附件 bytes 刪;admin 無 need-to-know 讀附件 → 拒(先紅後綠) | 實作A/驗收甲 | 既有附件回填分級 → 取所屬對話現值,寫回填說明 |
| f | `POST /api/attachments` 不驗擁有權(跨使用者注入;分支 coverage 0%) | service 內驗 conversation/message 屬 current_user,否則 403 | 0.5 | 無 | pytest:A 對 B 的 conversation 上傳 → 403(先紅後綠) | 實作B/驗收甲 | 低 |
| g | anilalm 零密等標示元件 | 從 shell `trust.jsx:543-575` 移植浮水印/badge 至 anilalm workspace 與 viewer | 2 | C4 拍板(樣式作法跟決策走) | 前端測試:機密 artifact 檢視含浮水印節點;`grep -rn "Watermark\|密等" apps/anilalm/src | wc -l` ≥1 | 實作A/驗收甲 | [G2] 若觸 WSChat.tsx 保住斷言 |
| h | Studio 產出無密等/AI 標示;`classification_level` 預設 None | render pipeline 強制頁首/頁尾標示(密等 + 「AI 生成」);None → 拒絕(fail-closed 對齊 contracts) | 2 | 無 | pytest:無分級請求 render → 4xx;產出 PPTX 解包含標示文字(整合測試) | 實作B/驗收甲 | 既有無分級呼叫端要先補值 → 偵察掃呼叫點 |
| i | 427 行 ASR 檔 byte-identical 兩份;無 JS workspace | 短期:CI 加 `diff` 檢查兩檔一致(drift 即 fail);中期 workspace 化列入 C4 決策文件附帶議題 | 0.5 | 無 | CI job:diff exit 0;seed 改一邊 → fail | 實作B/驗收甲 | workspace 遷移不在本包 |
| j | 供應鏈:toolkit pip 無 pin/hash、image mutable tag、bundle 無簽章 | `download-intranet-toolkit.sh`:constraints + `--require-hashes`;image 改 `@sha256:` digest;`build-and-export` 產 SHA256SUMS + 簽章(簽核人掛 W4-2);checksum 移到 exit 檢查之後 | 2 | 無 | 腳本 dry-run:無 hash 的包 → 拒;`grep "@sha256:" download-intranet-toolkit.sh` ≥3 | 實作A/驗收甲 | 上游 wheel 平台差異 → constraints 以目標平台(內網 B200 主機)為準生成 |
| **j2** | **⚠(Opus 5 缺漏審查新增)SBOM / CVE 監控 / 第三方 license 清冊 / NOTICE 全缺**(完整性審查 L5+L10;原計畫 `SBOM`/`CVE`/`dependabot`/`NOTICE`/`license` 命中皆 0)。`.github/` 對 `trivy\|grype\|syft\|pip-audit\|bandit\|semgrep\|codeql\|dependabot` **只有 1 筆**(pptx-renderer 的 `npm audit`),無 `dependabot.yml`;全 repo 無 SBOM/SPDX/CycloneDX 產出物、無 `NOTICE`/`THIRD-PARTY*`。而 repo 是 **GPL-3 且 PUBLIC**,三個前端 bundle(1,001 kB minified,內含 react/mermaid/katex/highlight.js/marked/DOMPurify)進 image 交付內網,**MIT/BSD/Apache 幾乎全部要求在 distribution 中保留 copyright notice**——目前一份都沒帶 | ① CI 產 CycloneDX SBOM(`syft` 或 `pip-audit --format cyclonedx` + `npm sbom`)入交付 bundle;② **air-gapped 的 CVE 流程寫成文件**(離線 DB 隨 bundle 更新的節奏 + 誰看 + 多久看一次)——這是組織程序不是腳本;③ 產 `THIRD-PARTY-NOTICES.txt`(license-checker 類工具)入前端 bundle 與 image;④ license 相容性檢查進 CI(引入 GPL-incompatible 套件時 fail) | 2 | j(同一供應鏈紀律) | ① `bundle` 內含 SBOM 檔且格式可被 parser 讀;② `THIRD-PARTY-NOTICES.txt` 存在且行數 ≥ 直接依賴數;③ CI seed:加一個 GPL-incompatible 假依賴 → fail;④ `docs/governance/cve-process.md` 存在含負責人欄 | 實作A(工具)/ Claude 家(流程文件)/ 驗收甲 + 驗收乙(文件) | ② 是組織事實不是程式碼事實,模型不得自行定案通報節奏 → 留簽核欄給 user;`docs/gpt-report/…:378` 早就寫了這四項,別再漏第三次 |
| k | 無回滾/維護模式/升級前自動備份 | `deploy-prod.sh` 升級路徑:先呼 backup、記前一 image digest、加 `rollback` 子命令(compose 回舊 digest + alembic downgrade 提示);nginx 維護頁 toggle | 2 | W1-6 | 測試 stack:升級→rollback 全鏈演練輸出存檔;`grep -n "rollback" deploy-prod.sh` ≥1 | 實作B/驗收甲 | alembic downgrade 未必總安全 → rollback 文案明示資料 schema 邊界 |
| l | ISO 42001 追溯 8 死欄(`0035:60-105` 建了,ORM/API/UI 全無) | ORM 補欄映射 + model registry API/UI 露出(唯讀先行) | 1 | 無 | drift 腳本不再列此差;governance model 頁顯示欄位(冒煙) | 實作B/驗收甲 | 低 |
| m | 混合檢索半成品 + 零中文分詞 | **決策件先行**:zhparser/pg_jieba 進 air-gapped image 的供應鏈成本評估(offline 編譯、image 重打包)寫 1 頁;核可後才排實作(S+M 綁一起,合成警告:只接 keyword_search 而無 CJK 分詞是無效功) | 0.5(決策件) | j(供應鏈紀律) | 決策文件存在含兩方案成本;user 簽核欄 | Claude 家/驗收乙 | 別先接無效的 `plainto_tsquery('simple')` |

---

### Wave 4 — 制度(與 Wave 1–3 平行;非工程,user 為主)

| 包 | 內容 | 驗收(機械) | 派工 |
|---|---|---|---|
| W4-1 | **S1 收斂**:五驗證器(Gate 5 素材/SLO 六指標/production restore/RTO・RPO/備份)各指派「產生器 owner + 期限」,或明確凍結該 Gate 並記錄 | `docs/governance/verifier-owners.md` 存在,五列各有具名 owner 與日期;凍結者有 user 簽註 | user 拍板;文件 Claude 家/驗收乙 |
| W4-2 | RACI 具名(`roles-responsibilities.md` 11 活動 7 角色零具名);`.env.example` 27 必填變數標註簽發者 | 兩檔 grep 具名(非職稱)≥ 每列一人 | user;文件同上 |
| W4-3 | 三個跨單位介面(`.12` gateway / MLSteam / CSPKI CRL)各一頁:對口、變更通知機制、SLA 期望;`.12` 的 `transport_target` 變更通知是 Gate 5 存亡條件(08.md L13) | `docs/governance/external-interfaces.md` 存在,三節各含對口姓名欄 | user 對接;文件 Claude 家/驗收乙 |
| W4-4 | 本國法規對映表(資通安全管理法/個資法/國家機密保護法/營業秘密法/檔案法 ↔ 平台控制項);`ai-incident-response.md` 填法定通報時限與 N 值。**⚠(Opus 5 缺漏審查補入)同時修 `data-governance.md:142-150` 的不可執行宣稱**——它寫「更正權:目前需透過 admin 人工處理」,而 `api/conversations.py` 與 `api/memory.py` 對 `require_admin|is_admin_tier` **grep=0**、`conversations.py:277` 硬綁 `user_id == current_user.id`,**admin 連讀都不行,更不可能改**;「刪除權對 memory 成立」也建立在 `ENABLE_MEMORY=False` 預設關之上(見 W1-3);「可攜權待規劃」已過時(匯出早就有,見 W1-1)。**這是 S5 類的第三個實例**(前兩個是加密模式、記憶 tab),與 W1-3 同一缺陷家族,只是落在治理文件而非 UI。另:`LoginView.vue` 對 `隱私\|個資\|告知\|同意` grep=0,而卡登會寫 `users.card_id` 等自標 High(個資)的欄位 → 登入頁需個資告知 | 對映表存在且五法各 ≥1 列;`grep -n "≥ N 人" ai-incident-response.md` =0;`data-governance.md` 四項權利各自的敘述與實作一致(驗收乙逐條對照程式碼);登入頁含個資告知連結(前端測試) | 法務/資安權責人;草稿 Claude 家/驗收乙(**條號正確性由法務簽,模型不得自行定案**) |
| W4-5 | 教材:終端使用者手冊(自 W1-10 user-guide 擴充)、管理員 clearance 開通手冊(接 W3-1 UI)、`training-records/` 目錄建立 | 三路徑存在;ISO 42001 compliance 表該列由紅轉黃(自評更新) | 文件 Claude 家/驗收乙 + user |
| W4-6 | Router 預設延遲決策件(`ui-verification-anila.md`:Router 64s vs 直指 6s;建議「路由決策模型與直答模型分開設定」) | 決策文件存在含三選項與量測數據引用;user 簽核欄 | Claude 家/驗收乙;實作(若核可)另開包 |
| **W4-7** | **⚠(Opus 5 缺漏審查新增)資料來源版權與 provenance 決策件**(完整性審查 L11;原計畫 `provenance`/`版權` 命中 0)。`models/ingestion.py` 對 `license\|copyright\|版權\|授權\|source_url\|provenance` **grep=0** → 使用者上傳廠商手冊、CNS/ISO 規範 PDF 進共用知識庫後,**平台無法回答「這份資料我們有權讓 LLM 重製並散布給全院嗎」**。決策件內容:哪些來源類別需登錄授權依據、是否強制填 `source_url`/`授權依據` 欄、既有文件如何回溯補登(或明確接受不補)、以及「不確定授權時的預設姿態」(fail-closed 不索引 vs 索引但標記)。**與 W3-12h 的 AI 生成標示相鄰但不同**:那條是產出面,這條是輸入面的權利鏈 | `docs/governance/data-provenance-policy.md` 存在,含四個問題各自的決定與 user/法務簽核欄;若決定加欄,附 migration 工作包編號 | 法務/資料權責人;草稿 Claude 家/驗收乙 |

---

## 5. 五份子計畫

### C1 · 93 個 naive timestamp 欄的 migration 計畫

**a. 範圍與分批**

> ⚠ **權威來源修正(Opus 5 缺漏審查)**:欄清單**不能**取自 W0-1 的 drift 輸出——ORM 與 PG 兩邊都是 naive,diff 為零(見 W0-1 改法 (b))。**權威 = 直接查 `information_schema`**:
> ```sql
> SELECT table_name, column_name FROM information_schema.columns
> WHERE table_schema='public' AND data_type='timestamp without time zone'
> ORDER BY table_name, column_name;   -- 預期 93 列
> ```
> W0-1 的**政策段**(naive ORM 宣告清單,預期約 104)是配套的第二份清單:PG 側轉了型別、ORM 側沒跟上,drift 才會開始咬人——所以兩份清單必須在**同一個 PR 內**一起收斂(見 W2-10 改法③)。

欄清單分批(稽核已知涵蓋):
- **批次 1(W2-10,小表、法律證據優先)**:`classification_events`、`policy_decisions`、`declassification_requests`(`created_at`/`decided_at`)、`classification_authority_assignments`(`created_at`/`revoked_at`)、`export_records`、`api_keys.expires_at`、各表 `classification_latched_at` 中屬小表者(conversations/artifacts)。
- **批次 2(獨立維護窗)**:大表 `messages`、`document_chunks`、`ingestion_*` 的 naive 欄——`ALTER TYPE ... USING` 觸發**全表重寫 + ACCESS EXCLUSIVE 鎖**,鎖時長 ∝ 表大小,必須量測後排窗。

**b. `USING` 子句**:`ALTER COLUMN <col> TYPE timestamptz USING <col> AT TIME ZONE 'Asia/Taipei'`(D4 已決,2026-07-26;判讀依據與前置條件見 §c)。

**c. 既有值判讀(D4)** —— ⚠ **2026-07-26 user 回覆「timezone 都採 UTC+8」,此處必須拆成兩層,因為它們是兩個不同的決定:**

| 層 | 決定 | 狀態 |
|---|---|---|
| **呈現層**(前端顯示、報表、log 可讀時間) | **一律 UTC+8 / `Asia/Taipei`** | ✅ **已採納**,即 W1-4④ 的共用 formatter(`zh-TW` + `timeZone:'Asia/Taipei'`);另可把 csp/db 容器加 `TZ: Asia/Taipei` 讓 log 也是台灣時間(對資料零影響,因為裸 `datetime.now()` 是 0 處) |
| **儲存/遷移層**(93 個 naive 欄的既有值怎麼解讀) | **user 拍板:視同 UTC+8**(2026-07-26,經一次反對意見後重申) | ⚠ **已記錄為 user 決定,但 W2-10 執行前須經資料權責人書面簽核**(見下方「執行前置」) |

**實作者反對意見與其證據**(◎Opus 5 2026-07-26 複驗;**已被 user 重申後覆蓋,此處保留供簽核人判斷**):

```
services/csp/app/  datetime.now(timezone.utc)  → 210 處
                   datetime.utcnow()           →   1 處(同為 UTC 牆鐘)
                   裸 datetime.now()           →   0 處
                   datetime.now(ZoneInfo/pytz)  →   0 處
容器:platform.yml 僅 n8n(:813)/gitlab(:847)設 TZ,csp/db 皆 UTC
```

→ 依上述兩條寫入路徑推斷,**既有 naive 值應為 UTC 牆鐘**。

**採 `Asia/Taipei` 判讀的可量測後果**(中性陳述,供簽核人評估):既有紀錄的絕對時點會**往前平移 8 小時**,受影響者包含 `classification_events`(分類異動 ledger)、`declassification_requests`(雙人降密核准)、`classification_authority_assignments`(公文文號權責指派)、`export_records`;且遷移後由 `datetime.now(timezone.utc)` 寫入的新值是真 UTC,故**遷移前後的紀錄在絕對時點上存在 8 小時不連續**。若後續判定此後果不可接受,改回 `'UTC'` 只需改 migration 的一個字串,**但必須在 W2-10 執行前**——執行後改回需另寫補償 migration。

⚠ **唯一例外要掃**:`api_keys.expires_at` 來自 client request body(`schemas/api_key.py:13`),若曾有 client 送裸本地時間就會偏 8 小時。批次前跑例外掃描 SQL(值域出現「未來 > 1h」或與 `created_at` 關係異常者標記人工審)。

### ⚠ 追加證據(2026-07-26,Opus 5 在 D4 拍板後才查到,兩份稽核與本計畫 rev.1 皆未涵蓋)

除了 Python 寫入端,**naive 欄還有第二條寫入路徑:DB 層 `server_default`**。實測 dev stack:

```
DB session TimeZone = Etc/UTC

有 server_default 的 naive 欄(節錄,含治理帳四張表):
  classification_events.created_at                 -> CURRENT_TIMESTAMP
  declassification_requests.created_at             -> CURRENT_TIMESTAMP
  classification_authority_assignments.created_at  -> CURRENT_TIMESTAMP
  policy_decisions.created_at                      -> now()
  citations.created_at                             -> now()
  document_chunks / ingestion_* / service_* …       -> CURRENT_TIMESTAMP
```

`CURRENT_TIMESTAMP` 是 timestamptz,寫進 `timestamp without time zone` 欄時**按 session TZ 轉換**。session TZ = `Etc/UTC` → **這條路徑存的也是 UTC 牆鐘**。

→ 兩條獨立寫入路徑(Python 211 處 + DB server_default)**都是 UTC**,無第三條路徑。

### ⛔ 由此浮現的陷阱:**不可以**把 `TZ: Asia/Taipei` 加到 csp-db 容器

我先前建議「順手把 csp/db 容器加 `TZ: Asia/Taipei` 讓 log 也是台灣時間」——**這條建議對 csp-db 是錯的,在此撤回**。一旦 DB session TZ 變成 `Asia/Taipei`,上表那些 `CURRENT_TIMESTAMP` → naive 欄就會開始存**本地時間**,而同一欄的舊值是 UTC → **同一個欄位混兩種語意,且無任何標記可區分**。

- csp **應用**容器加 TZ 無害(裸 `datetime.now()` 是 0 處),只影響 log 可讀性。
- **csp-db 容器在那 93 欄轉成 timestamptz 之前,TZ 必須維持 UTC。** 此條列入 W2-10 的風險欄與 `.env.example` 註解。

### 執行前置(W2-10 動工前必須齊備)

user 已拍板「視同 UTC+8」。因為此判讀會改動已寫入的治理紀錄,W2-10 執行前**必須**齊備下列三項,缺一不可:

1. **受影響範圍報表**:對 **`.15` 生產庫**(不是 dev)逐表列出 naive 欄的實際筆數。dev stack 現況供參考,**不可當生產數字**:
   ```
   audit_logs            17,178      policy_decisions   52
   其餘(classification_events / declassification_requests /
   export_records / messages / conversations / document_chunks /
   ingestion_documents / api_keys …)  reltuples = -1（dev 未 ANALYZE 或空表）
   naive 欄總數 = 93  ← 權威來源 information_schema，非 ORM diff
   ```
2. **資料權責人書面簽核**:文件須載明「本次遷移將使 N 筆治理紀錄的時點往前平移 8 小時」,並記錄簽核人、日期、依據。此文件與 migration 同 PR。
3. **可逆性**:`downgrade()` 用 `AT TIME ZONE 'Asia/Taipei'` 對稱還原(資訊無損);並在 upgrade 前對受影響表做一次獨立快照(不依賴 W1-6 的備份鏈,因為那條還在修)。

**遷移語句**:`ALTER COLUMN <col> TYPE timestamptz USING <col> AT TIME ZONE 'Asia/Taipei'`。

**migration 檔頭必須寫明**:此判讀為 2026-07-26 user 拍板;實作者(Opus 5)曾以「兩條寫入路徑皆為 UTC」提出反對意見並被重申;採用本判讀後,遷移**前後**寫入的紀錄在絕對時點上會有 8 小時不連續(遷移後由 `datetime.now(timezone.utc)` 寫入的值是真 UTC)。**把這段寫進 migration 而不是只寫在計畫裡**,是為了讓未來的稽核複查能自行判斷,而不是只看到一個沒有脈絡的 `AT TIME ZONE 'Asia/Taipei'`。

**例外掃描(不受判讀選擇影響,一律要做)**:批次前跑查核 SQL——任何欄的值域若出現「未來 > 1h」的時點,或與同列 timestamptz 欄的時差呈非零常數分布,標記人工審。`api_keys.expires_at` 因來自 client request body(`schemas/api_key.py:13`)須逐筆列出,不混入批次。

**d. 鎖與時限**:runtime 的 `lock_timeout=5000ms` 只掛 app engine;**alembic 的 MIGRATION_DATABASE_URL 明文不得繼承**(`config.py:77-81` ◎複驗)。批次 1 各表在 dev stack 快照庫實測 ALTER 耗時,寫入 migration 檔頭註解;批次 2 必先在還原副本上量測(這步驟同時是 W1-6 restore 演練的活用)。
**e. 與 `startup_migrations` 折回(W2-6)的先後**:**W2-6 先、C1 後**。理由:startup DDL 覆蓋的 8 張表若先做型別轉換、後做收編,同一批表要兩輪手術且 baseline 會漂;單一 schema 機制成立後,timestamptz 轉換是一次乾淨的 alembic revision。
**f. downgrade**:對稱 `USING <col> AT TIME ZONE 'Asia/Taipei'`(timestamptz→timestamp 取台北牆鐘),資訊無損、可完整還原 upgrade 前的位元組值。
**g. 驗收**:① CI 乾淨 PG:upgrade→drift 腳本 timestamp 類殘量 = 93 − 批次涵蓋數;② pytest(真 PG):`classification_events.created_at` 回傳帶 `+00:00` 且值與寫入時點一致(跨 TZ 環境變數重跑一次,值不變);③ downgrade 再 upgrade 冪等;④ 批次 1 各表 ALTER 實測耗時 < 5s(否則移入批次 2)。
**h. 派工**:實作A;驗收甲 + 驗收乙(判讀語意第二眼)。

### C2 · `access_grant` 收斂計畫

**a. 收斂對象(◎複驗修正)**:6 張持久 DAC 表——`user_model_permissions`(`models/user.py:8`)、`api_key_model_permissions`(`models/api_key.py:7`)、`user_agent_permissions`/`api_key_agent_permissions`(`models/agent.py:10,21`)、`service_access_grants`(`models/service_access_grant.py:32`)、`collection_access_grants`(`models/clearance.py:164`)。**排除**:`ExecutionGrant`(非表,runtime minted 契約,§1 F2);`clearance_grants`/`clearance_grant_compartments`(`models/clearance.py:66,130`)是 MAC 層,**永不併入**。
**b. 目標形狀**:`access_grants(id, resource_type, resource_id, principal_type{user,api_key,service,department}, principal_id, permission{read,use,manage}, granted_by, created_at, expires_at NULL)` + 部分索引 `(resource_type, resource_id)`、`(principal_type, principal_id)`。
**c. 鐵律**:最終授權 = `(access_grants 聯集判定) AND clearance_level_ok AND compartments_ok AND subject.is_active AND need_to_know`。**分級與 compartment 檢查留在 grant 之外當獨立 AND 條件**——Open WebUI 全 OR 姿態明確拒絕(合成 T2-1 ⚠)。實作上:單一入口函式(clearance/service 系)先算 MAC,再查 DAC;任何呼叫端不得自行 OR。
**d. 遷移路線:雙軌影子讀,不一次切** —— **D5 已決(2026-07-26,user 授權照建議執行)**。授權回歸 = 涉密事故,一次切不可接受。
- Phase 0(=W3-1):唯讀收斂——補 GET、`effective_access` UNION-ALL 視圖(跨 6 表)、Preview Access(傳任意 principal 走同一判定函式,零副作用)。
- Phase 1:建新表 + 舊表寫入時雙寫 + 回填;讀取仍走舊表;**影子讀**:每次授權判定同時查新表,不一致記 `access_grant_shadow_mismatch` 稽核列。
- Phase 2:影子不一致連續 14 天 = 0 → 讀切新表;舊表凍結一個 release(只讀)後 drop(獨立 migration,可 downgrade)。
- 每張表獨立走 Phase 1→2(先 collection_access_grants——它已有資料面解析,風險最小),不必六表齊步。
**e. Preview Access**:判定函式簽名允許 `as_principal` 覆載;governance UI「以此人視角預覽」= 呼同函式;audit 記 preview 事件(誰查了誰)。
**f. 工作量**:Phase 0 = 3(在 W3-1);Phase 1+2 = 8+(獨立排程,Wave 3 末啟動,跨 wave 收尾)。
**g. 驗收**:Phase 1 影子不一致計數可查詢(SQL);Phase 2 切換 PR 附 14 天零不一致證據;全程 clearance AND 語意測試(有 grant 無 clearance → 拒)在每 phase 重跑。
**h. 派工**:實作A(schema/引擎)+ 實作B(雙寫改造);驗收甲;Phase 2 切換加驗收乙。

### C3 · `messages.parent_id` 計畫(W2-3 的設計依據)

**a. migration**:`ADD COLUMN parent_id INTEGER NULL REFERENCES messages(id) ON DELETE SET NULL` + 索引 `(conversation_id, parent_id)`。nullable、無 rewrite,鎖成本低。回填:per conversation 依 `(created_at, id)` 排序,`parent_id` = 前一則 id(現況線性即單鏈);回填腳本冪等、可分段。downgrade:drop column。
**b. 語意**:編輯 user 訊息 = 在原 parent 下長**兄弟** user 節點(舊子樹保留);重生 assistant = 同 parent 下兄弟 assistant 節點。active path:`conversations` 加 `active_leaf_message_id`(nullable;NULL = 最新葉,向後相容)。讀取 API 預設回 active path(舊 client 形狀不變),`?tree=1` 回全樹。
**c. 舊分支保留政策(不可後補)**:分支節點各帶自己的 `classification_level`(欄位已在);對話層 latch = 全樹最高級(不可降級不變式沿用);`legal_hold`/retention 以**全樹**為單位;W3-12d 的 reaper 對非 active 分支可設較短保留(政策值 env,預設與主鏈相同)。刪除單一分支 = 僅 admin+audit 的顯式操作(不隨編輯發生)。
**d. 前端對接**:PR #50 的版本切換 UI(`chat.jsx:727-758`,現為本地 state 分組)改以 sibling set 驅動 N/M;編輯/重生後跳至新葉;`buildMessageHistory` 沿 active path 組歷史(修 T1-1 前端半邊)。
**e. 解鎖(記 backlog,不在本計畫排)**:多模型並排(同 parent 多 assistant children)、Arena/A-B、Open WebUI A-3 的 merge 姿態。
**f. 風險**:回填接錯鏈 → 演練 + 抽驗(W2-3 驗收);樹遍歷效能 → active path 走 recursive CTE 或 leaf 反向鏈,先以 500 則對話 fixture 壓測。

### C4 · 樣式策略 —— **D3 已決:甲案 CSS Modules + design tokens**(2026-07-26 user 拍板)

> 以下三案比較保留為決策紀錄。**執行時只走甲案**,並追加一條原文沒寫的配套:**W0-3 的 ESLint 必須同時加「禁新增 inline style」規則**(`react/forbid-dom-props` 或自訂 `no-restricted-syntax`),存量 966 處進 baseline ratchet。否則甲案是「漸進遷移」,而漸進遷移沒有煞車就只會讓 966 變 967。
> anilalm 的競爭 token 系統(`theme/tokens.ts`,深色預設、紫 accent)依 08.md C2 裁決**先拆後接**,成本 2 人日,列 Wave 3(與 W3-12g 的 anilalm 密等元件同窗做,同一批檔案)。

**現況(◎複驗)**:inline `style={{` shell 549、anilalm 417(合計 966);governance 25 條 `@media` 且零 inline 依賴;`@anila/ui` **只有 shell 在用**(anilalm/governance import 零命中);anilalm 另有一套競爭 token(`theme/tokens.ts`,深色預設、紫 accent,08.md C2)。零 `@media` 不是漏做,是 inline style 語法上寫不出 media query。

| 選項 | 內容 | 成本(966 處遷移路徑) | air-gapped | 主要風險 |
|---|---|---|---|---|
| 甲:CSS Modules + design tokens | 逐檔抽 inline → `.module.css`,tokens 走 CSS custom properties(`packages/tokens` 已在) | 漸進、可逐檔;每檔 0.5–2h;無新依賴(Vite 內建) | 完全相容(零 CDN/字型) | 慢;需 lint 規則擋新增 inline(W0-3 加 `react/forbid-dom-props` 類規則) |
| 乙:Tailwind | utility-first 全面改寫 markup | 大爆炸傾向;每檔重寫;devDeps + 掃描建置 | 相容(build-time),但 lock 供應鏈變重(W3-12j 紀律下每依賴都是成本) | 與既有 tokens 雙軌;團隊(1 人+AI)學習/審查成本;改寫誘發回歸而 E2E=0 |
| 丙:vanilla-extract 類零 runtime CSS-in-TS | 型別安全 token,build-time 產 CSS | 中;需 TS 化樣式層;新 build 依賴 | 相容 | 生態小;對 Vue(governance)不適用 → 三 app 不同構,違背收斂初衷 |

**推薦:甲**。理由:governance 已用純 CSS + `@media` 證明此路在本 repo 可行且唯一有響應式的 app 恰是它;零新依賴符合供應鏈紀律;可逐檔遷移 = 與 Wave 2/3 的前端工作包共乘(每包順手遷移其觸碰檔);`packages/tokens` 與 `@anila/ui` 的既有投資直接沿用。anilalm 的競爭 token 系統依 08.md C2 裁決**先拆後接**(成本記 2 人日,列 C4 附錄工作項)。
**斷點表建議(院內實際機型,W1-0 前測時順手確認)**:1366×768(公務筆電)、1920×1080(主流桌機)、2560×1440(製圖工作站);先不做 <1024 行動版(內網無手機場景,若 pad 巡檢需求出現再加 1024 斷點)。
**附帶議題**:JS workspace 化(root package.json workspaces)——解 ASR 雙檔複製(W3-12i)與三份 formatter 複製(W1-4④),建議與甲案同窗評估,獨立決策。
**驗收(決策文件本身)**:本節擴寫為 `docs/planning/style-strategy-decision.md`,含三案成本表、推薦、user 簽核欄;Wave 1 離開條件持有 user 選定紀錄。
**派工**:Claude 家擴寫;驗收乙;user 拍板。

### C5 · legacy 清單機制(讓 S3 反模式停止再生)

**a. 形式**:`infra/ci/legacy_ledger.json`——每條:`{pattern(regex 或 AST 規則), scope(glob), max_count, owner, exit_condition, deadline, notes}`;`infra/ci/check_legacy_ledger.py` 進 CI:實測計數 > max_count → fail;< max_count → 提示下修(手動 ratchet,防止靜默鬆動)。
**b. 為何 grep-ledger 而非 lint rule**:八對 legacy 橫跨 py/jsx/yaml/shell,單一 lint 生態蓋不住;計數 ratchet 對「禁止新增」這個目標等價且實作 1 天內。個別對(如 `classified`)可再加 ESLint `no-restricted-syntax`/ruff 自訂規則強化,列 notes。
**c. 首發八對**(基線 ◎複驗/稽核源):

| pattern 對 | 基線 | 退場條件 |
|---|---|---|
| `conv.classified` / `.classified` 判斷式(後端 12、shell 47 ◎複驗) | W1-1 後實測值 | 全部改吃 `classification_level`;欄位本身待 deprecation migration |
| `platform_links` vs `registered_service` | 偵察掃 | 讀寫全走 registered_service |
| `CSP_SERVICE_TOKEN` vs per-agent credential | audit 66% 遙測即證據 | cutover 完成,遙測歸零(W1-5⑥) |
| `is_legacy` service client | 偵察掃 | 清零 |
| `legacy_runtime_call` | 偵察掃 | 清零 |
| `proxy_service` facade vs `proxy/` package | 偵察掃 | facade 刪除 |
| 100k KDF vs 600k | 偵察掃 | 全量 rehash 完成 |
| SQLite conftest vs PG | `conftest.py:76` | 主迴路可跑 PG(W2-6 之後評估) |

**d. 制度接口**:任何 PR 引入「新 read model + 保留舊層」→ 必須同 PR 在 ledger 登記(PR 模板 checklist,與 W1-9③ 同處);owner 欄接 W4-2 具名。
**e. 驗收**:W1-7 的驗收即本機制首發驗收;每 Wave 收尾跑一次 ratchet 檢視(記於 wave 收尾 checklist)。

---

## 6. 不做清單(刻意排除,附理由)

### 6.1 軍方相關 —— **已拍板不做**(範圍決策 2026-07-24 立、**2026-07-26 user 再次確認**:純中科院內網應用;除非 user 點名,不排入)
被降級的具體條目,防日後以為是漏掉:
1. `anila-ops.sh:210-219` profile case 只認 `prod-intranet-card` → 軍方兩分支 8 個維運呼叫點失能(T2-3 內)。**對 in-scope 部署零影響**,不修。
2. `prod-military-passwd`/`trial-military` 無 CI(W0-6④)。
3. trial-military 33 檔/3,836 行刪減分支的 port 流程與衝突處理(含其刪掉的兩支後端測試)。
4. 軍方 profile 部署一條龍/runbook。
5. W3-6 的密碼政策對「admin 建帳唯一路徑」的軍方 profile 效益最大——功能照做(main 落地),但**不向軍方分支傳播**。
6. 每 Wave 收尾在 `branch-sync-ledger.md` 記軍方分支欠帳(§3),復軍方範圍時以此清單回補。

### 6.2 其他刻意排除
| 條目 | 理由 |
|---|---|
| i18n / 多語系 | 目標使用者全繁中;零需求證據 |
| 大型前端重建 | 合成 §1 已裁決:痛點根因是介面不存在;唯一無備援資產(前端分級執法)重建即裸奔(0 E2E) |
| Gate 5 production profile 產生腳本 | `infra/policy/gate5/README.md:144-159` 明文禁止 ◎複驗;做了 = 簽章失義。缺的是 runbook(W3-8) |
| at-rest 加密(pgcrypto/LUKS/TDE) | L 級且屬資安權責人決策;本計畫先修不實陳述(W1-3),決策件另立 |
| 配額「回滾」框架 | `0006` 刪除是當時合理設計決策(07.md #20);W3-4 是**新增能力**,不是修 bug——防止把設計決策當 bug 修 |
| admin 調閱使用者對話內容 | 現況「一律看不到」接近刻意且隱私上正確(07.md §5);「授權後調閱機制」屬治理決策,列 W4 議題不排工 |
| Open WebUI 程式碼引入/本體部署 | 品牌條款 50 人上限例外不成立;既定方針:只學設計 |
| 「DB 值永遠贏」的設定持久化姿態 | 與簽章 release 部署身分正面衝突(dissection §五);config 表本身是新功能,列 backlog 不入補救計畫 |
| rerank、訊息虛擬化、React.lazy 拆包、多模型並排/Arena、知識庫巢狀目錄/同步 diff、Valves UI、RRULE 排程 | 能力增強非補救;各自的前置(W3-12b benchmark、W2-9、C3)完成後由 roadmap 排 |
| 忘記密碼自助流程 | air-gapped 無 mail relay;admin 重設 + 文件化(W3-6 內註明) |
| `e2e/README.md` 所宣稱的 E2E **補齊** | **刪除不實 README 已排入 W0-6 驗收⑤**(rev.2 補;`AGENTS.md:217` 已承認過時)。**補 E2E 本體**是 Gate 4+ 工程,超出本計畫 |
| 內容式分類偵測(PII / presidio / 敏感詞 / auto-classify) | rev.2 的 W2-11 只做「顯式宣告 + latch 升級 + 抽查」。內容式偵測需模型與詞庫,air-gapped 供應鏈成本另計 → 決策件,不入本計畫 |
| 安全銷毀(shred/crypto-erase)與平台退役程序 | MEDIUM 且依賴 at-rest 加密決策;列 W4 決策件附帶議題 |
| `myCSPPlatform/`/`scraps/`/`runtime_logic/`/`cht/` 四目錄審查 | 稽核未涵蓋;若進交付包再擴大(合成 §9-8) |

---

## 7. 誠實聲明

**7.1 我未驗證、沿用稽核的**:93 欄清單全貌與 `alerts` 17 條缺失明細(實庫 introspection 屬 07/08,W0-1 腳本會重新產出權威清單);「同 pattern 33 處」to_thread 站點數(W2-7 以偵察掃描為準);`production_backup.py` 必要 env 的**確切數量**(「20 個」未逐一數,僅驗證 `_required_env` 機制存在 ◎;W1-6② 以偵察清單為準);backend 4 個 pytest 失敗的分類(單一證據源,合成 §9-3 已標);T2-10 `ask_user` 觸發機率(W3-11 排了偵察步驟);`.15` 生產 runtime 姿態(所有部署類工作包對 `.15` 的實效需現場複核);D2 三 token 值(依計畫在 W0-4 重算,未重算前不進任何門檻)。

**7.2 我不同意合成報告的(證據在 §1)**:F1 CI 觸發宣稱(重框定為 anilalm 無測試/軍方無 CI/node 單一化);F2 `execution_grants` 不是表(C2 範圍因此修正);F3 inline style 總量 966 非 549(C4 成本估算修正);F4 `_pg` 測試 34 個非 40(W0-2 驗收數字修正)。另:合成把「D2 未重算」放在註記層,我升為 Wave 0 工作(W0-4)——門檻用未驗數字比沒有門檻更糟。

**7.3 排序上與合成不同的**:① T0-4 的欄位 migration 從 Wave 1 挪到 Wave 2(W2-10),排在 W2-6 之後——同批表兩輪 schema 手術是自找的風險,合成把它與 Gate ① 「一起做」的說法低估了 `ALTER TYPE` 的表重寫成本(messages/document_chunks 是大表);其 S(邊界)/M(欄位)估值對批次 2 過於樂觀,本計畫拆批並要求先量測。② 串流稽核 fidelity(W1-5⑤)允許降級為文件化限制——它是六件套裡唯一動推論主路徑的,風險收益比另五件差。③ PR 收斂被前置為 Wave 閘門條件——合成完全沒處理兩個開啟中 PR 與新工作的衝突面,而這是排程的第一現實。

**7.4 本計畫自身的限制**:工作量人日是 1 人+AI 節奏的工程估算,未含 user 拍板等待與跨單位往返;Wave 0/1 若與 dev stack 日常使用撞資源,以 user 優先;所有「先紅後綠」驗收要求保留兩次執行輸出,驗收崗不得只看綠;本檔錨點在 `feat/ux-parity`(2026-07-26,HEAD d4122d8),PR #50 裁決後若 close,前端錨點需重定位(§2.1 已排 0.5 日)。
