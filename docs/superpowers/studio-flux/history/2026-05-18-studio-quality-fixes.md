# ANILA LM Studio 簡報生成 — 檢查報告與修正規格

> 給 Claude Code 的修正規格。
> 倉庫：`zzw09773/ANILA`，基準 HEAD：`910253c`（2026-05-18）。
> 診斷素材：`新進人員心得與技術規劃報告__1_.pptx`（15 張，gemma4 產出，包含 Phase 5 image_focus 與 Phase 6 image_prompt 結果）。

---

## 1. 範圍

修正目標：讓 Studio 在面對技術型來源文件時，產出 **「Layout 多樣性、版面飽和度、視覺保真度」** 都達到可直接交付的品質，而不需使用者人工後製。

修正不涉及：

- LLM 模型替換（`SLIDES_LLM_MODEL` / `VISION_LLM_MODEL` 由 ops 設定，與本次無關）
- 新增 layout kind（除 `agenda` 在 Fix 5 列為可選）
- 前端 UI（`ANILALM/src/`）
- 後端 retrieval（`anila-core` 的 embedder、chunking）

---

## 2. 現況診斷

### 2.1 投影片產出觀察

對診斷素材（15 張）逐張檢視，發現 layout 命中與版面飽和度問題如下：

| # | Title | 實際 layout_kind | 應為 | 問題 |
|---|---|---|---|---|
| 1 | 新進人員心得與技術規劃報告 | auto cover | auto cover | 副標 italic 對 CJK 顯示不佳 |
| 2 | 報告大綱 | standard | icon_rows 或新 `agenda` | 三條 bullet 上方一大片留白 |
| 3 | 一、技術背景回顧 | section_break | section_break | OK |
| 4 | 工業影像辨識的核心挑戰 | icon_rows | icon_rows | 三 icon 在主題下不夠語意化（rocket、table、sparkles）|
| 5 | 解決方案：雙分支特徵融合 | two_column | two_column | columns.bullets 各 2 條，下半空蕩 |
| 6 | 技術實作與最佳化 | standard | standard | OK |
| 7 | 研究成果：跨域泛化突破 | stat_callout | stat_callout | `supporting` 沒填，95% 上半空一大片 |
| 8 | 二、現行專案深化 | section_break | section_break | OK |
| 9 | 系統架構：領域驅動設計 | image_focus + image_prompt(FLUX) | image_focus + 真實圖 或 graphviz | **FLUX 圖含亂碼文字「Geneeration、KIGDKED、Retrievel」** |
| 10 | ReAct Agent 核心能力 | icon_rows | icon_rows | OK |
| 11 | Agentic Workflow 實作流程 | **standard** | **icon_rows**（感知 → 認知 → 行動三並列）| **漏選**：三步驟結構卻用 bullet|
| 12 | 三、技術擴充套件藍圖 | section_break | section_break | OK |
| 13 | Multi-Agent Supervisor 架構 | **standard** | icon_rows 或 diagram | **漏選**：架構描述卻用 bullet |
| 14 | Worker 專業分工設計 | two_column | two_column | 各欄 2 條 bullet，下半空蕩 |
| 15 | 結語與展望 | quote | quote | quote 文字偏上左、版面浪費 |

### 2.2 命中率

- **正確命中**：1, 3, 4, 5, 6, 7, 8, 10, 12, 14, 15 = **11/15 = 73%**
- **layout 漏選**（gemma4 應選非 standard 但選了 standard）：2, 11, 13 = **3 張**
- **layout 命中但內容/視覺有缺陷**：4 (icon 過於通用), 7 (supporting 漏填), 9 (FLUX 亂碼), 14 (bullets 過少), 15 (留白)

關鍵指標：**standard 比例 6/15 = 40%**，符合 prompt 規則 3（< 60%）。但若沒有那三張漏選的 standard，本應該降到 3/15 = 20%。

### 2.3 已驗證運作正常的元件（**請勿大改**）

- `schemas/studio.py` 的 Pydantic 結構（LAYOUT_KINDS、Stat、Quote、Column、IconRow、image_ref、image_prompt 都正確驗證）
- `pptx-skill/server.js` 的 layout dispatcher（`renderSlideByKind`）跟各 renderer 函式
- Job-based async pipeline（POST /jobs → poll → /pptx）
- `_extract_json_object` 從右往左找 balanced brace 的策略
- 文字正規化（`studio_text_normalizer`，簡轉繁、用詞對映）

