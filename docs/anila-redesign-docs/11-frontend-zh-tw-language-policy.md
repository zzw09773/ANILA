> ⚠ **2026-08-01 P2.1 契約更新**：下文若仍描述 `csk-`／`bsk-`／`X-CSP-Service-Token`／
> `CSP_SERVICE_TOKEN` 作為 agent 派工身分，該段已過時。現行＝5 分鐘派工 JWT＋JWKS 驗簽
> （開發者不領鑰匙；三級接入見 `docs/guides/developer-guide.md`）。
> 歷史段落未逐字改寫，以免破壞 redesign 文件結構。

# 11. Frontend 全繁體中文（zh-TW）語言政策

> Status: draft v0.2
> Purpose: 規定 ANILA 所有使用者可見文字一律使用繁體中文（台灣用語），並定義術語表、格式規範、豁免機制與工程化 enforcement。
> 取證基準：現況（Repo evidence）以 `origin/prod-intranet-card`（v1.2.0 系）為準；本機工作樹與其分歧時以 origin 為準。目標設計與現況相左時，一律以目標為準，現況段落僅作遷移起點對照。

---

## 1. 核心決策

> ✅ 已拍板（2026-07-02）：**繁體中文（台灣用語）是 ANILA 唯一介面語言。**

- 所有使用者可見文字——按鈕、標籤、選單、表單、提示、錯誤訊息、確認對話框、空狀態、aria-label、placeholder、toast、產出物（artifact）內文與 chrome——一律繁體中文＋台灣用語。
- **禁止簡體字**（零容忍）；**禁止大陸用語**（見 §4 術語表）。
- 不引入多語系切換：ANILA 是院內單一語系產品，**不做 i18n framework**（與 doc 02/10 的 MVP module boundary 決策同一精神——工程量花在核心能力，不花在用不到的 locale 切換）。字串集中化（§8）是為了術語一致與 lint，可不是為了翻譯。

---

## 2. 適用範圍

| 面 | 說明 |
|---|---|
| ANILA_UI（任務中心） | 全部 UI 文字，含 login、trace 面板、tweaks、trust chrome |
| ANILALM（我的知識庫／產出中心） | 全部 UI 文字，含 login、Studio wizard、job 狀態 |
| CSP frontend（治理中心） | 全部 UI 文字，含 nav、wizard、confirm、login（含 origin 卡登 UI） |
| Router 使用者可見字串 | 友善錯誤 chunk、trace step `label`／`detail`、clarify 模板、recompose prompt、follow-up chips |
| CSP backend `detail` | 會透出到 UI 的 HTTPException detail（前端 `readError`／`explainError` 皆原樣顯示） |
| anila-studio 產出物 | 簡報／報告／心智圖／資訊圖／資料表的內文、標題、preset 名、defect 摘要 |
| 靜態資源 | `index.html` title／meta／`lang` 屬性、changelog、guide |

**豁免機制**：唯一允許保留英文的是「非功能性裝飾字」（如登入頁終端風格 boot text、kicker 小字、`NORMAL · auth.tsx · UTF-8` 之類 cosplay chrome）。豁免項必須逐條登錄在本文件 §10 的豁免清單（含理由），由 owner 核准；owner 可隨時要求清零。**功能性文字（使用者需要讀懂才能操作的）一律不得豁免。**

**技術名詞政策**（與 doc 03 csk 命名裁決相容）：

- 憑證／協定／API 專有名（Agent Integration Key、Service Client Token、SSE、JWT、`csk-` prefix、API path、程式碼片段）保留英文原名，首次出現附繁中說明，例如「整合金鑰（Agent Integration Key）」。
- 品牌名（ANILA、CSP、ANILA LM）保留。
- 除上述外，UI 語彙一律繁中：功能名、按鈕、狀態、錯誤全中文。

---

## 3. 語言規則

1. **字**：只用繁體字元。簡體字元零容忍（CI lint，見 §9）。
2. **詞**：台灣用語。判準以 §4 術語表為準；表外爭議詞由 owner 裁決後補進表。
3. **句**：中文為主體；夾雜英文技術名詞時，中英之間留半形空格（例：「請妥善保管 Agent Integration Key，遺失需重新簽發」）。
4. **錯誤訊息**：使用者看得到的錯誤一律繁中、可行動（說明發生什麼＋下一步）；技術細節（status code、trace_id）可附在句尾括號。
5. **LLM 產出**：所有會呈現給使用者的 LLM 產出（follow-up chips、studio 內文、recompose 回覆）prompt 必須明示「以繁體中文（台灣用語）回覆」，並保留 OpenCC `s2twp` 之類的確定性後處理作為 backstop（studio 已有，見 §11）。

