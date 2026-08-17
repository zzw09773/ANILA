# Stage 4 — 插圖全用途路由（後端）

**Date:** 2026-05-21
**Branch:** feature/studio-flux
**Spec context:** `docs/specs/studio-flux/ANILA_Studio_FLUX_Spec.md` §7（Stage 4 — 全用途上線）
**Status:** 設計已核准，待寫實作計畫

---

## 1. 背景與決策

Stage 1-3 已讓**封面 hero** 走 rewriter + deck_style + 三道閘門（striping+VLM，CLIP de-scoped）。內文/章節插圖目前仍走舊 **Path 3**（直接拿 planner 的 `image_prompt` 丟 FLUX、N=1、無 rewriter、無 deck_style、無閘門）。Stage 4 把內文與章節插圖也接上同一條路徑。

**範圍裁定**（依使用者）：
- **只做核心後端路由切換**。不做前端「重新生成這張圖」按鈕（spec §7.4 砍掉）——使用者若要特定圖，改用 ANILA 對話框生圖。
- **section band 渲染**（server.js）多半已具備（`renderSectionBreak` 已支援圖全幅 + 標題漸層遮罩），本階段以驗證為主、預期零/極小改動。

**「deck 是一次性產物」前提**：沒有單圖重生 = 每次 job 都產一份全新 deck，不存在「改一張、保留其他」。因此 `seed = deck_base_seed + idx` 足夠；無需 title-綁-seed、無需 meta 讀回、無 idx 平移問題。設計維持簡潔。

---

## 2. 範圍

**In scope**
- `app/api/studio.py` `_hydrate_images`：
  - 抽共用 helper `_generate_slide_illustration(...)`（封面與內文/章節共用，use_case 參數化）。
  - 新增 `_infer_image_use_case(idx, slide)`。
  - 改寫 routing：以共用 helper 取代「現有封面 hero 專用段」+「舊 Path 3」。
  - per-deck 生圖上限保護 `MAX_GENERATED_IMAGES_PER_DECK`。
- 單元測試（`tests/` 新增）。
- server.js `renderSectionBreak` 對 BAND 圖的**驗證**（預期無需改 code；若確需微調另記）。

**Out of scope（明確不做）**
- 前端重生按鈕（§7.4）。
- **不得動** `schemas/studio.py` 的 `_check_image_kind_consistency` validator（見 §4 決策 A）。
- 不改 `derive_flux_prompt` 介面、不改 FLUX `/generate` 合約、不改 `flux_quality_gate`。
- LoRA/Redux、CLIP。

---

## 3. 共用 helper 與 use_case 推斷

### 3.1 `_infer_image_use_case(idx, slide) -> ImageUseCase`
判定順序（**順序重要**）：
1. `idx == 0` 或 `slide.get("layout_kind") == "cover"` → `COVER_HERO`
   （封面即使被標 `layout_kind=="section_break"`，因 idx==0 先判 HERO。）
2. `slide.get("layout_kind") == "section_break"` → `SECTION_BAND`
3. 其餘 → `CONTENT_ILLUSTRATION`

### 3.2 `_generate_slide_illustration(...)`
簽名：
```python
async def _generate_slide_illustration(
    slide: dict,
    *,
    idx: int,
    use_case: ImageUseCase,
    deck_style: "StyleDescriptor | None",
    flux_provider: "FluxImageProvider",
    deck_base_seed: int,
    llm: "_StudioLLMAdapter",
) -> bool:
    """生成單張插圖並就地寫回 slide。回傳是否成功放上圖（True=有圖）。"""
```
流程（由現有封面段抽取、use_case 參數化）：
1. `style = deck_style or get_style_descriptor()`。
2. `flux_prompt = await derive_flux_prompt(title=slide["title"], bullets=slide.get("bullets", []), use_case=use_case, style=style, llm=llm)`（try/except → None）。
   - **一律用 title+bullets 重寫**；planner 的 `image_prompt` 不作為 rewriter 輸入（決策 A）。