### 2.4 根因摘要

| 編號 | 根因 | 對應 Fix |
|---|---|---|
| R1 | Layout 決策跟內容撰寫綁在同一個 LLM call，模型注意力被內容吃掉，prompt 規則 3-5 沒被遵守 | Fix 1 |
| R2 | image_prompt 走 FLUX 不分內容類型，對「架構圖、流程圖」這類含 label 的圖無能為力 | Fix 2 |
| R3 | `renderStatCallout` / `renderTwoColumn` 沒有 vertical distribution，內容少就上半或下半空蕩 | Fix 3 |
| R4 | Schema 沒強制 `Stat.supporting` / `Column.bullets` 最少數量，LLM 自然偷懶 | Fix 3 |
| R5 | Icon 概念白名單只有 33 個 heroicons key，遇到 domain-specific 概念（跨機臺泛化、資料不平衡等）必然 fallback 到通用 icon | Fix 4 |
| R6 | 缺乏 deterministic 幾何 QA，gemma4 vision 對版面留白、未填滿等問題不敏感 | Fix 6 |

---

## 3. 修正項目（按優先級）

### Fix 1: Layout 分配後驗 + 強制 re-select 〔最高優先〕

**問題**：gemma4 在內容多時會偷懶把該用 icon_rows / two_column 的投影片寫成 standard（slide 11、13 是經典案例）。Prompt 規則 3「standard 不可超過 60%」是文字約束，沒有強制執行機制。

**目標**：在 `SlidesSpec` 通過 Pydantic 驗證後、render 前，加一道 deterministic 檢查；若發現 layout 分配不合規，再呼叫一次 LLM 要求 **只改 layout_kind 跟對應的 layout-specific 欄位、不改 bullets**。

**影響檔案**：

- `myCSPPlatform/backend/app/api/studio.py`
- 可能新增 `myCSPPlatform/backend/app/services/studio_layout_balancer.py`

**規格**：

1. 在 `_build_generation_prompt` 之後、`/render` 之前，新增 step `JOB_STEP_REBALANCING = "rebalancing"`（加到 `schemas/studio.py`）。

2. 寫一個 `_audit_layout_distribution(spec: SlidesSpec) -> list[LayoutViolation]`，檢查項目：

   - **V1 (hard)**：`standard` 佔比 > 60%
   - **V2 (hard)**：規則 4——chunks 含量化數字（`re.search(r'\d+(\.\d+)?\s*[%％]|\d{4,}|F1[-\s]?score|N\s*=\s*\d+', chunks_text)`）但 spec 內沒有任何 `stat_callout`
   - **V3 (soft)**：連續 3 張以上 `layout_kind == "standard"`（節奏問題）
   - **V4 (soft)**：投影片含 enumeration 關鍵字（「三大」「步驟」「階段」「面向」「核心能力」「workflow」「pipeline」)且 bullet count >= 3，但 layout 是 standard——很可能該是 icon_rows

3. 若 V1 或 V2 命中，呼叫 `_rebalance_layouts(db, user, spec, violations, chunks)`：
   - 給 LLM 看：原 spec 的精簡版（每張只列 `{id, title, layout_kind, bullet_count}`）+ 違規清單 + 修改候選（哪幾張 candidate 可改 layout）+ 規則
   - 要求輸出：`{"changes": [{"slide_index": int, "new_layout_kind": str, "new_payload": {...}}]}`
   - 限制：不可改 title、不可刪 slide、不可改 bullet 文字（只能 reorganize 進 layout payload）
   - 套用 changes 到原 spec、再跑一次 Pydantic 驗證

4. 若 rebalance 後 V1 仍違規，**記 warning log 後繼續**（不要阻擋 pipeline，使用者寧可拿到 60%+ standard 也不要拿到 502）。

5. Job step 流程更新：

   ```
   generating → rebalancing → rendering → qa → fixing → done
   ```

**Re-select prompt 草稿**（中文，gemma4 友善）：

