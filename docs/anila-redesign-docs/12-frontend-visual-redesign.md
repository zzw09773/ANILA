# 12. 前端視覺重設計 — 去駭客風・官方藍 institutional console

> Status: design v1（2026-07-02，Fable 5 設計；實作交 Opus）
> Scope: CSP 治理中心（apps/csp-governance-ui）主視覺 + 登入頁；ANILA shell 分類浮水印
> 前置：使用者拍板 ①淺色優先・官方藍 ②卡登卡片 hero
> 取證基準同 redesign docs：目標設計為準,現況僅遷移起點。

## 1. 為什麼改（診斷）

現況治理中心是「終端機／駭客電影」美學，四個指紋：碳黑深底 default、薄荷終端綠 accent（#7fd99b）、全等寬字、零圓角，加上登入頁的開機 log 動畫。針對中科院（軍方研究機構）治理後台，這觀感傳達的是玩具感而非可靠；且與全繁中政策（doc 11）打架——終端美學偏好英文 mono kicker，中文化還得開豁免清單讓 cosplay 存活。

**目標視覺語言**：中性、官方、可信、專業。淺底最讀作「官方」；深色加綠正是駭客聯想來源。

## 2. 已完成（Fable 設計交付）

`apps/csp-governance-ui/src/assets/styles/tokens.css` 已重寫（保留全部變數名 → 自動 cascade）：

- **淺色優先**：`:root` = 淺色（官方藍）；`[data-theme="dark"]` 保留但重調為柔和藍灰（非碳黑+霓虹）；`prefers-color-scheme` media query 翻為 dark。
- **官方藍 accent**：`--c-accent: #2b4c7e`（淺）/ `#6f8fc2`（深，去飽和）。移除終端綠。
- **中性冷灰**：底 `#f7f8fa`、卡面純白 `#ffffff`、冷 slate 前景（非 cream）。
- **溫圓角**：`--r-soft: 4px`、新增 `--r-md: 6px`/`--r-lg: 8px`/`--r-pill`。
- **sans 主字型**：新增 `--font-sans`（系統堆疊、air-gap 安全、中文 aware）；`--font-mono` 保留給資料。
- 追蹤/行高/motion 微調（去終端寬 caps 追蹤）。

## 3. 待實作（交 Opus）

### 3.1 字體翻轉（最高槓桿去終端指紋）

`apps/csp-governance-ui/src/assets/styles/main.css` @layer base：

- `body` `font-family: var(--font-mono)` → **`var(--font-sans)`**。
- `h1..h6` `font-family: var(--font-mono)` → **`var(--font-sans)`**（weight 600 保留）。
- **等寬只保留給資料語境**：新增 `.mono` utility（`font-family: var(--font-mono); font-variant-numeric: tabular-nums;`）；套用於：ID/slug/token/trace_id、時間戳、數值欄（usage 數字、延遲 ms、token 數）、代碼片段（`<pre>`/`<code>`/CLI snippet）、model/agent 名稱等識別碼。掃各 view 把這些欄位加 `.mono`。
- 目標：介面文字讀起來像正式後台，數字/ID 仍等寬對齊。驗收：`npm run build` 過 + 目視無等寬內文。

### 3.2 去 cosplay 掃除

逐 view 移除純表演性終端元素（非功能性）：

- **登入頁開機 log 動畫**（`LoginView.vue` 的 `bootlog`/`bootLines`）：整段移除（見 §3.3）。
- **kicker / eyebrow 英文小標**：如 `admin · iam`、`local · ldap · oidc · card` 這類 hint kicker → 移除或改為簡潔繁中副標。掃 `grep -rn 'kicker\|eyebrow\|hint=' views/`。
- **終端 chrome**：ASCII 邊框感、`$`/`>` prompt 裝飾、路徑 chrome → 移除。
- **保留**：`TermBox`/`TermBadge`/`TermStat` 等元件系統（不改名，避免 churn）；資訊密度（admin 工具吃密度）；功能性狀態點/badge。
- doc 11 豁免清單同步收窄：cosplay 消失後，登入 cosplay、`admin · iam` kicker 兩項豁免刪除（改一個小 ADR 註記；`CONFIDENTIAL` 浮水印豁免由 §3.4 取代為真浮水印，也刪豁免）。

### 3.3 登入頁卡登優先（版型：卡登 hero + 「其他登入」收底部）

`apps/csp-governance-ui/src/views/LoginView.vue` 重構（單欄、上方大標題、移除 bootlog）：

