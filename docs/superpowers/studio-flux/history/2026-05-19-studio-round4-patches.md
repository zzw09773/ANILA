# ANILA LM Studio — Round 4 補丁規格

> 對 `feature/studio-quality-fixes` HEAD `4f65bfa` 的後續修正。基於對 v4 (`11月學習心得報告_高效能推理引擎與_Agentic_RAG_實作.pptx`) 跟 tech_v3 (`新進人員心得與技術規劃報告__3_.pptx`) 兩份輸出的逐張比對。
>
> Round 3 達成的主要成就：**theme 切換真正生效**（同一個 codebase 對技術內容產出 corporate_navy、對心得內容產出 warm_journal、視覺語言徹底分流）。Round 4 是收尾工作，處理三個還沒解的 bug：
>
> 1. **H 漏審 image_focus** — V4 audit 只看 `standard` 投影片。gemma4 知道後就把該是 icon_rows 的內容塞給 image_focus + FLUX，產出像 tech_v3 slide 10 那種「Perception/Cognition/Action 三個彩色 capsule 配雜訊條碼」的圖。
> 2. **I 對 JSON-eaten `\r` 沒輒** — Patch I 的正則找字面 `\\rightarrow`，但 JSON parser 在反序列化時就把 `\r` 解成 CR 字元，到 normalize 已經是 `$<CR>ightarrow$`。v4 slide 11 還是看到 raw 殘骸。
> 3. **`(參 [N])` citation 灌到 slide 上** — RAG 引用標記出現在每一條 bullet 結尾，bullet 變長 30%、視覺被吃掉。應該搬到 speaker_notes 或拔掉。
>
> 另有一個小尾巴：warm_journal 的 icon fallback 點太大（0.6"），slide 8 第 3 row 那顆橘色填色圓視覺過於搶眼。
>
> 四個 patch 都獨立、合計 ~2 小時。建議順序 R → S → Q → T。

---

## 共用驗收素材

兩份輸入都要重跑：

```bash
# 1) 技術型（期望 corporate_navy 持平、slide 10 不再有 FLUX 雜訊）
curl -X POST .../jobs -d '{"collection_id": <技術型>, "preset": "經典報告結構"}'

# 2) 心得型（期望 warm_journal 持平、slide 11 LaTeX 修好、所有 bullet 不再有 (參 [N])）
curl -X POST .../jobs -d '{"collection_id": <心得型>, "preset": "經典報告結構"}'
```

每張投影片人工目測，重點檢查項：

- 任何 slide 不應有「彩色 capsule + 條碼雜訊」這種 FLUX 雜訊圖
- 沒有 raw `$\rightarrow$` 或 `$<換行>ightarrow$` 之類殘骸
- bullet 結尾沒有 `(參 [N])` 字串
- warm_journal slides 的 icon fallback 是小色點不是大色圓

---

## Patch R：V4 audit 延伸到 image_focus（FLUX disguise 防堵）

**問題**：tech_v3 slide 10「Agentic Workflow 執行流程」三條 bullet 都是 label-pattern（感知層 / 認知層 / 行動層），標題還有 `Workflow` + `流程` 雙重 V4 keyword 命中。但 layout 是 `image_focus`，配著 FLUX 生成的雜訊圖。**V4 完全沒 fire**，因為 `_audit_layout_distribution` 第一行就 `if s.layout_kind != "standard": continue`。

換句話說：gemma4 學會用 image_focus 繞開 V4。

**檔案**：`myCSPPlatform/backend/app/api/studio.py`

### R.1 audit 範圍擴大、但只審 FLUX path

把 V4 區段的 layout 過濾改成：

