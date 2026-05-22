# ANILA LM Studio — Round 5 補丁規格

> 對 `feature/studio-quality-fixes` HEAD `3aee569` 的後續修正。基於對 v5 (`11月學習心得報告_高效能推理引擎與合規_AI_實踐.pptx`) 跟 tech_v4 (`新進人員心得與技術規劃報告__4_.pptx`) 兩份輸出的逐張比對。
>
> Round 4 大部分 patch 生效（Q citation strip 乾淨、R 把 FLUX disguise 改成 graphviz 確實有作用），但 v5 暴露出三個獨立問題：
>
> 1. **Theme 在邊界 case 抖動**：v4 journal 選了 warm_journal、v5 journal 同類內容選了 corporate_navy。tone-based 偵測對 marginal 內容 non-deterministic。
> 2. **Patch S 沒覆蓋 graphviz DOT 字串**：tech_v4 slide 10 graphviz 圖內第一個橢圓寫著 `ightarrow$ Alert`——LaTeX 控制字元殘骸殘留在 `diagram_dot` field，因為這個 field 沒過 `normalize_text`。
> 3. **Patch H rebalance 從 Round 2 到 v5 還是沒生效**：journal_v5 slide 2 標準 label-bullet 結構，audit 應該抓到 V4 但實際沒被改成 icon_rows。猜了好幾次根因，沒有 log 就只能盲試。
>
> Round 5 三個 patch 都很小，**重點不在程式碼量、在 architectural correctness**：U 把 theme 從機率系統變回 deterministic、S-fix 補一個漏掉的 normalize 入口、H-diag 先收集資料再決定 H 怎麼修。

---

## 共用驗收素材

```bash
# 1) 心得型（期望 warm_journal 不再抖動）
curl -X POST .../jobs -d '{"collection_id": <心得>, "preset": "經典報告結構"}'

# 2) 技術型（期望 graphviz 圖內無 LaTeX 殘骸）
curl -X POST .../jobs -d '{"collection_id": <技術>, "preset": "經典報告結構"}'

# 跑完後撈 H-diag log（即使沒人工檢查也要存起來）
docker logs <csp-backend> 2>&1 | grep -E "rebalance|H-DIAG|RebalanceChange" > /tmp/round5-rebalance.log
```

---

## Patch U：Title-keyword theme override（最重要）

### 問題

journal v4（warm_journal） vs journal v5（corporate_navy）內容幾乎一樣，theme 卻不同。看 v5 chunks 的訊號分佈：

| 訊號 | warm_journal 計數 | corporate_navy 計數 |
|---|---|---|
| 第一人稱「我/我們」 | 0 | — |
| 「心得」keyword | 1（在 title）| — |
| 「問題-解法-價值」結構 | — | 4+ 條 bullet 都符合 |
| 中性技術詞彙 | — | 滿坑滿谷 |

按 Patch O 的 prompt 規則「**訊號衝突時取最強的**」，corporate_navy 是合理選擇。但這跟使用者意圖相反——**寫「11月學習心得報告」的人就是要呈現心得調性，不管 chunks 多技術**。

根本問題：**LLM 在邊界 case 的判斷不可重現**。同樣 collection 跑兩次可能拿到不同 theme。對「報告系統」這種期望可預期輸出的工具，這是 architectural 缺陷。

### 解法：title 強訊號做 deterministic override

Title 是作者**明確的框架選擇**（framing decision），優先於 chunks 的 subject-matter 推測。把這層 routing 從 prompt 規則（軟性）改成程式碼 routing（硬性）。

### 檔案

`myCSPPlatform/backend/app/api/studio.py`

### Diff

在檔案頂部、靠近其他常數的地方加：

