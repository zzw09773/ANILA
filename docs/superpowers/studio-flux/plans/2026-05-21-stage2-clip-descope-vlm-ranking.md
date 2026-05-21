# Stage 2 Gate — De-scope CLIP, VLM-score Ranking 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 移除 Stage 2 品質閘門裡未部署的 CLIP 閘，改由 striping + gemma4 VLM 兩道閘把關，並讓 VLM 多回一個 0–1 分數作為 N 候選排序依據。

**Architecture:** `gate_candidates` 是依賴注入式 orchestrator。移除 CLIP 注入點與其常數/Protocol/stub，閘門順序改為 striping（純 CV、便宜、先短路）→ VLM（語意 + 文字 + 排序分數）。`GeneratedImage.clip_score` 合約欄位保留但恆 None（spec 3.5 鎖死），排序改讀 `vlm_verdict["score"]`。

**Tech Stack:** Python 3.12, FastAPI, pytest + pytest-asyncio, numpy。backend venv：`myCSPPlatform/backend/.venv`。

**Spec:** `docs/superpowers/studio-flux/specs/2026-05-21-stage2-clip-descope-vlm-ranking-design.md`

---

## 檔案結構

- Modify: `myCSPPlatform/backend/app/api/studio.py`
  - `_Gemma4VlmGate.check`（~837-880）：prompt + 解析加 `score`。
  - `_gated_generate`（~1298, 1314-1323）：移除 `stub_clip_scorer` import 與 `clip_scorer=` / `flux_prompt=` 引數。
- Modify: `myCSPPlatform/backend/app/services/flux_quality_gate.py`
  - 移除 `ClipScorer` Protocol、`stub_clip_scorer`、`CLIP_THRESHOLD`、模組 docstring 的 CLIP 段。
  - `gate_candidates`：移除 `clip_scorer`/`flux_prompt` 參數、CLIP 閘；排序改 VLM score。
- Modify (test): `myCSPPlatform/backend/tests/test_flux_quality_gate.py`
  - 移除 CLIP 相關 import 與測試；改 mock VLM 帶 score；新增排序測試。

**所有指令於 `myCSPPlatform/backend/` 下執行，python 用 `.venv/bin/python`。**

---

## Task 1: VLM gate 多回 0–1 分數

**Files:**
- Modify: `myCSPPlatform/backend/app/api/studio.py:845-880`（`_Gemma4VlmGate.check`）

此為 LLM I/O 轉接器，無獨立單元測試（行為由 Task 2 的 mock VLM 排序測試 + spec 5.6 實機驗收涵蓋）。

- [ ] **Step 1: 改 prompt 要求 score**

把 `user_text`（約 845-852）改成：

```python
        user_text = (
            f"Does this image depict an abstract, text-free illustration of: "
            f"{concept}?\n"
            "Does it contain ANY letters, characters, digits, logos, or "
            "readable signage?\n"
            "Also rate 0.0-1.0 how cleanly and aptly it depicts the concept "
            "(1.0 = excellent, on-concept, no text or artifacts).\n"
            'Answer JSON only: {"match": bool, "has_text": bool, '
            '"score": <0.0-1.0>, "reason": "<short>"}'
        )
```

- [ ] **Step 2: 解析 score（clamp + 預設）**

把回傳 dict（約 876-880）改成：

```python
        score_raw = parsed.get("score", 0.0)
        try:
            score = max(0.0, min(1.0, float(score_raw)))
        except (TypeError, ValueError):
            score = 0.0
        return {
            "match": bool(parsed.get("match")),
            "has_text": bool(parsed.get("has_text")),
            "score": score,
            "reason": str(parsed.get("reason", "")),
        }
```

- [ ] **Step 3: unparseable 分支補 score=0.0**

把 fail-closed 回傳（約 875）改成：

```python
            return {"match": False, "has_text": True, "score": 0.0, "reason": "unparseable"}
```

- [ ] **Step 4: 驗證 app 仍可 import**

Run: `.venv/bin/python -c "import app.api.studio"`
Expected: 無輸出、exit 0（無 SyntaxError / ImportError）。

- [ ] **Step 5: Commit**

```bash
git add app/api/studio.py
git commit -m "feat(studio-flux): VLM gate 回傳 0-1 排序分數"
```

---

## Task 2: 移除 CLIP 閘、VLM 分數排序

**Files:**
- Test: `myCSPPlatform/backend/tests/test_flux_quality_gate.py`（全面改寫 mock/測試段）
- Modify: `myCSPPlatform/backend/app/services/flux_quality_gate.py`
- Modify: `myCSPPlatform/backend/app/api/studio.py:1298, 1314-1323`（wiring）

- [ ] **Step 1: 改寫測試檔的 import 與 mock/gate 測試段（RED）**

將 `tests/test_flux_quality_gate.py` 的 **import 區塊**（23-31）改成（移除 `CLIP_THRESHOLD`、`stub_clip_scorer`）：