```diff
@@ -1780,7 +1780,17 @@ def _audit_layout_distribution(...):
     # ── V4: enumeration title OR label-pattern bullets + 3+ bullets + standard ──
     for i, s in enumerate(slides):
-        if s.layout_kind != "standard":
-            continue
+        # Round 4: V4 also catches image_focus slides whose image is from
+        # the FLUX path (image_kind == "illustration"). Slides with
+        # image_kind="diagram" (graphviz) or a real image_ref are kept —
+        # they have a legitimate visual asset. FLUX illustrations layered
+        # over label-pattern bullets are the "disguise" pattern: gemma4
+        # picks image_focus to escape audit, but the bullet shape is still
+        # icon_rows material and the FLUX image is usually garbage on
+        # abstract / process content.
+        layout = s.layout_kind
+        if layout == "standard":
+            pass  # original V4 path
+        elif layout == "image_focus" and getattr(s, "image_kind", None) == "illustration":
+            pass  # FLUX disguise path
+        else:
+            continue
         if len(s.bullets) < 3:
             continue
```

更新 violation detail 字串包含 layout 資訊，讓 rebalance prompt 有上下文：

```diff
@@ -1803,7 +1813,7 @@
         detail_bits = [
-            f"slide #{i}「{s.title}」: {len(s.bullets)} bullets, standard layout",
+            f"slide #{i}「{s.title}」: {len(s.bullets)} bullets, {layout} layout",
         ]
+        if layout == "image_focus":
+            detail_bits.append("FLUX disguise — label-bullets under illustration")
         if title_match:
             detail_bits.append(f"title-keyword '{matched_kw}'")
```

### R.2 Rebalance prompt 認得 image_focus disguise

`_rebalance_layouts` 內的 prompt 要追加一條規則：當 violation detail 含 "FLUX disguise" 時，**強制轉成 icon_rows、清掉 image_prompt / image_kind / image_ref 三個欄位**。

找 rebalance prompt builder（grep `_rebalance_layouts` 內的 system / user prompt 構造），加入：

```python
# 在 rebalance prompt 的規則段落加：
"""
規則 6（Round 4 新增）：image_focus 路徑被 V4 命中、detail 含 'FLUX disguise' 的投影片，
**必須**改成 icon_rows、不可保留 image_focus。輸出 spec 時：
- 設 layout_kind = "icon_rows"
- 清空 image_kind、image_prompt、image_ref 三個欄位（設為 null）
- 從 bullets 萃取每個 row 的 heading（冒號前）與 description（冒號後）
- 若 bullet 沒有冒號，整條當 description、heading 取 chunks 中合理的概念詞
"""
```

### R.3 schema 容許 image_focus 轉 icon_rows 時清空 image 欄位

確認 `schemas/studio.py` 的 `Slide` model 允許 `image_kind`、`image_prompt`、`image_ref` 為 None。應該已經是這樣（既有設計），這步只是 verify 不要加 `model_validator` 強制 image_focus 必須有 image 欄位。

### 測試

**檔案**：`myCSPPlatform/backend/tests/test_layout_audit.py`

```python
def test_v4_catches_image_focus_with_flux_disguise():
    """Round 4: gemma4 sometimes picks image_focus+FLUX to dodge V4.
    
    Regression guard for tech_v3 slide 10 (Agentic Workflow 執行流程)
    which had 3 label-pattern bullets and FLUX noise image.
    """
    spec = SlidesSpec(title="t", slides=[
        Slide(
            title="Agentic Workflow 執行流程",
            bullets=[
                "感知層：偵測異常並警報",
                "認知層：Agent 檢索維修手冊",
                "行動層：生成具體操作建議",
            ],
            layout_kind="image_focus",
            image_kind="illustration",
            image_prompt="three-stage agent workflow",
        ),
    ])
    violations = _audit_layout_distribution(spec, chunks_text="")
    v4s = [v for v in violations if v.kind == "V4"]
    assert len(v4s) == 1
    assert "FLUX disguise" in v4s[0].detail


def test_v4_does_not_fire_on_image_focus_with_diagram():
    """image_focus + graphviz diagram is legitimate, should NOT be V4."""
    spec = SlidesSpec(title="t", slides=[
        Slide(
            title="Multi-Agent Supervisor 拓撲設計",
            bullets=[
                "Supervisor: 意圖識別與路由",
                "Doc Worker: ltree 垂直檢索",
                "Aero Expert: 工程運算 (DATCOM)",
            ],
            layout_kind="image_focus",
            image_kind="diagram",
            diagram_dot="digraph G { Supervisor -> Worker }",
        ),
    ])
    violations = _audit_layout_distribution(spec, chunks_text="")
    # Title-keyword would match for standard slide, but layout is image_focus
    # with image_kind=diagram — should be exempt.
    v4s = [v for v in violations if v.kind == "V4"]
    assert len(v4s) == 0


def test_v4_does_not_fire_on_image_focus_with_real_image():
    """image_focus + image_ref (real retrieved image) is also legitimate."""
    spec = SlidesSpec(title="t", slides=[
        Slide(
            title="Agentic Workflow",
            bullets=["階段一: ...", "階段二: ...", "階段三: ..."],
            layout_kind="image_focus",
            image_ref="abc123def456",
        ),
    ])
    violations = _audit_layout_distribution(spec, chunks_text="")
    v4s = [v for v in violations if v.kind == "V4"]
    assert len(v4s) == 0
```