```python
# ── Round 5: deterministic title-keyword theme overrides ──
#
# LLM tone detection (Patch O) is non-deterministic at signal boundaries.
# v4 picked warm_journal for "11月學習心得報告"; v5 picked corporate_navy
# on essentially the same content because chunks lean technical and the
# only warm_journal signal was the "心得" in the title.
#
# Architectural decision: title is the strongest author-intent signal —
# the framing the author explicitly chose. When title contains an
# unambiguous theme keyword, override the LLM's tone-based choice.
#
# Patterns are deliberately CONSERVATIVE (only high-confidence keywords).
# Title with no match leaves the LLM's choice intact. This is opt-in
# overriding, not blanket replacement.
_THEME_TITLE_OVERRIDES: list[tuple[re.Pattern[str], str]] = [
    # ── warm_journal: personal reflection framings ──
    (re.compile(r"心得|反思|回顧|感想|札記|手記"), "warm_journal"),

    # ── academic_paper: scholarly/conference framings ──
    (re.compile(
        r"論文|研究發表|期刊論文|workshop|conference paper|"
        r"研討會|學會發表"
    ), "academic_paper"),

    # ── startup_pitch: external pitch framings ──
    (re.compile(
        r"募資|產品發表|launch event|pitch deck|"
        r"投資人簡報|demo day"
    ), "startup_pitch"),

    # ── executive_brief: high-level briefing framings ──
    (re.compile(
        r"executive briefing|高層 review|主管 briefing|"
        r"季度 review|半年檢討|年度檢討"
    ), "executive_brief"),
]


def _apply_theme_title_override(spec: SlidesSpec) -> SlidesSpec:
    """Deterministic title-keyword override for theme selection.

    Runs AFTER schema validation succeeds so `spec.theme` is always a
    valid theme (either LLM-chosen or palette-derived). If `spec.title`
    matches a high-confidence keyword pattern, force the corresponding
    theme.

    Idempotent and pure: same input → same output, no I/O beyond logging.
    No-op when title has no match (preserves LLM choice).
    """
    title = (spec.title or "").strip()
    if not title:
        return spec

    for pattern, target_theme in _THEME_TITLE_OVERRIDES:
        if pattern.search(title):
            if spec.theme != target_theme:
                logger.info(
                    "Theme title-override: '%s' matched %r → "
                    "switching theme %s → %s",
                    title, pattern.pattern, spec.theme, target_theme,
                )
                spec.theme = target_theme
            else:
                logger.debug(
                    "Theme title-override: '%s' matched %r, "
                    "theme already %s (no-op)",
                    title, pattern.pattern, target_theme,
                )
            return spec

    return spec
```

### 套入 pipeline

找 spec 通過 Pydantic 驗證之後的第一個 post-processing call（應該是 `_patch_studio_saturation` 附近）。把 `_apply_theme_title_override` 排在 **saturation 之前**，這樣後續所有 step 都看到正確的 theme：

```diff
@@ -... in the job pipeline ...
 try:
     spec = SlidesSpec.model_validate(spec_dict)
 except ValidationError as exc:
     # ... existing fallback ...

+# Round 5: deterministic title-keyword theme override.
+# Runs first so saturation / audit / rebalance / render all see the
+# correct theme. Idempotent if LLM already picked correctly.
+spec = _apply_theme_title_override(spec)
+
 # Existing saturation pass:
 spec_dict_patched = _patch_studio_saturation(
     spec.model_dump(mode="json"), chunk_filenames
 )
```

### Prompt 註記（選做但建議）

在 `_build_generation_prompt` 的 theme 段落結尾加一行說明，讓 LLM 知道 title 有 hard override，不要花力氣對抗：

```diff
@@ -460,4 +460,6 @@
             '訊號衝突時取最強的；無明確訊號用 corporate_navy。',
             '整份簡報只能挑一個 theme；不要在 slides 內切換。',
+            '',
+            '（注意：若 title 含「心得/反思/論文/募資」等明確 framing 詞，後端會強制 override；你可不必額外處理。）',
             "",
```

### 測試

**檔案**：`myCSPPlatform/backend/tests/test_theme_override.py`（新增）

