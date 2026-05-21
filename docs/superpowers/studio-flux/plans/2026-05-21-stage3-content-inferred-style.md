# Stage 3 — 依內容自動推斷 deck 風格 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓每份簡報的視覺風格由 LLM 看專案內容自動推斷一次（deck 層級、free-form suffix + no-text 護欄 + 穩定 style_id），取代寫死的 house style。

**Architecture:** 新增 `infer_deck_style()` 到 `flux_style.py`（純函式 + 注入式 LLM，可獨立測）。Job pipeline 在 render 前呼叫一次，得 `StyleDescriptor`，經 `_render_pptx` → `_hydrate_images` 透傳，取代 cover-hero 路徑裡的 `get_style_descriptor(brand_id=None)`。任何推斷失敗安全退回 `_DEFAULT_STYLE`。

**Tech Stack:** Python 3.12, FastAPI, pytest + pytest-asyncio, hashlib(stdlib)。backend venv：`myCSPPlatform/backend/.venv`。LLM adapter 合約：`async def complete(*, system: str, user: str) -> str`（沿用 rewriter 的 `_LLMCompleter`）。

**Spec:** `docs/superpowers/studio-flux/specs/2026-05-21-stage3-content-inferred-style-design.md`

**所有指令於 `myCSPPlatform/backend/` 下執行，python 用 `.venv/bin/python`。git attribution 全域關閉、勿加 Co-Authored-By、勿用 --no-verify。**

---

## 檔案結構

- Modify: `myCSPPlatform/backend/app/services/flux_style.py`
  - 新增 `infer_deck_style(*, title, content_sample, llm) -> StyleDescriptor` + 私有 `_LLMCompleter` Protocol、`_normalize_suffix`、`_hash8`、常數。`StyleDescriptor`/`get_style_descriptor`/`_DEFAULT_STYLE` 不變。
- Create (test): `myCSPPlatform/backend/tests/test_flux_style.py`
- Modify: `myCSPPlatform/backend/app/api/studio.py`
  - `_render_pptx`（def ~1602）：加 `deck_style: "StyleDescriptor | None" = None` 參數，透傳給 `_hydrate_images`。
  - `_hydrate_images`（def ~1349）：加 `deck_style: "StyleDescriptor | None" = None` 參數；cover-hero 區塊（~1468）改用它。
  - Job pipeline（render 呼叫 ~2806 之前）：呼叫一次 `infer_deck_style`，組 `content_sample`，把結果傳入 `_render_pptx`。

---

## Task 1: `infer_deck_style` + 測試（核心，TDD）

**Files:**
- Create: `myCSPPlatform/backend/tests/test_flux_style.py`
- Modify: `myCSPPlatform/backend/app/services/flux_style.py`

- [ ] **Step 1: 寫失敗測試**

建立 `tests/test_flux_style.py`，內容：

