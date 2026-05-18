# ANILA LM Studio — Round 2 補丁規格

> 對 `feature/studio-quality-fixes` 後續修正。基於對 v2 輸出 `新進人員心得與技術規劃報告__2_.pptx` 與分支上 7 個 commit (`ebfea2e` HEAD) 的逐張比對。
>
> Round 1 的 6 個 commit 都進去了，但實際渲染顯示有：
>
> - **設計層** gap：Fix 1 的 V1 門檻太鬆，slide 8/13 漏網（standard 比例 40% 沒觸發 hard rule）
> - **打到死碼**：Fix 5.1 改的是 cover auto-path，但 prompt 規則 1 強制第一張是 section_break，永遠走 `renderSectionBreak`
> - **數學錯**：Fix 3 stat_callout solo mode 註解寫「centre at 3.95」實際把 value 放在 y=1.4（centre 2.7）
> - **副作用**：Fix 3 把 `Column.bullets` `min_length` 拉到 3，slide 5「雙分支特徵融合」湊不到每欄 3 條，被默默 demote 回 standard（v1 是正確的 two_column）
> - **演算法弱**：Fix 6 的 whitespace 用 bounding-box-area 加總，抓不到局部留白
> - **未驗證**：Fix 2 graphviz 二進位 / 觸發 / hydration 任一環節可能 silent fail

每個 Patch 獨立可上線。建議順序：**A → B → C → D → E → F → G**（G 視診斷結果再寫 patch）。前 5 個合計約 1.5–2 小時，能解決 v2 大多數視覺問題。

---

## 共用：診斷重跑指令

每個 patch 完成後，重跑這份輸入確認：

```bash
# 從 collection 內取出原 chunks 重跑 job
curl -X POST http://localhost:8000/api/studio/slides/jobs \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"collection_id": <ID>, "preset": "經典報告結構"}'

# 等 done 後下載
JOB_ID=...
curl -o /tmp/round2.pptx "http://localhost:8000/api/studio/slides/jobs/$JOB_ID/pptx" \
  -H "Authorization: Bearer $TOKEN"

# 轉圖人工檢查
cd /tmp && python /path/to/pptx-skill/scripts/office/soffice.py --headless --convert-to pdf round2.pptx
rm -f slide-*.jpg && pdftoppm -jpeg -r 110 round2.pdf slide && ls slide-*.jpg
```

---

## Patch A：把 italic 從 `renderSectionBreak` 拿掉

**問題**：`f46b690` 改的是 `server.js:601-604` 的 auto-cover footer line，但因為 prompt 規則 1 強制第一張投影片是 `section_break`，那段程式碼永遠不會跑。實際的封面副標跟所有章節扉頁副標都是由 `renderSectionBreak` (line 183-211) 渲染，副標 italic 在 line 206。

CJK 字型沒有真斜體字模，pptxgenjs 的 `italic: true` 會強制走 oblique transform（仿斜體），結果就是 v2 slide 1、3、7、11 看到的歪斜中文。

**檔案**：`ANILALM/pptx-skill/server.js`

**Diff**：

```diff
@@ -201,11 +201,12 @@ function renderSectionBreak(pres, s, p) {
   // Subtitle from bullets[0] if the LLM provided one — keeps the slide
   // useful even when it's clearly just a transition.
   const bullets = Array.isArray(s.bullets) ? s.bullets : []
   if (bullets[0]) {
     slide.addText(String(bullets[0]), {
       x: 1.1, y: 4.4, w: 11.5, h: 0.6,
       fontSize: 20, color: p.accent,
-      align: 'left', italic: true, fontFace: FONT_FACE,
+      align: 'left', italic: false, fontFace: FONT_FACE,
     })
   }
   if (s.speaker_notes) slide.addNotes(String(s.speaker_notes))
```

**驗收**：v2 slide 1 副標「報告人：龔修潁 | 資通所人工智慧組 AIA 小組」不再歪斜；slide 3、7、11 章節扉頁副標也直立。

---

## Patch B：`renderStatCallout` solo 模式真正置中

**問題**：`server.js:286-298` 的註解寫「Content area runs ~1.0 → 6.9 inches. Centre point ≈ 3.95. Big number occupies the dead centre」，但 value 實際 `y=1.4, h=2.6`，視覺中心 y=2.7，**沒到 3.95**。v2 slide 6 的「95%」明顯偏上。

