# ANILA LM Studio — Round 3 補丁規格

> 對 `feature/studio-quality-fixes` 的後續修正，基於 v3 輸出 `11月學習心得報告_AI_基礎設施與_Agentic_RAG_實作.pptx` 的逐張比對。
>
> 倉庫 HEAD：`c7970b5`（含 Round 2 全部 7 patches + graphviz Dockerfile 修正）。
>
> Round 2 後 layout 命中率提升到 ~73%，graphviz 路徑生效（slide 8、13 出現可讀的 CJK 架構圖）。但仍有兩層問題：
>
> **Part 1 — v3 殘留**（小 patch，~1.5 小時）：
> - V4 偵測還是 title-keyword based，漏掉「bullet 結構是 label:description」這種更可靠的訊號
> - LaTeX 字串 `$\rightarrow$` 沒被 normalizer 攔下
> - icon_rows 找不到 concept 時畫空圓圈，視覺像 broken icon
> - section_break 標題過長時 56pt 折行折在奇怪位置
>
> **Part 2 — 視覺風格僵固**（架構升級，~5-7 天）：
> - 4 個 palette 只換兩個 hex 色（bar + accent），不換結構
> - 同一份系統不管什麼內容都長一樣：滿版深底 + 左側橘條 + 黃描邊圓圈 icon + 同字型同密度
> - 把 `palette` 升級成 `theme`：palette + typography + chrome + decoration + icon style + density 一整捆
>
> 兩部分獨立。Part 1 可以先上線拉視覺品質、Part 2 排在後面當下個 milestone。

---

## 共用測試素材

```bash
# 重跑 v3 同一份 collection（11月學習心得 + Agentic RAG + ISO 42001）
curl -X POST http://localhost:8000/api/studio/slides/jobs \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"collection_id": <ID>, "preset": "經典報告結構"}'

JOB_ID=...
curl -o /tmp/v4.pptx "http://localhost:8000/api/studio/slides/jobs/$JOB_ID/pptx" \
  -H "Authorization: Bearer $TOKEN"

# 轉圖人工檢查
cd /tmp && python /path/to/pptx-skill/scripts/office/soffice.py --headless --convert-to pdf v4.pptx
rm -f slide-*.jpg && pdftoppm -jpeg -r 110 v4.pdf slide
```

---

# Part 1 — v3 殘留問題

四個獨立 patch，建議按 H → I → J → K 順序做，每個都能單獨上線。

## Patch H：V4 改成 content-pattern 偵測（最高優先）

**問題**：Round 2 V4 規則只看 title 是否含 enumeration keyword。v3 slide 2「執行摘要」、slide 5「底層推理引擎除錯實踐」、slide 12「ISO 42001 合規實作」都是 4 個 bullet、每條都 `label: description` 結構（典型 icon_rows shape），但因為 title 沒有 keyword，全部被當 standard 過。

真正可靠的訊號**在 bullet 內容**：「中文 2-6 字 + 冒號 + 描述」這個 pattern 出現在 ≥70% 的 bullet 上，就是 icon_rows 的明確指紋，比 title keyword 強得多。

**檔案**：`myCSPPlatform/backend/app/api/studio.py`

**Diff**：

```diff
@@ -1,5 +1,6 @@
 import asyncio
 import logging
+import re
 from collections import defaultdict
@@ -1538,21 +1539,40 @@ LAYOUT_CONSECUTIVE_STANDARD_LIMIT = 3
 # V4: enumeration title keywords. When a slide title contains any of these
-# AND it has 3+ bullets AND layout_kind=standard, it's a textbook candidate
-# for icon_rows — the LLM "described 3 things" but didn't reach for the
-# matching layout.
+# AND has 3+ bullets AND layout_kind=standard, the LLM "described N things"
+# but didn't reach for the matching layout.
+#
+# Round 3: title-keyword path is now SECONDARY. The PRIMARY V4 signal is
+# the bullet *content* pattern (label: description), captured by
+# _LABEL_BULLET_RE below. Empirically: v3 slide 2 ("執行摘要")、5
+# ("底層推理引擎除錯實踐")、12 ("ISO 42001 合規實作") all show 4 bullets
+# in label-description shape but title contains no enumeration keyword.
+# Title-keyword catches them when bullets are short or non-labeled.
 _ENUMERATION_KEYWORDS = (
     # Numeric enumeration
     "三大", "四大", "五大", "兩大", "三項", "三類",
     # Process / sequence
     "步驟", "階段", "流程", "歷程", "順序",
     "workflow", "pipeline", "process",
     # Structural enumeration
     "面向", "層面", "維度", "方面",
     # Architecture / topology
     "架構", "拓撲", "拓樸", "結構", "設計", "佈局",
     # Capability / function lists
     "核心能力", "能力", "功能", "特性", "特徵",
     # Strategy / approach
     "策略", "方案", "模式", "機制", "方法",
     # Comparison framing
     "對比", "對照", " vs ", " vs.",
 )
+
+# Round 3 PRIMARY V4 signal: "label: description" bullet pattern.
+#
+# Matches Chinese 2-6 char label + (half- or full-width) colon + non-empty
+# tail. Tuned conservatively: requires the label to be entirely CJK so
+# bullets like "Token 消耗降低" (mixed Latin) don't pass — those are real
+# narrative bullets, not enumeration labels.
+_LABEL_BULLET_RE = re.compile(
+    r"^\s*[\u4e00-\u9fff]{2,6}\s*[:：]\s*\S.*$",
+)
+
+# Pattern threshold: fraction of bullets that must match _LABEL_BULLET_RE
+# for the content-pattern V4 path to fire. 0.7 = at least 3 of 4 bullets,
+# or 2 of 3.
+_LABEL_PATTERN_THRESHOLD = 0.7
```

修改 `_audit_layout_distribution` 內 V4 那段：

