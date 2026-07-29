# 履歷 —— redesign 收斂之後到 2026-07-28

> 平台擁有者決定回到 2026-07-03 的 redesign 收斂點重新出發,砍掉這之後的一切
> (含新增功能)。本檔是那段期間的**紀錄**:做過什麼、在哪個 commit。
>
> **本檔只記錄,不建議。** 要不要撿回任何一項,是重來時的決定,不在這裡預判。
>
> **什麼都沒被刪掉。** 所有內容都在下面的 tag 裡,git 可以完整取回。

---

## 座標

| | |
|---|---|
| **redesign 收斂點** | `a4118a3`(2026-07-03)「main 收斂為 anila-redesign 單一版本」 |
| **重啟分支** | `restart/from-redesign` → `a4118a3` |
| **本段歷史存放** | `attic/2026-07-28/main` → `4524598` |
| | `attic/2026-07-28/prod-intranet-card` → `2701ef6` |
| | `attic/2026-07-28/prod-military-passwd` → `0373ef8` |
| | `attic/2026-07-28/trial-military` → `c84c18c` |

取回方式:`git log a4118a3..attic/2026-07-28/main`、`git show <sha>`、
`git diff a4118a3 attic/2026-07-28/main -- <路徑>`、
`git checkout attic/2026-07-28/main -- <路徑>`。

---

## 規模

25 天,**373 個 commit**,**+317,502 / −10,730 行**,1,238 個檔案。

| | `a4118a3` | `4524598` |
|---|---|---|
| `.env.example` 變數 | 33 | 125 |
| `infra/ci` 檢查腳本 | 3 | 29 |
| csp 測試檔 | 57 | 160 |
| `docs/planning` 文件 | 2 | 14 |
| alembic migration | 53 | 87 |

commit 類型:`fix` 108、`feat` 53、`docs` 37、`test` 7、`ci` 5、`chore` 5、
`ops` 3、`perf` 2、`security` 1、`style` 1、`deploy` 1、`build` 1。

---

## 一、新增功能

### 語音輸入(ASR)
| commit | 日期 | 內容 |
|---|---|---|
| `fab3dd5` | 07-20 | ASR decoder 服務(faster-whisper) |
| `fb64ede` | 07-20 | ASR gateway 服務(WS + VAD 切句 + JWT) |
| `7c93aa6` | 07-20 | 前端語音輸入(anilalm + anila-shell) |
| `ab30858` | 07-20 | 部署接線(compose / nginx / air-gap inventory) |
| `1aec685` | 07-20 | 加進 dev.yml |

### 圖像生成(FLUX)
| commit | 日期 | 內容 |
|---|---|---|
| `ba7bd92` `4fff856` | 07-07 | FLUX 遷移到 OpenAI Images API |
| `921ff2c` `01fd04e` | 07-07 | image-primary 主圖像模型動態指定 |
| `4674e70` `7c96d59` | 07-07 | flux2-dev-agent 動態後端解析 + 熱切換 + JPEG |

### 知識庫與產出(ANILALM)
| commit | 日期 | 內容 |
|---|---|---|
| `c46ae54` `483fbfc` | 07-04 | NotebookLM 式互動橫向心智圖,點節點直接提問 |
| `ae66df2` `313ad19` | 07-04 | `/outputs` 跨知識庫產出總覽 |
| `444f222` | 07-25 | artifact 版本歷史 |
| `a76ccb5` | 07-27 | 知識庫歸屬建立它的產品(origin 邊界) |

### 聊天介面(ANILA shell)
| commit | 日期 | 內容 |
|---|---|---|
| `f481f8f` | 07-11 | `@anila/ui` 共用設計系統 + shell 殼層 pilot |
| `670400f` | 07-25 | slash commands、命令面板、封存、標籤 |
| `488302b` | 07-25 | 引用相關度與逐文件分組 |
| `e759e2e` | 07-27 | 訊息改為樹狀,編輯產生分支而非覆寫 |
| `58388c4` | 07-27 | 對話清單 cursor 分頁 |
| `f797cab` | 07-27 | 部署關閉公開分享時隱藏分享入口 |