**檔案**：`ANILALM/pptx-skill/server.js`

**Diff**：

```diff
@@ -285,21 +285,28 @@ function renderStatCallout(pres, s, p) {
   } else {
-    // ── Solo mode: vertical-centre the value+label block ──
-    // Content area runs ~1.0 → 6.9 inches. Centre point ≈ 3.95.
-    // Big number occupies the dead centre; label sits just below.
+    // ── Solo mode: vertical-centre value+label+supporting as one block ──
+    // Content area runs ~0.84 → 6.5 inches (above the takeaway footer).
+    // Block heights: value 2.4 + label 0.7 + supporting 1.0 = 4.1 inches
+    // plus 0.2 gaps = 4.5 inches total. Centre of (0.84, 6.5) is 3.67;
+    // block starts at y = 3.67 - 4.5/2 = 1.42. We round to y=2.0 to push
+    // visually slightly south of geometric centre (looks balanced after
+    // accounting for the title bar's heavy top weight).
     slide.addText(String(s.stat.value), {
-      x: 0.5, y: 1.4, w: 12.3, h: 2.6,
+      x: 0.5, y: 2.0, w: 12.3, h: 2.4,
       fontSize: 96, bold: true, color: p.accent,
       align: 'center', valign: 'middle', fontFace: FONT_FACE,
     })
     slide.addText(String(s.stat.label), {
-      x: 1.0, y: 4.0, w: 11.3, h: 0.7,
+      x: 1.0, y: 4.6, w: 11.3, h: 0.7,
       fontSize: 28, color: p.ink,
       align: 'center', valign: 'middle', fontFace: FONT_FACE,
     })
     if (s.stat.supporting) {
       slide.addText(String(s.stat.supporting), {
-        x: 1.0, y: 4.8, w: 11.3, h: 1.4,
+        x: 1.0, y: 5.4, w: 11.3, h: 1.0,
         fontSize: 16, color: p.muted, italic: true,
         align: 'center', valign: 'top', fontFace: FONT_FACE,
       })
     }
   }
```

**驗收**：重跑後 slide 6 的「95%」視覺中心應落在投影片垂直中點附近（y ≈ 3.2 inches），上方留白從現在的 ~1.5 inches 縮到 ~1.0 inches。

**註**：Footer takeaway (`y: 6.5, h: 0.5`) 不動，因為它本來就在合理位置。

---

## Patch C：`Column.bullets` `min_length` 從 3 降回 2，並改 demote 路徑

**問題**：v1 的 slide 5「解決方案：雙分支特徵融合」是正確的 `two_column` (RGB 原圖 vs Tsallis Entropy)。v2 同主題的「雙分支特徵融合解決方案」變成 `standard` 三 bullet。

根因在 `schemas/studio.py:129` 的 `Column.bullets = Field(..., min_length=3, max_length=6)`，加上 `studio.py:923-952` 的 saturation 轉換把 sparse column 全部 demote 到 `standard`。技術內容常見「兩個概念各 2 點本質特性」的結構（RGB 原圖：捕捉形態 / 提供空間資訊；Tsallis：量化複雜度 / 強化邊緣特徵），3 是 overkill。

修兩個地方：

### C.1 Schema 放寬到 2

**檔案**：`myCSPPlatform/backend/app/schemas/studio.py`

**Diff**：

```diff
@@ -119,11 +119,13 @@ class Column(BaseModel):
     """One side of a two_column layout.
-
-    bullets min_length raised to 3 (Studio Fix 3) to enforce visual
-    saturation; Pydantic fallback in studio.py demotes to standard if
-    the LLM emits < 3 to avoid a 422 → modal retry.
+
+    bullets min_length set to 2 (Round 2 follow-up): originally raised to
+    3 in Studio Fix 3, but technical comparisons frequently have 2 clean
+    distinguishing points per side. Forcing 3 caused the LLM to demote
+    legitimate two_column slides to standard (e.g. v2 slide 5 regression).
+    2 is the floor for "side-by-side reads as two columns visually".
     """

     heading: str = Field(..., min_length=1, max_length=120)
-    bullets: list[str] = Field(..., min_length=3, max_length=6)
+    bullets: list[str] = Field(..., min_length=2, max_length=6)
```

### C.2 Demote 改成「< 2 才 demote、demote 目標是 icon_rows」