**驗收**：tech_v3 重跑後 slide 10 應該變成 icon_rows（三個 row：感知層 / 認知層 / 行動層 + 對應 icon），FLUX 雜訊圖消失。slide 14（Supervisor 拓撲）跟 slide 8（RAG 垂直切分架構）的 graphviz 圖**保留**，因為 image_kind=diagram。

---

## Patch S：LaTeX strip 處理 JSON-eaten 控制字元

**問題**：v4 slide 11 右欄出現：

```
實現「失敗 $
ightarrow$ 思考 $
ightarrow$ 重試」之韌性流程
```

`$\rightarrow$` 的 `\r` 被 JSON parser 解成 CR 字元（U+000D），到 `strip_latex` 跑的時候資料已經是 `$<CR>ightarrow$`。Patch I 的正則 `r"\$\\rightarrow\$"` 找字面 `\r`，**永遠 miss**。同樣的 bug 也可能發生在 `\t` (`\tightarrow$` → `$<TAB>ightarrow$`)、`\n` (`$<LF>ightarrow$`)。

**檔案**：`myCSPPlatform/backend/app/services/studio_text_normalizer.py`

**Diff**：

```diff
@@ -... (top of file, near _LATEX_REPLACEMENTS) ...

+# Round 4: JSON-eaten control-char fallbacks.
+#
+# When gemma4 emits LaTeX like "$\rightarrow$" in JSON string content,
+# the JSON parser interprets `\r`, `\t`, `\n` as actual control chars
+# (CR/TAB/LF) BEFORE this normalizer sees the text. The literal-LaTeX
+# regex in _LATEX_REPLACEMENTS never matches because the backslash is
+# already gone. These string-level replacements catch the post-parse
+# residue. Order matters: catch the broken variant first, then the
+# literal one (which only fires if JSON wasn't involved, e.g. direct
+# Python string input in tests).
+_LATEX_BROKEN_CHAR_REPLACEMENTS = {
+    # \r → CR (most common with $\rightarrow$ in JSON)
+    "$\rightarrow$": "→",   # actual CR between $ and "ightarrow"
+    "$\rightarrow":  "→",   # no closing $
+    # \t → TAB (rare, but $\to$ shares the same vulnerability via \t prefix)
+    "$\tightarrow$": "→",
+    "$\tightarrow":  "→",
+    # \n → LF (with $\n... patterns)
+    "$\nightarrow$": "→",
+    "$\nightarrow":  "→",
+    # \v (vertical tab), \f (form feed), \b (backspace) — same family
+    "$\vightarrow$": "→",
+    "$\fightarrow$": "→",
+    "$\bightarrow$": "→",
+    # Backslash-eaten Greek letters via control chars (less common but observed)
+    # `\theta` → CR + "heta" if JSON parses `\t` first
+    "$\theta$":     "θ",    # $ + TAB + "heta" + $
+    "$\nu$":        "ν",    # $ + LF + "u" + $
+}
+
 _LATEX_REPLACEMENTS = {
     # Known literal commands (only fires if JSON didn't eat the backslash —
     # e.g. when LLM output goes through a path that double-escapes, or in
     # unit tests with raw Python strings).
     r"\$\\rightarrow\$": "→",
     # ... existing entries unchanged ...
 }
```

修改 `strip_latex` 函式：