---

## 4. 術語表（seed；隨遷移擴充）

| ✅ 台灣用語 | ❌ 禁用 | 備註 |
|---|---|---|
| 檔案 | 文件（當 file 用時） | 既有收斂計畫已明定「用檔案不用文件」；「文件」僅限 document/說明文件語意 |
| 使用者 | 用戶 | |
| 登入／登出 | 登錄／注銷 | |
| 資料／數字 | 數據 | 「數據簡報」preset 建議更名「資料簡報」（見 §10 修正項） |
| 伺服器 | 服務器 | |
| 網路 | 網絡 | |
| 軟體／程式 | 軟件／程序 | |
| 影片 | 視頻 | |
| 螢幕 | 屏幕 | |
| 資訊 | 信息 | |
| 滑鼠／點選 | 鼠標／單擊 | |
| 預設 | 默認 | |
| 相容 | 兼容 | |
| 品質 | 質量 | |
| 效能 | 性能 | |
| 元件 | 組件 | |
| 儲存 | 保存 | 「妥善保存」作 keep-safely 語意可接受，作 save 語意用「儲存」 |
| 佇列／快取 | 隊列／緩存 | |

studio 的 `studio_llm.py` 台灣用語對映表與 OpenCC `s2twp` 詞庫是現成基礎，術語表應與其對齊、單一來源化（§8）。

---

## 5. 格式規範

| 項目 | 規則 |
|---|---|
| 日期時間 | 一律 `zh-TW` locale；顯示時區固定 **Asia/Taipei**（院內單一時區，不看瀏覽器時區）。相對時間沿用 ANILA_UI `time.js` 的繁中口語（剛剛／N 分鐘前／今天／昨天） |
| 數字 | `Intl.NumberFormat('zh-TW')`；千分位、百分比一致 |
| `<html lang>` | 三前端統一 **`zh-TW`**（現況 anila-ui 用 `zh-Hant`、其餘 `zh-TW`，不一致） |
| 中英混排 | 中文與英文／數字之間半形空格；全形標點 |
| 字型 | 沿用現有 air-gap system stack（含 `Microsoft JhengHei` / `PingFang TC` / `Noto Sans CJK TC`），**禁止外部字型 CDN**（現況已達成）；CSP frontend 的 mono stack 需補 CJK fallback 家族（現況缺，中文字落到系統預設） |

---

## 6. Router 與 backend 字串規則

- Router 使用者可見面（友善錯誤、trace `label`、clarify 模板）現況已繁中——**維持並列為 contract**；trace `kind` 是機器 token（`direct/dispatch/...`），不呈現為文字，維持英文。
- **follow-up chips prompt 必須補語言指示**：現況 `prompt_suggestion.py` 的 system prompt 是英文含英文範例、無語言指示，chips 語言全靠模型鏡射——改為明示繁中＋繁中範例（目標修正項）。
- CSP backend 會透出的 `detail` 一律繁中。現況約 68% 已繁中；**英文叢集中在 ingestion 模組**（documents/search/collections 的 "Empty file"、"Document not found"、"archive total exceeds 1 GB cap" 等）——列 P1 改寫。
- 前端顯示 backend `detail` 的路徑（`readError`／`explainError`）維持原樣顯示，語言責任在 backend；前端 fallback 訊息一律繁中。

---

## 7. anila-studio 產出物規則

- 現況是全 codebase 語言紀律最強的一環：prompt 層繁中台灣用語強制＋OpenCC `s2twp` 確定性後處理（治 Gemma 4 簡體洩漏）——**全部保留，並升格為 contract**（新增產出類型必掛 normalizer）。
- preset 顯示名全中文：`Lightning Talk` → 「閃電簡報（Lightning Talk）」；「數據簡報」→「資料簡報」（連動 ANILALM CommandModal／generators 與 backend preset 顯示名；preset **id**（`stats_brief`）不動）。
- Vision-QA defect 摘要已繁中（prompt 繁中）；geometric-QA 摘要語言由 Node renderer 端決定——遷移時盤點該端字串（現況未查證，標待盤點）。