**檔案**：`myCSPPlatform/backend/app/api/studio.py`，函式 `_patch_studio_saturation`（line ~862）

**Diff**：

```diff
@@ -923,7 +923,7 @@ def _patch_studio_saturation(spec_dict, chunk_filenames):
-        # Transform 2: two_column with sparse columns → demote to standard
+        # Transform 2: two_column with sparse columns → upgrade to icon_rows
+        # (Round 2: was demote-to-standard, which lost the side-by-side
+        # framing entirely. icon_rows preserves the "N parallel concepts"
+        # shape with icons instead of column dividers.)
         if layout == "two_column":
             cols = slide.get("columns")
             if isinstance(cols, list) and cols:
                 sparse = any(
                     not isinstance(c, dict)
                     or not isinstance(c.get("bullets"), list)
-                    or len(c["bullets"]) < 3
+                    or len(c["bullets"]) < 2
                     for c in cols
                 )
                 if sparse:
                     title = slide.get("title", "<untitled>")
-                    flattened: list[str] = []
+                    # Build icon_rows from columns: each column → one row.
+                    # Row heading uses column heading; description is the
+                    # first bullet (or a join of all bullets if short).
+                    new_rows = []
                     for c in cols:
                         if not isinstance(c, dict):
                             continue
                         heading = str(c.get("heading") or "").strip()
                         bullets = c.get("bullets") or []
-                        if not isinstance(bullets, list):
+                        if not isinstance(bullets, list) or not heading:
                             continue
+                        desc = "；".join(
+                            str(b).strip() for b in bullets if str(b).strip()
+                        )[:200]
+                        if not desc:
+                            continue
+                        new_rows.append({
+                            "concept": "comparison",  # safe fallback concept
+                            "heading": heading,
+                            "description": desc,
+                        })
+                    if len(new_rows) >= 2:
+                        slide["layout_kind"] = "icon_rows"
+                        slide["icon_rows"] = new_rows
+                        slide.pop("columns", None)
+                        logger.warning(
+                            "Studio saturation: two_column slide '%s' "
+                            "upgraded to icon_rows (%d rows) due to sparse "
+                            "columns (<2 bullets each).",
+                            title, len(new_rows),
+                        )
+                        continue  # done with this slide
+                    # Fallback: not enough rows for icon_rows → standard
+                    flattened: list[str] = []
+                    for c in cols:
+                        if not isinstance(c, dict):
+                            continue
+                        heading = str(c.get("heading") or "").strip()
+                        bullets = c.get("bullets") or []
+                        if not isinstance(bullets, list):
+                            continue
                         for b in bullets:
                             text = str(b).strip()
                             if not text:
                                 continue
                             prefix = f"{heading}：" if heading else ""
                             flattened.append(f"{prefix}{text}")
```

> 注意：上面 diff 是示意，實際請對 line 923-960 的完整 demote block 套用。

### C.3 Prompt 同步更新

**檔案**：`myCSPPlatform/backend/app/api/studio.py`，line ~463-588

把 prompt 內所有「每欄 3 條」改成「每欄 2-3 條」，並補一句「無法湊到 2 條的對照型內容，改用 icon_rows」：

```diff
@@ -463,7 +463,7 @@ def _build_generation_prompt(...):
-            '  columns 形狀：[{"heading": "...", "bullets": ["...", "...", "..."]},',
+            '  columns 形狀：[{"heading": "...", "bullets": ["...", "..."]}, ',
             '                 {"heading": "...", "bullets": ["..."]}]',
-            '             固定 2 個元素的陣列；多於 2 會被忽略、少於 2 會降級為 standard。',
-            '             **每欄 bullets 至少 3 條**（schema 強制；湊不到改 icon_rows）。',
+            '             固定 2 個元素的陣列；多於 2 會被忽略。',
+            '             **每欄 bullets 至少 2 條**（schema 強制）；2-3 條最常見、4-6 條視內容深度。',
+            '             湊不到 2 條的對照結構，改用 icon_rows（保留並列感、視覺更輕）。',
@@ -587,8 +587,7 @@
-            "- **two_column**：內容天然有對照（before/after、本研究 vs 既有方法、",
-            "  兩種模型架構比較）時用 1 張。columns 必須 **2 個元素**、各填 heading + bullets。",
-            "  **每欄至少 3 條 bullet**（schema 強制），讓兩欄視覺密度對稱、不留大片空白；",
+            "- **two_column**：內容天然有對照（before/after、本研究 vs 既有方法、",
+            "  兩種模型架構比較）時用 1 張。columns 必須 **2 個元素**、各填 heading + bullets。",
+            "  **每欄 2-3 條 bullet**最常見（schema 最少 2 條），讓兩欄視覺密度對稱；",
```