```python
import pytest

from myCSPPlatform.backend.app.api.studio import _apply_theme_title_override
from myCSPPlatform.backend.app.schemas.studio import Slide, SlidesSpec


def _make_spec(title: str, theme: str = "corporate_navy") -> SlidesSpec:
    return SlidesSpec(
        title=title,
        theme=theme,
        slides=[Slide(title="t", bullets=["x"], layout_kind="standard")],
    )


@pytest.mark.parametrize("title,expected", [
    # warm_journal
    ("11月學習心得報告", "warm_journal"),
    ("Q3 工作反思紀錄", "warm_journal"),
    ("年度回顧與展望", "warm_journal"),
    ("實習感想分享", "warm_journal"),

    # academic_paper
    ("關於 LLM 的研討會論文摘要", "academic_paper"),
    ("Workshop 投稿稿件", "academic_paper"),

    # startup_pitch
    ("產品發表簡報", "startup_pitch"),
    ("Series A 募資簡報", "startup_pitch"),

    # executive_brief
    ("Q3 高層 review", "executive_brief"),
    ("半年檢討會議簡報", "executive_brief"),
])
def test_title_override_forces_theme(title: str, expected: str):
    """Title with high-confidence keyword should force target theme."""
    spec = _make_spec(title, theme="corporate_navy")  # start as default
    result = _apply_theme_title_override(spec)
    assert result.theme == expected


def test_no_match_preserves_llm_choice():
    """Title without keyword leaves LLM's theme choice intact."""
    spec = _make_spec("CNC 鐵屑辨識技術規劃", theme="academic_paper")
    result = _apply_theme_title_override(spec)
    assert result.theme == "academic_paper"  # not overridden


def test_override_idempotent_when_theme_matches():
    """If LLM already chose the right theme, no change but no error."""
    spec = _make_spec("學習心得", theme="warm_journal")
    result = _apply_theme_title_override(spec)
    assert result.theme == "warm_journal"


def test_v5_regression():
    """Exact title from v5 that regressed. Must override to warm_journal."""
    spec = _make_spec("11月學習心得報告", theme="corporate_navy")
    result = _apply_theme_title_override(spec)
    assert result.theme == "warm_journal"


def test_empty_title_safe():
    spec = _make_spec("", theme="corporate_navy")
    result = _apply_theme_title_override(spec)
    assert result.theme == "corporate_navy"


def test_first_match_wins():
    """When title contains keywords from multiple patterns, first wins.
    
    Order: warm_journal > academic_paper > startup_pitch > executive_brief.
    """
    # Contrived title with both warm_journal and academic_paper keywords
    spec = _make_spec("心得論文發表", theme="corporate_navy")
    result = _apply_theme_title_override(spec)
    assert result.theme == "warm_journal"  # earlier in list wins
```

### 驗收

- v5 心得型素材重跑 → theme 必為 warm_journal（不再抖動）
- 跑 5 次以上同 collection 確認穩定（每次都 warm_journal）
- 技術型素材（title 無 keyword）→ theme 保持 LLM 選的 corporate_navy
- 既有測試（`test_theme_resolves_from_legacy_palette` 等）全綠

### 為什麼這條這麼重要

「為什麼這次又長一樣？」這個感覺，是因為**使用者預期 deterministic 工具**。一個會抖動的工具不是工具、是賭場。Patch U 把 theme 從 LLM 推論變成 schema-level routing decision，從這個改動之後，「同個 title 永遠走同個 theme」——這是 architectural correctness 的勝利，比任何視覺修正都重要。

---

## Patch S-fix：normalize `diagram_dot` 字串

### 問題

tech_v4 slide 10 graphviz 圖內第一個橢圓寫著：

```
感知 (偵測鐵屑 $
ightarrow$ Alert)
```

`$\rightarrow$` 的 `\r` 被 JSON parser 解成 CR，殘骸留在 graphviz 渲染後的圖裡。

Patch S（Round 4）已經把 `_LATEX_BROKEN_CHAR_REPLACEMENTS` 加進 `strip_latex`，但 `strip_latex` 只在 `normalize_text` 的入口跑，而 `normalize_text` 只覆蓋 **slide-level text fields**：title、bullets、stat.*、column.*、icon_rows.*。

**`diagram_dot` 是另一條路徑**——LLM 把 DOT 字串塞進這個 field，hydration code 直接送進 `dot` subprocess，沒有經過任何文字 normalizer。

### 解法

在 graphviz hydration 之前對 `diagram_dot` 跑同一個 `strip_latex` + `strip_inline_citations`。

### 檔案

`myCSPPlatform/backend/app/services/diagram_renderer.py`（或不論這個 service 叫什麼，總之就是把 `diagram_dot` 送進 `dot` binary 之前的那個 function）

### Diff