```diff
 def strip_latex(text: str) -> str:
     if not text:
         return text
     text = str(text)
+    # Round 4: handle JSON-eaten control chars FIRST. These are not regex
+    # patterns — they're literal string replacements because the broken
+    # characters are real CR/TAB/LF in the data.
+    for broken, fixed in _LATEX_BROKEN_CHAR_REPLACEMENTS.items():
+        text = text.replace(broken, fixed)
     for pattern, replacement in _LATEX_REPLACEMENTS.items():
         text = re.sub(pattern, replacement, text)
     text = _GENERIC_LATEX_RE.sub(r"\1", text)
     return text
```

### 測試

**檔案**：`myCSPPlatform/backend/tests/test_studio_text_normalizer.py`

```python
def test_strip_latex_json_eaten_carriage_return():
    """Round 4: regression guard for v4 slide 11.
    
    When JSON content has "$\\rightarrow$", the JSON parser interprets
    `\\r` as CR. The text reaching strip_latex looks like
    "$<CR>ightarrow$" not "$\\rightarrow$".
    """
    # Simulate post-JSON-parse string with actual CR character
    broken = "失敗 " + "$" + "\r" + "ightarrow$" + " 思考"
    assert strip_latex(broken) == "失敗 → 思考"


def test_strip_latex_json_eaten_tab():
    broken = "A " + "$" + "\t" + "ightarrow$" + " B"
    assert strip_latex(broken) == "A → B"


def test_strip_latex_json_eaten_linefeed():
    broken = "A " + "$" + "\n" + "ightarrow$" + " B"
    assert strip_latex(broken) == "A → B"


def test_strip_latex_handles_unclosed_broken_variant():
    """$\\rightarrow without closing $ — happens when LLM truncates."""
    broken = "失敗 " + "$" + "\r" + "ightarrow" + " 後續..."
    assert strip_latex(broken) == "失敗 → 後續..."


def test_strip_latex_literal_path_still_works():
    """Patch I behaviour preserved for non-JSON paths (e.g. direct tests)."""
    text = r"Observation $\rightarrow$ Thought"
    assert strip_latex(text) == "Observation → Thought"
```

**驗收**：v4 slide 11 右欄顯示「失敗 → 思考 → 重試」、無殘留錢字符或 control char artifact。

---

## Patch Q：剝掉 bullet 結尾的 `(參 [N])` citation 標記

**問題**：v4 每一條 bullet 結尾都帶 `(參 [5])`、`(參 [7])` 這種 RAG citation 標記，視覺上灌水、字數 +30%。對讀者沒實際意義（讀者看 slide 上不知道 [5] 是哪個 chunk），對開發/稽核者來說也應該在 speaker_notes 或 metadata、不是 visible content。

**檔案**：`myCSPPlatform/backend/app/services/studio_text_normalizer.py`

**新增**：

```python
# Round 4: RAG citation markers at end of bullets.
#
# Pattern variants observed in production:
#   "(參 [5])"           half-width parens, half-width brackets
#   "（參 [5]）"         full-width parens, half-width brackets
#   "(參 [12])"          multi-digit
#   "( 參 [5] )"         with internal whitespace
#   "(參考 [5])"         alternative wording (rare)
#
# These belong in speaker_notes (already populated by the LLM with
# chunk reference info), not on the visible slide. Strip end-of-string
# occurrences; intra-text citations like "如 (參 [5]) 所述" are rare
# and harder to safely auto-strip, so we leave them.
_CITATION_RE = re.compile(
    r"\s*[\(（]\s*參(?:考)?\s*[\[【]\s*\d+\s*[\]】]\s*[\)）]\s*$",
)


def strip_inline_citations(text: str) -> str:
    """Remove RAG citation markers (e.g. '(參 [5])') from end of text.

    The Studio LLM is instructed to cite sources for traceability; those
    citations are useful in `speaker_notes` for audit but bloat the visible
    slide. Strip end-anchored occurrences only — defensive about touching
    intra-text references.

    Pure function. Idempotent. Safe on empty input.
    """
    if not text:
        return text
    text = str(text)
    # Strip up to 3 trailing citation tokens (some bullets cite multiple
    # chunks: "...部署 (參 [5]) (參 [10])"). 3 is plenty in practice.
    for _ in range(3):
        new = _CITATION_RE.sub("", text).rstrip()
        if new == text:
            break
        text = new
    return text
```