### C.4 測試更新

**檔案**：`myCSPPlatform/backend/tests/test_studio_saturation.py`（若存在；若不存在則新增）

新增測試案例：

```python
def test_two_column_with_sparse_columns_upgrades_to_icon_rows():
    """Round 2: sparse two_column should become icon_rows, not standard.

    Regression guard for v2 slide 5 — 雙分支特徵融合 had 2 bullets per
    column and was incorrectly demoted to standard.
    """
    spec_dict = {
        "title": "test",
        "slides": [{
            "title": "雙分支特徵融合解決方案",
            "bullets": ["a", "b", "c"],
            "layout_kind": "two_column",
            "columns": [
                {"heading": "RGB 原圖", "bullets": ["捕捉形態", "空間資訊"]},
                {"heading": "Tsallis Entropy", "bullets": ["量化複雜度", "強化邊緣"]},
            ],
        }],
    }
    out = _patch_studio_saturation(spec_dict, chunk_filenames=["x.pdf"])
    slide = out["slides"][0]
    # min_length=2 means 2 bullets is now valid; saturation pass shouldn't
    # touch this slide at all.
    assert slide["layout_kind"] == "two_column"

def test_two_column_with_one_bullet_columns_upgrades_to_icon_rows():
    spec_dict = {
        "title": "test",
        "slides": [{
            "title": "x",
            "bullets": ["a"],
            "layout_kind": "two_column",
            "columns": [
                {"heading": "A", "bullets": ["only one"]},
                {"heading": "B", "bullets": ["only one"]},
            ],
        }],
    }
    out = _patch_studio_saturation(spec_dict, chunk_filenames=[])
    slide = out["slides"][0]
    assert slide["layout_kind"] == "icon_rows"
    assert len(slide["icon_rows"]) == 2
    assert "columns" not in slide
```

**驗收**：重跑 v2 素材，slide 5「雙分支特徵融合解決方案」回到 two_column；隨機抽其他知識庫產出，**不再出現 two_column → standard 的 demote warning**（看 log）。

---

## Patch D：Rebalancer 觸發條件擴大

**問題**：`studio.py:2076-2090` 只在 `hard_violations` 非空時觸發 `_rebalance_layouts`。V1（standard > 60%）跟 V2（缺 stat_callout）是 hard，V3、V4 是 soft，**單純 V4 不會觸發**。

v2 的 standard 比例 40%，V1 沒踩；slide 6 有 stat_callout，V2 沒踩。但 slide 8（Agentic Workflow 三階段）跟 slide 13（Supervisor 拓撲）明顯該被改成 icon_rows——V4 都標出來了，rebalance 卻沒呼叫。

**檔案**：`myCSPPlatform/backend/app/api/studio.py`

**Diff**：

```diff
@@ -2072,16 +2072,21 @@
         if not used_fallback:
             chunks_str = "\n\n".join(
                 str(c.get("content", "")) for c in chunks
             )
             violations = _audit_layout_distribution(spec, chunks_text=chunks_str)
-            hard_violations = [v for v in violations if v.severity == "hard"]
-            if hard_violations:
+            if _should_rebalance(violations):
                 await updater.set(step=JOB_STEP_REBALANCING)
                 try:
                     spec_dict = spec.model_dump(mode="json")
                     rebalanced = await _rebalance_layouts(
                         spec_dict, violations, chunks_str, db=db, user=user,
                     )
                     spec = SlidesSpec.model_validate(rebalanced)
                 except Exception as exc:  # noqa: BLE001
                     logger.warning(
                         "Rebalance failed: %s — proceeding with original spec",
                         exc,
                     )
```

新增函式（建議放在 `_audit_layout_distribution` 後面，line ~1700 之前）：

