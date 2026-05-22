# ANILA LM Studio — Theme Picker 與 Wizard 規格

> **新分支**：`feature/studio-wizard`（從 main 開，不接續 `feature/studio-quality-fixes`）。
>
> 這份是新方向、非 quality-fixes 收尾。Round 6 已經把 H bug 收完，quality-fixes 可以 merge main 結案。
>
> **目標**：把 theme 選擇從「系統猜」改成「使用者選」。Round 3 Patch P 已經把 `theme_override` 加進 backend API，**但前端從來沒接這個欄位**——`ANILALM/src/api/studio.ts:84` 的 `createSlidesJob()` 只送 `collection_id / preset / extra_instructions / skip_retrieval`，theme override 路徑空轉了三輪。
>
> 這份規格分兩階段：
>
> - **Phase A**：在既有 CommandModal 加一個 theme picker step（5 個視覺選項 + auto），把 `theme_override` 接到 API。**~3 小時**。
> - **Phase B**：加 wizard 模式（4-5 個下拉問題 → map 到 theme），給「不知道哪個 theme 適合」的使用者一條導引路徑。**~1.5 天**。
>
> Phase A 一旦上線就立刻解決「為什麼又長一樣」這個感受問題——使用者自己決定，沒人能抱怨系統猜錯。
>
> Phase C（多變體並行 render）跟 Phase D（`brand.yaml` 企業客製）**不在本次範圍**，等 A + B 跑一陣子驗證使用者行為再評估。

---

## 共用前提

### 既有檔案

| 路徑 | 目前職責 | 本次改動 |
|---|---|---|
| `ANILALM/src/api/studio.ts` | 對 backend `/api/studio/slides/jobs` 的 fetch client | A: 加 `themeOverride` 欄位 |
| `ANILALM/src/workspace/CommandModal.tsx` | Format → Preset 選擇 + 觸發 job 的 modal（518 行多 step UI）| A: 新增 theme step；B: 新增 wizard 模式 |
| `ANILALM/src/types.ts` | `SlidesArtifact` 等型別定義 | A: 加 `theme?: string` 到 artifact |
| `ANILALM/src/studio/generators.ts` | 前端 fallback artifact store（非主路徑）| 不動 |

### 新增檔案

| 路徑 | 用途 |
|---|---|
| `ANILALM/src/studio/themes.ts` | Theme 元資料（id / 中文名 / 描述 / 預覽色票），單一 source of truth |
| `ANILALM/src/workspace/ThemePicker.tsx` | A: 5+1 個 theme 卡片選擇器，含 SVG 預覽縮圖 |
| `ANILALM/src/workspace/StudioWizard.tsx` | B: 4 題下拉 + 自動映射到 theme 的精靈 |
| `ANILALM/src/studio/themeMapping.ts` | B: wizard 答案 → theme id 的映射表 + 推論函式 |

### 後端確認

`myCSPPlatform/backend/app/api/studio.py` 的 `SlidesJobRequest` 已含 `theme_override: str | None`（Round 3 commit `a5eee3b`）。這份 spec 假設這個欄位正常運作；如果發現 Round 3 P 的後端側其實沒上線、本次得補。

---

# Phase A — Theme Picker

## A.1 集中 theme 元資料

**檔案**：`ANILALM/src/studio/themes.ts`（新增）