```python
# diagram_renderer.py 頂部 import
from myCSPPlatform.backend.app.services.studio_text_normalizer import (
    strip_latex,
    strip_inline_citations,
)


async def render_dot_to_png(dot: str) -> bytes | None:
    """Render a graphviz DOT string to PNG bytes via the `dot` subprocess.
    
    Round 5: pre-process DOT text through the same normalizer as
    slide-level fields. LLM occasionally emits LaTeX (`$\\rightarrow$`)
    inside node labels; the JSON parser eats the backslash; the residue
    ends up rendered inside graphviz boxes as gibberish ("ightarrow$").
    """
    if not dot:
        return None

    # Round 5: normalize text BEFORE feeding to dot. Same passes as
    # studio_text_normalizer applies to slide-level fields.
    dot = strip_inline_citations(dot)
    dot = strip_latex(dot)

    # ... existing render logic (subprocess + timeout) ...
```

### 為什麼不只在 strip_latex 本身做更多事

可能會問：能不能在 `strip_latex` 內偵測「我是不是在處理 DOT 字串、要保留更多語法」？**不行**。這條路會讓 normalizer 變脆弱。乾淨架構是：normalizer 對「給人看的純文字」做轉換，每個欄位**主動呼叫** normalizer，不要讓 normalizer 猜上下文。`diagram_dot` 是新的欄位類型 → 它要負責自己 opt-in normalize。

### 測試

**檔案**：`myCSPPlatform/backend/tests/test_diagram_renderer.py`

```python
def test_diagram_dot_strips_broken_latex_before_render():
    """Round 5: diagram_dot must pass through strip_latex.
    
    Regression guard for tech_v4 slide 10 where 'ightarrow$' showed up
    in graphviz output because $\\rightarrow$ wasn't normalized.
    """
    # Simulate what reaches render_dot_to_png after JSON parse
    broken_dot = (
        'digraph G {\n'
        '  perception [label="感知 (偵測 $\rightarrow Alert)"];\n'
        '  cognition  [label="認知 (RAG 檢索)"];\n'
        '  action     [label="行動 (生成建議)"];\n'
        '  perception -> cognition -> action;\n'
        '}\n'
    )
    # Don't actually run dot; mock subprocess and just verify the input
    # passed to it.
    with patch("myCSPPlatform.backend.app.services.diagram_renderer.subprocess") as m:
        m.run.return_value.stdout = b"FAKE_PNG"
        m.run.return_value.returncode = 0
        asyncio.run(render_dot_to_png(broken_dot))

    actual_dot_passed = m.run.call_args.kwargs.get("input") or m.run.call_args.args[0]
    # The broken LaTeX must be gone before reaching dot
    assert "ightarrow" not in str(actual_dot_passed)
    assert "→" in str(actual_dot_passed)
```

### 驗收

- 重跑 tech_v4 素材，slide 10 graphviz 橢圓內顯示「感知 (偵測 → Alert)」、不再有 `ightarrow$` 殘骸
- 既有 graphviz 路徑測試全綠
- 任何含 `(參 [N])` 的 DOT label 也會被剝乾淨

---

## Patch H-diag：為 rebalance 加觀測

### 問題

Patch H 從 Round 3 到 v5 都沒徹底生效。journal_v5 slide 2：

```
title: "執行摘要"
bullets:
  - "技術掌握：克服 TensorRT-LLM 曲線，實現高效部署"
  - "核心突破：設計法律 Agentic RAG，解決「迷失中間」現象"
  - "底層除錯：分析 C++ 記憶體對齊瑕疵，確保系統穩定"
  - "合規實踐：導入 ISO 42001，落實「開發即合規」理念"
layout_kind: "standard"
```

`_LABEL_BULLET_RE` 對這四條 bullet 100% match（我跑過驗證）。所以 audit 階段 **V4 必定 fire**、`_should_rebalance` 必定回 True、`_rebalance_layouts` 必定被呼叫。但結果是 slide 還是 standard。

**中間哪裡掉了不知道**。猜了好幾次（IconRow.concept 漏填？rebalance LLM 拒絕修改？Pydantic validation fail？）但沒有 log 證實。

### 解法

**先觀測、再修補**。加詳細 logging 到 rebalance pipeline，跑一次完整 job，撈 log 看真實發生什麼。Round 5 的這個 patch **不修任何邏輯**，只增加可觀測性。

### 檔案

`myCSPPlatform/backend/app/api/studio.py`

### 該加 log 的位置