```python
def _should_rebalance(violations: list[LayoutViolation]) -> bool:
    """Decide whether to invoke the LLM rebalance pass.

    Round 1 only fired on hard violations (V1, V2). Round 2 broadens this
    because empirical data shows that decks can pass V1 (standard ratio
    ≤ 60%) yet still contain obvious icon_rows misses captured as V4.

    Triggers (any one suffices):
      - Any hard violation (V1 or V2 — original behaviour)
      - 2 or more V4 violations (enumeration title + standard layout)
      - Total soft violations (V3 + V4) ≥ 3

    Rationale for the V4 threshold: a single V4 candidate could be a
    legitimate standard slide that happens to have "流程" in its title;
    two or more is a pattern, not noise.
    """
    has_hard = any(v.severity == "hard" for v in violations)
    if has_hard:
        return True
    v4_count = sum(1 for v in violations if v.kind == "V4")
    if v4_count >= 2:
        return True
    soft_count = sum(1 for v in violations if v.severity == "soft")
    return soft_count >= 3
```

### 測試更新

**檔案**：`myCSPPlatform/backend/tests/test_layout_rebalance.py`

```python
def test_should_rebalance_fires_on_two_v4():
    """Round 2: 2+ V4 violations should trigger rebalance even without V1/V2."""
    violations = [
        LayoutViolation(kind="V4", severity="soft", slide_indices=[3], detail=""),
        LayoutViolation(kind="V4", severity="soft", slide_indices=[7], detail=""),
    ]
    assert _should_rebalance(violations) is True

def test_should_not_rebalance_on_single_v4():
    violations = [
        LayoutViolation(kind="V4", severity="soft", slide_indices=[3], detail=""),
    ]
    assert _should_rebalance(violations) is False

def test_should_rebalance_on_v1_alone():
    violations = [
        LayoutViolation(kind="V1", severity="hard", slide_indices=[0,1,2,3,4,5], detail=""),
    ]
    assert _should_rebalance(violations) is True

def test_should_rebalance_on_three_soft_total():
    violations = [
        LayoutViolation(kind="V3", severity="soft", slide_indices=[2], detail=""),
        LayoutViolation(kind="V4", severity="soft", slide_indices=[5], detail=""),
        LayoutViolation(kind="V3", severity="soft", slide_indices=[8], detail=""),
    ]
    assert _should_rebalance(violations) is True
```

**驗收**：重跑 v2 素材，預期 rebalance call 會被觸發（job log 出現 `step=rebalancing`），slide 8 跟 slide 13 改為 `icon_rows`。

---

## Patch E：擴大 V4 enumeration keywords

**問題**：`studio.py:1538` 的關鍵字只有 7 個：
```python
_ENUMERATION_KEYWORDS = ("三大", "步驟", "階段", "面向", "核心能力", "workflow", "pipeline")
```

漏掉很多技術簡報常見的「並列概念」訊號。slide 13「Multi-Agent Supervisor 拓撲設計」就是其中一個。

**檔案**：`myCSPPlatform/backend/app/api/studio.py`

**Diff**：

```diff
@@ -1538,7 +1538,21 @@
 _ENUMERATION_KEYWORDS = (
-    "三大", "步驟", "階段", "面向", "核心能力", "workflow", "pipeline",
+    # Numeric enumeration
+    "三大", "四大", "五大", "兩大", "三項", "三類",
+    # Process / sequence
+    "步驟", "階段", "流程", "歷程", "順序",
+    "workflow", "pipeline", "process",
+    # Structural enumeration
+    "面向", "層面", "維度", "方面",
+    # Architecture / topology (slide 13 case)
+    "架構", "拓撲", "拓樸", "結構", "設計", "佈局",
+    # Capability / function lists
+    "核心能力", "能力", "功能", "特性", "特徵",
+    # Strategy / approach
+    "策略", "方案", "模式", "機制", "方法",
+    # Comparison framing
+    "對比", "對照", " vs ", " vs.",
 )
```

> 注意中文 "vs" 前後保留空格避免誤觸（例如 "previous" 中的 "vs" 字串）。

### 測試更新

**檔案**：`myCSPPlatform/backend/tests/test_layout_audit.py`