3. `flux_prompt is None`（rewriter 回 USE_GRAPHVIZ 或被清空）→ 不生圖，回 `False`（呼叫端走 fallback）。
4. `seed = deck_base_seed + idx`；`vlm = _Gemma4VlmGate(llm._db, llm._user)`。
5. `best, retry_count = await _gated_generate(flux_provider, flux_prompt, use_case=use_case, seed=seed, style_id=style.style_id, concept_en=flux_prompt, vlm=vlm)`（try/except → best=None）。
   - **`concept_en` 維持 Stage 1 既有做法（傳整串 `flux_prompt`）**。已知取捨：flux_prompt 含風格詞，VLM 的 match 語意會被稍微稀釋；但封面（Stage 1）即此做法且實測語意對齊良好，故沿用、保持一致。**本階段不調整 VLM concept 粒度**；若驗收發現 CONTENT 語意對齊明顯變差，另開項目處理（例如讓 rewriter 額外回一個英文概念短語）——**不要在本階段自作主張改 concept_en**。
6. `best is not None` → 寫 `slide["image_data"]`（base64 PNG）+ `slide["image_gen_meta"]`（use_case/flux_prompt/seed/style_id/clip_score/vlm_verdict/retry_count）+ `pop` 掉 `image_prompt`/`image_kind`/`diagram_dot`，回 `True`。
7. 否則寫 fallback 的 `image_gen_meta`（含 `fallback` 標記）、回 `False`。

封面現有的「成功/fallback」行為即此流程的 HERO 特例。

---

## 4. routing 改寫（`_hydrate_images` 迴圈）

Path 1（`image_ref` 既有圖）、Path 2（`diagram_dot`→graphviz）**優先序與行為不變**（curated > diagram > generative）。

**互斥鏈（釘死）**：Path 1/2 成功放圖後即 `continue` 跳出當圈，**不會** fall through 到下面的 `wants_illustration` 判定。只有 Path 1/2 皆未命中（或命中後失敗 fallthrough）的 slide 才評估 `wants_illustration`。這是既有 `_hydrate_images` 的 if-continue 鏈結構——沿用，不要改成平行判斷。

之後，計算該 slide 是否要插圖（**決策 A 的觸發訊號**）：
```python
is_cover = idx == 0 or slide.get("layout_kind") == "cover"
is_band = slide.get("layout_kind") == "section_break"
is_content = slide.get("image_kind") == "illustration" or bool(slide.get("image_prompt"))
wants_illustration = is_cover or is_band or is_content
```
- **決策 A（釘死）**：planner 為過 `_check_image_kind_consistency` validator 仍會附 `image_prompt`；helper **無視它、用 title+bullets 重寫**。CONTENT 訊號用「`image_kind=="illustration"` 或有 `image_prompt`」。**不鬆綁 validator、不改 schema**。被丟棄的 image_prompt token 成本可忽略（deck 一次性）。

FLUX 工具鏈就緒（`flux_provider is not None and deck_base_seed is not None and llm is not None`）且 `wants_illustration`：
```python
# 順序釘死：先算 use_case（後續多處用到），再 CONTENT 密度門檻 → 上限 → 生成。
use_case = _infer_image_use_case(idx, slide)
# CONTENT 密度門檻：renderer 只在 image_focus 版面顯示內文插圖，而 image_focus
# 是「圖主文輔」半版版面，bullets 太多會擠壞。bullets > 上限的 CONTENT 直接走
# fallback（純文字 standard）、不生圖、不佔額度。HERO/BAND 不受此限（全幅）。
if (use_case is ImageUseCase.CONTENT_ILLUSTRATION
        and len(slide.get("bullets") or []) > CONTENT_ILLUSTRATION_MAX_BULLETS):
    _apply_illustration_fallback(slide, use_case)
    continue
if generated_count >= MAX_GENERATED_IMAGES_PER_DECK:
    # 上限保護：長尾走 fallback（不呼叫 helper、不耗 GPU）
    logger.warning("per-deck image cap %d reached; slide %d (%s) → fallback",
                   MAX_GENERATED_IMAGES_PER_DECK, idx, use_case.value)
    _apply_illustration_fallback(slide, use_case)
    continue
generated_count += 1   # 計「已進入生成」的張數（不論成功與否）
ok = await _generate_slide_illustration(slide, idx=idx, use_case=use_case,
        deck_style=deck_style, flux_provider=flux_provider,
        deck_base_seed=deck_base_seed, llm=llm)
if ok:
    # CONTENT 成功 → 轉 image_focus 版面，讓 renderer 既有 renderImageFocus 顯示
    # 該圖（standard 等版面不會渲染 image_data）。HERO/BAND 維持原版面（走
    # renderSectionBreak 全幅），不可改。
    if use_case is ImageUseCase.CONTENT_ILLUSTRATION:
        slide["layout_kind"] = "image_focus"
else:
    _apply_illustration_fallback(slide, use_case)
continue
```
`generated_count` 在迴圈外初始化為 0。**`use_case` 必須在上限檢查之前算好**（否則 fallback 取用未定義的 use_case → NameError/舊值）。HERO（idx 0）最先處理，必生得到；上限保護長尾 BAND/CONTENT。

