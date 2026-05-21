# ANILA LM Studio — Round 6 補丁規格

> 對 `feature/studio-quality-fixes` HEAD `4de0ecf` 的最後一個 H bug 修正。
>
> **這份 spec 與前面 5 輪的本質差別**：基於實測 production log 寫的，不是推測。Round 5 H-diag 加的 `[H-DIAG]` logging 撈到完整 rebalance pipeline 軌跡（Job `j_de1d67dac4d508da13bc238fd29bf917`，2026-05-20 01:43），證實前面幾輪所有「rebalance 失敗」的根因猜測**全錯**。

---

## 1. Log 證實的真實狀況

完整 trail：

```
01:43:44 - audit found 2 violations: V4@[9], V4@[14]
01:43:44 -   V4@9 「TensorRT-LLM 崩潰問題分析」: bullet-pattern 4/4
01:43:44 -   V4@14「未來架構觀察與建議」: title-keyword '架構'
01:43:44 - should_rebalance: True
01:44:29 - rebalance LLM proposed 1 change: ['slide9→icon_rows']
01:44:29 - applied change to slide 9: standard → icon_rows
01:44:29 - post-rebalance: 1 V4 remain (was 2)
```

Slide 9 的 rebalance payload 是高品質 icon_rows（4 個 row：異常偵測 / 根因分析 / 緩解計畫 / 量化成果），Pydantic 驗證通過、套用成功。

Slide 14 被 LLM **主動跳過**。它的 V4 違規來源純粹是 title 含「架構」二字，bullets 是流動敘述體（不是 label:description）。LLM 判斷「強塞進 icon_rows 要硬掰 heading 反而醜」，所以保留 standard——**這是正確判斷**。

但 post-audit 仍把它計為 "1 V4 remain"，看起來像 rebalance 失敗，事實上是 audit 把一個 LLM 已正確處理的 case 持續標為違規。

## 2. 根本問題：V4 把兩種強度差很多的訊號當同一件事

| 訊號 | 確定性 | 適合 trigger rebalance |
|---|---|---|
| **Bullets 100% 是 `label:description`** | 強——結構明確、轉 icon_rows 機械可行 | ✅ 是 |
| **Title 含「架構/流程/設計/方案」keyword** | 弱——只是 hint、bullet 結構不一定支援 | ❌ 不應該 |

把兩者都當 `V4 soft violation` 等同於要求 LLM 對流動文體硬轉，LLM 拒絕後 audit 再把它記為失敗。**這是 audit 規則設計的問題、不是 rebalance 路徑的問題**。

---

## 3. Patch V：V4 拆成 V4_CONTENT / V4_TITLE，audit 跟 rebalance 對齊判斷

### 影響檔案

- `myCSPPlatform/backend/app/api/studio.py`
- `myCSPPlatform/backend/tests/test_layout_audit.py`
- `myCSPPlatform/backend/tests/test_layout_rebalance.py`（若存在）

### 3.1 Audit 階段拆 violation kind

找 `_audit_layout_distribution` 函式內 V4 那段（commit `4de0ecf` 約 line 1773-1815）：