```
你正在審查一份已產出的簡報 spec。原 spec 有 layout 分配問題，請只調整
layout_kind 跟對應的 layout-specific 欄位，**不要修改 title 或 bullet 文字**。

違規：
- standard 比例 7/15 = 47% [若 > 60% 顯示]
- 缺少 stat_callout，但檢索段落含量化數據 "{陳列偵測到的數字}"

候選改造（這幾張的 bullet 結構很像非 standard layout）：
- slide_index=10, title="Agentic Workflow 實作流程"
  bullets=["感知 (Perception)：...", "認知 (Cognition)：...", "行動 (Action)：..."]
  → 看起來像 3-row icon_rows，每 row heading 取冒號前段，description 取後段
- slide_index=12, title="Multi-Agent Supervisor 架構"
  bullets=[...]
  → 看起來像 icon_rows

輸出 JSON：
{
  "changes": [
    {"slide_index": 10, "new_layout_kind": "icon_rows",
     "new_payload": {"icon_rows": [{"concept": "...", "heading": "...", "description": "..."}, ...]}},
    ...
  ]
}

不要改其他張、不要 reasoning、不要程式碼塊。
```

**驗收**：

- 用診斷素材的原 chunks 重跑 pipeline，比對輸出 spec：
  - slide 2、11、13（或對應位置）的 layout_kind **不可全部是 standard**
  - slide 7（或對應數據張）必定有 `layout_kind == "stat_callout"` 且 `stat.supporting` 非空
- standard 比例 < 50%（嚴於 prompt 規則 3 的 60%）
- 規則 4 違規率 < 5%（rebalance 後）

---

### Fix 2: FLUX 不畫圖表，diagram 走 graphviz 〔次高優先〕

**問題**：診斷素材 slide 9 用 `image_prompt` 走 FLUX.2-dev 生 RAG 架構圖，結果出現 "Geneeration"、"KIGDKED"、"Retrievel"、亂碼方塊——這是 diffusion model 對文字無能為力的標準症狀。FLUX 適合做情境插畫，**不適合做含 label 的架構圖、流程圖、bar chart**。

**目標**：在 prompt 教 LLM 區分 illustration vs diagram，diagram 改走 graphviz 文字描述渲染。

**影響檔案**：

- `myCSPPlatform/backend/app/schemas/studio.py`（新增 `image_kind` 欄位）
- `myCSPPlatform/backend/app/api/studio.py`（prompt 補充、新增 diagram 渲染分支）
- `ANILALM/pptx-skill/server.js`（新增 `/render-diagram` endpoint 或在 `/render` 內處理 diagram spec → PNG）
- 新增 Dockerfile 依賴：`graphviz`（apt 包，air-gapped image 內可離線安裝）

**規格**：

1. **Schema 變更**（`schemas/studio.py`）：

   ```python
   class Slide(BaseModel):
       ...
       image_kind: Literal["illustration", "diagram"] | None = None
       image_prompt: str | None = Field(default=None, max_length=500)
       # 新增：當 image_kind == "diagram" 時填，graphviz DOT 文字
       diagram_dot: str | None = Field(default=None, max_length=3000)
   ```

   驗證規則（model_validator）：

   - `image_kind="illustration"` → 必須有 `image_prompt`，不可有 `diagram_dot`
   - `image_kind="diagram"` → 必須有 `diagram_dot`，不可有 `image_prompt`
   - 兩者擇一互斥；都沒填則 `image_focus` 退化為 standard

2. **Prompt 變更**（`_build_generation_prompt`）：

   - 把 image_focus 段落改寫，明確區分：
     - 「**架構圖、流程圖、有 label 的示意圖、bar chart、Venn diagram**」→ `image_kind="diagram"` + `diagram_dot`
     - 「**情境插畫、概念意象、無文字的視覺輔助**」→ `image_kind="illustration"` + `image_prompt`
   - 給 1-2 個 DOT 範例（Supervisor / Worker 架構、3 階段流程）
   - 強調：DOT 內可以用中文 label（graphviz 透過 fontname 屬性支援 CJK，需在 server.js 注入字型）