**renderer 顯示前提（為何要轉 image_focus）**：server.js 只有 `renderSectionBreak`（section_break/cover，全幅）與 `renderImageFocus`（image_focus，定位圖）會渲染 slide 的 `image_data`；`standard`/`stat_callout`/`quote`/`two_column`/`icon_rows` 一律忽略 `image_data`。故 CONTENT 插圖成功後必須把版面轉成 `image_focus`（`image_focus` 已在 `LAYOUT_KINDS` 內、合法），否則生出的圖不會顯示。HERO 走封面/section_break、BAND 走 section_break，皆由 `renderSectionBreak` 全幅顯示，不需轉版面。

`deck_style` 透過 `_hydrate_images` 既有 `deck_style` 參數（Stage 3 已加）取得。

---

## 5. per-deck 生圖上限

```python
MAX_GENERATED_IMAGES_PER_DECK = 15
CONTENT_ILLUSTRATION_MAX_BULLETS = 3   # CONTENT 超過此 bullets 數不轉 image_focus、走純文字
```
- 計數語意：每**進入** `_generate_slide_illustration`（即實際耗 GPU 嘗試）就 +1，不論最終接受或 fallback。CONTENT 因密度門檻被擋下的（未生成）不計數。
- 達上限後，後續本該生圖的 slide 直接走 `_apply_illustration_fallback`（不呼叫 helper）。
- 目的：防 production 失控（spec §7.5 的 ≤10 分/deck 是目標非保證；大量章節扉頁×N=2×重試最壞會遠超）。HERO + 前段一定生得到，長尾退主題底不傷大雅。

---

## 6. fallback 對映（`_apply_illustration_fallback(slide, use_case)`，spec 5.3）

| use_case | fallback 行為 |
|---|---|
| `COVER_HERO` | 不放圖（`image_data` 不設）→ renderer 用主題色封面；pop `image_prompt`/`image_kind`；寫 `image_gen_meta.fallback="solid_theme_cover"` |
| `SECTION_BAND` | 不放圖 → 章節扉頁用主題底（無 band）；pop `image_prompt`/`image_kind`；`fallback="theme_section_break"` |
| `CONTENT_ILLUSTRATION` | 丟圖欄位（pop `image_prompt`/`image_kind`/`diagram_dot`）→ renderer 的 `image_focus`→`standard` 自動退純文字版面；`fallback="text_only"` |

（HERO 的 fallback 即把現有封面 fallback 邏輯收進此函式。）

---

## 7. renderer（server.js）

**修正（2026-05-21 實測）**：renderer 只在兩種版面渲染 slide 的 `image_data`：`renderSectionBreak`（section_break/cover，全幅）與 `renderImageFocus`（image_focus，定位圖）；其餘版面忽略 `image_data`。原先「renderer 零改動」假設**錯誤** —— CONTENT 插圖落在 `standard` 等版面會被丟棄。