---

## 8. 工程機制：字串集中化（非 i18n framework）

每個前端建立輕量字串模組（例：`src/strings/`），規則：

```text
- 單一語系（zh-TW），無 locale 切換、無翻譯 pipeline。
- UI 元件不得內嵌可見文字字面值；一律引用 strings 模組（裝飾性豁免項除外）。
- strings 模組按功能分檔（auth.ts / chat.ts / studio.ts ...），高內聚。
- 術語表（§4）落為 shared 檔（可由三前端與 studio prompt 共用），
  單一來源，lint 依它跑。
```

理由：現況三前端零 i18n 基礎、全硬編（見 §11）。集中化的目的是**術語一致＋可 lint＋可盤點**，不是翻譯；引入 i18next 級框架對單語系產品是負資產。

---

## 9. Enforcement（CI／lint；目標新增）

現況零 enforcement（無 CI、無 lint、無 hook）。目標：

1. **簡體字元掃描**：以嚴選 simplified-only codepoint 集（≈250 字）掃 `src/`＋backend 使用者可見字串檔，命中即 fail。注意誤判集（准／只／休／幕 為正當繁體用法），需用白名單化的字元集而非粗略 Unicode 區段。
2. **大陸用語掃描**：依 §4 禁用欄掃描（含繁體字形的大陸詞：用戶／登錄／服務器／網絡／視頻／屏幕／信息／數據…），命中即警告、白名單放行（如註解、preset id）。
3. **locale 掃描**：`toLocale*` 呼叫必須帶 `'zh-TW'`；禁止 `'en-GB'` 等硬編（現況 CSP frontend 有 8 處 en-GB）。
4. **`lang` 屬性檢查**：三前端 `index.html` 皆為 `zh-TW`。
5. **PR checklist**：新增 UI 文字＝繁中＋查術語表；新增 LLM prompt＝含繁中指示。
6. contract test（doc 09 §14）：backend 使用者可見 `detail` 抽樣斷言不含簡體字元。

---

## 10. 豁免清單與修正項

**豁免清單（owner 已核形式；新增需 owner 核准）**：

| 項 | 位置 | 理由 |
|---|---|---|
| 登入頁終端 cosplay chrome | ANILALM `LoginPage`（`~/anilalm — auth`、`NORMAL · auth.tsx · UTF-8`）、CSP `LoginView` boot text、ANILA_UI login 裝飾行 | 非功能性裝飾字；設計語彙 |
| kicker 小字 | CSP 各 view 的 `admin · iam` 類小標 | 非功能性；但同頁功能文字仍須繁中 |
| CONFIDENTIAL 浮水印字樣 | ANILA_UI `trust.jsx` | 國際慣用戳記；可並列「機密」由 owner 決定 |

**已知修正項（遷移 backlog，見 §12 優先序）**：

- 「用戶」→「使用者」（ANILA_UI `app.jsx` help 文案 1 處）。
- 「數據簡報」→「資料簡報」（ANILALM 3 處＋studio preset 顯示名）。
- 「文件」→「檔案」（file 語意，三前端約 38 處 vs 檔案 6 處——規模化收斂）。
- CSP frontend `en-GB` 日期 locale 8 處→`zh-TW`＋Asia/Taipei。
- anila-ui `lang="zh-Hant"`→`zh-TW`。
- ingestion 模組英文 `detail` 叢改寫繁中。
- `prompt_suggestion.py` 補繁中指示與繁中範例。
- CSP admin UI 英文主體（nav／按鈕／confirm／註冊精靈／改密碼 modal）全面繁中化——**本政策最大工程量**。

---

## 11. Repo evidence / 現況補齊

> 調查於本機工作樹（main 系）；除 login/auth 面外，三前端與 origin 差異僅 14 檔（多為登入相關），本節結論對兩系皆成立。**origin 卡登 UI（`LoginView.vue` 卡片流程、`caAuth.js`）未在本次調查範圍，遷移時需另行對照本政策盤點。**