### Router / 模型治理
| commit | 日期 | 內容 |
|---|---|---|
| `57c7ba9` | 07-22 | R7 DIRECT_ANSWER 模型治理閘 |
| `13d419f` | 07-22 | R7.1 從 CSP 模型註冊表推導直答上限 |
| `e2bca4d` | 07-20 | Router auto-route demo 與 proxy 相容文件 |
| `f9b067f` | 07-24 | 明確不健康的模型直接 503(circuit breaker) |
| `145f86f` | 07-24 | `ANILA_STREAM_INCLUDE_USAGE` 旗標 |
| `1001664` | 07-24 | 補零支援 <4000 維 embedding |
| `3a0dd24` | 07-22 | auto-seed router sentinel + 一次性 secrets bootstrap |

### 稽核與治理後台
| commit | 日期 | 內容 |
|---|---|---|
| `4edd570` | 07-23 | 使用者推論稽核軌跡(含真實 client IP) |
| `56e9b2a` | 07-23 | 稽核查詢 admin 頁 |
| `eb2b959` | 07-23 | owner-gated 稽核檢視授權 |
| `0f25368` | 07-22 | 分類治理面完整化 |
| `f9222b4` | 07-26 | 治理稽核查得到、寫入不再靜默 |
| `d4c0fab` | 07-27 | 上傳時宣告文件密等 + 抽查 |
| `78b3f4c` `8d8655c` | 07-27 | 服務健康總覽端點 + 首頁健康卡/告警卡 |
| `111e19e` | 07-27 | `anila-ops.sh health` 納入告警 |

### 平台基礎
| commit | 日期 | 內容 |
|---|---|---|
| `0a1d18d` | 07-26 | 結構化錯誤信封與 API 契約 |
| `808897e` | 07-27 | `X-Request-ID` 貫穿回應頭、錯誤信封、access log |
| `567c0b4` | 07-27 | 治理帳 + api_keys timestamp 轉 timestamptz |
| `3584735` `78cd879` | 07-26 | shell 與 governance 的頂層錯誤邊界 |
| `23bd09b` | 07-25 | 冪等的 dev-up entrypoint + 最小 dev env 範本 |
| `3cc655d` | 07-24 | dev csp 映像可選 py-spy + probe 調校 |
| `beac4a3` `f06be82` | 07-04 | 登入後預設跳 `/anila/app`、全字階 +1px |

---

## 二、修復的缺陷

### 分類分級
| commit | 日期 | 內容 |
|---|---|---|
| `1039a2b` | 07-25 | 命令面板開啟的分類洩漏 |
| `709ca9a` | 07-27 | 比較檢視的泡泡不再丟失 classified 姿態 |
| `554485f` | 07-27 | 文件密等對外可見,讓人查得到 |

> 背景:複製 / 匯出 / 分享 / 列印 / 稽核五個 gate 原本都掛在 legacy 的
> `classified` boolean,而鏡射規則是 `classified = level >= 機密` ——
> 營業秘密的 `classified` 是 `False`,在那五個面上等同無機密。

### 安全
| commit | 日期 | 內容 |
|---|---|---|
| `4d844b7` | 07-27 | 加鎖重讀補 `populate_existing` —— 一個現行的 legal hold 繞過 |
| `29db21b` | 07-27 | 三張稽核帳真的 append-only(REVOKE + trigger),不再只是註解 |
| `a035495` | 07-27 | 受控對話的內文搜尋命中落稽核(堵字串存在性 oracle) |
| `33b9573` | 07-23 | 正式姿態的 agent 拒絕落 denied 稽核列 |
| `54719d3` | 07-23 | 稽核 write-ahead 排序、outcome fidelity、CSV 中和、owner ACL |
| `a6c37d6` | 07-23 | 失敗日誌遮蔽、解析拒絕、政策分類、keyset 匯出 |
| `38e0ece` | 07-26 | API key 到期驗證 500(naive/aware 比較) |

### 效能與可用性
| commit | 日期 | 內容 |
|---|---|---|
| `a035495` 一系 | 07-27 | RAG 熱路徑連線佔用 —— 實測修前 **4 VU 全平台鎖死、負載停 8 分鐘後 `/health` 仍逾時**;修後降級點推到 32 VU 且會恢復 |
| `5e70e18` | 07-28 | `idle_in_transaction_session_timeout`(150s)小於 `LLM_TIMEOUT`(300s) |
| `89975d5` | 07-28 | 容器日誌無輪替、nginx 限流鍵在真實 TCP 對端 |
| `20ed02f` | 07-28 | router `/docs` 等匿名可讀;ANILALM 下線的 `/api/personal` 雙掛載缺口 |