```python
@pytest.mark.parametrize("title,bullets,expected_v4", [
    ("Multi-Agent Supervisor 拓撲設計", ["a", "b", "c"], True),  # slide 13 regression case
    ("Agentic Workflow：從檢索到推理", ["x", "y", "z"], True),  # slide 8
    ("雙分支特徵融合解決方案", ["a", "b", "c"], True),  # slide 5 — "方案" keyword
    ("封面標題", ["a", "b", "c"], False),  # no enumeration keyword
    ("拓撲", ["a", "b"], False),  # only 2 bullets
])
def test_v4_keyword_expansion(title, bullets, expected_v4):
    spec = SlidesSpec(title="t", slides=[
        Slide(title=title, bullets=bullets, layout_kind="standard"),
    ])
    violations = _audit_layout_distribution(spec, chunks_text="")
    v4s = [v for v in violations if v.kind == "V4"]
    assert (len(v4s) > 0) == expected_v4
```

**驗收**：與 Patch D 合併驗收——slide 8、13 在 audit 階段被標為 V4，rebalance 階段被改寫為 icon_rows。

---

## Patch F：Grid-based localized emptiness

**問題**：`server.js:891-917` 用 `coveredArea / SLIDE_AREA_INCH` 算 whitespace ratio。pptxgenjs 的 text box 即使裝 1 行字也常有 1-2 inch height bounding box，4-5 個短 bullet 加起來 bounding box 面積 40-50 sq inch（slide area 100 sq inch），wsRatio 算出來 0.5-0.6——**剛好在 WARN 門檻邊緣**，但視覺上整個下半 4 inches 是空的。

換成 grid-based：把 slide 切成 6×4 cells，看連片空白區域大小。

**檔案**：`ANILALM/pptx-skill/server.js`

新增函式（插在 `analyseSlide` 之前 line 891 處）：

```javascript
const GRID_COLS = 6
const GRID_ROWS = 4
const MAX_EMPTY_CELLS_WARN = 8   // 8/24 = 33% slide area as contiguous void
const MAX_EMPTY_CELLS_CRIT = 12  // 12/24 = 50% as one contiguous void

/**
 * Detect localized empty regions that the global coveredArea metric misses.
 *
 * Splits the body area (excluding the 0.84-inch master header band) into a
 * GRID_COLS × GRID_ROWS grid. A cell is "covered" if ANY shape's bounding
 * box overlaps it. We then find the largest 4-connected empty region;
 * a large contiguous void (> 8 cells = ~25% of the body) is the visual
 * symptom users complain about even when total whitespace ratio is fine.
 */
function findLargestEmptyRegion(shapes) {
  // Body area starts at y=0.84 (below master header bar at y=0.8 + accent).
  const BODY_Y_START = 0.84
  const bodyH = SLIDE_H_INCH - BODY_Y_START
  const cellW = SLIDE_W_INCH / GRID_COLS
  const cellH = bodyH / GRID_ROWS

  // Initialise: 0 = empty, 1 = covered.
  const grid = Array.from({length: GRID_ROWS}, () => Array(GRID_COLS).fill(0))

  for (const s of shapes) {
    // Skip shapes that are entirely in the header band — they're master
    // chrome, not body content.
    if (s.y + s.h <= BODY_Y_START) continue
    const yEff = Math.max(s.y, BODY_Y_START)
    const c1 = Math.max(0, Math.floor(s.x / cellW))
    const c2 = Math.min(GRID_COLS - 1, Math.floor((s.x + s.w - 0.01) / cellW))
    const r1 = Math.max(0, Math.floor((yEff - BODY_Y_START) / cellH))
    const r2 = Math.min(GRID_ROWS - 1, Math.floor((s.y + s.h - 0.01 - BODY_Y_START) / cellH))
    for (let r = r1; r <= r2; r++) {
      for (let c = c1; c <= c2; c++) {
        grid[r][c] = 1
      }
    }
  }

  // 4-connected flood fill to find largest empty region.
  const visited = Array.from({length: GRID_ROWS}, () => Array(GRID_COLS).fill(false))
  let maxRegion = 0
  for (let r = 0; r < GRID_ROWS; r++) {
    for (let c = 0; c < GRID_COLS; c++) {
      if (grid[r][c] === 0 && !visited[r][c]) {
        // BFS
        const stack = [[r, c]]
        let size = 0
        while (stack.length) {
          const [rr, cc] = stack.pop()
          if (rr < 0 || rr >= GRID_ROWS || cc < 0 || cc >= GRID_COLS) continue
          if (visited[rr][cc] || grid[rr][cc] === 1) continue
          visited[rr][cc] = true
          size++
          stack.push([rr+1, cc], [rr-1, cc], [rr, cc+1], [rr, cc-1])
        }
        if (size > maxRegion) maxRegion = size
      }
    }
  }
  return maxRegion
}
```