1. **Audit 結果**：哪些 violations 被偵測到，包含完整 detail
2. **Should-rebalance 判斷**：為何決定要 rebalance（哪條規則命中）
3. **Rebalance LLM call 輸入**：完整 prompt（或至少 hash + 長度）
4. **Rebalance LLM call 輸出**：raw response 字串
5. **JSON parse 結果**：proposed changes 物件
6. **每個 RebalanceChange 套用**：成功 / Pydantic 失敗（含完整 error）
7. **Final spec 對 audit**：套用後跑一次 audit，看 V4 是否還在

### Diff（示意——對應實際 code 路徑套用）

```diff
@@ in _audit_layout_distribution ... return statement
+    if violations:
+        logger.info(
+            "[H-DIAG] audit found %d violations: %s",
+            len(violations),
+            [f"{v.kind}({v.severity})@{v.slide_indices}" for v in violations],
+        )
+        for v in violations:
+            logger.info("[H-DIAG]   %s: %s", v.kind, v.detail)
+    else:
+        logger.info("[H-DIAG] audit found no violations")
     return violations


@@ in _should_rebalance
 def _should_rebalance(violations: list[LayoutViolation]) -> bool:
     has_hard = any(v.severity == "hard" for v in violations)
     v4_count = sum(1 for v in violations if v.kind == "V4")
     soft_count = sum(1 for v in violations if v.severity == "soft")
-    return has_hard or v4_count >= 2 or soft_count >= 3
+    decision = has_hard or v4_count >= 2 or soft_count >= 3
+    logger.info(
+        "[H-DIAG] should_rebalance: hard=%s v4=%d soft=%d → %s",
+        has_hard, v4_count, soft_count, decision,
+    )
+    return decision


@@ in _rebalance_layouts ... around the LLM call
+    logger.info(
+        "[H-DIAG] rebalance LLM call: prompt_len=%d, n_violations=%d",
+        len(prompt), len(violations),
+    )
     raw_response = await chat_complete(...)  # existing call
+    logger.info(
+        "[H-DIAG] rebalance LLM raw response (first 2KB): %s",
+        str(raw_response)[:2000],
+    )

     try:
         proposed = json.loads(raw_response)
+        changes = proposed.get("changes", [])
+        logger.info(
+            "[H-DIAG] rebalance proposed %d changes: %s",
+            len(changes),
+            [
+                f"slide{c.get('slide_index')}→{c.get('new_layout_kind')}"
+                for c in changes
+            ],
+        )
     except json.JSONDecodeError as exc:
+        logger.warning("[H-DIAG] rebalance JSON parse failed: %s", exc)
         # ... existing fallback ...

@@ for each change application
     try:
         # ... apply change to spec_dict ...
+        logger.info(
+            "[H-DIAG] applied change to slide %d: %s → %s",
+            change_idx, old_layout, new_layout,
+        )
     except (ValidationError, KeyError, TypeError) as exc:
+        logger.warning(
+            "[H-DIAG] change %d FAILED to apply: %s\n  raw change: %s",
+            change_idx, exc, change,
+        )

@@ after re-validation
+    try:
+        new_spec = SlidesSpec.model_validate(spec_dict)
+        post_audit = _audit_layout_distribution(new_spec, chunks_text)
+        post_v4 = [v for v in post_audit if v.kind == "V4"]
+        logger.info(
+            "[H-DIAG] post-rebalance audit: %d V4 remain (was %d)",
+            len(post_v4),
+            sum(1 for v in violations if v.kind == "V4"),
+        )
+    except ValidationError as exc:
+        logger.warning("[H-DIAG] post-rebalance SlidesSpec invalid: %s", exc)
```

### 為什麼用 `[H-DIAG]` 前綴

所有 log 行都加這個 marker 讓 grep 容易：

```bash
docker logs <csp-backend> 2>&1 | grep "\[H-DIAG\]" | tail -100
```

可以一次撈出整條 rebalance pipeline 的時序。Round 5 部署後跑一次有問題的 job、把 log 撈下來、人工判讀，**這時候才知道下一個 patch 怎麼寫**。

### 不做的事

- 不改 rebalance prompt
- 不改 IconRow schema constraints
- 不改 `_should_rebalance` 邏輯
- 不改 audit 偵測

純加 logging。Production 影響：每個 rebalance 多 ~6-10 行 log，可忽略。

### 驗收

- 重跑 journal_v5 素材
- `docker logs <csp-backend> | grep "\[H-DIAG\]"` 至少有 8-10 行
- 從 log 應該可以明確答出：
  - V4 audit 有 fire 嗎？（YES 預期）
  - rebalance LLM 提了幾個 change？（0 vs 1+ 是關鍵）
  - 哪些 change 套用失敗？失敗原因？