```python
"""Stage 3 — content-inferred deck style (infer_deck_style) tests."""
from __future__ import annotations

import pytest

from app.services.flux_style import (
    _DEFAULT_STYLE,
    StyleDescriptor,
    infer_deck_style,
)


class _StubLLM:
    """Returns a fixed completion regardless of prompt."""

    def __init__(self, reply: str) -> None:
        self._reply = reply

    async def complete(self, *, system: str, user: str) -> str:  # noqa: ARG002
        return self._reply


class _RaisingLLM:
    async def complete(self, *, system: str, user: str) -> str:  # noqa: ARG002
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_infer_returns_descriptor_with_auto_id():
    s = await infer_deck_style(
        title="Q3 財報",
        content_sample="營收年增 20%",
        llm=_StubLLM("deep navy and amber palette, soft editorial illustration"),
    )
    assert isinstance(s, StyleDescriptor)
    assert s.style_id.startswith("auto-")
    assert "deep navy" in s.suffix


@pytest.mark.asyncio
async def test_infer_deterministic_style_id():
    llm = _StubLLM("muted teal palette, flat editorial illustration")
    a = await infer_deck_style(title="t", content_sample="c", llm=llm)
    b = await infer_deck_style(title="t", content_sample="c", llm=llm)
    assert a.style_id == b.style_id


@pytest.mark.asyncio
async def test_infer_appends_no_text_guard():
    s = await infer_deck_style(
        title="t", content_sample="c",
        llm=_StubLLM("warm cinematic lighting, oil painting texture"),
    )
    assert "no text" in s.suffix.lower()


@pytest.mark.asyncio
async def test_infer_empty_falls_back_to_default():
    s = await infer_deck_style(title="t", content_sample="c", llm=_StubLLM("   "))
    assert s is _DEFAULT_STYLE


@pytest.mark.asyncio
async def test_infer_too_short_falls_back():
    s = await infer_deck_style(title="t", content_sample="c", llm=_StubLLM("blue"))
    assert s is _DEFAULT_STYLE


@pytest.mark.asyncio
async def test_infer_llm_exception_falls_back():
    s = await infer_deck_style(title="t", content_sample="c", llm=_RaisingLLM())
    assert s is _DEFAULT_STYLE


@pytest.mark.asyncio
async def test_infer_normalizes_whitespace():
    s = await infer_deck_style(
        title="t", content_sample="c",
        llm=_StubLLM("line one palette\n\n  line two   lighting texture"),
    )
    assert "\n" not in s.suffix
    assert "  " not in s.suffix


@pytest.mark.asyncio
async def test_infer_distinct_outputs_distinct_ids():
    a = await infer_deck_style(
        title="t", content_sample="c",
        llm=_StubLLM("teal flat editorial illustration palette"),
    )
    b = await infer_deck_style(
        title="t", content_sample="c",
        llm=_StubLLM("crimson baroque oil painting palette"),
    )
    assert a.style_id != b.style_id
```

- [ ] **Step 2: 跑測試確認 RED**

Run: `.venv/bin/python -m pytest tests/test_flux_style.py -q`
Expected: FAIL — `ImportError: cannot import name 'infer_deck_style'`.

- [ ] **Step 3: 實作 `infer_deck_style`**

在 `app/services/flux_style.py`：頂部 import 區（`from dataclasses import dataclass` 附近）加：

```python
import hashlib
import logging
from typing import Protocol

logger = logging.getLogger(__name__)
```

在檔尾（`get_style_descriptor` 之後）加：

```python
# ── Stage 3: content-inferred deck style ───────────────────────────────────
_STYLE_SYSTEM = """\
You are an art director. Given a presentation's title and a sample of its
source content, output ONE concise visual house-style descriptor for the
deck's slide illustrations: palette, illustration/render style, lighting,
mood, and composition. 12-40 words. Output ONLY the style phrase — no
preamble, no explanation, no quotes, no sentences describing the topic.
The illustrations must contain no text or letters.
"""

# Positive "no text" guard. FLUX is guidance-distilled and ignores negative
# prompts, so this must ride inside the positive suffix. Appended whenever the
# LLM's free-form style omits it.
_NO_TEXT_GUARD = "no text, no letters, no symbols, no signage"

_MAX_CONTENT_SAMPLE = 1500  # chars of source content fed to the LLM: keep it cheap
_MAX_SUFFIX_LEN = 400
_MIN_SUFFIX_LEN = 10


class _LLMCompleter(Protocol):
    """Minimal LLM contract: one completion. (Same shape as the rewriter's.)"""

    async def complete(self, *, system: str, user: str) -> str: ...


def _hash8(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:8]


def _normalize_suffix(text: str) -> str:
    """Collapse all whitespace (incl. newlines) to single spaces, trim, cap."""
    collapsed = " ".join(text.split())
    return collapsed[:_MAX_SUFFIX_LEN].strip()


async def infer_deck_style(
    *,
    title: str,
    content_sample: str,
    llm: _LLMCompleter,
) -> StyleDescriptor:
    """Infer one house style for the whole deck from its title + content.

    Free-form: the LLM writes a style phrase; we normalize it, guarantee the
    no-text guard, and derive a stable ``style_id`` from the suffix hash so the
    FLUX provider cache (contract 3.3) is deterministic for the same style.

    Any failure (LLM error, empty/too-short output) degrades to
    ``_DEFAULT_STYLE`` — style inference must never break a job.
    """
    sample = (content_sample or "")[:_MAX_CONTENT_SAMPLE]
    user = f"TITLE: {title}\nCONTENT:\n{sample}"
    try:
        raw = await llm.complete(system=_STYLE_SYSTEM, user=user)
    except Exception as e:  # noqa: BLE001
        logger.warning("Style inference LLM call failed: %s — using default", e)
        return _DEFAULT_STYLE

    suffix = _normalize_suffix(raw or "")
    if len(suffix) < _MIN_SUFFIX_LEN:
        logger.warning(
            "Style inference produced too-short suffix %r — using default", suffix
        )
        return _DEFAULT_STYLE

    if "no text" not in suffix.lower():
        suffix = f"{suffix}, {_NO_TEXT_GUARD}"

    return StyleDescriptor(style_id=f"auto-{_hash8(suffix)}", suffix=suffix)
```