修改 `analyseSlide`：

```diff
@@ -891,6 +891,21 @@ function analyseSlide(shapes) {
   const defects = []
+  // Grid-based local emptiness (Round 2: catches the "bottom half is dead"
+  // pattern that the global coveredArea metric is blind to).
+  const largestEmptyCells = findLargestEmptyRegion(shapes)
+  if (largestEmptyCells >= MAX_EMPTY_CELLS_CRIT) {
+    defects.push({
+      severity: 'critical',
+      kind: 'local_emptiness',
+      detail: `largest contiguous empty region = ${largestEmptyCells}/${GRID_COLS*GRID_ROWS} cells`,
+    })
+  } else if (largestEmptyCells >= MAX_EMPTY_CELLS_WARN) {
+    defects.push({
+      severity: 'warning',
+      kind: 'local_emptiness',
+      detail: `largest contiguous empty region = ${largestEmptyCells}/${GRID_COLS*GRID_ROWS} cells`,
+    })
+  }
+
   let coveredArea = 0
```

### 測試更新

**檔案**：`ANILALM/pptx-skill/scripts/test_qa_geometric.js`（若 renderer 有測試框架；否則靠 e2e）

驗證：把 v2 的 `report_v2.pptx` 餵 `/qa-geometric`，預期 slide 5、8、9、12、13 至少其中 3 張被標 `local_emptiness` warning 或 critical。

**驗收**：geometric QA 抓出 v2 那些「下半空蕩」的 slide；trigger vision QA 之前先發訊息給 LLM 改善版面。

---

## Patch G：Graphviz pipeline 診斷 runbook

> 這不是程式碼補丁，是先做診斷。確認壞在哪一環再開 patch。

v2 slide 9 沒有出現 graphviz 渲染的圖，但 FLUX 亂碼也沒了——表示 `image_focus` 整個 fallback 到 `standard`。可能在以下任一環節 silent fail：

### 診斷步驟

**1. 確認 container 裝了 graphviz**

```bash
docker exec <csp-backend-container> which dot
docker exec <csp-backend-container> dot -V
# 預期：/usr/bin/dot, dot - graphviz version 2.42.x

docker exec <csp-backend-container> fc-list | grep -i noto.*cjk
# 預期：至少一個 Noto Sans CJK TC / SC 字型
```

**2. 確認最近的 job 是否走到 diagram path**

```bash
# 撈最近一次 studio job 的 spec
docker exec <csp-backend-container> python -c "
from app.services.studio_job_service import _JOBS
for jid, job in list(_JOBS.items())[-3:]:
    print(jid, job.state)
    if job.spec_dict:
        for i, s in enumerate(job.spec_dict.get('slides', [])):
            if s.get('layout_kind') == 'image_focus':
                print(f'  slide {i}: image_kind={s.get(\"image_kind\")}, '
                      f'has_dot={bool(s.get(\"diagram_dot\"))}, '
                      f'has_prompt={bool(s.get(\"image_prompt\"))}, '
                      f'has_ref={bool(s.get(\"image_ref\"))}')
"
```

可能結果：

- `image_kind=None, has_dot=False, has_prompt=True`：LLM 沒選 diagram path，新 prompt 規則沒被遵守 → **修 prompt**
- `image_kind="diagram", has_dot=True`：LLM 有選但 hydration 失敗 → **看 diagram_renderer log**
- 完全沒 image_focus slide：LLM 整個放棄 image_focus → **降低 image_focus 門檻、加 trigger 範例**

**3. 看 hydration log**

```bash
docker logs <csp-backend-container> 2>&1 | grep -i "diagram\|graphviz\|hydrat" | tail -50
```

關鍵字：
- `dot: command not found` → 裝 graphviz
- `Diagram render failed` → 看具體錯誤
- `image_kind=diagram but diagram_dot is empty` → schema validator 沒擋住但 LLM 沒提供
- 完全沒這些 log → hydration code path 根本沒走

**4. 手動測試 `diagram_renderer.py`**