```typescript
// 與 backend schemas/studio.py 的 THEMES list 對應
// 顯示用元資料：id 是給 API、name+description 是給 UI、swatches 是預覽縮圖用

export type ThemeId =
  | 'auto'                  // 不指定，讓系統照舊邏輯（Patch O tone + Patch U title-override）
  | 'corporate_navy'
  | 'academic_paper'
  | 'warm_journal'
  | 'executive_brief'
  | 'startup_pitch'

export interface ThemeDescriptor {
  id: ThemeId
  name: string                // 「企業技術」「個人心得」等
  description: string         // 一句話定位
  suitableFor: string[]       // 三個 tag：「給同事」「給主管」等
  swatches: {
    bar: string               // 主色 / 標題列
    accent: string            // 強調色
    ink: string               // 文字色
    bg: string                // 背景色
  }
  typography: 'sans' | 'serif'
  iconStyle: 'outlined-circle' | 'soft-filled' | 'monochrome-dot' | 'filled-pill' | 'minimal-dot'
}

export const THEMES: ThemeDescriptor[] = [
  {
    id: 'auto',
    name: '自動偵測',
    description: '依據文件 title 與內容自動挑選風格',
    suitableFor: ['不知道哪個適合', '快速生成', '相信系統判斷'],
    swatches: { bar: '#888', accent: '#aaa', ink: '#222', bg: '#fff' },
    typography: 'sans',
    iconStyle: 'outlined-circle',
  },
  {
    id: 'corporate_navy',
    name: '企業技術',
    description: '深藍配琥珀，給同事或主管的技術 / 業務報告',
    suitableFor: ['給同事', '給主管', '技術深度報告'],
    swatches: { bar: '#1E2761', accent: '#F4B740', ink: '#1A1A1A', bg: '#FFFFFF' },
    typography: 'sans',
    iconStyle: 'outlined-circle',
  },
  {
    id: 'academic_paper',
    name: '學術研究',
    description: '炭灰配 serif 字型，學術發表 / 深度研究風',
    suitableFor: ['研討會', '研究報告', '基本面分析'],
    swatches: { bar: '#FFFFFF', accent: '#A0826D', ink: '#212121', bg: '#FFFFFF' },
    typography: 'serif',
    iconStyle: 'monochrome-dot',
  },
  {
    id: 'warm_journal',
    name: '個人心得',
    description: '米白配棕褐，第一人稱回顧 / 學習紀錄',
    suitableFor: ['個人心得', '反思記錄', '軟性分享'],
    swatches: { bar: '#FFF8F0', accent: '#D2691E', ink: '#4A3429', bg: '#FFFCF7' },
    typography: 'sans',
    iconStyle: 'soft-filled',
  },
  {
    id: 'executive_brief',
    name: '高層 Briefing',
    description: '黑白極簡無 chrome，結論導向 ≤ 10 張',
    suitableFor: ['給 C-level', '極簡', '結論導向'],
    swatches: { bar: '#FFFFFF', accent: '#1A1A1A', ink: '#1A1A1A', bg: '#FFFFFF' },
    typography: 'sans',
    iconStyle: 'minimal-dot',
  },
  {
    id: 'startup_pitch',
    name: '對外發表',
    description: '巨型字級 + 高彩度，對客戶 / 投資人 / 產品發表',
    suitableFor: ['對外發表', '投資人', '產品介紹'],
    swatches: { bar: '#2F3C7E', accent: '#F96167', ink: '#1A1A1A', bg: '#FFFFFF' },
    typography: 'sans',
    iconStyle: 'filled-pill',
  },
]

export function findTheme(id: ThemeId): ThemeDescriptor {
  return THEMES.find(t => t.id === id) ?? THEMES[0]
}
```

> 注意：`auto` 不送到 backend——當使用者選 auto 時，API call **不帶 `theme_override` 欄位**（或送 `null`），讓 backend 走既有的 Patch O + U 自動邏輯。其他 5 個 id **必須** 跟 `schemas/studio.py:THEMES` 字串完全對應。

## A.2 API client 加 themeOverride

**檔案**：`ANILALM/src/api/studio.ts`

```diff
@@ -47,6 +47,11 @@ export interface CreateSlidesJobInput {
   preset: string
   extraInstructions?: string
   /** Skip RAG retrieval; let the LLM free-write. */
   skipRetrieval?: boolean
+  /**
+   * Force a specific visual theme. When set, bypasses backend's tone
+   * detection and title-keyword override (Patch O + U). Send undefined
+   * to use auto selection. Valid: corporate_navy | academic_paper |
+   * warm_journal | executive_brief | startup_pitch.
+   */
+  themeOverride?: string
 }
@@ -85,6 +90,7 @@ export async function createSlidesJob(
       preset: input.preset,
       extra_instructions: input.extraInstructions,
       skip_retrieval: input.skipRetrieval ?? false,
+      theme_override: input.themeOverride,  // undefined → JSON omits the key
     }),
   })
   return readJsonOrThrow<JobStatus>(res, 'createJob')
 }
```