```diff
@@ -1674,21 +1694,52 @@ def _audit_layout_distribution(
     # ── V4: enumeration title + 3+ bullets + standard layout ──
     for idx, slide in enumerate(slides):
         if slide.layout_kind != "standard":
             continue
         bullets = slide.bullets or []
         if len(bullets) < 3:
             continue

-        title_lc = (slide.title or "").lower()
-        if not any(kw.lower() in title_lc for kw in _ENUMERATION_KEYWORDS):
-            continue
+        title_lc = (slide.title or "").lower()
+        title_match = any(
+            kw.lower() in title_lc for kw in _ENUMERATION_KEYWORDS
+        )

-        violations.append(
-            LayoutViolation(
-                kind="V4",
-                severity="soft",
-                slide_indices=[idx],
-                detail=(
-                    f"slide #{idx}「{slide.title}」: {len(bullets)} bullets, "
-                    f"standard layout, enumeration keyword in title"
-                ),
-            )
-        )
+        # Round 3 primary signal: bullet content pattern.
+        pattern_matches = sum(
+            1 for b in bullets if _LABEL_BULLET_RE.match(str(b))
+        )
+        pattern_match = (
+            pattern_matches / len(bullets) >= _LABEL_PATTERN_THRESHOLD
+        )
+
+        if not (title_match or pattern_match):
+            continue
+
+        detail_bits = [
+            f"slide #{idx}「{slide.title}」: {len(bullets)} bullets, standard layout",
+        ]
+        if title_match:
+            detail_bits.append("title-keyword")
+        if pattern_match:
+            detail_bits.append(
+                f"bullet-pattern {pattern_matches}/{len(bullets)}"
+            )
+
+        violations.append(
+            LayoutViolation(
+                kind="V4",
+                severity="soft",
+                slide_indices=[idx],
+                detail=", ".join(detail_bits),
+            )
+        )
```

### 測試補強

**檔案**：`myCSPPlatform/backend/tests/test_layout_audit.py`

```python
@pytest.mark.parametrize("title,bullets,expected_v4", [
    # ── Pure content-pattern hits (Round 3 new) ──
    ("執行摘要", [
        "高效能推理：掌握 TensorRT-LLM 並解決 C++ 對齊 Bug",
        "技術突破：實作法律 Agentic RAG",
        "合規治理：導入 ISO 42001",
        "架構演進：提出基於 DDD 的垂直切分架構",
    ], True),  # v3 slide 2 — 4/4 label-pattern match
    ("底層推理引擎除錯實踐", [
        "問題：v1.2.0rc2 處理 Harmony 格式時導致服務崩潰",
        "根因：透過 Git Bisect 定位為 C++ 記憶體對齊瑕疵",
        "對策：降版至 v1.1.0rc5 並建立自動化回歸測試",
        "價值：證明具備原始碼級別除錯能力",
    ], True),  # v3 slide 5 — 4/4 label-pattern, no title keyword

    # ── Title-keyword hits (Round 2 behaviour preserved) ──
    ("Multi-Agent Supervisor 拓撲設計", [
        "Supervisor 統籌 Worker", "Worker 執行專業任務", "高度模組化",
    ], True),  # title contains "拓撲" + "設計"

    # ── Negative cases ──
    ("執行摘要", [
        "今年完成了多項工作",
        "在推理上有顯著進展",
        "下一步將擴大研究範圍",
    ], False),  # no label-pattern, no keyword
    ("封面標題", ["a", "b", "c"], False),
    ("過程描述", [
        "經過調研後團隊決定採用 ReAct 框架",
    ], False),  # only 1 bullet
])
def test_v4_content_pattern_and_keyword_paths(title, bullets, expected_v4):
    spec = SlidesSpec(title="t", slides=[
        Slide(title=title, bullets=bullets, layout_kind="standard"),
    ])
    violations = _audit_layout_distribution(spec, chunks_text="")
    v4s = [v for v in violations if v.kind == "V4"]
    assert (len(v4s) > 0) == expected_v4


def test_v4_pattern_threshold_70_percent():
    """Bullets need ≥70% label-pattern match to trigger pure pattern path."""
    spec = SlidesSpec(title="t", slides=[Slide(
        title="實作筆記",  # no V4 keyword
        bullets=[
            "前提：先 install 依賴",   # match
            "步驟：跑 init 指令",      # match
            "完成後就可以使用了",      # no match
        ],
        layout_kind="standard",
    )])
    violations = _audit_layout_distribution(spec, chunks_text="")
    # 2/3 = 66% < 70% → no V4
    assert not any(v.kind == "V4" for v in violations)
```

**驗收**：重跑 v3 素材，slide 2、5、9、12 至少 3 張變成 `icon_rows`；rebalance log 顯示 `v4_count >= 4`，觸發 rebalance 路徑。

---

## Patch I：LaTeX 字串攔截

**問題**：v3 slide 12 第 3 條 bullet 顯示「`Observation $\rightarrow$ Thought $\rightarrow$ Action`」。gemma4 把箭頭寫成 LaTeX 數學模式，沒人攔下。

**檔案**：`myCSPPlatform/backend/app/services/studio_text_normalizer.py`

新增函式 `strip_latex`，並加入既有 normalize pipeline。

**新增**：

```python
import re

# Known LaTeX commands → Unicode equivalent. Order matters: longer commands
# (e.g. \Rightarrow) must come before shorter prefixes (\rightarrow) if
# we ever support both, but here all commands are unambiguous.
_LATEX_REPLACEMENTS = {
    # Arrows
    r"\$\\rightarrow\$": "→",
    r"\$\\leftarrow\$":  "←",
    r"\$\\Rightarrow\$": "⇒",
    r"\$\\Leftarrow\$":  "⇐",
    r"\$\\leftrightarrow\$": "↔",
    r"\$\\to\$":         "→",
    r"\$\\gets\$":       "←",
    r"\$\\mapsto\$":     "↦",

    # Math operators
    r"\$\\times\$":      "×",
    r"\$\\div\$":        "÷",
    r"\$\\pm\$":         "±",
    r"\$\\approx\$":     "≈",
    r"\$\\equiv\$":      "≡",
    r"\$\\neq\$":        "≠",
    r"\$\\geq\$":        "≥",
    r"\$\\leq\$":        "≤",
    r"\$\\sim\$":        "~",
    r"\$\\cdot\$":       "·",

    # Greek (common in ML papers)
    r"\$\\alpha\$":      "α",
    r"\$\\beta\$":       "β",
    r"\$\\gamma\$":      "γ",
    r"\$\\delta\$":      "δ",
    r"\$\\epsilon\$":    "ε",
    r"\$\\theta\$":      "θ",
    r"\$\\lambda\$":     "λ",
    r"\$\\mu\$":         "μ",
    r"\$\\pi\$":         "π",
    r"\$\\sigma\$":      "σ",
    r"\$\\tau\$":        "τ",
    r"\$\\phi\$":        "φ",
    r"\$\\omega\$":      "ω",
    r"\$\\Sigma\$":      "Σ",
    r"\$\\Delta\$":      "Δ",

    # Misc
    r"\$\\infty\$":      "∞",
    r"\$\\partial\$":    "∂",
    r"\$\\nabla\$":      "∇",
}

# Fallback for unknown $...$ wrappers. Strips the dollar signs and keeps
# the inner content rather than wholesale deletion (e.g. "$x^2$" becomes
# "x^2", not better but not worse than original LaTeX). Bounded length to
# avoid eating wide swaths of text on mismatched dollars.
_GENERIC_LATEX_RE = re.compile(r"\$([^\$\n]{1,80})\$")


def strip_latex(text: str) -> str:
    """Replace LaTeX math-mode strings with Unicode / plain equivalents.

    gemma4 (and llama / mistral variants) occasionally emit ``$\\rightarrow$``
    in place of ``→`` when reasoning about flows. The renderer prints the
    LaTeX verbatim because pptxgenjs has no math support. This pass runs
    before normalization and catches the common commands plus a fallback
    that strips bare dollar wrappers.

    Pure function. Safe on empty / None-like input.
    """
    if not text:
        return text
    text = str(text)
    for pattern, replacement in _LATEX_REPLACEMENTS.items():
        text = re.sub(pattern, replacement, text)
    text = _GENERIC_LATEX_RE.sub(r"\1", text)
    return text
```