```python
from app.services.flux_quality_gate import (
    HF_ENERGY_THRESH,
    _decode_png_to_gray,
    _has_striping_artifact,
    gate_candidates,
    striping_energy,
)
```

將 **mock VLM ~ 檔尾**（即第 127 行 `# ── mock VLM` 起到檔案結束 242）整段**替換**為：

```python
# ── mock VLM ────────────────────────────────────────────────────────────────
class _MockVlm:
    def __init__(self, verdict: dict) -> None:
        self._verdict = verdict
        self.calls = 0

    async def check(self, png_bytes: bytes, *, concept: str) -> dict:  # noqa: ARG002
        self.calls += 1
        return dict(self._verdict)


class _QueueVlm:
    """Returns a different verdict per call, in order — for ranking tests."""

    def __init__(self, verdicts: list[dict]) -> None:
        self._q = list(verdicts)
        self.calls = 0

    async def check(self, png_bytes: bytes, *, concept: str) -> dict:  # noqa: ARG002
        self.calls += 1
        return dict(self._q.pop(0))


def _candidate() -> GeneratedImage:
    return GeneratedImage(png_bytes=_solid_png(), seed=1, accepted=False)


# ── gate_candidates ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_gate_all_pass_picks_highest_vlm_score():
    c_low = _candidate()
    c_high = _candidate()
    vlm = _QueueVlm([
        {"match": True, "has_text": False, "score": 0.3, "reason": "ok"},
        {"match": True, "has_text": False, "score": 0.9, "reason": "ok"},
    ])
    best = await gate_candidates(
        [c_low, c_high],
        concept_en="abstract teal swirl",
        vlm=vlm,
    )
    assert best is not None
    assert best is c_high
    assert best.vlm_verdict["score"] == 0.9
    assert best.accepted is True
    # CLIP de-scoped: clip_score never set.
    assert best.clip_score is None


@pytest.mark.asyncio
async def test_gate_rejects_has_text():
    vlm = _MockVlm({"match": True, "has_text": True, "score": 0.8, "reason": "logo"})
    best = await gate_candidates([_candidate()], concept_en="c", vlm=vlm)
    assert best is None


@pytest.mark.asyncio
async def test_gate_rejects_no_match():
    vlm = _MockVlm({"match": False, "has_text": False, "score": 0.1})
    best = await gate_candidates([_candidate()], concept_en="c", vlm=vlm)
    assert best is None


@pytest.mark.asyncio
async def test_gate_rejects_striping_before_vlm():
    striped = GeneratedImage(png_bytes=_striped_png(), seed=1, accepted=False)
    vlm = _MockVlm({"match": True, "has_text": False, "score": 1.0})
    best = await gate_candidates([striped], concept_en="c", vlm=vlm)
    assert best is None
    assert vlm.calls == 0  # striping gate is before VLM (short-circuits)


@pytest.mark.asyncio
async def test_gate_empty_candidates_returns_none():
    best = await gate_candidates(
        [], concept_en="c",
        vlm=_MockVlm({"match": True, "has_text": False, "score": 1.0}),
    )
    assert best is None
```

- [ ] **Step 2: 跑測試確認 RED**

Run: `.venv/bin/python -m pytest tests/test_flux_quality_gate.py -q`
Expected: FAIL —— `gate_candidates()` 仍要求 `clip_scorer` 引數（`TypeError: ... missing ... 'clip_scorer'`），且 `test_gate_rejects_striping_before_vlm` 因現順序 CLIP→striping 仍會跑（striping 在第二道也會 reject，但簽名錯誤先 fail）。

- [ ] **Step 3: 移除 flux_quality_gate.py 的 CLIP 常數/Protocol/stub**

刪除模組 docstring 中描述 CLIP 那道閘門的段落（約 8-16 的第 1 點），其餘兩道改編號為 1=striping、2=VLM。

刪除 `ClipScorer` Protocol（78-82）與 `stub_clip_scorer` 函式（94-105）。

刪除 `CLIP_THRESHOLD` 常數與其上方註解（約 48-56）。保留 `HF_ENERGY_THRESH` 與 `FLAT_REGION_FRAC` 不動。

確認 `from typing import Protocol`（仍被 `Vlm` 使用）保留。

- [ ] **Step 4: 改寫 gate_candidates（移除 CLIP、VLM 排序）**

把 `gate_candidates`（109-175）整個函式替換成：