> JSON.stringify 對 `undefined` 值會自動省略 key，所以 auto case 不需要額外處理。Backend `SlidesJobRequest.theme_override` 的 default `None` 會接住。

## A.3 ThemePicker 元件

**檔案**：`ANILALM/src/workspace/ThemePicker.tsx`（新增）

```typescript
import { THEMES, type ThemeId } from '../studio/themes'

interface Props {
  selected: ThemeId
  onSelect: (id: ThemeId) => void
}

/**
 * Grid of theme cards. Each card shows:
 * - A 4-row color swatch strip (bar/accent/ink/bg)
 * - Name + 1-line description
 * - "Suitable for" tags
 *
 * Mobile-friendly: cards reflow into 2 cols on narrow viewports.
 */
export function ThemePicker({ selected, onSelect }: Props) {
  return (
    <div className="theme-grid">
      {THEMES.map(theme => (
        <button
          key={theme.id}
          type="button"
          className={`theme-card ${selected === theme.id ? 'is-selected' : ''}`}
          onClick={() => onSelect(theme.id)}
          aria-pressed={selected === theme.id}
        >
          <ThemeSwatch theme={theme} />
          <div className="theme-card-body">
            <div className="theme-card-name">{theme.name}</div>
            <div className="theme-card-desc">{theme.description}</div>
            <div className="theme-card-tags">
              {theme.suitableFor.map(tag => (
                <span key={tag} className="theme-card-tag">{tag}</span>
              ))}
            </div>
          </div>
        </button>
      ))}
    </div>
  )
}

function ThemeSwatch({ theme }: { theme: typeof THEMES[number] }) {
  // 80×56 SVG mockup: title bar, accent strip, body lines
  const { swatches } = theme
  return (
    <svg viewBox="0 0 80 56" className="theme-swatch" aria-hidden>
      {/* Background */}
      <rect width="80" height="56" fill={swatches.bg} stroke="#e0e0e0" strokeWidth="0.5" />
      {/* Title bar — variant by theme.iconStyle / chrome */}
      {(theme.id === 'corporate_navy' || theme.id === 'warm_journal' || theme.id === 'startup_pitch') && (
        <rect x="0" y="0" width="80" height="10" fill={swatches.bar} />
      )}
      {theme.id === 'academic_paper' && (
        <line x1="6" y1="11" x2="74" y2="11" stroke="#999" strokeWidth="0.5" />
      )}
      {/* Accent — left strip for corporate/warm, none for academic/exec */}
      {(theme.id === 'corporate_navy' || theme.id === 'warm_journal') && (
        <rect x="0" y="10" width="2" height="46" fill={swatches.accent} />
      )}
      {/* Body lines — sans/serif treatment differs */}
      <rect x={theme.id === 'academic_paper' ? 8 : 6} y="18" width="50" height="2.5" fill={swatches.ink} opacity="0.85" />
      <rect x={theme.id === 'academic_paper' ? 8 : 6} y="24" width="60" height="2" fill={swatches.ink} opacity="0.55" />
      <rect x={theme.id === 'academic_paper' ? 8 : 6} y="29" width="55" height="2" fill={swatches.ink} opacity="0.55" />
      {/* Icon hint — small circle for circle styles, dot for others */}
      {theme.iconStyle === 'outlined-circle' && (
        <circle cx="68" cy="40" r="4" stroke={swatches.accent} strokeWidth="1" fill="none" />
      )}
      {theme.iconStyle === 'soft-filled' && (
        <circle cx="68" cy="40" r="3.5" fill={swatches.accent} />
      )}
      {theme.iconStyle === 'filled-pill' && (
        <circle cx="68" cy="40" r="5" fill={swatches.accent} />
      )}
      {(theme.iconStyle === 'monochrome-dot' || theme.iconStyle === 'minimal-dot') && (
        <circle cx="68" cy="40" r="1.2" fill={swatches.ink} opacity="0.5" />
      )}
    </svg>
  )
}
```