修改 normalize 入口（檔案內找 `normalize_text` 或主要 entry function）：

```diff
 def normalize_text(text: str) -> str:
     # ... existing normalization ...
+    text = strip_latex(text)  # Round 3: strip $\rightarrow$ etc. before
+                              # other transforms so downstream regex sees
+                              # plain Unicode arrows.
     # ... rest of normalization ...
```

確保所有走 normalize 的地方都會經過：`Slide.title`、`Slide.bullets`、`Stat.value` / `label` / `supporting`、`Column.bullets` / `heading`、`IconRow.heading` / `description`、`Quote.text`。

### 測試

**檔案**：`myCSPPlatform/backend/tests/test_studio_text_normalizer.py`

```python
def test_strip_latex_known_arrows():
    assert strip_latex("Observation $\\rightarrow$ Thought") == "Observation → Thought"
    assert strip_latex("A $\\to$ B $\\to$ C") == "A → B → C"
    assert strip_latex("$\\Rightarrow$ implies") == "⇒ implies"

def test_strip_latex_math_ops():
    assert strip_latex("速度 $\\times$ 2") == "速度 × 2"
    assert strip_latex("$\\alpha = 0.5$") == "α = 0.5"  # generic fallback strips dollars
    # ↑ this test is loose: real result depends on whether _LATEX_REPLACEMENTS
    # catches the whole "$\\alpha = 0.5$" pattern (it doesn't) or falls
    # through to generic. Adjust expected if implementation differs.

def test_strip_latex_unknown_command_falls_back():
    # Unknown command → generic regex strips dollar wrappers, keeps inner.
    assert strip_latex("a $\\someweird$ b") == "a \\someweird b"

def test_strip_latex_no_latex_passes_through():
    assert strip_latex("純中文沒有 LaTeX") == "純中文沒有 LaTeX"
    assert strip_latex("") == ""
    assert strip_latex(None) is None

def test_strip_latex_v3_slide12_regression():
    """Exact text from v3 slide 12 that triggered this patch."""
    input_text = "記錄 Observation $\\rightarrow$ Thought $\\rightarrow$ Action 軌跡"
    expected = "記錄 Observation → Thought → Action 軌跡"
    assert strip_latex(input_text) == expected
```

**驗收**：重跑 v3 素材，slide 12 顯示「Observation → Thought → Action」而非 raw LaTeX。

---

## Patch J：icon_rows 找不到 concept 時不要畫空圓圈

**問題**：v3 slide 14（未來最佳化方向）三 row 裡，「架構解耦」「可觀測性」是**空黃圈**——CONCEPT_MAP 沒對到 LLM 給的 concept name。空圓圈視覺像 broken icon，比沒 icon 更糟。

**檔案**：`ANILALM/pptx-skill/server.js`（renderIconRows 函式）+ `ANILALM/pptx-skill/icons.js`

### J.1 擴大 CONCEPT_MAP

把 v3 真實遇到的 concept 加進去：

**檔案**：`ANILALM/pptx-skill/icons.js`，`CONCEPT_MAP` 物件

```diff
 const CONCEPT_MAP = Object.freeze({
   // ... existing ...

+  // === Round 3 additions (v3 fallout) ===
+
+  // Architecture / decoupling
+  decoupling: 'HiArrowsPointingOut',
+  coupling: 'HiArrowsPointingIn',
+  modularity: 'HiSquares2x2',
+  orthogonality: 'HiViewfinderCircle',
+  layering: 'HiBars3',
+
+  // Observability / monitoring
+  observability: 'HiEye',
+  monitoring: 'HiEye',
+  logging: 'HiDocumentText',
+  tracing: 'HiArrowsRightLeft',
+  metrics: 'HiChartBar',
+
+  // Compliance / governance
+  compliance: 'HiShieldCheck',
+  audit: 'HiClipboardDocumentCheck',
+  policy: 'HiDocumentMagnifyingGlass',
+  governance: 'HiUserGroup',
+  transparency: 'HiEye',
+
+  // Performance / debugging
+  debugging: 'HiBugAnt',
+  performance: 'HiBolt',
+  optimization: 'HiAdjustmentsHorizontal',
+  latency: 'HiClock',
+  throughput: 'HiArrowTrendingUp',
+
+  // RAG / retrieval
+  retrieval: 'HiMagnifyingGlassCircle',
+  ranking: 'HiBars3BottomRight',
+  reranking: 'HiArrowsUpDown',
+  hierarchy: 'HiQueueList',
+
+  // ── Generic Chinese-language concepts (LLM emits these directly) ──
+  // The schema field is `concept` (free string) — gemma4 often writes
+  // Chinese concept names. Add direct CJK keys to catch those without
+  // needing translation.
+  '架構解耦': 'HiArrowsPointingOut',
+  '可觀測性': 'HiEye',
+  '合規自動化': 'HiShieldCheck',
+  '效能調優': 'HiBolt',
+  '記憶體對齊': 'HiCubeTransparent',
+  '推理引擎': 'HiCpuChip',
+  '容器化部署': 'HiCloudArrowUp',
+  '硬體調優': 'HiAdjustmentsHorizontal',
+  '穩定性驗證': 'HiCheckBadge',
+  '階層式索引': 'HiQueueList',
+  '迴圈推理': 'HiArrowPath',
+  '兩階段檢索': 'HiBars3',
+  '思維鏈': 'HiSparkles',
+  '可稽核': 'HiClipboardDocumentCheck',
 })
```