整合進 normalize pipeline。找 `normalize_text` (或主 entry function) 並加：

```diff
 def normalize_text(text: str) -> str:
     # ... existing transforms ...
+    text = strip_inline_citations(text)
     text = strip_latex(text)
     # ... rest unchanged ...
```

順序重要：先剝 citation 再 strip latex，因為 citation 在尾端、不會影響 LaTeX pattern。

### 套用範圍

確認所有走 normalize 的 slide 欄位都會經過：
- `Slide.title`
- `Slide.bullets[i]`
- `Stat.value`, `Stat.label`, `Stat.supporting`
- `Column.heading`, `Column.bullets[i]`
- `IconRow.heading`, `IconRow.description`
- `Quote.text`, `Quote.attribution`

**不要**剝 `Slide.speaker_notes` 裡的 citation——那邊保留是合規/稽核用途。

### 測試

**檔案**：`myCSPPlatform/backend/tests/test_studio_text_normalizer.py`

```python
def test_strip_citation_basic_half_width():
    assert strip_inline_citations("完成部署 (參 [5])") == "完成部署"

def test_strip_citation_full_width_parens():
    assert strip_inline_citations("完成部署（參 [5]）") == "完成部署"

def test_strip_citation_multi_digit():
    assert strip_inline_citations("實作 (參 [12])") == "實作"

def test_strip_citation_with_internal_whitespace():
    assert strip_inline_citations("成果 ( 參 [5] )") == "成果"

def test_strip_citation_multiple_at_end():
    """Some bullets cite multiple chunks consecutively."""
    assert strip_inline_citations("整合方案 (參 [5]) (參 [10])") == "整合方案"

def test_strip_citation_alternative_wording():
    assert strip_inline_citations("結論 (參考 [3])") == "結論"

def test_strip_citation_intra_text_preserved():
    """Intra-text citations are NOT stripped — too risky for false positives."""
    txt = "如 (參 [5]) 所述，我們完成了部署"
    assert strip_inline_citations(txt) == "如 (參 [5]) 所述，我們完成了部署"

def test_strip_citation_no_citation_passes_through():
    assert strip_inline_citations("純粹陳述沒有引用") == "純粹陳述沒有引用"
    assert strip_inline_citations("") == ""
    assert strip_inline_citations(None) is None

def test_strip_citation_v4_journal_regression():
    """Exact bullet text from v4 slide 2."""
    input_text = "完成 gpt-oss-20b 與 NV-Embed-v2 之容器化部署，針對 GH200 進行記憶體調優 (參 [5])"
    expected = "完成 gpt-oss-20b 與 NV-Embed-v2 之容器化部署，針對 GH200 進行記憶體調優"
    assert strip_inline_citations(input_text) == expected
```

**驗收**：v4 重跑，所有 bullet 不再以 `(參 [N])` 結尾；speaker_notes 仍保留來源追蹤資訊。

---

## Patch T：warm_journal icon fallback dot 縮小

**問題**：v4 slide 8 第 3 row「長文字迷失」是 unknown concept，Patch J 用 fallback 路徑畫小色點代替空圓圈。但 warm_journal theme 的 `iconTreatment.iconSize: 0.6`（給實際 heroicon glyph 用），fallback 路徑直接套用這個 size 結果畫了個 0.6" 大色圓，視覺過於搶眼、像個「未渲染狀態」。

**檔案**：`ANILALM/pptx-skill/server.js`

找 renderIconRows 內的 fallback 分支（grep `unknown concept` 或 J 標記），修：