**CSS**（加進既有 stylesheet 或 component-scoped）：

```css
.theme-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px;
}
.theme-card {
  background: var(--card-bg);
  border: 2px solid transparent;
  border-radius: 8px;
  padding: 8px;
  cursor: pointer;
  text-align: left;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.theme-card:hover { border-color: var(--accent); }
.theme-card.is-selected { border-color: var(--accent); background: var(--card-bg-active); }
.theme-swatch { width: 100%; aspect-ratio: 80 / 56; }
.theme-card-name { font-size: 14px; font-weight: 600; }
.theme-card-desc { font-size: 12px; opacity: 0.75; line-height: 1.4; }
.theme-card-tags { display: flex; flex-wrap: wrap; gap: 4px; }
.theme-card-tag { font-size: 10px; padding: 2px 6px; background: var(--tag-bg); border-radius: 4px; }
```

> 顏色變數（`--card-bg` 等）對齊既有 `ANILALM/src/theme/tokens.ts`。如果沒有對應 token、就用硬編碼然後留 TODO 等下個 design pass 處理。

## A.4 整合到 CommandModal

**檔案**：`ANILALM/src/workspace/CommandModal.tsx`

既有 modal 已經是多 step 結構（`step` state 變數）。在 preset 選擇之後、submit 之前加一個新 step：

```diff
@@ at the imports
+ import { ThemePicker } from './ThemePicker'
+ import type { ThemeId } from '../studio/themes'

@@ in the component body (after existing state hooks)
+ const [themeId, setThemeId] = useState<ThemeId>('auto')

@@ in submit() — slides path
-        const job = await createSlidesJob({
-          collectionId: collection.id,
-          preset: presetName,
-          extraInstructions: extra.trim() || undefined,
-        })
+        const job = await createSlidesJob({
+          collectionId: collection.id,
+          preset: presetName,
+          extraInstructions: extra.trim() || undefined,
+          themeOverride: themeId === 'auto' ? undefined : themeId,
+        })

@@ in JSX render — add a new step between preset selection and final confirmation
+ {step === 2 && format?.k === 'slides' && (
+   <div className="modal-step">
+     <h3>選擇視覺風格</h3>
+     <p className="muted">
+       選「自動偵測」由系統依文件 title 與內容判斷；
+       或直接挑一個你想要的風格。
+     </p>
+     <ThemePicker selected={themeId} onSelect={setThemeId} />
+     <div className="modal-step-nav">
+       <button onClick={() => setStep(1)}>上一步</button>
+       <button onClick={() => setStep(3)} className="primary">下一步</button>
+     </div>
+   </div>
+ )}
```

> **注意**：上面的 step 編號是示意，要對照 modal 既有的 step 流程調整。Report path（`format.k === 'report'`）不需要 theme picker，跳過這個 step。

### 不要動的部分

- Report 生成流程完全不變（markdown 輸出沒有 theme 概念）
- WSStudio polling / artifact rendering 不變
- 既有 preset 選擇 step 行為不變
- 既有 `extraInstructions` textarea 不變

## A.5 Artifact 顯示 theme（小加值）

**檔案**：`ANILALM/src/types.ts`

```diff
 export interface SlidesArtifact {
   id: string
   kind: 'slides'
   collectionId: number
   title: string
   preset: string
+  /** Theme ID used; undefined when auto. For sidebar display + retry. */
+  theme?: string
   slides: SlideArtifact[]
   ...
 }
```

在 sidebar / artifact viewer 把 `theme` 顯示成小 badge（「個人心得」「企業技術」等），讓使用者一眼看出每份 deck 用了哪個風格。

## A.6 Phase A 驗收

1. CommandModal 開啟 → format=slides → preset 選好 → **新出現 theme picker step**
2. Theme picker 顯示 6 個卡片（auto + 5 個 theme），每個有色票預覽 + 名稱 + 描述
3. 選「自動偵測」→ API 不送 `theme_override` → backend 走 Patch O+U（既有行為，現有 deck 不變）
4. 選 `warm_journal` → API 送 `theme_override: "warm_journal"` → 不管 title 是什麼、不管 chunks tone，**強制走 warm_journal**
5. 對「中華航空基本面研究報告」這種 title 沒命中 Patch U 的素材，**可以手動選 academic_paper、立即看到米白 serif 視覺**——這個 case 是這次最重要的驗收點
6. Sidebar artifact 卡片顯示「個人心得」「企業技術」等 theme badge
7. 既有所有 job（report path / slides path with auto theme）行為不變