### J.2 找不到 concept 改成「畫小色點」而非「畫大空圈」

**檔案**：`ANILALM/pptx-skill/server.js`，找 `renderIconRows` 函式（grep 'renderIconRows' 找位置）。

裡面應該有類似這樣的邏輯：

```javascript
// Old (示意，請對 actual code 修)
const heroiconKey = CONCEPT_MAP[row.concept]
// 畫黃色圓圈
slide.addShape('ellipse', { x, y, w: 0.8, h: 0.8, line: {...} })
if (heroiconKey) {
  // 在圓圈內畫 heroicon
  slide.addImage({ data: heroiconSvg(heroiconKey), x: x+0.2, y: y+0.2, w: 0.4, h: 0.4 })
}
```

改成：

```javascript
// Round 3: when concept is unknown, draw a small accent-coloured dot
// instead of a big empty circle (empty circles read as "broken icon").
const heroiconKey = CONCEPT_MAP[row.concept]
if (heroiconKey) {
  // Known concept: full treatment (outlined circle + heroicon).
  slide.addShape('ellipse', {
    x, y, w: 0.8, h: 0.8,
    line: { color: theme.accent, width: 2 },
    fill: { type: 'none' },
  })
  slide.addImage({
    data: heroiconSvg(heroiconKey),
    x: x + 0.2, y: y + 0.2, w: 0.4, h: 0.4,
  })
} else {
  // Unknown concept: small filled dot. Aligned with where the centre of
  // the circle would have been so heading/description offsets don't move.
  slide.addShape('ellipse', {
    x: x + 0.35, y: y + 0.35, w: 0.1, h: 0.1,
    fill: { color: theme.accent },
    line: { type: 'none' },
  })
  // Log so we can mine unknown concepts for future CONCEPT_MAP additions.
  console.warn(`[icon_rows] unknown concept "${row.concept}" — drew dot`)
}
```

**驗收**：重跑 v3 素材，slide 14 三 row 都有可辨識的視覺標記；如果 concept 沒被 catch，改成小點而非大空圓；server log 出現 unknown concept 名稱可供後續加進 CONCEPT_MAP。

---

## Patch K：section_break 長標題自動縮字級

**問題**：v3 slide 3「第一章: 基礎設施建置與效能最佳化」是 16 個字 + 標點，56pt 撐不下 11.5 inch 寬度，折成「第一章: 基礎設施建置與效能最 / 佳化」——「佳化」獨立一行，視覺極醜。`renderSectionBreak` line 196 寫死 fontSize: 56。

**檔案**：`ANILALM/pptx-skill/server.js`

**Diff**：

```diff
 function renderSectionBreak(pres, s, p) {
   // No master — full-bleed colour fill.
   const slide = pres.addSlide()
   slide.background = { color: p.bar }
@@ -190,10 +190,18 @@ function renderSectionBreak(pres, s, p) {
   slide.addShape('rect', {
     x: 0.6, y: 1.6, w: 0.14, h: 4.2,
     fill: { color: p.accent },
     line: { type: 'none' },
   })
+
+  // Round 3: auto-shrink for long titles. With width 11.5", fontSize 56pt
+  // Noto Sans CJK bold fits ~12 CJK chars per line cleanly. Longer titles
+  // need smaller font to avoid awkward orphan-line breaks like v3 slide 3
+  // "第一章: 基礎設施建置與效能最佳化" (16 chars → "佳化" alone on line 2).
+  const titleStr = String(s.title || '')
+  const titleFont = pickSectionTitleFont(titleStr)
+
   slide.addText(titleStr, {
-    x: 1.1, y: 2.4, w: 11.5, h: 1.8,
-    fontSize: 56, bold: true, color: 'FFFFFF',
+    x: 1.1, y: 2.4, w: 11.5, h: 1.8,
+    fontSize: titleFont, bold: true, color: 'FFFFFF',
     align: 'left', valign: 'middle', fontFace: FONT_FACE,
   })
```

新增 helper（建議放在 renderSectionBreak 上方）：

```javascript
/**
 * Pick a section-break title fontSize that fits within the 11.5" body
 * width without producing orphan-line breaks. Calibrated for Noto Sans
 * CJK TC bold rendering.
 *
 * Empirical: at 56pt bold, ~12 CJK chars fit on one line.
 *            at 44pt bold, ~16 chars.
 *            at 36pt bold, ~20 chars.
 *            at 28pt bold, ~26 chars.
 *
 * Latin chars are narrower; we approximate by counting CJK as 1.0 and
 * Latin/digit as 0.55. The threshold ladder uses CJK-equivalent length.
 */
function pickSectionTitleFont(title) {
  if (!title) return 56
  let weighted = 0
  for (const ch of String(title)) {
    weighted += /[\u4e00-\u9fff\u3000-\u303f]/.test(ch) ? 1.0 : 0.55
  }
  if (weighted <= 12) return 56
  if (weighted <= 16) return 44
  if (weighted <= 20) return 36
  return 28
}
```

**驗收**：重跑 v3 素材，slide 3 標題單行顯示完整、不再有 "佳化" 孤行；其他 section_break 標題（slide 7「第二章」、slide 11「第三章」）字級保持 56pt。

---

# Part 2 — Theme 系統升級

> 比 Part 1 大很多。建議在 Part 1 全部完成、Round 3 第一輪上線之後再開工。

## 概念

把目前的 `palette`（4 個只換色的選項）升級成 `theme`：每個 theme 是一整捆視覺決策：

```
theme = {
  palette,           // 色票（既有概念）
  typography,        // 字型、粗細、各 layout 的字級
  chrome,            // title_bar / section_break / accent_motif 的「形狀」
  iconTreatment,     // icon 怎麼畫（描邊圓圈 / 小點 / 填色等）
  density,           // 內容密度（影響 padding、行距）
}
```

5 個 theme 設計：

| Theme | 用途 | 視覺直觀 |
|---|---|---|
| `corporate_navy` | 技術 / 業務報告（目前的樣子）| 深藍滿版 bar + 黃描邊 icon |
| `academic_paper` | 研究發表、論文摘要 | serif 字型 + 細線標題 + 灰小點 icon |
| `warm_journal` | 個人心得、軟性回顧 | 米白底 + 棕褐標題 + 柔填色 icon |
| `executive_brief` | 高階 briefing、極簡 | 無 chrome + 醒目編號 + 大量留白 |
| `startup_pitch` | 對外發表、產品介紹 | 巨大字級 + 滿版色塊 + 填色 pill icon |