- **零 i18n 基礎**：三前端皆無 i18n framework、無 locale 檔，字串全硬編。無任何時區處理（全瀏覽器本地時間）。無 CI／lint 語言檢查（repo 無 `.github/`）。
- **語言比例（可見字串啟發式掃描）**：ANILA_UI 約 76% 繁中（英文存量約 25–30 項：login 裝飾、`routing trace` 面板頭、`Agents` 側欄標、Tweaks、`(no diff returned)` 等）；ANILALM 約 70% 繁中（英文集中在 login cosplay 與 `Studio download {status}:` 類錯誤前綴，約 15 項）；**CSP frontend 約 77% 英文**（nav／kicker／按鈕／confirm／login／註冊精靈全英，屬刻意的 terminal 設計語彙）——是本政策的最大改造面。
- **格式現況**：ANILALM 已用 `Intl.RelativeTimeFormat('zh-TW')`／`toLocaleString('zh-TW')`；ANILA_UI 有手寫繁中相對時間（`runtime/time.js`）；**CSP frontend 硬編 `en-GB` 日期 8 處**、裸 `toLocaleString()` 數處。`lang` 屬性不一致（anila-ui `zh-Hant`，其餘 `zh-TW`）。
- **字型**：三前端皆已是 air-gap system stack、明文註記不載 Google Fonts；anila-ui／ANILALM stack 含 CJK TC 家族；CSP mono stack 缺 CJK 家族。studio 的 infographic renderer 釘選 `Noto Sans CJK TC` 防豆腐字、Docker 內建 `fonts-noto-cjk`。（`ANILALM/_design/prototype.html` 仍連 fonts.googleapis——設計稿殘留，不在建置產物內。）
- **Backend**：CSP `detail` 約 68% 繁中（auth／proxy／agents 繁中；**ingestion 模組為英文叢**），前端兩條路徑（`readError`／`explainError`）皆原樣透出。Router 使用者可見字串以繁中為主體（友善錯誤 chunk、trace label「Router 分析意圖中／選擇 agent／呼叫 {agent}」、clarify 模板、recompose prompt 全繁中）；**follow-up chips 的 system prompt 是英文、無語言指示**（`prompt_suggestion.py`）。
- **Studio**：`studio_text_normalizer.py` 以 OpenCC **`s2twp`**（字＋台灣詞轉換）後處理 slides／datatables／mindmaps／infographics（reports 走自家 `_normalize_spec`）；prompt 層明示「台灣繁體中文、用詞也要台灣本土」＋台灣用語對映表。preset 顯示名繁中（「Lightning Talk」除外）。
- **污染現況**：簡體字元 **0 處**（以嚴選 codepoint 集掃描；粗略掃描會誤判 准／只／休／幕）。大陸用語 7 處（多為輕微：「用戶」2 處其一僅註解、「數據簡報／數據」4 處、「妥善保存」1 處邊界可接受）。「文件 vs 檔案」38:6——與既有收斂計畫（`docs/superpowers/plans/2026-07-01-intranet-card-product-convergence.md`「use 檔案, not 文件」）規模化相違。
- **既有慣例**：唯一成文規則即上述收斂計畫一行；de-facto 術語來源是 studio 的對映表＋OpenCC 詞庫。`AGENTS.md` 的繁中規定僅約束 assistant 回覆，不及 UI 文案。

---

## 12. Migration（併入 doc 10 切片節奏）

| 優先 | 工作 | 規模 |
|---|---|---|
| P1 | 使用者面英文存量清零：ANILA_UI（~25–30 項）＋ANILALM（~15 項）＋backend ingestion `detail` 叢＋`prompt_suggestion.py` 語言指示 | 小～中 |
| P2 | 格式統一：`en-GB`→`zh-TW`（8 處）＋Asia/Taipei 時區、`lang` 統一 `zh-TW`、CSP mono stack 補 CJK | 小 |
| P3 | 術語收斂：文件→檔案（38 處）、用戶→使用者、數據簡報→資料簡報、Lightning Talk 中文化 | 中 |
| P4 | **CSP 治理中心全面繁中化**（nav／按鈕／confirm／精靈／login；豁免清單內裝飾字除外）＋origin 卡登 UI 盤點 | 大 |
| P5 | 工程機制：字串集中化（§8）＋CI lint（§9）＋術語表單一來源化 | 中 |

Done 條件：

- 三前端可見字串（豁免清單外）100% 繁中台灣用語；lint 全綠。
- backend 透出 `detail` 與 Router 使用者可見字串 100% 繁中。
- LLM 產出面全部帶繁中指示＋normalizer backstop。
- 日期／數字全 `zh-TW`＋Asia/Taipei；`lang` 全 `zh-TW`。