### 預期會看到三種症狀之一

從之前的猜測，最可能的三個結果：

**症狀 A**：LLM 回 `{"changes": []}` 完全不改。代表 rebalance prompt 沒說服力 / gemma4 評估後覺得不該動。**修法**：強化 prompt，列具體 candidate slide 跟建議的轉換方式。

**症狀 B**：LLM 提了 change、但每個都 ValidationError：「IconRow concept must be one of CONCEPT_MAP keys」或類似。**修法**：放寬 IconRow.concept 限制（接受任意字串、unknown 走 fallback dot）；或在 rebalance prompt 列出 CONCEPT_MAP 給 LLM 挑。

**症狀 C**：change 套用成功、但 post-rebalance audit 又 V4 fire。代表轉換結果還是 standard——可能是 saturation pass 之後又被降回 standard。**修法**：檢查 saturation pass 的 demote 邏輯（Patch C 那條 two_column→icon_rows 是不是反向把 icon_rows→standard 了）。

跑一次 Round 5 之後就知道是哪種，第二輪精準修就好。

---

## 整體驗收

跑完 U + S-fix + H-diag 後重跑兩份素材：

### journal_v5 期望

| 觀察項 | v5 狀況 | Round 5 預期 |
|---|---|---|
| 封面 theme | corporate_navy（深藍）| **warm_journal**（米白棕褐，Patch U 強制）|
| Slide 2 layout | standard 4 label-bullets | **icon_rows**（Patch H-diag log 顯示根因）|
| 任何 bullet 含 `(參 [N])` | 無（Q 已修）| 無 |
| 任何 LaTeX 殘骸 | 待驗 | 無 |

### tech_v4 期望

| 觀察項 | v4 狀況 | Round 5 預期 |
|---|---|---|
| 封面 theme | corporate_navy | corporate_navy（title 無 keyword、保持 LLM 選擇）|
| Slide 10 graphviz 圖內文字 | `感知 (偵測鐵屑 $\nightarrow$ Alert)` 有殘骸 | **「感知 (偵測鐵屑 → Alert)」乾淨**（Patch S-fix）|
| 其他 graphviz 圖 | 正常 | 正常（不受影響）|

### 通過條件

1. journal_v5 (或任何 title 含「心得」的素材) **跑 3 次以上、theme 必定 = warm_journal**
2. tech_v4 重跑、graphviz 圖內**零 LaTeX 殘骸**
3. 任何 V4 命中的 job 都會產出 `[H-DIAG]` log、可從 log 推斷 rebalance 為何失敗
4. 既有測試全綠 + 新增測試全綠（U 跟 S-fix 各自的 test file）

---

## 實作順序

| 順序 | Patch | 檔案 | 預估 |
|---|---|---|---|
| 1 | U — title-keyword theme override | `studio.py` + `test_theme_override.py` | 45 min |
| 2 | S-fix — normalize diagram_dot | `diagram_renderer.py` + 1 test | 20 min |
| 3 | H-diag — rebalance logging | `studio.py` 多個位置加 log | 30 min |

合計 **~1.5 小時**。

**U 先做**——它解決使用者最在意的「為什麼又一樣？」這個感受問題，30 分鐘就能上線、立刻看到效果。S-fix 跟 H-diag 沒有特定順序。

H-diag 跑完一輪 production job 之後**會產生需要新 patch 的需求**，預期會有 Round 6 收尾 H 那個從 Round 2 拖到現在的尾巴。

---

## 不在本次範圍

- 修 H 邏輯（先觀測再決定，不盲改）
- 改 rebalance prompt（同上）
- 改 IconRow / Column schema constraints（同上）
- 改 academic_paper / executive_brief / startup_pitch 主題視覺（這幾個 theme 跟 warm_journal 已經實作，沒看到視覺 bug）
- 新增 layout kind
- 前端 UI

---

**Round 5 報告產出時間**：2026-05-19
**診斷依據**：journal_v5 + tech_v4 兩份輸出 + 分支 HEAD `3aee569` 比對 + 多輪人工目測
**驗收**：U 跑 3 次以上確認穩定、S-fix 對 graphviz 圖肉眼檢查、H-diag log 人工 review