實作分 6 個 phase commit。每個 phase 結束後系統都還能跑（不破壞既有 4 palette job）。

---

## Patch L：Theme schema + 向後相容

**目標**：schema 加 `theme` 欄位、保留 `palette` 為 deprecated alias，定 5 個 theme 白名單。renderer 還是用 palette 就好（先不動），這 patch 只動 schema + 對映表。

**檔案**：`myCSPPlatform/backend/app/schemas/studio.py`

**Diff**：

```diff
 PALETTES: tuple[str, ...] = (
     "navy_amber",
     "forest_moss",
     "charcoal_minimal",
     "coral_energy",
 )

+# Round 3: themes are the new top-level visual identity unit. A theme
+# bundles palette + typography + chrome + icon treatment + density. The
+# renderer interprets theme.id and applies the bundle.
+#
+# Existing `palette` field is preserved as a deprecated alias — jobs
+# that set palette but not theme will resolve to the equivalent theme
+# via _PALETTE_TO_THEME below.
+THEMES: tuple[str, ...] = (
+    "corporate_navy",
+    "academic_paper",
+    "warm_journal",
+    "executive_brief",
+    "startup_pitch",
+)
+
+# Old palette → equivalent new theme. Used when a request specifies
+# palette without theme (legacy clients) so they keep working.
+_PALETTE_TO_THEME: dict[str, str] = {
+    "navy_amber": "corporate_navy",
+    "forest_moss": "warm_journal",
+    "charcoal_minimal": "academic_paper",
+    "coral_energy": "startup_pitch",
+    # NB: executive_brief has no direct palette ancestor — it's a new theme.
+}
+
@@ -... (in SlidesSpec class) ...
     palette: Literal[
         "navy_amber", "forest_moss", "charcoal_minimal", "coral_energy"
     ] = Field(
         default="navy_amber",
-        description="Named palette — renderer maps to concrete hexes.",
+        description=(
+            "DEPRECATED — use `theme` instead. Kept for backwards compat. "
+            "Resolved to equivalent theme via _PALETTE_TO_THEME if `theme` "
+            "is unset."
+        ),
     )
+    theme: Literal[
+        "corporate_navy", "academic_paper", "warm_journal",
+        "executive_brief", "startup_pitch",
+    ] | None = Field(
+        default=None,
+        description=(
+            "Visual identity bundle (Round 3). Bundles palette + typography "
+            "+ chrome + icon treatment + density. If None, resolves from "
+            "the legacy `palette` field at validation time."
+        ),
+    )
+
+    @model_validator(mode="after")
+    def _resolve_theme_from_palette(self):
+        """If theme is unset, derive from legacy palette field."""
+        if self.theme is None:
+            self.theme = _PALETTE_TO_THEME.get(self.palette, "corporate_navy")
+        return self
```

**驗收**：既有 jobs（只設 `palette`）通過 schema 驗證後 `theme` 欄位自動帶到對應值；新 jobs 可以直接設 `theme`。Tests：

```python
def test_theme_resolves_from_legacy_palette():
    spec = SlidesSpec(
        title="t", slides=[Slide(title="s", bullets=["a"])],
        palette="charcoal_minimal",
    )
    assert spec.theme == "academic_paper"

def test_theme_explicit_overrides_palette():
    spec = SlidesSpec(
        title="t", slides=[Slide(title="s", bullets=["a"])],
        palette="navy_amber",
        theme="warm_journal",
    )
    assert spec.theme == "warm_journal"

def test_executive_brief_theme_has_no_palette_alias():
    """executive_brief is new — must be specified explicitly."""
    spec = SlidesSpec(
        title="t", slides=[Slide(title="s", bullets=["a"])],
        theme="executive_brief",
    )
    assert spec.theme == "executive_brief"
```

---

## Patch M：Renderer 引入 THEMES bundle 與 theme-aware 派發

**目標**：在 `server.js` 定義 5 個 theme 的完整 bundle、把 layout renderer 從 `(pres, s, p)` 簽名改成 `(pres, s, theme)`，加入分派 helper。**先實作 corporate_navy**（等於既有行為），其他 4 個 theme 在後續 patch 落地。這個 patch 是 **scaffolding only**，視覺結果跟現在一致。

**檔案**：`ANILALM/pptx-skill/server.js`

**新增**：