```python
async def gate_candidates(
    candidates: list[GeneratedImage],
    *,
    concept_en: str,
    vlm: Vlm,
) -> GeneratedImage | None:
    """Return the best accepted candidate, or ``None`` if all fail.

    Each candidate runs striping -> VLM, cheapest-first: a pixel-level
    striping artifact short-circuits before the VLM round-trip. Accepted
    candidates are ranked by the VLM's 0-1 quality ``score`` and the highest
    wins. CLIP was de-scoped (see spec 2026-05-21 design); ``clip_score`` is
    left None for audit-schema stability.
    """
    scored: list[GeneratedImage] = []
    for c in candidates:
        # Reset audit state — a retried/reused candidate must not carry a
        # stale verdict.
        c.accepted = False

        # Gate 1: striping / barcode artifact (pure CV, cheapest — first).
        if _has_striping_artifact(c.png_bytes):
            logger.info("Gate reject (striping artifact detected)")
            continue

        # Gate 2: VLM semantic + text check (also yields the 0-1 rank score).
        try:
            verdict = await vlm.check(c.png_bytes, concept=concept_en)
        except Exception as e:  # noqa: BLE001
            logger.warning("VLM check raised, skipping candidate: %s", e)
            continue
        c.vlm_verdict = verdict
        if not verdict.get("match") or verdict.get("has_text"):
            logger.info("Gate reject (VLM): %s", verdict)
            continue

        c.accepted = True
        scored.append(c)
        logger.info("[gate] candidate accepted: vlm=%s", c.vlm_verdict)

    if not scored:
        logger.info(
            "[gate] all %d candidate(s) rejected for concept=%r",
            len(candidates), concept_en[:50],
        )
        return None
    best = max(scored, key=lambda x: float((x.vlm_verdict or {}).get("score", 0.0)))
    logger.info(
        "[gate] %d/%d accepted; best vlm score=%.2f",
        len(scored), len(candidates),
        float((best.vlm_verdict or {}).get("score", 0.0)),
    )
    return best
```

- [ ] **Step 5: 修 studio.py wiring**

`app/api/studio.py` 約 1298 的 import 改成（移除 `stub_clip_scorer`）：

```python
    from app.services.flux_quality_gate import gate_candidates
```

約 1314-1323 的呼叫替換成（移除 `clip_scorer=`、`flux_prompt=` 與 CLIP 註解）：

```python
        best = await gate_candidates(
            candidates,
            concept_en=concept_en,
            vlm=vlm,
        )
```

- [ ] **Step 6: 跑測試確認 GREEN + app import**

Run: `.venv/bin/python -m pytest tests/test_flux_quality_gate.py -q`
Expected: PASS（全部）。

Run: `.venv/bin/python -c "import app.api.studio; import app.services.flux_quality_gate"`
Expected: 無輸出、exit 0。

- [ ] **Step 7: Commit**

```bash
git add app/services/flux_quality_gate.py app/api/studio.py tests/test_flux_quality_gate.py
git commit -m "feat(studio-flux): 去除 CLIP 閘,改 striping+VLM 兩道閘並用 VLM 分數排序 (spec 5.5)"
```

---

## Task 3: spec 5.6 驗收（rebuild 後，多為 user 觸發）

**前置**：Task 1-2 已 commit。rebuild 需 user 授權、user 觸發：
`docker compose -f docker-compose-dev.yml up -d --build csp`

- [ ] **驗收 1 — has_text 重生/fallback**：user 從 frontend 觸發一個會逼出文字的封面（如品牌標語），由 `docker logs anila-platform-dev-csp-1` 觀察 `Gate reject (VLM)` + 重試 log，最終接受或 fallback。
- [ ] **驗收 2 — VLM verdict 進 audit**：確認生成 deck 的 `image_gen_meta.vlm_verdict` 含 `score`/`match`/`has_text`，`clip_score` 為 null（現有 `studio.py:1518-1526` 已記錄，無需改 code，僅驗證）。
- [ ] **驗收 3 — 單張延遲 ≤30s**：user 觸發單張封面 job，由 log 時間戳算 N=2 候選 + gate 總延遲。
- [ ] **驗收 4 — 全 fail fallback 不塞壞圖**：已由 `test_gate_empty_candidates_returns_none` + `_gated_generate` 耗盡 retry 回 `(None, ...)` 的既有路徑覆蓋；user 實機確認壞圖 deck 退成純文字 layout、不顯示空圖。

---

## Self-Review

**Spec coverage：**
- §4.1 VLM 加 score → Task 1。
- §4.2 移除 CLIP + striping→VLM 順序 + VLM 排序 → Task 2 Step 3-4。
- §4.3 wiring → Task 2 Step 5。
- §3 clip_score 恆 None → Task 2 Step 4（不設值）+ 測試斷言 `best.clip_score is None`。
- §7 測試計畫（移除 CLIP 測試、新增排序/striping 短路/全 reject）→ Task 2 Step 1。
- §8 / spec 5.6 → Task 3。

**Placeholder scan：** 無 TBD/TODO；每段含實際 code 與指令。

**Type consistency：** `gate_candidates(candidates, *, concept_en, vlm)` 簽名在 Task 2 的 impl、測試、wiring 三處一致；`vlm_verdict["score"]` 在 Task 1 產出、Task 2 排序消費，鍵名一致。