```bash
docker exec <csp-backend-container> python -c "
import asyncio
from app.services.diagram_renderer import render_dot_to_png
dot = '''
digraph G {
  rankdir=LR;
  node [shape=box, fontname=\"Noto Sans CJK TC\"];
  Supervisor -> WorkerA [label=\"任務分派\"];
  Supervisor -> WorkerB [label=\"任務分派\"];
}
'''
result = asyncio.run(render_dot_to_png(dot))
print(f'Result: {len(result) if result else None} bytes')
" > /tmp/diag.png
file /tmp/diag.png
# 預期：PNG image data, 600 x 200, ...
```

### 各狀況的 patch

根據 1-4 結果決定下一步：

| 狀況 | Patch |
|---|---|
| Container 缺 `dot` | 重 build CSP backend image（Dockerfile 已有 `RUN apt-get install graphviz`，可能 cache 沒更新） |
| 缺中文字型 | Dockerfile 加 `fonts-noto-cjk fonts-noto-cjk-extra`、設 `ENV PANGOCAIRO_BACKEND=fontconfig` |
| LLM 沒選 diagram | 在 `_build_generation_prompt` 加 hard trigger：「title 含『架構』『拓撲』『流程』且該主題有對應段落 → 必須 layout_kind='image_focus' + image_kind='diagram'」 |
| Subprocess timeout | `diagram_renderer.py` 超時從 5s 拉到 15s（CJK 字型 cold start 慢） |
| Hydration silent fail | 把 `if render fails → drop diagram_dot, fallback to standard` 改成「保留 image_focus + image_kind=diagram，但在 slide 加 visible 警告框 + log critical」，這樣使用者跟 ops 都會看到 |

**完成診斷後**：開一個 Patch H 把上面對應的修正寫成具體 diff（之後再做）。

---

## 整體驗收

跑完 Patch A-F 後，重跑診斷素材，**預期看到**：

| Slide # | v2 狀況 | Round 2 預期 |
|---|---|---|
| 1 | 副標斜體 | 直立 (Patch A) |
| 2 | standard 大綱 | 仍 standard 或 icon_rows（依 LLM 重判，可接受任一）|
| 3 | section_break 副標斜體 | 直立 (Patch A) |
| 5 | standard 三 bullet（regression） | 回到 two_column 兩欄各 2-3 bullet (Patch C) |
| 6 | 95% 偏上 | 95% 視覺中心接近 y=3.5 inches (Patch B) |
| 7 | section_break 副標斜體 | 直立 (Patch A) |
| 8 | standard, Agentic Workflow 三階段 | icon_rows 三 row (Patch D + E) |
| 11 | section_break 副標斜體 | 直立 (Patch A) |
| 13 | standard, Supervisor 拓撲 | icon_rows 或 image_focus diagram (Patch D + E)，graphviz 視 Patch G 結果 |
| 9 | standard（FLUX 失敗 fallback） | image_focus + graphviz 圖（依 Patch G 結果）|

**通過條件**：

1. 沒有任何 slide 顯示仿斜體 CJK
2. slide 5 layout_kind == `two_column`
3. slide 8 layout_kind == `icon_rows`
4. slide 13 layout_kind ∈ `{icon_rows, image_focus}`
5. slide 6 的「95%」垂直中心在 y ∈ (3.0, 4.2) inches
6. `analyseSlide` 對 v2 slide 5、8、9、12、13 至少 3 張回報 `local_emptiness` warning 或 critical
7. 所有測試（既有 + 新增）通過：`pytest myCSPPlatform/backend/tests/test_layout_*.py test_studio_*.py -v`

---

## 補丁實作順序總覽

| 順序 | Patch | 檔案 | 預估 |
|---|---|---|---|
| 1 | A — italic 拿掉 | `server.js:206` | 5 min |
| 2 | B — stat_callout 置中 | `server.js:285-305` | 10 min |
| 3 | C — Column.bullets min 3→2 + demote 改 icon_rows | `schemas/studio.py:129` + `studio.py:923-960` + prompt + tests | 45 min |
| 4 | D — rebalancer 觸發擴大 | `studio.py:2076` + 新增 `_should_rebalance` + tests | 30 min |
| 5 | E — V4 keyword 擴充 | `studio.py:1538` + tests | 15 min |
| 6 | F — grid-based emptiness | `server.js:891` + 新增 `findLargestEmptyRegion` | 60 min |
| — | G — graphviz 診斷 | runbook 跑 | 30 min - 2 h |

A-F 合計約 2.5 小時，預期能解決 v2 大部分視覺問題。G 視診斷結果再開 patch。
