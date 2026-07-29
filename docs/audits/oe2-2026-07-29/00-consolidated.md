# OE-2 系統性過度工程稽核 — 彙整定稿（2026-07-29）

> 方法：6 個領域並行稽核（opus，互斥檔案集，逐構造套 HANDOFF §4b 三問判準），
> 之後由 sol（GPT 家）跨家覆核 19 條承重宣稱：**10 CONFIRMED / 9 PARTIAL / 0 REFUTED**。
> PARTIAL 皆為「核心成立、原宣稱過滿」，修正已折入本檔。
> 細部逐構造表：本目錄 `a`–`f` 六份領域報告；覆核判定：`sol-verify.md`。
> 判準與背景：`HANDOFF-2026-07-29.md` §4b；規格權威：`SYSTEM-MAP.md`。

## 0. 總量

| | CAT-A 規格有要 | CAT-B 沒要但無害 | CAT-C 沒要且有持續成本 |
|---|---|---|---|
| 合計（338 構造） | **101** | **80** | **157**（其中 ~20 條屬 OE-1 已裁決範圍） |

擁有者的判斷獲得證實：這棵樹確實系統性殘留舊 doc 00–10 的過度工程，
agent 七態核准只是第一個被抓到的。但同時查出約 10 條**反向缺口**（規格有要、碼沒做）
與 4 個實作缺陷——稽核的產出不只是「砍」，還有「補」。

## 1. CAT-C 收斂決策包（157 條聚成 6 包）

| # | 包 | 內容摘要 | 排程建議 |
|---|---|---|---|
| D1 | **span/trace/task 子系統退場** | 域 A 全部 50 條＋域 E task 管線＋域 F `trace_id` 欄。規格 §7:211 逐字否定；`task/任務/狀態機/快照` 在規格 399 行零命中（sol #1 CONFIRMED）。熱路徑每則訊息多付 2 span 列＋2 commit＋Task/Snapshot/audit 三列；`token_usage.task_id` 唯寫不讀（sol #6 核心成立） | OE-1 之後（屆時 span 只剩 dev 面板一個消費者）。守衛遷移條款見 §4 |
| D2 | **GUI 服務註冊表叢退場** | `registered_services` 35 欄表＋`service_launches`/`service_audit_callbacks`（全 repo 零讀取，前端還在呼叫不存在的 GET 端點，sol #7 CONFIRMED）＋`platform_links` 殭屍表＋`registry_backfill`。規格只認固定三站（§1）；sol #4 註記 §1:19-35 可支持「最低限度三站導覽設定」，收斂目標即此 | 獨立包。**`launch/token.py` 的 RS256＋JWKS 機械不刪，改造成 P2.1 的 agent 派工 token**（TTL 10 分改回規格 5 分） |
| D3 | **artifact 契約收斂** | 6 表 92 欄／16 契約型別／2 狀態機 → 規格對 Studio 產出全文僅 ~9 行（§5:167＋§11；sol #5 補：§1:49 功能表也列了一行）。`artifact_jobs` 19 欄與 Redis job store 重複記帳；versions 表無規格依據（規格明言不要網頁編輯器）；「必綁 task/snapshot」硬閘造成 ALM 不建 Task 就 422、產出登錄靜默失效 | Studio 本身可延後，本包同步延後；但 G3（3 天自刪）屬「補」，不隨包延後 |
| D4 | **agents registry 殘餘收斂** | manifest 契約叢（sol #3：規格支持「自我描述」§3:114 的最小形狀，不支持完整 trace/classification 契約與 `extra="forbid"` 422 硬閘）＋`runtime_config`（工具權限/沙箱＋30 秒輪詢，零依據）＋shadow/draft 註冊模式＋`OPENWEBI_PIPE_COMPATIBLE` 死字彙 | **建議併入 OE-1 同一工作包**（同檔案、同 migration 窗口） |
| D5 | **policy 治理殘餘收斂** | 降級申請雙人流程（規格零命中「降級/降密/雙人」，與 §0「一人維運」衝突；sol #2 註記「改密等」本身仍須落稽核 §8:275）＋「機密審批權責」名冊與授予/確認/撤銷端點＋`PolicyDecision` 第二本治理帳（規格只認一本 append-only 稽核帳）＋分類盤點端點（唯一目的已被「資料庫砍掉重來」取消）＋舊 boolean 鏡射 | 降級流程去留**待擁有者裁決**（R1）；boolean 鏡射與 allow 判定式**併入 OE-4 工作包**（真正的外流閘門掛在 `conversations.classified`；task-less allow 不落列問題一併矯正，sol #15） |
| D6 | **model_registry 死欄位＋死端點** | r1_0005 七欄中五欄（`protocol`/`supports_*`×3/`owner_department_id`）全樹零邏輯消費者（sol #8 CONFIRMED）＋deprecated `/health-check` 別名＋孤兒 resume proxy 端點 | 小包，機械性高，可交弱級批次 |