- [ ] **Step 4: 跑測試確認 GREEN**

Run: `.venv/bin/python -m pytest tests/test_flux_style.py -q`
Expected: PASS（8 個）。

- [ ] **Step 5: Commit**

```bash
git add app/services/flux_style.py tests/test_flux_style.py
git commit -m "feat(studio-flux): 依內容自動推斷 deck 風格 infer_deck_style (spec §6)"
```

---

## Task 2: 透傳 `deck_style` 經 `_render_pptx` → `_hydrate_images`

**Files:**
- Modify: `myCSPPlatform/backend/app/api/studio.py`（`_hydrate_images` def ~1349、cover-hero ~1468；`_render_pptx` def ~1602、hydrate 呼叫 ~1640）

無獨立單元測試（接線層，由 §9 實機驗收涵蓋）。改動以 default 參數加入，行為向後相容。

- [ ] **Step 1: `_hydrate_images` 加參數**

在 `_hydrate_images` 簽名（~1349-1355）的 keyword-only 區塊末尾、`llm` 之後加一行參數：

```python
    llm: "_StudioLLMAdapter | None" = None,
    deck_style: "StyleDescriptor | None" = None,
```

（若 `StyleDescriptor` 尚未在該檔 import：cover-hero 區塊已 `from app.services.flux_style import get_style_descriptor`；在那行旁改成 `from app.services.flux_style import get_style_descriptor, StyleDescriptor` —— 但因型別只用於字串註解 `"StyleDescriptor | None"`，不 import 也可執行；為清楚仍建議補 import。確認 import 後不造成未使用警告即可。）

- [ ] **Step 2: cover-hero 區塊改用 `deck_style`**

把 ~1468 行：

```python
            style = get_style_descriptor(brand_id=None)
```

改為：

```python
            style = deck_style or get_style_descriptor()
```

- [ ] **Step 3: `_render_pptx` 加參數並透傳**

在 `_render_pptx` 簽名（~1602-1608）keyword-only 區塊末尾、`llm` 之後加：

```python
    llm: "_StudioLLMAdapter | None" = None,
    deck_style: "StyleDescriptor | None" = None,
```

在其 `_hydrate_images(...)` 呼叫（~1640-1648）的引數末尾、`llm=llm,` 之後加一行：

```python
            llm=llm,
            deck_style=deck_style,
```

- [ ] **Step 4: import 驗證**

Run: `.venv/bin/python -c "import app.api.studio"`
Expected: 無輸出、exit 0。

- [ ] **Step 5: Commit**

```bash
git add app/api/studio.py
git commit -m "feat(studio-flux): 透傳 deck_style 至 hydration cover-hero 路徑"
```

---

## Task 3: Job pipeline 推斷一次並傳入

**Files:**
- Modify: `myCSPPlatform/backend/app/api/studio.py`（render 呼叫 ~2806 之前的 pipeline 區塊）

無獨立單元測試（接線層，由 §9 實機驗收涵蓋）。

- [ ] **Step 1: 在 render 之前插入推斷**

在 `# ── Step 7: render ──`（~2804）那行**之前**插入：