3. **後端執行 graphviz**：

   - 新增 service `myCSPPlatform/backend/app/services/diagram_renderer.py`：
     ```python
     async def render_dot_to_png(dot: str) -> bytes:
         # 用 subprocess 跑 dot -Tpng，timeout 5s
         # 失敗（DOT 語法錯）回 None，外層走 standard fallback
     ```
   - 把產出的 PNG bytes 上傳 ingestion_images 表（複用既有 image_ref 機制），再把 image_id 寫回 spec.slides[i].image_ref，這樣 renderer 不需要改。
   - 注意：`dot` 預設找不到 CJK 字型，需在 Dockerfile 裝 `fonts-noto-cjk` 並設環境變數 `PANGOCAIRO_BACKEND=fontconfig`。

4. **Renderer 不需大改**（`ANILALM/pptx-skill/server.js` 的 `renderImageFocus`）——只要 `image_ref` 解得出 bytes 即可。

**驗收**：

- 重跑診斷素材：slide 9 的 RAG 架構圖必須是 graphviz 產出（清晰 label，無亂碼），或退化為 icon_rows
- 隨機抽 10 個技術文件跑 pipeline，FLUX 產出的圖**不含可辨識的英文/中文 label 亂碼**（人工檢查）
- DOT 語法錯誤的 slide 自動 fallback 到 standard，pipeline 不 fail

---

### Fix 3: stat_callout / two_column 版面飽和度 〔高優先〕

**問題**：

- `renderStatCallout`（`pptx-skill/server.js`）只渲染中央大數字 + 下方 label，supporting 欄位有也可能小一號擺旁邊，上半永遠空一大片。
- `renderTwoColumn` columns.bullets 最少 1 條就過 schema，LLM 經常各欄寫 2 條 bullet，導致下半 5 inches 空蕩。
- Schema 層 `Stat.supporting` optional + `Column.bullets` min_length=1 是這兩者的源頭。

**目標**：Schema 強制下限 + Renderer 主動填滿可用空間。

**影響檔案**：

- `myCSPPlatform/backend/app/schemas/studio.py`
- `ANILALM/pptx-skill/server.js`
- `myCSPPlatform/backend/app/api/studio.py`（prompt）

**規格**：

1. **Schema**：

   ```python
   class Stat(BaseModel):
       value: str = Field(..., min_length=1, max_length=20)
       label: str = Field(..., min_length=1, max_length=120)
       supporting: str = Field(..., min_length=20, max_length=200)  # 改為 required
       # 新增 optional 對比欄位，stat_callout 飽和的關鍵
       baseline: str | None = Field(default=None, max_length=20)
       baseline_label: str | None = Field(default=None, max_length=60)

   class Column(BaseModel):
       heading: str = Field(..., min_length=1, max_length=120)
       bullets: list[str] = Field(..., min_length=3, max_length=6)  # 從 1 改為 3
   ```

   - **降級策略**：Pydantic 違規時不要直接 422。在 `_validate_or_correct_spec` 內，若 `Stat.supporting` 為空，自動補一句「來源：{第 N 條 chunk filename}」並 log warning；若 `Column.bullets` < 3，自動把該欄退化為 standard 排版的 bullets，並把 `layout_kind` 改回 `standard`。這延續 schema 註解的「降級不失敗」原則。

2. **Prompt 變更**：

   - stat_callout 段落補：「**必填 supporting：寫 20-100 字的數字脈絡（baseline、樣本數、實驗條件、結果意義）；不可只寫『重要突破』『顯著進步』這類空話**」
   - 給 1 個正例：
     ```
     stat: {value: "95%", label: "CT350 機臺鐵屑覆蓋率偵測率",
            supporting: "雙分支架構相比單分支 ResNet18 基準的 78%，提升 17 個百分點；測試集為 10 個機臺切換批次，N=2,400",
            baseline: "78%", baseline_label: "單分支基準"}
     ```
   - two_column 段落補：「**每欄至少 3 條 bullet**；若內容湊不到 3 條，改用 icon_rows 或 standard」

3. **Renderer 變更**（`pptx-skill/server.js`）：

   `renderStatCallout`：

   - 若 `stat.baseline` 存在，畫成 **左右對比**：左小字 baseline + baseline_label，中間箭頭，右大字 value + label。
   - 若 `stat.baseline` 不存在，把現有單一大字版面**垂直置中於整個內容區（1.0 → 6.9 inches）**，supporting 移到 value 下方 0.5 inch 處。
   - 加 footer 段：把 `bullets` 的第 0 條當作 1 句 takeaway 印在 slide 底部 6.5 inches 處（弱化字級 14pt muted 色）。

   `renderTwoColumn`：

   - 計算 column 內容總高度，若 < 4 inches，在每欄底部加一條「[視覺分隔線]」加備援補充段（例如 column.footnote，schema 新增 optional `footnote: str | None`）。
   - 或更簡單：強制 column.bullets 至少 3 條，由 prompt + schema fallback 保證。