對策（不動 Node）：CONTENT 插圖成功後由後端把 `layout_kind` 轉 `image_focus`（見 §4），借既有 `renderImageFocus` 顯示。HERO/BAND 仍走 `renderSectionBreak` 全幅，server.js **本階段不改**。實作時驗證含 BAND（section_break）與 CONTENT（轉 image_focus）的 deck 都正常顯示。

---

## 8. 錯誤處理

- rewriter 例外 / 回 None / `_gated_generate` 例外 / 全候選 fail → 該 slide 走對應 use_case 的 fallback；**絕不**因單張插圖失敗中斷整個 deck。
- 上限保護觸發 → fallback + warning。
- Path 1/2（curated 圖、diagram）行為完全不變。

---

## 9. 測試計畫（TDD：先紅後綠）

`_infer_image_use_case`（純函式，易測）：
1. `idx=0` → HERO（即使 `layout_kind="section_break"`）。
2. `layout_kind="cover"` → HERO。
3. `idx>0, layout_kind="section_break"` → BAND。
4. 一般內文 slide → CONTENT。

`_generate_slide_illustration`（mock `flux_provider`/`_gated_generate`/rewriter via 注入或 monkeypatch）：
5. 成功 → `slide["image_data"]` 設定、`image_gen_meta.use_case` 正確、回 True。
6. rewriter 回 None → 不設 image_data、回 False。
7. `_gated_generate` 回 (None, n) → 回 False（呼叫端會 fallback）。

`_apply_illustration_fallback`：
8. 三種 use_case 各自設正確 `fallback` 標記、pop 掉 image 欄位、不設 `image_data`。

routing / 上限（以小型 spec_dict + mock 跑 `_hydrate_images` 或抽出的 routing 子函式）：
9. **超過 `MAX_GENERATED_IMAGES_PER_DECK` → 後續 slide 走 fallback、不再呼叫 helper**（用 spy 斷言 helper 呼叫次數 == 上限）。
10. section_break slide 自動被視為 wants_illustration（BAND）。
11. 有 `image_prompt` 的內文 slide → wants_illustration（CONTENT），且 helper 收到的是 rewriter 結果、非原 image_prompt。

CONTENT 密度門檻 / image_focus 轉換：
12. CONTENT slide、bullets ≤ 3、helper 成功 → 該 slide `layout_kind` 變 `"image_focus"`。
13. CONTENT slide、bullets > 3（`> CONTENT_ILLUSTRATION_MAX_BULLETS`）→ helper **未被呼叫**（spy 斷言）、走 fallback（`text_only`）、`layout_kind` 不變。
14. HERO / BAND 成功 → `layout_kind` **不被**改成 `image_focus`（維持原版面）。

server.js band/image_focus 顯示由 §10 實機驗收涵蓋，不寫 Node 單元測試。

---

## 10. 驗收（spec §7.5，按本階段範圍）

1. 跑一份含封面 + 多章節扉頁 + 內文插圖的 deck → 三種 use_case 圖都語意對齊、無文字、套同一 deck_style。
2. graphviz 結構圖（image_kind="diagram"）不受影響、照常渲染。
3. 章節扉頁 BAND 圖在 renderer 正常顯示（全幅 + 標題可讀）。
4. 人為造一份超過 15 張圖需求的 deck → 第 16 張起走 fallback、log 有 cap warning、job 正常完成。
5. 單 deck 端到端時間記錄（≤10 分為目標；超過不阻擋，但記錄供觀察）。

（實機觸發一律 user 從 frontend 點，本機 read-only 撈 log。）

---

## 11. 風險

- 每章節扉頁自動生 BAND → 生圖數/總延遲明顯上升；以 `MAX_GENERATED_IMAGES_PER_DECK=15` 設硬上限止血（使用者已接受此取捨）。
- rewriter 用 title+bullets 重寫內文插圖 → 可能不如 planner 原 image_prompt 精準；換得風格一致+無文字+閘門把關，且與封面一致（決策 A）。
- FLUX 服務偶發 500（如先前觀察到的 index-out-of-bounds）→ 既有 fallback 路徑正確止損，不塞壞圖。