---

# Phase B — Wizard

> Phase A 完成、上線、跑一週看使用者實際選哪些 theme 之後再開工。可以根據實際使用率調整 wizard 問題。

## B.1 為什麼要 wizard

Phase A 解決「**知道想要什麼的人**」（直接選 theme 卡片）。但很多使用者**不知道**哪個 theme 適合——「給客戶看的合規報告」該選哪個？「給投資人的產品介紹」該選哪個？

Wizard 把問題反過來問：**問場合、不問風格**。系統根據答案推出建議 theme，使用者可以接受或手動覆寫。

## B.2 問題設計

四題下拉，每題 4-6 個選項。設計原則：問**具體場景**而不是抽象審美。

```typescript
// ANILALM/src/studio/themeMapping.ts

export type Audience = 'colleagues' | 'manager' | 'client' | 'investor' | 'academic' | 'self'
export type Tone = 'strict' | 'neutral' | 'reflective' | 'persuasive' | 'minimal'
export type Format = 'tech-share' | 'business-analysis' | 'learning-log' | 'pitch' | 'briefing' | 'research'
export type Length = 'compact' | 'standard' | 'extended'

export interface WizardAnswers {
  audience: Audience
  tone: Tone
  format: Format
  length: Length
}

/**
 * Map wizard answers to a recommended theme.
 *
 * Priority logic (first match wins):
 * 1. format='learning-log' OR tone='reflective'     → warm_journal
 * 2. format='research' OR audience='academic'       → academic_paper
 * 3. audience='investor' OR format='pitch'          → startup_pitch
 * 4. audience='client' AND length='compact'         → executive_brief
 * 5. audience='manager' AND tone='minimal'          → executive_brief
 * 6. default                                        → corporate_navy
 */
export function recommendTheme(answers: WizardAnswers): ThemeId {
  if (answers.format === 'learning-log' || answers.tone === 'reflective') {
    return 'warm_journal'
  }
  if (answers.format === 'research' || answers.audience === 'academic') {
    return 'academic_paper'
  }
  if (answers.audience === 'investor' || answers.format === 'pitch') {
    return 'startup_pitch'
  }
  if (answers.audience === 'client' && answers.length === 'compact') {
    return 'executive_brief'
  }
  if (answers.audience === 'manager' && answers.tone === 'minimal') {
    return 'executive_brief'
  }
  return 'corporate_navy'
}
```

## B.3 UI 流程

`ANILALM/src/workspace/StudioWizard.tsx`（新增）：

四個 step、每 step 一題、可前進可後退。最後一頁顯示**推論結果**（「依據你的選擇，我們建議使用『個人心得』風格」）+ 配色 swatch，以及「**手動更改**」按鈕可跳回 ThemePicker。

在 CommandModal 上方加一個 toggle：「**精靈模式 / 進階模式**」。精靈模式跑 StudioWizard、進階模式直接顯示 ThemePicker。預設啟用精靈模式（給新使用者最低門檻），記憶上次選擇到 localStorage。

```typescript
// 在 CommandModal 加：
const [mode, setMode] = useState<'wizard' | 'picker'>(
  () => (localStorage.getItem('studio.theme-mode') as 'wizard' | 'picker') ?? 'wizard'
)

useEffect(() => {
  localStorage.setItem('studio.theme-mode', mode)
}, [mode])
```

## B.4 Wizard 也可選擇影響其他 spec 參數（選做）

進階：除了 theme，wizard 答案還可以 implicit 影響：

| 答案 | 影響 |
|---|---|
| `length: 'compact'` | 加 `extra_instructions: "簡報以 ≤ 10 張為目標"` |
| `audience: 'client'` | 加 `extra_instructions: "避免內部技術術語、加上業務價值說明"` |
| `audience: 'investor'` | 加 `extra_instructions: "突出 ROI / TAM / 成果數據"` |