```
┌─────────────────────────────┐
│         ANILA 治理中心          │   ← 大標題(sans)+ 一行副標
│                              │
│    ┌──────────────────┐     │
│    │   ▢  自然人憑證卡登入   │     │   ← 主視覺卡片:插卡→偵測→PIN
│    │  請插入憑證卡並確認    │     │      沿用既有 handleCardLogin/
│    │  本機元件運作中       │     │      detectedCard/PIN 流程,只改版型
│    │  [   偵測憑證卡   ]   │     │
│    └──────────────────┘     │
│                              │
│   ──────  其他登入方式  ──────   │   ← 分隔線
│   帳密登入 · 單一登入(SSO)      │   ← 次要:預設收合/淡化,
│                              │      點開才展帳密表單 + OIDC 鈕
└─────────────────────────────┘
```

- 卡登卡片為**唯一主視覺**（`--c-surface-1` 白卡、`--r-md` 圓角、accent 邊或標題）；文案一目了然（「請插入自然人憑證卡」大字 + 明確 CTA）。
- 帳密（local）+ OIDC 收進底部「其他登入方式」：一條分隔線下，預設淡化或收合（`<details>` 或次要連結展開）。功能全保留，只降視覺權重。
- 卡登流程邏輯（challenge/verify/PIN/detect）**逐字保留**——只改版型與樣式，不動安全路徑。
- 移除 `bootlog` 開機動畫與相關 CSS/資料。
- 內網卡登模式（`REQUIRE_CARD_LOGIN_ONLY=true`）下應直接呈現卡登卡片為全部；帳密區可依旗標隱藏（若現況已有此邏輯則保留）。
- 全繁中；`npm run build` 過。

### 3.4 真分類浮水印（取代假 CONFIDENTIAL）

現況 `apps/anila-shell/src/trust.jsx` 的 `ClassifiedCorner`：紅角標、硬寫英文 `CONFIDENTIAL`、條件 `{classified && ...}`（`chat.jsx:315,474`）。問題：不反映真實五級、恆為英文——是裝飾語彙非真標記。與 Slice 3 建立的真五級分類（`classificationLevelBadge`）語義混淆，在真機密具法律效力的機構是安全 UX 硬傷。

**改為真浮水印**（反映實際 `classification_level`）：

- 分級（`ClassificationLevel`）：無機密 < 營業秘密 < 機密 < 極機密 < 絕對機密。
- **機敏模式 = 機密（含）以上**顯示浮水印；顯示**實際級別中文**（機密／極機密／絕對機密），非固定 CONFIDENTIAL。
- 營業秘密：不上全浮水印，維持既有 level badge（`classificationLevelBadge`，Slice 3c）即可。
- 無機密：無浮水印。
- 視覺分級遞進：機密（warn 琥珀角標）→ 極機密（danger 紅角標）→ 絕對機密（danger 紅 + 更重，如加底紋/邊框）。角標文字用真級別中文。
- 資料來源：優先讀 conversation 的 `classificationLevel`（Slice 3c app.jsx 已映）；缺欄位時回退既有 boolean `classified` → 顯示「機密」（floor，與 backfill 一致），不再顯示英文 CONFIDENTIAL。
- 位置/互動沿用現況（右上角標、與 `AuditWatermark` 並存不衝突）。
- 元件更名建議 `ClassifiedCorner` → `ClassificationWatermark`（takes level prop）；更新 `chat.jsx` 兩處呼叫傳 level。
- shell vitest 補測：機密/極機密/絕對機密各渲染對應中文+樣式；無機密不渲染；缺 level 回退機密。既有 213 測試不退步 + `npm run build` 過。

## 4. 實作紀律

- 工作於既有 stack（Vue 3 / Tailwind / React shell），不遷框架、不改元件名（除 §3.4 建議更名）。
- token 已改 → 顏色/圓角自動 cascade，低風險；字體翻轉與去 cosplay 為主要人工項。
- 不破功能：卡登/帳密/OIDC 安全路徑逐字保留；每步 `npm run build`（+ shell `npm test`）。
- 全繁中（台灣用語）；跑 `infra/ci/lint-zh-tw.sh` 不得新增命中。
- 分域檔案集：治理 UI（Opus A：main.css + views 去 cosplay + 字體）、登入頁（Opus B：LoginView）、shell 浮水印（Opus C：trust.jsx/chat.jsx/app.jsx）互斥並行。