```diff
 } else {
-  // Unknown concept: small filled dot. Aligned with where the centre of
-  // the circle would have been so heading/description offsets don't move.
+  // Round 4: fallback dot must NOT use theme.iconTreatment.iconSize —
+  // that size is calibrated for actual heroicon glyphs (e.g. warm_journal
+  // uses 0.6" for a soft_filled icon). For an unknown-concept marker, a
+  // tiny accent dot is the right semantic ("this is a list item, no
+  // specific concept available"). Hard-coded 0.12" regardless of theme.
+  const FALLBACK_DOT_SIZE = 0.12
+  const cx = x + (iconTreatment.circleSize || 0.8) / 2
+  const cy = y + (iconTreatment.circleSize || 0.8) / 2
   slide.addShape('ellipse', {
-    x: x + 0.35, y: y + 0.35, w: 0.1, h: 0.1,
+    x: cx - FALLBACK_DOT_SIZE / 2,
+    y: cy - FALLBACK_DOT_SIZE / 2,
+    w: FALLBACK_DOT_SIZE, h: FALLBACK_DOT_SIZE,
     fill: { color: theme.accent },
     line: { type: 'none' },
   })
   console.warn(`[icon_rows] unknown concept "${row.concept}" — drew dot`)
 }
```

> 注意：以上 diff 是示意。實際 `renderIconRows` 內變數名稱可能不同（可能是 `palette.accent` 而非 `theme.accent`），請對應實際命名套用。

### 驗收

v4 slide 8 第 3 row 的 icon 位置出現一個小色點（約 0.12"），跟另外兩 row 的填色 icon 視覺重量平衡、不會搶戲。

---

## 整體驗收

跑完 R、S、Q、T 後，重跑兩份素材：

### tech_v3 期望

| Slide | v4 狀況 | Round 4 預期 |
|---|---|---|
| 10 | image_focus + FLUX 三 capsule 雜訊圖 | icon_rows，三 row（感知層 / 認知層 / 行動層）+ 對應 icon |
| 8 | image_focus + graphviz（保留）| 保持 image_focus + graphviz（不應被 R 誤傷）|
| 14 | image_focus + graphviz（保留）| 保持 image_focus + graphviz |

### v4 journal 期望

| Slide | v4 狀況 | Round 4 預期 |
|---|---|---|
| 8 第 3 row | 大顆橘色填色圓 | 小顆橘色點 |
| 11 右欄 | `失敗 $<CR>ightarrow$ 思考` | `失敗 → 思考 → 重試` |
| 所有 bullet | 結尾 `(參 [N])` | 拔掉 |

### 通過條件

1. **無任何 FLUX 雜訊圖**：所有 image_focus slide 要嘛走 graphviz、要嘛被 R 改成 icon_rows
2. **無任何 raw LaTeX 殘骸**：含控制字元變種
3. **無任何 bullet 結尾 `(參 [N])`**：speaker_notes 仍保留 traceability
4. **warm_journal fallback dot ≤ 0.15"**：跟既有 icon 視覺重量平衡
5. **既有 graphviz path 完全不受影響**：R 的 audit 過濾要正確識別 `image_kind == "diagram"`
6. **既有測試全綠 + 新增測試全綠**：`pytest myCSPPlatform/backend/tests/ -v`

---

## 實作順序

| 順序 | Patch | 檔案 | 預估 |
|---|---|---|---|
| 1 | S — JSON-eaten LaTeX 控制字元 fallback | `studio_text_normalizer.py` + tests | 30 min |
| 2 | Q — citation strip | `studio_text_normalizer.py` + tests | 30 min |
| 3 | T — fallback dot 縮小 | `server.js` | 15 min |
| 4 | R — V4 audit 延伸到 image_focus + rebalance prompt 規則 6 | `studio.py` audit + rebalance + tests | 1 h |

S/Q/T 三個獨立、改動小，先做掉立刻能驗收。R 比較複雜（要連動 rebalance prompt），最後做。

合計 **~2 小時 15 分鐘**。

---

## 不在本次範圍

- 新增 layout kind
- 換 LLM 或 prompt 結構大改
- 前端 UI 變更
- Slide 11 那種「3 bullets 中 1 條無冒號」的不均勻結構問題（這是 content-layer 問題，要回去改生成 prompt 的 bullet shape 規則，留到未來 Round）

---

**Round 4 報告產出時間**：2026-05-19
**診斷依據**：v4 journal + tech_v3 兩份輸出 + 分支 HEAD `4f65bfa` codebase 比對 + 實際 regex 測試
**驗收**：兩份素材重跑、對照本文「整體驗收」表 + 通過條件