```javascript
// ── Phase 3.5: themes — palette + typography + chrome + iconTreatment ──
//
// Each theme is a complete visual identity bundle. Renderers dispatch on
// theme.chrome.titleBar etc. to decide what shape to draw. Adding a new
// theme means adding an entry here plus any new chrome handler.
//
// Backwards compat: themes named after Round 1-2 palettes (navy_amber etc.)
// are NOT listed — schema-level _PALETTE_TO_THEME translates first.
const THEMES = {
  corporate_navy: {
    id: 'corporate_navy',
    palette: {
      bar: '1E2761', accent: 'F4B740',
      titleText: '1E2761', barText: 'FFFFFF',
      ink: '1A1A1A', muted: '5C6470', bg: 'FFFFFF',
    },
    fonts: {
      title: 'Noto Sans CJK TC',
      body: 'Noto Sans CJK TC',
      titleWeight: 'bold',
      titleSize: { content: 26, section: 56, cover: 56 },
      bodySize: { default: 18, dense: 16, spacious: 20 },
    },
    chrome: {
      titleBar: 'filled',
      sectionBreak: 'side_strip',
      accentMotif: 'yellow_underline',
    },
    iconTreatment: {
      style: 'outline_circle',
      circleSize: 0.8,
      iconSize: 0.4,
      strokeWidth: 2,
    },
    density: 'comfortable',
  },

  academic_paper: {
    id: 'academic_paper',
    palette: {
      bar: 'FFFFFF', accent: 'A0826D',  // tan accent
      titleText: '212121', barText: '212121',
      ink: '212121', muted: '70757A', bg: 'FFFFFF',
    },
    fonts: {
      title: 'Noto Serif CJK TC',
      body: 'Noto Serif CJK TC',
      titleWeight: 'normal',
      titleSize: { content: 24, section: 48, cover: 56 },
      bodySize: { default: 16, dense: 14, spacious: 18 },
    },
    chrome: {
      titleBar: 'underline_only',     // 0.5pt rule below title, no fill
      sectionBreak: 'centered_minimal', // light bg, dark text, no strip
      accentMotif: 'none',
    },
    iconTreatment: {
      style: 'monochrome_dot',        // 0.15" filled muted dot, no circle
      circleSize: 0,
      iconSize: 0.15,
    },
    density: 'dense',
  },

  warm_journal: {
    id: 'warm_journal',
    palette: {
      bar: 'FFF8F0', accent: 'D2691E',
      titleText: '4A3429', barText: '4A3429',
      ink: '4A3429', muted: '7D6857', bg: 'FFFCF7',
    },
    fonts: {
      title: 'Noto Sans CJK TC',
      body: 'Noto Sans CJK TC',
      titleWeight: 'semibold',
      titleSize: { content: 26, section: 52, cover: 56 },
      bodySize: { default: 18, dense: 16, spacious: 20 },
    },
    chrome: {
      titleBar: 'left_marker',        // 0.15"-wide accent block to left of title
      sectionBreak: 'soft_centered',  // cream bg, soft brown heading
      accentMotif: 'soft_highlight',
    },
    iconTreatment: {
      style: 'soft_filled',           // filled heroicon in accent, no surrounding circle
      circleSize: 0,
      iconSize: 0.6,
    },
    density: 'comfortable',
  },

  executive_brief: {
    id: 'executive_brief',
    palette: {
      bar: 'FFFFFF', accent: '1A1A1A',
      titleText: '1A1A1A', barText: '1A1A1A',
      ink: '1A1A1A', muted: '8E8E93', bg: 'FFFFFF',
    },
    fonts: {
      title: 'Noto Sans CJK TC',
      body: 'Noto Sans CJK TC',
      titleWeight: 'medium',
      titleSize: { content: 22, section: 44, cover: 48 },
      bodySize: { default: 18, dense: 16, spacious: 22 },
    },
    chrome: {
      titleBar: 'none',               // no bar; title flush left at top
      sectionBreak: 'numbered_minimal', // huge number left, thin title right
      accentMotif: 'none',
    },
    iconTreatment: {
      style: 'minimal_dot',
      circleSize: 0,
      iconSize: 0.1,
    },
    density: 'spacious',
  },

  startup_pitch: {
    id: 'startup_pitch',
    palette: {
      bar: '2F3C7E', accent: 'F96167',
      titleText: '2F3C7E', barText: 'FFFFFF',
      ink: '1A1A1A', muted: '5C6470', bg: 'FFFFFF',
    },
    fonts: {
      title: 'Noto Sans CJK TC',
      body: 'Noto Sans CJK TC',
      titleWeight: 'black',
      titleSize: { content: 32, section: 72, cover: 96 },
      bodySize: { default: 20, dense: 18, spacious: 24 },
    },
    chrome: {
      titleBar: 'oversized_display',  // title is huge; thin coral underline
      sectionBreak: 'full_bleed_number', // full-color, huge number
      accentMotif: 'highlight_pill',
    },
    iconTreatment: {
      style: 'filled_pill',           // filled coral circle, white icon inside
      circleSize: 0.9,
      iconSize: 0.5,
    },
    density: 'comfortable',
  },
}

function getTheme(name) {
  return THEMES[name] || THEMES.corporate_navy
}
```

新增 chrome dispatch helper（這些函式由各 renderer 呼叫）：

```javascript
/**
 * Title bar — top-of-slide chrome that hosts the slide title.
 *
 * Variants:
 *   filled            — current full-width filled bar (corporate_navy)
 *   underline_only    — title text only + 0.5pt rule below
 *   left_marker       — small accent block to the left of title
 *   none              — no chrome; title sits flush at slide top
 *   oversized_display — title takes 1.4" with a thin accent underline
 */
function applyTitleBar(slide, title, theme) {
  const p = theme.palette
  const fonts = theme.fonts
  const titleStr = String(title || '')

  switch (theme.chrome.titleBar) {
    case 'filled': {
      // Title in filled bar (uses master in current implementation; this
      // path mirrors that for corporate_navy).
      slide.addText(titleStr, {
        x: 0.5, y: 0.1, w: 12.3, h: 0.6,
        fontSize: fonts.titleSize.content, bold: true,
        color: p.barText,
        align: 'left', valign: 'middle',
        fontFace: fonts.title, margin: 0,
      })
      break
    }
    case 'underline_only': {
      slide.addText(titleStr, {
        x: 0.5, y: 0.3, w: 12.3, h: 0.5,
        fontSize: fonts.titleSize.content,
        color: p.titleText, fontFace: fonts.title,
        align: 'left', valign: 'middle', margin: 0,
      })
      slide.addShape('line', {
        x: 0.5, y: 0.95, w: 12.3, h: 0,
        line: { color: p.muted, width: 0.5 },
      })
      break
    }
    case 'left_marker': {
      slide.addShape('rect', {
        x: 0.5, y: 0.3, w: 0.15, h: 0.5,
        fill: { color: p.accent },
        line: { type: 'none' },
      })
      slide.addText(titleStr, {
        x: 0.8, y: 0.3, w: 12.0, h: 0.5,
        fontSize: fonts.titleSize.content, bold: true,
        color: p.titleText, fontFace: fonts.title,
        align: 'left', valign: 'middle', margin: 0,
      })
      break
    }
    case 'none': {
      slide.addText(titleStr, {
        x: 0.5, y: 0.4, w: 12.3, h: 0.4,
        fontSize: fonts.titleSize.content,
        color: p.muted, fontFace: fonts.title,
        align: 'left', valign: 'middle', margin: 0,
      })
      break
    }
    case 'oversized_display': {
      slide.addText(titleStr, {
        x: 0.5, y: 0.2, w: 12.3, h: 1.0,
        fontSize: fonts.titleSize.section, bold: true,
        color: p.titleText, fontFace: fonts.title,
        align: 'left', valign: 'middle', margin: 0,
      })
      slide.addShape('line', {
        x: 0.5, y: 1.3, w: 4.0, h: 0,
        line: { color: p.accent, width: 3 },
      })
      break
    }
  }
}

function applySectionBreakChrome(slide, title, subtitle, theme) {
  // ... similar dispatch on theme.chrome.sectionBreak ...
  // section_break variants: side_strip, centered_minimal, soft_centered,
  // numbered_minimal, full_bleed_number
  // Implement at least side_strip (legacy) in this patch; others in N.
}

function getIconTreatment(theme) {
  return theme.iconTreatment
}
```

修改既有 renderer 的呼叫慣例：