```python
        # ── Stage 3: infer the deck's visual house style from its content,
        # once per deck, so every slide shares one visual language. Only when
        # the FLUX cover-hero path will actually run; failure degrades to the
        # default style inside infer_deck_style.
        deck_style = None
        if get_flux_provider() is not None and deck_base_seed is not None:
            from app.services.flux_style import infer_deck_style

            style_sample = spec.title or ""
            if chunks:
                style_sample += "\n" + "\n\n".join(
                    str(c.get("content", "")) for c in chunks
                )
            deck_style = await infer_deck_style(
                title=spec.title or "",
                content_sample=style_sample,
                llm=flux_llm,
            )
```

（`chunks` 在此 scope 必定已定義——pipeline 於 ~2690 以 `chunks: list[...] = []` 初始化；`flux_llm` 於 ~2678 定義；`deck_base_seed` 於 ~2675；`get_flux_provider` 已於檔內 import。）

- [ ] **Step 2: render 呼叫傳入 deck_style**

把 ~2806 的呼叫：

```python
        pptx_bytes, pptx_path = await _render_pptx(
            spec, images_lookup, deck_base_seed=deck_base_seed, llm=flux_llm,
        )
```

改為：

```python
        pptx_bytes, pptx_path = await _render_pptx(
            spec, images_lookup, deck_base_seed=deck_base_seed, llm=flux_llm,
            deck_style=deck_style,
        )
```

- [ ] **Step 3: import + 全測試驗證**

Run: `.venv/bin/python -c "import app.api.studio"` → 無輸出、exit 0。
Run: `.venv/bin/python -m pytest tests/test_flux_style.py tests/test_flux_quality_gate.py -q` → 全 PASS。

- [ ] **Step 4: Commit**

```bash
git add app/api/studio.py
git commit -m "feat(studio-flux): job pipeline 依內容推斷 deck 風格並注入 render (spec §6)"
```

---

## Task 4: 驗收（spec §9，rebuild 後、user 觸發）

**前置**：Task 1-3 已 commit。rebuild 需 user 授權/觸發：
`docker compose -f docker-compose-dev.yml up -d --build csp`

- [ ] **驗收 1**：user 觸發 job → `docker logs anila-platform-dev-csp-1` 看 cover 的 flux_prompt 尾巴帶推斷出的 suffix（且含 `no text`），`image_gen_meta.style_id` 形如 `auto-xxxxxxxx`。
- [ ] **驗收 2**：同專案重跑（低溫）→ 多數得相同 `style_id`、命中 cache。
- [ ] **驗收 3**：不同主題兩專案 → 推斷 suffix 明顯不同。
- [ ] **驗收 4**：推斷失敗情境（如 LLM 暫不可用）→ job 仍完成、退 `_DEFAULT_STYLE`、log 有 warning。

實機觸發一律 user 從 frontend 點，本機 read-only 撈 log。

---

## Self-Review

**Spec coverage：**
- §3 `infer_deck_style`（free-form、低溫、normalize、no-text 護欄、hash style_id、失敗退 default）→ Task 1 Step 3 + 測試。
- §4 內容樣本（title + chunks 截斷）+ 呼叫點（render 前、FLUX 啟用才推）→ Task 3 Step 1。
- §5 串接（`_render_pptx`/`_hydrate_images` 加參數、`deck_style or get_style_descriptor()`）→ Task 2。
- §8 測試計畫（7 案 + 決定性/護欄/正規化/例外）→ Task 1 Step 1（含 8 個測試，涵蓋全部）。
- §9 驗收 → Task 4。

**Placeholder scan：** 無 TBD/TODO；每段含實際 code 與指令。

**Type consistency：** `infer_deck_style(*, title, content_sample, llm) -> StyleDescriptor` 在實作、測試、pipeline 三處一致；`deck_style` 參數名在 `_hydrate_images`/`_render_pptx`/pipeline 一致；`style_id` 前綴 `auto-` 在實作與驗收一致；`_LLMCompleter.complete(*, system, user)` 與 pipeline 傳的 `flux_llm`（`_StudioLLMAdapter`，已被 rewriter 以同合約使用）相容。