這些都透過已有的 `extra_instructions` 欄位送、不需要新 API。但要避免**疊太多 instruction** 把 prompt 變混亂——同時最多注入兩條。

## B.5 Phase B 驗收

1. 預設開啟精靈模式、四題下拉問題
2. 答完顯示推薦 theme + 配色預覽
3. 「我要手動選」按鈕跳到 ThemePicker（Phase A 的元件）
4. 「精靈模式 / 進階模式」toggle 切換、選擇記憶 localStorage
5. 推薦邏輯涵蓋常見場合（特別是「客戶」「投資人」「同事 + 學習回顧」這幾種對應）
6. Phase A 的所有驗收條件不退化

---

# 實作順序

## Phase A（建議當第一個 PR）

| 順序 | 步驟 | 預估 |
|---|---|---|
| 1 | `themes.ts` 元資料 | 20 min |
| 2 | `studio.ts` API client 加 `themeOverride` | 10 min |
| 3 | `types.ts` `SlidesArtifact.theme` 欄位 | 5 min |
| 4 | `ThemePicker.tsx` + CSS + SVG swatches | 1 h |
| 5 | `CommandModal.tsx` 整合 step + state | 45 min |
| 6 | Sidebar artifact card 顯示 theme badge | 20 min |
| 7 | 手動測試（5 個 theme 各跑一次、auto 跑一次） | 30 min |

**合計 ~3 小時**。

## Phase B（A 上線、收一週使用資料後）

| 順序 | 步驟 | 預估 |
|---|---|---|
| 1 | `themeMapping.ts` + 單元測試 | 1 h |
| 2 | `StudioWizard.tsx` 四 step UI | 4 h |
| 3 | CommandModal mode toggle + localStorage | 1 h |
| 4 | 推薦結果頁 + 「我要手動選」回退 | 1.5 h |
| 5 | `extra_instructions` 注入（選做）| 2 h |
| 6 | 手動測試 + 至少 10 種答案組合驗證推薦正確 | 1.5 h |

**合計 ~1.5 天**。

---

# 不在本次範圍

- **多變體並行 render**（同 spec、3 個 theme 各一份 pptx、thumbnail 比對）——Phase C，等 A+B 跑過再決定
- **`brand.yaml` 企業客製**——Phase D，更後面
- **後端新 theme**（如 `finance_research` 給金融研究報告）——觀察 academic_paper 是否足以覆蓋
- **Theme 預覽改用真實 render 縮圖**（取代 SVG mockup）——Phase A 用 SVG 已足夠
- **既有 quality-fixes patches 行為**——這條分支跟 `feature/studio-quality-fixes` 各自獨立、不衝突

---

# 與 quality-fixes 分支的關係

Round 6 V4_CONTENT/V4_TITLE 拆分跟這份 wizard 分支**完全獨立**：

- quality-fixes 改 backend audit + render quality
- wizard 改 frontend UX
- 共用的 `theme_override` API 是 Round 3 Patch P 早就加好的，兩條分支都讀同一個欄位

建議：

1. quality-fixes Round 6 驗收通過 → merge main
2. 從 main 開 `feature/studio-wizard`
3. Phase A 完成 → 開 PR → merge → 上 staging 給少數使用者測試
4. 收一週使用資料（哪些 theme 被選最多？auto vs manual 比例？）→ 根據實際使用調整 Phase B 的 wizard 問題設計
5. Phase B 完成 → 開 PR → merge

---

**Spec 產出時間**：2026-05-19
**前提**：Round 3 Patch P（backend `theme_override` 欄位）已在 main、可從 API 接收。如果驗證後發現實際上沒接好，本 spec 第 0 步要先把 backend 那條補回去。
**驗收**：Phase A 完成後對「中華航空基本面研究報告」這種 title 沒命中 Patch U 的素材手動選 academic_paper、得到米白 serif 視覺。Phase B 完成後對新使用者不需任何文件就能跑出合理 theme 的 deck。