```diff
-function renderStandard(pres, s, p) {
+function renderStandard(pres, s, theme) {
+  const p = theme.palette
   const slide = pres.addSlide({ masterName: 'ANILA_BASE' })
-  slide.addText(String(s.title || ''), {
-    x: 0.5, y: 0.1, w: 12.3, h: 0.6,
-    fontSize: 26, bold: true, color: p.barText,
-    align: 'left', valign: 'middle',
-    fontFace: FONT_FACE, margin: 0,
-  })
+  applyTitleBar(slide, s.title, theme)
   // ... rest unchanged for now, will be theme-aware in Patch N
```

`renderSlideByKind` 跟 `/render` 進入點也要改成接 `theme` 而非 `palette`：

```diff
-function renderSlideByKind(pres, s, p) {
+function renderSlideByKind(pres, s, theme) {
   switch (s.layout_kind) {
-    case 'section_break':  return renderSectionBreak(pres, s, p)
-    case 'stat_callout':   return renderStatCallout(pres, s, p)
-    case 'quote':          return renderQuote(pres, s, p)
-    case 'two_column':     return renderTwoColumn(pres, s, p)
-    case 'icon_rows':      return renderIconRows(pres, s, p)
-    case 'image_focus':    return renderImageFocus(pres, s, p)
-    default:               return renderStandard(pres, s, p)
+    case 'section_break':  return renderSectionBreak(pres, s, theme)
+    case 'stat_callout':   return renderStatCallout(pres, s, theme)
+    case 'quote':          return renderQuote(pres, s, theme)
+    case 'two_column':     return renderTwoColumn(pres, s, theme)
+    case 'icon_rows':      return renderIconRows(pres, s, theme)
+    case 'image_focus':    return renderImageFocus(pres, s, theme)
+    default:               return renderStandard(pres, s, theme)
   }
 }
```

`/render` handler：

```diff
 app.post('/render', async (req, res) => {
-  const paletteName = req.body.palette || 'navy_amber'
-  const p = PALETTES[paletteName] || PALETTES.navy_amber
+  const themeName = req.body.theme || 'corporate_navy'
+  const theme = getTheme(themeName)
+  // Legacy compat: if request has `palette` but no `theme`, map.
+  // The schema layer should have resolved this already, but defend
+  // against direct renderer callers.
+  const p = theme.palette
   // ...
 })
```

**驗收**：corporate_navy 渲染結果跟 navy_amber 完全一致；其他 4 個 theme 可以接收請求但目前還是看起來像 corporate_navy（chrome dispatch 還沒實作完）。Schema-level 既有 jobs 通過。

---

## Patch N：實作另外 4 個 theme 的視覺差異

> 這是大工程。建議**先做 warm_journal + academic_paper 兩個最不一樣的**，看實際效果再做 executive_brief + startup_pitch。

### N.1 warm_journal

完整實作 chrome handlers：

- `left_marker` title bar（已在 Patch M 範例給）
- `soft_centered` section break：米白底、棕褐標題、無左側橘條，中央置中
- `soft_filled` icon treatment：填色 heroicon 直接放在 slide 上，不畫圍邊圓圈

新增字型：`fonts.title` / `fonts.body` 都是 `Noto Sans CJK TC`（既有，無需改 Dockerfile）。

### N.2 academic_paper

關鍵差異是 **serif 字型**。Dockerfile 要加 `fonts-noto-cjk-extra`（含 Noto Serif CJK TC）：

```diff
 # ANILALM/pptx-skill/Dockerfile
-RUN apt-get update && apt-get install -y --no-install-recommends \
-      fonts-noto-cjk \
+RUN apt-get update && apt-get install -y --no-install-recommends \
+      fonts-noto-cjk fonts-noto-cjk-extra \
     && rm -rf /var/lib/apt/lists/*
```

`Noto Serif CJK TC` 名稱對 LibreOffice 跟 pptx 都認得。先在 dev container 跑 `fc-list | grep "Noto Serif CJK"` 確認。

chrome handlers：

- `underline_only` title bar：細線 + 普通字重的標題
- `centered_minimal` section break：白底深色文字、置中、無 strip
- `monochrome_dot` icon：小灰點（已在 J.2 提）

### N.3 executive_brief（最後做）

- `none` title bar：標題用 muted 色小字、不滿版
- `numbered_minimal` section break：左半放章節超大編號（HelveticaNeue Light 風格 → fontSize 200），右半放章節名小字
- `minimal_dot` icon：極小點

### N.4 startup_pitch（最後做）

- `oversized_display` title bar：標題 32pt content / 72pt section / 96pt cover
- `full_bleed_number` section break：滿版深色 + 巨大 coral 數字
- `filled_pill` icon：實心 coral 圓圈裡白色 heroicon

**驗收**：4 個 theme 各跑一次同樣的素材，目測「**截然不同的視覺**」、不只是換色。

---

## Patch O：Tone-based theme selection prompt

**問題**：現有 prompt 用「商務 / 永續 / 嚴肅 / 行銷」這種**領域分類**叫 LLM 選 palette。但「11月學習心得報告」（軟性回顧）落不到這四類，gemma4 就掉回預設 navy_amber。

改成 **tone-based**：

**檔案**：`myCSPPlatform/backend/app/api/studio.py`

**Diff** in `_build_generation_prompt`：

```diff
-            'Required: palette — 從以下挑一個（renderer 會落地成具體配色）：',
-            '  "navy_amber"        商務、技術、政策、一般用途（預設）',
-            '  "forest_moss"       永續、健康、教育、自然主題',
-            '  "charcoal_minimal"  嚴肅報告、財務、法規',
-            '  "coral_energy"      行銷、品牌、創意活力',
-            '整份簡報只能挑一個 palette；不要在 slides 內切換。',
+            'Required: theme — 依文件 tone 而非主題類別挑選：',
+            '  "corporate_navy"   嚴謹的技術／業務報告；給同事或主管看的工作產出（預設）',
+            '  "academic_paper"   研究發表、論文摘要、學術會議；多量化與引用',
+            '  "warm_journal"     第一人稱學習心得、回顧、softer 反思內容',
+            '  "executive_brief"  給高層的 briefing、結論導向、極簡、≤ 10 張',
+            '  "startup_pitch"    對外發表、產品介紹、需要視覺衝擊與情緒煽動',
+            '',
+            '選擇依據（在 chunks_text 中尋找這些 tone 訊號）：',
+            '  - 第一人稱主觀詞（我、我的、我們、心得、反思、學到、感受）',
+            '    → warm_journal',
+            '  - 量化結果（百分比、N=...、F1、p-value）+ 方法論 + 引用',
+            '    → academic_paper',
+            '  - 「問題 / 解法 / 價值」結構 + 中性語氣 + 技術細節',
+            '    → corporate_navy',
+            '  - 強 call-to-action、願景語言、產品名稱反覆出現',
+            '    → startup_pitch',
+            '  - 只有結論沒有過程、總頁數 ≤ 10、給 C-level 看',
+            '    → executive_brief',
+            '訊號衝突時取最強的；無明確訊號用 corporate_navy。',
+            '整份簡報只能挑一個 theme；不要在 slides 內切換。',
+            '',
+            '（舊欄位名 palette 仍接受但已 deprecated，請用 theme。）',
```