```diff
@@ -1773,7 +1773,12 @@ def _audit_layout_distribution(...) -> list[LayoutViolation]:
-    # ── V4: enumeration title OR label-pattern bullets + 3+ bullets + standard ──
+    # ── V4_CONTENT / V4_TITLE: bullet-pattern (strong) vs title-keyword (hint) ──
+    #
+    # Round 6 split: production log (Job j_de1d67dac4d508da13bc238fd29bf917)
+    # showed that title-keyword-only violations are correctly skipped by the
+    # rebalance LLM (bullets aren't structurable). Counting them as
+    # violations to retry against is a false-positive trap. Separating the
+    # kinds lets `_should_rebalance` filter on signal strength.
     for i, s in enumerate(slides):
         layout = s.layout_kind
         if layout == "standard":
@@ -1802,30 +1807,47 @@
         pattern_matches = sum(
             1 for b in s.bullets if _LABEL_BULLET_RE.match(str(b))
         )
         pattern_match = (
             pattern_matches / len(s.bullets) >= _LABEL_PATTERN_THRESHOLD
         )

-        if not (title_match or pattern_match):
+        # No signal at all → not a V4 candidate
+        if not (title_match or pattern_match):
             continue

-        detail_bits = [
-            f"slide #{i}「{s.title}」: {len(s.bullets)} bullets, {layout} layout",
-        ]
-        if layout == "image_focus":
-            detail_bits.append("FLUX disguise — label-bullets under illustration")
-        if title_match:
-            detail_bits.append(f"title-keyword '{matched_kw}'")
-        if pattern_match:
-            detail_bits.append(
-                f"bullet-pattern {pattern_matches}/{len(s.bullets)}"
-            )
-
-        violations.append(
-            LayoutViolation(
-                kind="V4",
-                severity="soft",
-                slide_indices=[i],
-                detail=", ".join(detail_bits),
-            )
-        )
+        # Round 6: split by signal strength.
+        # Content-pattern (bullets ARE label:description) → strong, soft severity
+        # Title-keyword only (no pattern match) → hint, new "hint" severity
+        base_detail = (
+            f"slide #{i}「{s.title}」: {len(s.bullets)} bullets, {layout} layout"
+        )
+        if layout == "image_focus":
+            base_detail += ", FLUX disguise"
+
+        if pattern_match:
+            # Strong signal — bullets clearly structured for icon_rows.
+            # Even if title also matches, this is the dominant kind.
+            detail = (
+                f"{base_detail}, bullet-pattern "
+                f"{pattern_matches}/{len(s.bullets)}"
+            )
+            if title_match:
+                detail += f" + title-keyword '{matched_kw}'"
+            violations.append(
+                LayoutViolation(
+                    kind="V4_CONTENT",
+                    severity="soft",
+                    slide_indices=[i],
+                    detail=detail,
+                )
+            )
+        else:
+            # title_match alone — weak hint, not actionable as violation.
+            # Logged for visibility, but `_should_rebalance` ignores hints.
+            violations.append(
+                LayoutViolation(
+                    kind="V4_TITLE",
+                    severity="hint",
+                    slide_indices=[i],
+                    detail=f"{base_detail}, title-keyword '{matched_kw}' (hint only)",
+                )
+            )
```

新增 `"hint"` 到 severity 的 Literal type（schema 內部 type，找 `LayoutViolation` 定義加）：

```diff
 class LayoutViolation(BaseModel):
     kind: Literal["V1", "V2", "V3", "V4_CONTENT", "V4_TITLE"]
-    severity: Literal["hard", "soft"]
+    severity: Literal["hard", "soft", "hint"]
     slide_indices: list[int]
     detail: str
```

> 注意：`LayoutViolation` 是 internal type，不是 SlidesSpec 的對外 schema。如果它在 schema/studio.py 也有定義，記得同步。

### 3.2 `_should_rebalance` 對齊新分類

```diff
@@ in _should_rebalance ...
 def _should_rebalance(violations: list[LayoutViolation]) -> bool:
+    """Decide whether the rebalance LLM call is worth running.
+
+    Round 6: only trigger on STRONG signals. Title-keyword V4 (hint
+    severity) is left as visible-but-not-actionable — production log
+    showed the LLM correctly skips these and our post-audit was
+    falsely counting them as failures.
+    """
     has_hard = any(v.severity == "hard" for v in violations)
-    v4_count = sum(1 for v in violations if v.kind == "V4")
-    soft_count = sum(1 for v in violations if v.severity == "soft")
-    decision = has_hard or v4_count >= 2 or soft_count >= 3
+    content_v4 = sum(1 for v in violations if v.kind == "V4_CONTENT")
+    decision = has_hard or content_v4 >= 1
     logger.info(
-        "[H-DIAG] should_rebalance: hard=%s v4=%d soft=%d → %s",
-        has_hard, v4_count, soft_count, decision,
+        "[H-DIAG] should_rebalance: hard=%s v4_content=%d → %s",
+        has_hard, content_v4, decision,
     )
     return decision
```

**門檻從 2 降到 1**：content-pattern V4 是強訊號，1 張就值得 rebalance。

### 3.3 Rebalance prompt 也只列 content / hard 違規

找 `_rebalance_layouts` 內構建 prompt 的地方，過濾 violations 列表：

```diff
@@ in _rebalance_layouts ...
-    violation_summary = "\n".join(f"- {v.detail}" for v in violations)
+    # Round 6: hint-severity violations are informational only.
+    # Listing them in the prompt invites the LLM to waste tokens
+    # explaining why it doesn't want to change them.
+    actionable = [v for v in violations if v.severity != "hint"]
+    violation_summary = "\n".join(f"- {v.detail}" for v in actionable)
+    if not actionable:
+        logger.info(
+            "[H-DIAG] rebalance skipped: only hint violations present"
+        )
+        return spec  # no work to do
```

### 3.4 Post-audit log 也要對齊

最後 post-rebalance 那條 log 也要區分 kind：