### 時區
`567c0b4` 與同期一系:naive 欄存 UTC 牆鐘、API 序列化不帶 offset、JS 依規格
當本地時間解 → 每個時間戳在 UI 差 8 小時。判讀於 07-26 拍板台北、07-27 改判 UTC。

---

## 三、加入的把關機制

| 機制 | commit | 擋什麼 |
|---|---|---|
| Gate 0–6 閘門體系 | 多個 | 分階段上線條件 |
| `check_orm_pg_drift.py` | `51f32e8` 等 | ORM 與 PG schema 漂移 |
| `check_legacy_ledger.py` | `fbc74c0` | legacy `classified` 引用數 |
| `check_for_update_populate.py` | `4d844b7` | 缺 `populate_existing` 的加鎖重讀 |
| `check_response_datetime_utc.py` | 07-27 | 回應 datetime 不帶 offset |
| capability freeze | `ec4ee06` 一系 | 能力面擴張 |
| image-lock + 氣隙映像清冊 | 多個 | 同名 tag 掉包映像 |
| 部署姿態契約(fail-closed) | `7c53534` | 設定與部署身分不符 |
| 卡片 CRL / 憑證政策 OID 強制 | `ec4ee06`(07-13) | 被撤銷的卡登入 |
| test governance / skip 註冊表 | 多個 | 測試靜默跳過 |
| ESLint + a11y gate | `0e22753` | 前端 error |
| WCAG 對比 gate | `21bfe07` | 色彩對比 |
| bundle 預算 fail-build | 多個 | 前端體積退化 |
| zh-TW lint | 多個 | 簡體字 |
| `.env.example` 姿態推導 | `c822751` | 分支姿態被覆蓋 |

> ⚠ **這張表裡有一項不屬於這裡。** 稽核帳的 append-only(`29db21b`,REVOKE +
> BEFORE UPDATE trigger)在 2026-07-29 的需求 QA 中經擁有者確認**是需求,不是
> 自長出來的機制** —— 威脅模型明確包含特權內部人,原話是「防止 admin 權限人員
> 偷偷做假」。它跟上表其他項目的性質不同,不要一起丟。
>
> 判準是這句:**加密防資料、稽核防人。** 真正的保障是「你動手會留下刪不掉的
> 紀錄」,而不是「你完全動不了」。詳見 `SYSTEM-MAP.md` §8。

---

## 四、資料庫

`a4118a3` 的 alembic 鏈最新是 **`r1_0008`**;`4524598` 是 **`r1_0044`**,
中間 **34 個 migration**。

**⚠ 這一節原本寫著「這是唯一無法用 git 取回的部分,動手前需要先決定資料怎麼
處理」—— 2026-07-29 的需求 QA 推翻了它。**

擁有者確認:`.15` 上現有的資料只有**少數同事試用的對話**,**沒有知識庫**,
而且**全部可以刪除**。

所以不存在資料遷移問題:**資料庫砍掉重建即可**,不需要驗證那 34 支 `downgrade()`,
也不需要寫壓縮 migration。原本那段敘述會讓人以為有一個必須先解決的阻礙,
然後花時間去處理一個不存在的問題。

(若日後有真實資料才需要重新考慮這一節。)

---

## 五、產物清單(在 attic 裡,非程式碼)

| 東西 | 路徑 |
|---|---|
| 負載基線量測 | `attic:docs/planning/load-baseline.md` |
| k6 負載腳本(含直打 gateway 對照組) | `attic:infra/loadtest/` |
| 現場側錄工具(唯讀取樣) | `attic:infra/capture/` |
| 從零部署演練紀錄(八個阻礙 + `.env` 配方) | `attic:docs/planning/launch-2026-07-29-runbook.md` |
| 補救計畫 / 稽核合成報告 / 交接 | `attic:docs/planning/*-2026-07-2*.md` |
| 開發路線圖(Gate 制) | `attic:docs/planning/anila-development-roadmap.md` |

---

## 六、本檔作者聲明

本檔由當時參與開發的 agent(Claude)所寫,而它也是上述累積的參與方之一。
2026-07-28 當天本 agent 仍在新增機制,直到擁有者指出方向錯誤為止;
第三節的清單包含當天新增的項目。