**驗收**：

- 重跑診斷素材：slide 7 的 stat_callout 必須含 baseline 對比或填滿垂直區的 supporting；slide 5、14 的 two_column 每欄至少 3 個 bullet
- 用 `pptx-skill/scripts/office/soffice.py` 把產出轉 PDF → JPG，目測**沒有任何 slide 出現「上半空 3 inches 以上」或「下半空 3 inches 以上」**

---

### Fix 4: Icon 概念白名單擴充 〔中優先〕

**問題**：`pptx-skill/icons.js` 的 `CONCEPT_MAP` 只有 33 個 heroicons key，且都是泛用概念（user、success、error、metrics 等）。遇到「跨機臺泛化差」「資料不平衡」「特徵提取困難」等 domain-specific 概念，LLM 被迫硬塞 generic icon（rocket、table、sparkles），結果就是診斷素材 slide 4 那種 icon 跟主題對不上的狀況。

**目標**：擴充至 ~100 個概念，按 domain 分組；給 LLM domain hint 限縮選擇空間。

**影響檔案**：

- `ANILALM/pptx-skill/icons.js`
- `myCSPPlatform/backend/app/api/studio.py`（prompt 內的白名單）

**規格**：

1. **`CONCEPT_MAP` 擴充**（不一定要完全用我列的，這只是建議分組）：

   ```javascript
   const CONCEPT_MAP = Object.freeze({
     // === Generic（既有 33 個保留）===
     ...原本的 keys,

     // === Industrial / Manufacturing ===
     machine: 'HiCog8Tooth',
     factory: 'HiBuildingOffice2',
     sensor: 'HiSignal',
     defect: 'HiExclamationCircle',
     quality_control: 'HiCheckBadge',
     calibration: 'HiAdjustmentsHorizontal',
     anomaly: 'HiExclamationTriangle',

     // === ML / AI ===
     model: 'HiCpuChip',
     training: 'HiAcademicCap',
     inference: 'HiBolt',
     embedding: 'HiCubeTransparent',
     classification: 'HiSquares2x2',
     regression: 'HiArrowTrendingUp',
     overfitting: 'HiArrowsPointingIn',
     generalization: 'HiArrowsPointingOut',
     feature_extraction: 'HiBeaker',
     imbalance: 'HiScale',  // 已有 comparison，imbalance 借用同 icon
     fine_tuning: 'HiWrenchScrewdriver',
     agent: 'HiUserCircle',
     reasoning: 'HiLightBulb',
     retrieval: 'HiMagnifyingGlassCircle',

     // === System Architecture ===
     supervisor: 'HiUserGroup',
     worker: 'HiWrench',
     orchestration: 'HiQueueList',
     hierarchy: 'HiSquaresPlus',
     vertical_split: 'HiViewColumns',
     fanout: 'HiArrowsRightLeft',  // 借用

     // === Process / Workflow ===
     perception: 'HiEye',
     cognition: 'HiCpuChip',
     action: 'HiPlay',
     step_one: 'HiNumberedList',  // 序列步驟通用
     alert: 'HiBellAlert',
     iteration: 'HiArrowPath',

     // === Outcome / Impact ===
     improvement: 'HiArrowTrendingUp',
     reduction: 'HiArrowTrendingDown',
     breakthrough: 'HiSparkles',
     limitation: 'HiNoSymbol',
   });
   ```

   完整 heroicons 名稱列表參考 `node_modules/@heroicons/react`，所有 `Hi*` 都可用。

2. **Prompt 變更**：

   - 把白名單分組顯示，並加一個欄位讓 LLM 先報 domain：

     ```
     ── icon_rows 出現前，先決定 domain ──
     domain 從以下擇一寫進 speaker_notes 第 0 行：
       "industrial" / "ml_ai" / "system_arch" / "process" / "outcome" / "generic"

     ── icon_rows.concept 必須從對應 domain 的白名單挑 ──
     [industrial] machine, factory, sensor, defect, quality_control, ...
     [ml_ai] model, training, inference, embedding, feature_extraction, ...
     [system_arch] supervisor, worker, orchestration, ...
     [process] perception, cognition, action, alert, iteration, ...
     [outcome] improvement, reduction, breakthrough, limitation, ...
     [generic] (既有 33 個)
     ```