```diff
@@ ...
-    post_v4 = [v for v in post_audit if v.kind == "V4"]
-    logger.info(
-        "[H-DIAG] post-rebalance audit: %d V4 remain (was %d)",
-        len(post_v4),
-        sum(1 for v in violations if v.kind == "V4"),
-    )
+    pre_content = sum(1 for v in violations if v.kind == "V4_CONTENT")
+    post_content = sum(1 for v in post_audit if v.kind == "V4_CONTENT")
+    post_hint = sum(1 for v in post_audit if v.kind == "V4_TITLE")
+    logger.info(
+        "[H-DIAG] post-rebalance audit: V4_CONTENT %d→%d, V4_TITLE %d (hints)",
+        pre_content, post_content, post_hint,
+    )
```

### 3.5 測試更新

`test_layout_audit.py` 內**既有的 V4 測試**要更新預期值。所有舊測試裡 `v.kind == "V4"` 要改成具體 `V4_CONTENT` 或 `V4_TITLE`：

```python
# 既有測試（用 v3 slide 2 那種 100% label-pattern 案例）改：
def test_v4_content_fires_on_pure_label_pattern():
    """Bullets all `label: description`, no title keyword.
    
    Should be V4_CONTENT (strong signal, will trigger rebalance).
    """
    spec = SlidesSpec(title="t", slides=[Slide(
        title="執行摘要",  # 沒 V4 keyword
        bullets=[
            "基礎設施：完成 TensorRT-LLM 掌握與 GH200 調優",
            "效能最佳化：設計法律 Agentic RAG",
            "合規治理：實作 ISO 42001 結構化 CoT 日誌",
            "底層除錯：分析 TensorRT-LLM 記憶體對齊瑕疵",
        ],
        layout_kind="standard",
    )])
    violations = _audit_layout_distribution(spec, chunks_text="")
    v4_content = [v for v in violations if v.kind == "V4_CONTENT"]
    v4_title = [v for v in violations if v.kind == "V4_TITLE"]
    assert len(v4_content) == 1
    assert len(v4_title) == 0


# 新增：title-keyword-only 案例
def test_v4_title_fires_as_hint_only():
    """Title contains keyword, but bullets are flowing narrative.
    
    Round 6: this is the slide 14 production case
    ('未來架構觀察與建議' with 3 narrative bullets).
    Should fire as V4_TITLE hint, NOT trigger rebalance.
    """
    spec = SlidesSpec(title="t", slides=[Slide(
        title="未來架構觀察與建議",
        bullets=[
            "未來可考慮導入 Multi-Agent Supervisor 架構提升擴展性",
            "建議優先評估垂直切分知識庫對檢索延遲的影響",
            "結合 ISO 42001 合規要求設計可稽核 AI 開發流程",
        ],
        layout_kind="standard",
    )])
    violations = _audit_layout_distribution(spec, chunks_text="")
    v4_content = [v for v in violations if v.kind == "V4_CONTENT"]
    v4_title = [v for v in violations if v.kind == "V4_TITLE"]
    assert len(v4_content) == 0
    assert len(v4_title) == 1
    assert v4_title[0].severity == "hint"


def test_should_rebalance_only_on_content_or_hard():
    """Title-keyword hints alone should NOT trigger rebalance.
    
    Regression guard for Job j_de1d67dac4d508da13bc238fd29bf917
    where slide 14's V4_TITLE was counted as a remaining violation.
    """
    title_only_violations = [
        LayoutViolation(
            kind="V4_TITLE", severity="hint",
            slide_indices=[14], detail="...",
        ),
    ]
    assert _should_rebalance(title_only_violations) is False

    content_violations = [
        LayoutViolation(
            kind="V4_CONTENT", severity="soft",
            slide_indices=[9], detail="...",
        ),
    ]
    assert _should_rebalance(content_violations) is True


def test_v4_content_threshold_is_one():
    """Round 6: a single V4_CONTENT triggers rebalance.
    
    Previous threshold (`v4_count >= 2`) sometimes left a single
    obvious icon_rows candidate unmodified.
    """
    one_content = [
        LayoutViolation(
            kind="V4_CONTENT", severity="soft",
            slide_indices=[2], detail="...",
        ),
    ]
    assert _should_rebalance(one_content) is True


def test_v4_both_signals_classified_as_content():
    """When BOTH title-keyword and bullet-pattern fire,
    content-pattern is the dominant kind (strong signal wins).
    """
    spec = SlidesSpec(title="t", slides=[Slide(
        title="Agentic Workflow 執行流程",  # title-keyword '流程'
        bullets=[
            "感知層：偵測異常並警報",
            "認知層：Agent 檢索維修手冊",
            "行動層：生成具體操作建議",
        ],  # bullet-pattern 3/3
        layout_kind="standard",
    )])
    violations = _audit_layout_distribution(spec, chunks_text="")
    kinds = [v.kind for v in violations]
    assert "V4_CONTENT" in kinds
    assert "V4_TITLE" not in kinds  # 兩個訊號都中時，CONTENT 蓋掉 TITLE
```