## 2. 反向缺口（規格有要、碼沒有——是「補」不是「砍」）

| # | 缺口 | 規格依據 | 覆核 |
|---|---|---|---|
| G1 | 升密路徑稽核不全：`apply_classification` 本身不寫 `audit_logs`，只有對話包裝層有記；非對話路徑漏 | §8:275「改密等」 | sol #12 PARTIAL（對話路徑有記） |
| G2 | `audit_logs` 無防竄改（無 append-only／hash chain／trigger），且 `users.py:745-748` 可 UPDATE 既有稽核列 | §8:278-280（威脅模型含特權內部人） | sol #13 CONFIRMED＋加碼 |
| G3 | Studio 產出「設時限自動刪（暫定 3 天）」零實作：`expires_at` 唯寫、無 reaper；`GET /api/artifacts/{id}` 讀 metadata/storage_ref 不落稽核 | §5:167、§8:255/275 | sol #9 CONFIRMED、#14 PARTIAL（非內容串流） |
| G4 | ceiling 路徑 task-less 的機密 allow 不寫 PolicyDecision（部分其他路徑有寫）→ 併 OE-4 矯正 | §8:241-242 | sol #15 PARTIAL |
| G5 | 模型「整批帶入 upstream `/v1/models`」不存在 | §6:197 | sol #17 CONFIRMED |
| G6 | `bound_collection_id` 單數 FK，規格明言一 agent 可綁多庫 | §4:140 | sol #18 CONFIRMED |
| G7 | 「`.12` 呼叫連續失敗」告警在真流量不觸發（只有 health loop 會叫） | §9:302 | 域 E 回報，未跨家覆核 |
| G8 | 訊息樹後端無 parent 結構；前端已有 client-side 分支切換（`revisions[].tail`）但不持久化、重載即失 | §1:46 | sol #19 PARTIAL → **OW-1 已拍板做完整功能** |

## 3. 實作缺陷（非過度工程、非缺口，是 bug）

| # | 缺陷 | 覆核 |
|---|---|---|
| B1 | `health_checker.py:73` 探測 agent 未帶 `endpoint_kind="agent"` → 純 http agent（aiops 標準情境）恆被判離線＋持續告警 | sol #11 CONFIRMED |
| B2 | `agents.classification_ceiling` 全樹無寫入路徑 → OE-1 拍板保留的 ceiling 防線恆為 no-op | sol #10 CONFIRMED |
| B3 | `auto_seed.py` 繞過 `_enforce_endpoint_url`（SSRF/scheme 閘）、每次開機覆寫 admin 的端點編輯、自動核准（違 §2:94「不能自動核准」） | sol #16 CONFIRMED |
| B4 | 前端呼叫後端不存在的 `GET .../audit-callbacks`（隨 D2 一併清） | sol #7 CONFIRMED |

## 4. 收斂時的守衛遷移條款（每包執行時必查）

- 域 A：`_ingest_producer` 不信 client 自報、`GET /api/traces` 存取控制、`_resolve_acting_user` fail-closed——宿主退場時，fail-closed 語意**搬進用量歸屬路徑的測試**，不可隨 task 消失。
- 域 E／F：SSRF 註冊閘、`_guard_outbound`、憑證 AES-GCM、RLS、gateway Bearer 域割——全部 CAT-A 紅線，收斂包 SCOPE (out) 必列。
- 域 D：service-token 閘、單向閂鎖、owner-scope 三者 CAT-A，不得因帶 doc 引用而併砍。

## 5. 待擁有者裁決

| # | 事項 | 建議 |
|---|---|---|
| R1 | 降級申請雙人流程去留（規格零依據＋一人維運衝突；但誤標更正需要**某種**路徑） | 退場，改為 admin 單人改密等＋強制落稽核（G1 一併補）；OE-3 落地後誤標機率大減 |
| R2 | 服務註冊表叢（D2）收斂為固定三站設定 | 同意收斂；token.py 機械保留給 P2.1 |
| R3 | D4 併入 OE-1 同包執行 | 併，同檔案同窗口，省一輪審查 |

## 6. 測試面警示（執行收斂時的既知代價）

- 域 D：25 測試中 18 鎖規格外構造；`test_export_allow` 斷言「機密可匯出」與規格門檻表**直接相反**——收斂時測試要照規格重寫，不是照舊測試修碼。
- 域 C：`test_contract_classification.py:96` 把規格合法值「密」鎖成必須被拒——OE-3 首要修正對象。
- 基線紀律：既有 37 failed/1 skipped/2 errors 為環境相依 pre-existing（HANDOFF §1），收斂各包驗收時以此為基線比對。