3. **Fallback 行為不變**：未知 concept 仍然「不畫 icon、只渲染 heading + description」，不要拋錯。

**驗收**：

- 重跑診斷素材 slide 4：跨機臺泛化差 → `generalization` 或 `machine`、資料不平衡 → `imbalance`、特徵提取困難 → `feature_extraction`，目測 icon 跟概念有語意連結
- `CONCEPT_MAP` 至少 80 個 entry
- 隨機抽 5 個其他知識庫產出，icon_rows 的 concept 不再大量 fallback 到 `success` / `metrics` / `network`

---

### Fix 5: 封面 italic 與 agenda layout 〔低優先，小改動〕

**問題**：

- 封面副標 italic 對 CJK 顯示效果差（faux-italic）。
- 報告大綱（slide 2）用 standard 渲染，三條 bullet 上方一大片留白；技術型簡報的 agenda 通常希望有編號 + 章節縮排。

**影響檔案**：

- `ANILALM/pptx-skill/server.js`

**規格**：

1. **封面 italic 拿掉**：`server.js` line ~604 的 `italic: true` 改為 `italic: false`，並把 `color: p.muted` 改為較深的 `1A1A1A` + `fontSize: 14`（既有 12 → 14）。

2. **新增 layout kind `agenda`**（可選）：

   - Schema：`LAYOUT_KINDS` 加 `"agenda"`；新增 optional `Slide.agenda_items: list[AgendaItem] | None`，`AgendaItem = {chapter: str, title: str, lead: str}`
   - Renderer：左側大字「議程 / Agenda」直書或斜排，右側 3-5 條編號項目，每項：圓圈內中文數字 + heading + 一行 lead
   - Prompt：強制 slide 1（封面後）若 title 含「大綱、議程、目錄、Agenda、Outline」則 `layout_kind="agenda"`

   若評估 layout 新增成本太高，**Fix 5 第 2 點可以延後**，把 Fix 1 的 V4 規則改為「title 含 agenda 關鍵字 → 強制 icon_rows」即可。

**驗收**：

- slide 1 副標題顯示為正體（非斜體）
- slide 2（若實作 agenda）顯示為左右分欄、左議程標題、右編號清單

---

### Fix 6: Deterministic 幾何 QA 〔中優先，可平行進行〕

**問題**：vision QA 用 gemma4 vision 偵測 layout 缺陷（留白、溢出），實測對「上半空 3 inches」「下半空 3 inches」這類「沒錯但難看」的問題抓不出來。gemma4 vision 不是專家。

**目標**：在 vision QA 前加一層 deterministic 幾何 QA，靠 pptxgenjs 內部座標檢查，比 vision model 便宜且穩定。

**影響檔案**：

- 新增 `ANILALM/pptx-skill/server.js` 的 `/qa-geometric` endpoint
- 新增 `myCSPPlatform/backend/app/services/geometric_qa.py`
- `myCSPPlatform/backend/app/api/studio.py`（pipeline 插入）

**規格**：

1. **`/qa-geometric` endpoint**：

   - 輸入：base64 編碼的 .pptx
   - 邏輯：用 `pptxgenjs` 反向解析 slide.shapes 拿到所有 shape 的 `{x, y, w, h, text_length}`，計算：
     - **留白比例**：每張 slide 計算 `(slide_area - sum(shape_area)) / slide_area`，> 0.55 標 warning
     - **溢出**：任何 shape 的 `x + w > 13.33` 或 `y + h > 7.5` 標 critical
     - **重疊**：兩個非 master shape 的 bounding box 交集 > 0.1 sq inch 標 warning
     - **文字長度 vs box**：若 `text_length / (w * h) > 50`（粗估每平方 inch 容納 50 個 CJK 字元），標 warning「可能溢出」
   - 輸出：與 `VisualDefect` 相容的 list