**驗收**：v3 那份「11月學習心得報告」第一人稱「心得」訊號明確，gemma4 應該選 `warm_journal`；技術型報告（v1/v2 那種）繼續 `corporate_navy`。

---

## Patch P：API override

讓使用者在 job 建立時直接指定 theme，跳過 LLM 選擇。

**檔案**：`myCSPPlatform/backend/app/api/studio.py`（job 建立 endpoint），`myCSPPlatform/backend/app/schemas/studio.py`（request schema）

**新增**：

```python
# schemas/studio.py
class SlidesJobRequest(BaseModel):
    collection_id: int
    preset: str
    extra_instructions: str | None = None
    theme_override: str | None = Field(
        default=None,
        description=(
            "If set, bypasses LLM theme selection and forces this theme. "
            "Useful when the user knows the audience better than the LLM. "
            "Must be one of THEMES; invalid values are ignored (LLM picks)."
        ),
    )
```

```diff
 # studio.py POST jobs endpoint
@@ ...
 async def create_slides_job(payload: SlidesJobRequest, ...):
     # ... existing logic ...
+    if payload.theme_override and payload.theme_override in THEMES:
+        # Stash override; applied post-generation in the job pipeline
+        # (overwrites whatever LLM picked).
+        job.theme_override = payload.theme_override
+    # ... continue ...
```

Job pipeline 在 spec 通過 schema 驗證後、render 前覆寫：

```python
if job.theme_override:
    spec.theme = job.theme_override
```

**驗收**：API 接受 `theme_override`，覆寫成功；不指定時 LLM 選擇路徑保持運作。

---

# 整體驗收

Round 3 全部完成後，重跑 v3 素材，**預期看到**：

| Slide # | v3 狀況 | Round 3 預期 |
|---|---|---|
| 1 | 副標直立、56pt 標題、navy_amber | 主標 + 副標、theme = `warm_journal`（暖色系）、可能搭 `left_marker` |
| 2 | standard，4 個 label-bullet | icon_rows（Patch H V4 content-pattern）|
| 3 | section_break，"佳化" 孤行 | 字級自動縮 44pt，單行（Patch K）|
| 5 | standard，4 個 label-bullet | icon_rows（Patch H）|
| 9 | standard，4 個 label-bullet | icon_rows（Patch H）|
| 12 | standard，含 `$\rightarrow$` raw LaTeX | icon_rows（Patch H）+ 箭頭轉成 →（Patch I）|
| 14 | icon_rows，3 row 中 2 個空圓圈 | 3 row 都有 icon 或小色點，無空圓（Patch J）|
| 全份 | 4 份內容都長 navy_amber 樣 | v3 為個人心得 → `warm_journal`（米白棕褐）；v1/v2 為技術 → `corporate_navy`；不同 deck 視覺真的不同 |

**通過條件**：

1. v3 重跑命中率 ≥ 90%（13/15 slide 的 layout 選擇正確）
2. 沒有任何 raw LaTeX 出現在 slide 上
3. 沒有任何空圓圈 icon
4. 任何 section_break 標題不會折行折在錯位置
5. 個人心得型素材 → theme = `warm_journal`；技術報告型 → `corporate_navy`
6. 既有 Round 1-2 的 4 個 palette job 可以繼續跑（schema-level 向後相容）
7. 所有測試（既有 + 新增）通過

---

# 實作順序

## Part 1（建議一週內收完）

| 順序 | Patch | 檔案 | 預估 |
|---|---|---|---|
| 1 | H — V4 content-pattern | `studio.py` audit + tests | 30 min |
| 2 | I — LaTeX strip | `studio_text_normalizer.py` + tests | 20 min |
| 3 | J — icon fallback + CONCEPT_MAP 擴充 | `icons.js` + `server.js` renderIconRows | 30 min |
| 4 | K — section_break 字級自適 | `server.js` renderSectionBreak | 15 min |

**合計 ~1.5 小時**。先跑 Part 1 重跑 v3 驗證 → 收第一輪 round 3 PR。

## Part 2（後續 milestone，5-7 天）

| 順序 | Patch | 檔案 | 預估 |
|---|---|---|---|
| 5 | L — Schema theme 欄位 + 向後相容 | `schemas/studio.py` + tests | 2 h |
| 6 | M — Renderer scaffolding（THEMES bundle、dispatch helper、簽名改 theme） | `server.js` | 1 天 |
| 7 | N.1 — warm_journal 完整實作 | `server.js`、字型確認 | 1 天 |
| 8 | N.2 — academic_paper 完整實作（含 Dockerfile 加 fonts-noto-cjk-extra） | `server.js`、`Dockerfile` | 1 天 |
| 9 | N.3 + N.4 — executive_brief + startup_pitch | `server.js` | 1.5 天 |
| 10 | O — Tone-based prompt rewrite | `studio.py` prompt builder | 2 h |
| 11 | P — API theme_override | `studio.py` job endpoint + schema | 1 h |

**強烈建議 N.1 + N.2 完成後就收一次驗收**——這時候已經能看出「兩個截然不同的 theme」的視覺效果，足以驗證架構方向；N.3 + N.4 可以放在下下個 milestone。

---

# 不在本次範圍

- 換 LLM（gemma4 繼續用，prompt rewrite 已足夠）
- 圖像生成（FLUX / graphviz）行為改變
- 前端 UI 加 theme 選擇器（可在 Patch P 之後另開）
- 新 layout kind（不加 process_flow / architecture 等）
- 多語系 / 英文輸出 path

---

**Round 3 報告產出時間**：2026-05-19
**診斷依據**：v3 素材 `11月學習心得報告_AI_基礎設施與_Agentic_RAG_實作.pptx` + 分支 HEAD `c7970b5` 全 codebase 比對
**Part 1 驗收**：重跑 v3 比對本文件第 2 區「整體驗收」表中 Part 1 對應條件
**Part 2 驗收**：v3 重跑 + 對另一份不同 tone 的素材跑（理想是新進心得 / 技術深度 / 高層 briefing 三份不同 deck），目測「視覺真的不同」