---

## 4. 驗收

### 4.1 重跑同一份 collection（Job j_de1d67dac4d508da13bc238fd29bf917 的素材）

預期 `[H-DIAG]` log：

```
audit found 2 violations: V4_CONTENT@[9], V4_TITLE@[14]
  V4_CONTENT@9: ..., bullet-pattern 4/4
  V4_TITLE@14: ..., title-keyword '架構' (hint only)
should_rebalance: hard=False v4_content=1 → True
rebalance LLM proposed 1 changes: ['slide9→icon_rows']
applied change to slide 9: standard → icon_rows
post-rebalance audit: V4_CONTENT 1→0, V4_TITLE 1 (hints)
```

**關鍵**：post-rebalance log 不再有「V4 remain」這種看起來像失敗的字樣。Slide 14 仍出現在 hint 計數但**不影響任何決策**。

### 4.2 通過條件

1. `pytest myCSPPlatform/backend/tests/test_layout_*.py -v` 全綠（含新增 5 個 test）
2. 重跑前面任一份素材（journal_v5、tech_v4、v6）：
   - Slide 視覺輸出**沒有退化**——所有原本被正確轉為 icon_rows 的 slide 仍然是 icon_rows
   - 標題含「架構/流程」但 bullets 為流動文體的 slide 仍是 standard（**這是預期行為，不再被視為 bug**）
3. `[H-DIAG]` log 出現 `V4_CONTENT` / `V4_TITLE` 拆分標籤
4. `should_rebalance` 決策 log 顯示 `v4_content=N`，不再有 `v4=N soft=M` 混合計數

### 4.3 不會發生的事

- ❌ Slide 14 那種「title 命中、bullet 不結構化」的 slide 強迫變成 icon_rows
- ❌ Rebalance 對著流動文體硬掰 heading
- ❌ Post-audit 把 LLM 正確處理的 case 標為「V4 remain」

---

## 5. 為什麼這次值得信

對比前 5 輪 H 修補：

| Round | 假設根因 | 證據 |
|---|---|---|
| Round 2 | Pydantic validation error | 推測 |
| Round 3 | IconRow.concept 沒在 CONCEPT_MAP | 推測 |
| Round 4 | image_focus disguise | 部分推測（FLUX 雜訊圖是觀察、但 disguise 是推測） |
| Round 5 | 三種症狀 A/B/C，等 log | 等 log |
| **Round 6** | **Audit 把 hint 當違規** | **production log 證實** |

前 5 輪都是「看 pptx 推測」。Round 6 是「看 log 看見實際數值跟序列」。

---

## 6. 實作順序

| 順序 | 步驟 | 預估 |
|---|---|---|
| 1 | `LayoutViolation.kind` Literal 新增 V4_CONTENT/V4_TITLE，severity 加 "hint" | 2 min |
| 2 | `_audit_layout_distribution` V4 區段拆 if 分支 | 10 min |
| 3 | `_should_rebalance` 改 content-pattern threshold | 2 min |
| 4 | `_rebalance_layouts` prompt 過濾 hint violations | 5 min |
| 5 | Post-audit log 重寫 | 3 min |
| 6 | 既有測試的 `kind == "V4"` 改 `V4_CONTENT` / `V4_TITLE` | 10 min |
| 7 | 新增 5 個 test | 10 min |
| 8 | 跑一次 production job 確認 log 輸出 | 5 min |

**合計 ~45 分鐘**（不到一小時，就把拖了 5 輪的東西收掉）。

---

## 7. 不在本次範圍

- 改 rebalance prompt 的內容措辭（沒必要——log 證實 LLM 行為正確）
- 動 IconRow.concept schema（沒必要——驗證有過）
- 改 CONCEPT_MAP（沒必要——slide 9 那種 Detection/Analysis/... 走 J fallback 還能看）
- Vision QA 修補
- FLUX 正面觸發（前一輪討論過、留到下個 milestone）

---

## 8. 後續：H bug 真的收完之後

如果這次驗收通過、log 看起來乾淨，**Round 6 可以是這條 quality-fixes 分支的最後一個 round**。剩下可以考慮：

1. 把分支 merge 到 main
2. 開新分支處理下個議題（FLUX 正面觸發、或新 layout kind、或前端 UI）
3. H-diag logging 可以保留（成本低、未來 debug 有用），或調降到 DEBUG level

---

**Round 6 產出時間**：2026-05-20
**診斷依據**：production `[H-DIAG]` log（Job `j_de1d67dac4d508da13bc238fd29bf917`，2026-05-20 01:43:44 UTC）
**驗收**：重跑同 collection、`[H-DIAG]` log 用語對齊新分類、所有 test 全綠