2. **Pipeline 整合**：

   - 在 vision QA 前插入 geometric QA
   - 兩者結果合併，去重（同 slide 同問題只留嚴重度較高的）
   - 若 geometric QA 已標 critical，不必跑 vision QA 該張（省 token）

**驗收**：

- 對診斷素材跑 geometric QA：slide 2、7、11、13、15 至少要被標出「留白過多」的 warning
- 跨 10 份隨機產出，geometric QA 抓出的 critical 至少有 80% 真的是 user-visible defect（人工 spot-check）
- vision QA 呼叫次數下降（geometric QA 攔截一部分）

---

## 4. 不在本次範圍

- 換 LLM 或新增 LLM provider（保持 `SLIDES_LLM_MODEL = "gemma4"`）
- 真實圖檢索品質改善（`STUDIO_IMAGE_TOP_K`、`STUDIO_IMAGE_MIN_SCORE` 不動）
- 前端 UI 變更
- 換掉 pptxgenjs 改其他 lib
- 新增 podcast / video / mindmap 等其他 Studio 9 種輸出
- 多語系 / 英文輸出 path

---

## 5. 整體驗收（拿這份檔案跑 end-to-end）

把本報告的素材 chunks（從 collection 取出）重新跑一次 `POST /api/studio/slides/jobs`，等 done 狀態後下載 .pptx，並用 `ANILALM/pptx-skill/scripts/office/soffice.py` 轉 PDF → JPG 視覺檢查。

**通過條件**：

1. `standard` layout 佔比 ≤ 50%
2. 至少 1 張 `stat_callout` 且 `stat.supporting` 非空
3. 任何 image_focus 的圖**不含可辨識的英文/中文亂碼**（FLUX 退場、graphviz 接手或 fallback）
4. 任何 slide 的 4 象限**不存在「整個象限完全空白」**（geometric QA pass）
5. Slide 11、13（或對應的多階段／架構描述張）的 layout_kind **不能是 standard**
6. Job 不可因任何單一 schema 違規而 502；fallback 路徑必須生效

---

## 6. 實作順序建議

| 階段 | 工項 | 估時 |
|---|---|---|
| Sprint 1 | Fix 3（schema + renderer） + Fix 5.1（italic） | 1 天 |
| Sprint 1 | Fix 4（icon 白名單擴充） | 0.5 天 |
| Sprint 2 | Fix 1（layout rebalancer） | 1.5 天 |
| Sprint 2 | Fix 6（geometric QA） | 1 天 |
| Sprint 3 | Fix 2（FLUX 分流 + graphviz） | 2 天 |
| Sprint 3 | Fix 5.2（agenda layout，可選） | 0.5 天 |

**強烈建議 Sprint 1 + 2 完成後就先收一次驗收**，因為 Fix 1 + 3 + 6 已經能把 visible 品質拉到「不需要人工後製」的程度。Fix 2（FLUX 分流）影響範圍最大，獨立 sprint 處理。

---

## 7. 附錄：本次參照的 codebase 位置

| 路徑 | 用途 | 修改範圍 |
|---|---|---|
| `myCSPPlatform/backend/app/api/studio.py` | 主 pipeline + prompt + vision QA | Fix 1, 2, 3, 6 |
| `myCSPPlatform/backend/app/schemas/studio.py` | Pydantic schemas | Fix 1, 2, 3 |
| `myCSPPlatform/backend/app/services/studio_text_normalizer.py` | 文字正規化 | 不動 |
| `myCSPPlatform/backend/app/services/studio_job_service.py` | Job 狀態管理 | Fix 1（新 step） |
| `ANILALM/pptx-skill/server.js` | pptxgenjs renderer 各 layout 函式 | Fix 3, 5 |
| `ANILALM/pptx-skill/icons.js` | `CONCEPT_MAP` heroicons 映射 | Fix 4 |
| `ANILALM/pptx-skill/Dockerfile` | renderer image | Fix 2（裝 graphviz） |
| `ANILALM/src/studio/generators.ts` | 前端 fallback artifact，**不走實際 pipeline** | 不動 |

---

**這份報告產出時間**：2026-05-18
**診斷依據**：素材 pptx + repo HEAD `910253c` 全 codebase reading
**修正後再次驗證**：請拿原素材重跑，比對本報告第 2.1 表格與第 5 節通過條件
