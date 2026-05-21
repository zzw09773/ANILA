# Stage 4 — 插圖全用途路由（後端）實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓內文與章節插圖也走封面那條 rewriter + deck_style + 品質閘門路徑，抽共用 helper、use_case 參數化，並加 per-deck 生圖上限保護。

**Architecture:** 在 `studio.py` 抽三個模組級函式（`_infer_image_use_case`、`_apply_illustration_fallback`、`_generate_slide_illustration`），把 `_hydrate_images` 裡「封面 hero 專用段」+「舊 Path 3」改寫成統一的 illustration routing（判定 wants_illustration → 算 use_case → 上限檢查 → helper → 失敗走 fallback）。helper 失敗只回 False、不寫 fallback meta；fallback 由 routing 端的 `_apply_illustration_fallback` 統一處理（唯一 fallback 寫入點）。

**Tech Stack:** Python 3.12, FastAPI, pytest + pytest-asyncio。backend venv：`myCSPPlatform/backend/.venv`。

**Spec:** `docs/superpowers/specs/2026-05-21-stage4-illustration-routing-design.md`

**所有指令於 `myCSPPlatform/backend/` 下執行，python 用 `.venv/bin/python`。git attribution 全域關閉、勿加 Co-Authored-By、勿用 --no-verify。**

**鐵則（spec 釘死，違反即錯）：**
- **不得修改** `app/schemas/studio.py` 的 `_check_image_kind_consistency` validator（決策 A）。
- helper 一律用 `title`+`bullets` 經 rewriter 重寫，**不讀** slide 的 `image_prompt` 當生成輸入。
- `concept_en` 沿用 Stage 1 做法（傳整串 `flux_prompt`），**本階段不改 VLM concept 粒度**。

---

## 檔案結構

- Modify: `myCSPPlatform/backend/app/api/studio.py`
  - 常數區（~149-154，FLUX_GATE_* 旁）：加 `MAX_GENERATED_IMAGES_PER_DECK = 15`。
  - `_hydrate_images`（def ~1349）**之前**：新增 `_infer_image_use_case`、`_apply_illustration_fallback`、`_generate_slide_illustration` 三個模組級函式。
  - `_hydrate_images` 迴圈：`generated_count` 初始化 + 改寫 routing（取代 ~1457-1599 的封面段 + Path 3）。
- Create (test): `myCSPPlatform/backend/tests/test_studio_illustration_routing.py`

模組級可用名稱（已存在）：`base64`(54)、`logger`、`_Gemma4VlmGate`(818)、`_gated_generate`(1286)、`FLUX_GATE_MAX_RETRIES`(149)。`derive_flux_prompt`/`get_style_descriptor`/`ImageUseCase` 在新函式內以區域 import 取得（與既有 `_hydrate_images` 一致，且利於測試 monkeypatch）。

---

## Task 1: `_infer_image_use_case`（純函式）

**Files:**
- Create: `myCSPPlatform/backend/tests/test_studio_illustration_routing.py`
- Modify: `myCSPPlatform/backend/app/api/studio.py`

- [ ] **Step 1: 寫失敗測試**

建立 `tests/test_studio_illustration_routing.py`：

```python
"""Stage 4 — illustration routing (use_case inference / fallback / helper / routing)."""
from __future__ import annotations

import pytest

from app.api.studio import _infer_image_use_case
from app.schemas.studio import ImageUseCase


def test_use_case_idx0_is_hero():
    assert _infer_image_use_case(0, {"layout_kind": "standard"}) is ImageUseCase.COVER_HERO


def test_use_case_idx0_section_break_still_hero():
    # cover is commonly tagged layout_kind=section_break; idx 0 wins → HERO.
    assert _infer_image_use_case(0, {"layout_kind": "section_break"}) is ImageUseCase.COVER_HERO


def test_use_case_layout_cover_is_hero():
    assert _infer_image_use_case(3, {"layout_kind": "cover"}) is ImageUseCase.COVER_HERO


def test_use_case_section_break_is_band():
    assert _infer_image_use_case(2, {"layout_kind": "section_break"}) is ImageUseCase.SECTION_BAND


def test_use_case_default_is_content():
    assert _infer_image_use_case(4, {"layout_kind": "standard"}) is ImageUseCase.CONTENT_ILLUSTRATION
```

- [ ] **Step 2: 跑測試確認 RED**

Run: `.venv/bin/python -m pytest tests/test_studio_illustration_routing.py -q`
Expected: FAIL — `ImportError: cannot import name '_infer_image_use_case'`.

- [ ] **Step 3: 實作（加在 `_hydrate_images` def 之前）**

在 `app/api/studio.py`，`async def _hydrate_images(` 那行**之前**加：

```python
def _infer_image_use_case(idx: int, slide: dict) -> "ImageUseCase":
    """Map a slide to its FLUX use_case. Order matters: idx 0 / layout 'cover'
    is the hero even when also tagged section_break."""
    from app.schemas.studio import ImageUseCase

    if idx == 0 or slide.get("layout_kind") == "cover":
        return ImageUseCase.COVER_HERO
    if slide.get("layout_kind") == "section_break":
        return ImageUseCase.SECTION_BAND
    return ImageUseCase.CONTENT_ILLUSTRATION
```

- [ ] **Step 4: 跑測試確認 GREEN**

Run: `.venv/bin/python -m pytest tests/test_studio_illustration_routing.py -q`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add app/api/studio.py tests/test_studio_illustration_routing.py
git commit -m "feat(studio-flux): _infer_image_use_case 推斷插圖 use_case (spec §7)"
```

---

## Task 2: `_apply_illustration_fallback`（唯一 fallback 寫入點）

**Files:**
- Modify: `myCSPPlatform/backend/app/api/studio.py`
- Modify: `myCSPPlatform/backend/tests/test_studio_illustration_routing.py`

- [ ] **Step 1: 追加失敗測試**

在 `tests/test_studio_illustration_routing.py` 末尾追加：

```python
from app.api.studio import _apply_illustration_fallback


def test_fallback_hero_label_and_drops_image():
    slide = {"image_data": "x", "image_prompt": "p", "image_kind": "illustration"}
    _apply_illustration_fallback(slide, ImageUseCase.COVER_HERO)
    assert slide["image_gen_meta"]["fallback"] == "solid_theme_cover"
    assert slide["image_gen_meta"]["use_case"] == "cover_hero"
    assert "image_data" not in slide
    assert "image_prompt" not in slide
    assert "image_kind" not in slide


def test_fallback_band_label():
    slide = {}
    _apply_illustration_fallback(slide, ImageUseCase.SECTION_BAND)
    assert slide["image_gen_meta"]["fallback"] == "theme_section_break"


def test_fallback_content_label():
    slide = {"diagram_dot": "d"}
    _apply_illustration_fallback(slide, ImageUseCase.CONTENT_ILLUSTRATION)
    assert slide["image_gen_meta"]["fallback"] == "text_only"
    assert "diagram_dot" not in slide
```

- [ ] **Step 2: 跑測試確認 RED**

Run: `.venv/bin/python -m pytest tests/test_studio_illustration_routing.py -q`
Expected: FAIL — `ImportError: cannot import name '_apply_illustration_fallback'`.

- [ ] **Step 3: 實作（加在 `_infer_image_use_case` 之後）**

```python
def _apply_illustration_fallback(slide: dict, use_case: "ImageUseCase") -> None:
    """No usable image for this slide: drop image fields so the renderer
    degrades (theme cover / theme section break / text-only standard layout),
    and record the fallback in image_gen_meta. Never sets image_data.

    This is the single fallback writer — the generation helper returns False
    without writing meta, and the routing calls this for both gate-failure and
    the per-deck cap.
    """
    from app.schemas.studio import ImageUseCase

    label = {
        ImageUseCase.COVER_HERO: "solid_theme_cover",
        ImageUseCase.SECTION_BAND: "theme_section_break",
        ImageUseCase.CONTENT_ILLUSTRATION: "text_only",
    }[use_case]
    meta = slide.get("image_gen_meta") or {}
    meta.setdefault("use_case", use_case.value)
    meta["fallback"] = label
    slide["image_gen_meta"] = meta
    slide.pop("image_data", None)
    slide.pop("image_prompt", None)
    slide.pop("image_kind", None)
    slide.pop("diagram_dot", None)
```

- [ ] **Step 4: 跑測試確認 GREEN**

Run: `.venv/bin/python -m pytest tests/test_studio_illustration_routing.py -q`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add app/api/studio.py tests/test_studio_illustration_routing.py
git commit -m "feat(studio-flux): _apply_illustration_fallback 統一 fallback 寫入 (spec §6)"
```

---

## Task 3: `_generate_slide_illustration`（共用生成 helper）

**Files:**
- Modify: `myCSPPlatform/backend/app/api/studio.py`
- Modify: `myCSPPlatform/backend/tests/test_studio_illustration_routing.py`

- [ ] **Step 1: 追加失敗測試**

在測試檔末尾追加：

```python
import app.api.studio as studio_mod
from app.services.flux_image_provider import GeneratedImage


class _StubVlm:
    def __init__(self, *a, **k):  # accepts _Gemma4VlmGate(db, user) shape
        pass


def _patch_vlm(monkeypatch):
    monkeypatch.setattr(studio_mod, "_Gemma4VlmGate", _StubVlm)


def _patch_rewriter(monkeypatch, returns):
    async def _fake(*, title, bullets, use_case, style, llm):
        _fake.seen = {"title": title, "bullets": bullets, "use_case": use_case}
        return returns
    monkeypatch.setattr(
        "app.services.flux_prompt_rewriter.derive_flux_prompt", _fake
    )
    return _fake


@pytest.mark.asyncio
async def test_helper_success_sets_image_and_meta(monkeypatch):
    _patch_vlm(monkeypatch)
    _patch_rewriter(monkeypatch, "a calm teal abstract scene")
    accepted = GeneratedImage(png_bytes=b"\x89PNGfake", seed=1007, accepted=True)
    accepted.vlm_verdict = {"match": True, "has_text": False, "score": 0.9}

    async def _fake_gate(provider, prompt, *, use_case, seed, style_id, concept_en, vlm):
        return accepted, 0
    monkeypatch.setattr(studio_mod, "_gated_generate", _fake_gate)

    slide = {"title": "Resilience", "bullets": ["a", "b"], "image_prompt": "IGNORED"}
    ok = await studio_mod._generate_slide_illustration(
        slide, idx=7, use_case=ImageUseCase.CONTENT_ILLUSTRATION,
        deck_style=None, flux_provider=object(), deck_base_seed=1000, llm=object(),
    )
    assert ok is True
    assert slide["image_data"].startswith("data:image/png;base64,")
    assert slide["image_gen_meta"]["use_case"] == "content_illustration"
    assert slide["image_gen_meta"]["vlm_verdict"]["score"] == 0.9


@pytest.mark.asyncio
async def test_helper_uses_title_bullets_not_image_prompt(monkeypatch):
    _patch_vlm(monkeypatch)
    fake = _patch_rewriter(monkeypatch, "scene")
    accepted = GeneratedImage(png_bytes=b"x", seed=1, accepted=True)
    accepted.vlm_verdict = {"match": True, "has_text": False, "score": 1.0}

    async def _fake_gate(provider, prompt, *, use_case, seed, style_id, concept_en, vlm):
        return accepted, 0
    monkeypatch.setattr(studio_mod, "_gated_generate", _fake_gate)

    slide = {"title": "T", "bullets": ["x"], "image_prompt": "DO NOT USE"}
    await studio_mod._generate_slide_illustration(
        slide, idx=1, use_case=ImageUseCase.CONTENT_ILLUSTRATION,
        deck_style=None, flux_provider=object(), deck_base_seed=1000, llm=object(),
    )
    assert fake.seen["title"] == "T"
    assert fake.seen["bullets"] == ["x"]


@pytest.mark.asyncio
async def test_helper_rewriter_none_returns_false(monkeypatch):
    _patch_vlm(monkeypatch)
    _patch_rewriter(monkeypatch, None)  # USE_GRAPHVIZ / stripped empty
    slide = {"title": "T", "bullets": ["x"]}
    ok = await studio_mod._generate_slide_illustration(
        slide, idx=1, use_case=ImageUseCase.CONTENT_ILLUSTRATION,
        deck_style=None, flux_provider=object(), deck_base_seed=1000, llm=object(),
    )
    assert ok is False
    assert "image_data" not in slide


@pytest.mark.asyncio
async def test_helper_gate_reject_returns_false(monkeypatch):
    _patch_vlm(monkeypatch)
    _patch_rewriter(monkeypatch, "scene")

    async def _fake_gate(provider, prompt, *, use_case, seed, style_id, concept_en, vlm):
        return None, 3
    monkeypatch.setattr(studio_mod, "_gated_generate", _fake_gate)
    slide = {"title": "T", "bullets": ["x"]}
    ok = await studio_mod._generate_slide_illustration(
        slide, idx=1, use_case=ImageUseCase.CONTENT_ILLUSTRATION,
        deck_style=None, flux_provider=object(), deck_base_seed=1000, llm=object(),
    )
    assert ok is False
    assert "image_data" not in slide
```

- [ ] **Step 2: 跑測試確認 RED**

Run: `.venv/bin/python -m pytest tests/test_studio_illustration_routing.py -q`
Expected: FAIL — `AttributeError: module 'app.api.studio' has no attribute '_generate_slide_illustration'`.

- [ ] **Step 3: 實作（加在 `_apply_illustration_fallback` 之後）**

```python
async def _generate_slide_illustration(
    slide: dict,
    *,
    idx: int,
    use_case: "ImageUseCase",
    deck_style: "StyleDescriptor | None",
    flux_provider: "FluxImageProvider",
    deck_base_seed: int,
    llm: "_StudioLLMAdapter",
) -> bool:
    """Rewrite → gate → write image. Returns True iff an image was placed.

    On any failure (rewriter raised / USE_GRAPHVIZ / gate raised / all
    candidates rejected) returns False WITHOUT writing fallback meta — the
    caller invokes _apply_illustration_fallback. ``concept_en`` keeps the
    Stage 1 convention (the whole flux_prompt); the slide's own image_prompt
    is intentionally ignored (decision A) — title+bullets drive the rewriter.
    """
    from app.services.flux_prompt_rewriter import derive_flux_prompt
    from app.services.flux_style import get_style_descriptor

    style = deck_style or get_style_descriptor()
    title = slide.get("title", "")
    try:
        flux_prompt = await derive_flux_prompt(
            title=title,
            bullets=slide.get("bullets", []),
            use_case=use_case,
            style=style,
            llm=llm,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "FLUX rewriter failed for slide '%s' (%s): %s",
            title or "<untitled>", use_case.value, e,
        )
        return False
    if not flux_prompt:  # USE_GRAPHVIZ or stripped empty
        logger.info(
            "FLUX rewriter returned no prompt for slide '%s' (%s) — fallback.",
            title or "<untitled>", use_case.value,
        )
        return False

    seed = deck_base_seed + idx
    vlm = _Gemma4VlmGate(llm._db, llm._user)
    try:
        best, retry_count = await _gated_generate(
            flux_provider,
            flux_prompt,
            use_case=use_case,
            seed=seed,
            style_id=style.style_id,
            concept_en=flux_prompt,
            vlm=vlm,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "FLUX gated generation errored for slide '%s' (%s): %s",
            title or "<untitled>", use_case.value, e,
        )
        return False
    if best is None:
        logger.warning(
            "FLUX gate rejected all candidates for slide '%s' (%s) after %d "
            "retries — fallback.", title or "<untitled>", use_case.value, retry_count,
        )
        return False

    slide["image_data"] = (
        "data:image/png;base64," + base64.b64encode(best.png_bytes).decode("ascii")
    )
    slide["image_gen_meta"] = {
        "use_case": use_case.value,
        "flux_prompt": flux_prompt,
        "seed": best.seed,
        "style_id": style.style_id,
        "clip_score": best.clip_score,
        "vlm_verdict": best.vlm_verdict,
        "retry_count": retry_count,
    }
    slide.pop("image_prompt", None)
    slide.pop("image_kind", None)
    slide.pop("diagram_dot", None)
    return True
```

- [ ] **Step 4: 跑測試確認 GREEN**

Run: `.venv/bin/python -m pytest tests/test_studio_illustration_routing.py -q`
Expected: all pass (12).

- [ ] **Step 5: Commit**

```bash
git add app/api/studio.py tests/test_studio_illustration_routing.py
git commit -m "feat(studio-flux): _generate_slide_illustration 共用生成 helper (spec §3)"
```

---

## Task 4: routing 改寫 + per-deck 上限

**Files:**
- Modify: `myCSPPlatform/backend/app/api/studio.py`（常數區、`_hydrate_images` 迴圈）
- Modify: `myCSPPlatform/backend/tests/test_studio_illustration_routing.py`

- [ ] **Step 1: 追加 routing 失敗測試**

在測試檔末尾追加：

```python
@pytest.mark.asyncio
async def test_routing_triggers_only_illustration_slides(monkeypatch):
    calls = []

    async def _spy(slide, *, idx, use_case, **kw):
        calls.append((idx, use_case))
        return True
    monkeypatch.setattr(studio_mod, "_generate_slide_illustration", _spy)

    slides = [
        {"title": "Cover", "bullets": ["x"], "layout_kind": "section_break"},     # idx0 → HERO
        {"title": "Plain", "bullets": ["x"], "layout_kind": "standard"},          # no marker → skip
        {"title": "Sec", "bullets": ["x"], "layout_kind": "section_break"},       # idx2 → BAND
        {"title": "Ill", "bullets": ["x"], "layout_kind": "standard",
         "image_prompt": "p"},                                                    # idx3 → CONTENT
    ]
    out = await studio_mod._hydrate_images(
        {"slides": slides}, {}, "/tmp",
        flux_provider=object(), deck_base_seed=1000, llm=object(),
    )
    assert [c[0] for c in calls] == [0, 2, 3]  # plain slide skipped
    assert calls[0][1] is ImageUseCase.COVER_HERO
    assert calls[1][1] is ImageUseCase.SECTION_BAND
    assert calls[2][1] is ImageUseCase.CONTENT_ILLUSTRATION


@pytest.mark.asyncio
async def test_routing_per_deck_cap(monkeypatch):
    from app.api.studio import MAX_GENERATED_IMAGES_PER_DECK

    calls = []

    async def _spy(slide, *, idx, use_case, **kw):
        calls.append(idx)
        return True
    monkeypatch.setattr(studio_mod, "_generate_slide_illustration", _spy)

    n = MAX_GENERATED_IMAGES_PER_DECK + 5
    slides = [
        {"title": f"S{i}", "bullets": ["x"], "layout_kind": "standard",
         "image_prompt": "p"}
        for i in range(n)
    ]
    out = await studio_mod._hydrate_images(
        {"slides": slides}, {}, "/tmp",
        flux_provider=object(), deck_base_seed=1000, llm=object(),
    )
    assert len(calls) == MAX_GENERATED_IMAGES_PER_DECK
    # slides past the cap got fallback meta, not a helper call
    capped = out["slides"][MAX_GENERATED_IMAGES_PER_DECK]
    assert capped["image_gen_meta"]["fallback"] == "text_only"
```

- [ ] **Step 2: 跑測試確認 RED**

Run: `.venv/bin/python -m pytest tests/test_studio_illustration_routing.py -q`
Expected: FAIL — `ImportError: cannot import name 'MAX_GENERATED_IMAGES_PER_DECK'`（且 routing 行為未改）。

- [ ] **Step 3: 加常數**

在 `app/api/studio.py` 的 FLUX_GATE 常數附近（`FLUX_GATE_SEED_STRIDE = 1024` 之後）加：

```python
# Stage 4: hard ceiling on generated images per deck. Beyond this, remaining
# illustration slides take the theme/text fallback instead of spending GPU.
# Protects against runaway latency on decks with many section breaks.
MAX_GENERATED_IMAGES_PER_DECK = 15
```

- [ ] **Step 4: 改寫 routing 迴圈**

在 `_hydrate_images` 的 `for idx, slide in enumerate(slides):` 那行**之前**，加迴圈計數初始化：

```python
    slides = spec_dict.get("slides") or []
    generated_count = 0
    for idx, slide in enumerate(slides):
```
（若 `slides = spec_dict.get("slides") or []` 已存在，只在它與 `for` 之間插入 `generated_count = 0`。）

將現有「Path 4b 封面 hero 段 + Path 3」整段（從 `# Path 4b: FLUX Stage 1 cover hero.` 註解到 Path 3 結尾，即原 ~1457 到 `slide.pop("image_kind", None)` Path 3 except 區塊結束、`return spec_dict` 之前）**整段替換**為：

```python
        # Path 4: FLUX illustration — cover hero / section band / content,
        # all through the same rewriter + deck_style + quality gate. Replaces
        # the Stage 1 cover-only block and the legacy image_prompt Path 3.
        wants_illustration = (
            idx == 0
            or slide.get("layout_kind") in ("cover", "section_break")
            or slide.get("image_kind") == "illustration"
            or bool(slide.get("image_prompt"))
        )
        toolchain_ready = (
            flux_provider is not None
            and deck_base_seed is not None
            and llm is not None
        )
        if wants_illustration and toolchain_ready:
            # Order matters: compute use_case BEFORE the cap check (the cap
            # fallback needs it).
            use_case = _infer_image_use_case(idx, slide)
            if generated_count >= MAX_GENERATED_IMAGES_PER_DECK:
                logger.warning(
                    "per-deck image cap %d reached; slide %d (%s) -> fallback",
                    MAX_GENERATED_IMAGES_PER_DECK, idx, use_case.value,
                )
                _apply_illustration_fallback(slide, use_case)
                continue
            generated_count += 1
            ok = await _generate_slide_illustration(
                slide,
                idx=idx,
                use_case=use_case,
                deck_style=deck_style,
                flux_provider=flux_provider,
                deck_base_seed=deck_base_seed,
                llm=llm,
            )
            if not ok:
                _apply_illustration_fallback(slide, use_case)
            continue
        if wants_illustration and slide.get("image_prompt"):
            # FLUX toolchain not wired for this deployment but a legacy prompt
            # is present: drop it so the renderer doesn't act on an unused field.
            slide.pop("image_prompt", None)
            slide.pop("image_kind", None)
```

- [ ] **Step 5: 清掉 `_hydrate_images` 內變成未用的區域 import**

改寫後，`_hydrate_images` 函式體內原本的區域 import 可能不再被使用（rewriter/style/use_case 已移進 helper）。檢查函式內是否仍引用：
Run: `.venv/bin/python - <<'PY'
import ast, pathlib
src = pathlib.Path("app/api/studio.py").read_text()
print("derive_flux_prompt uses:", src.count("derive_flux_prompt"))
print("get_style_descriptor uses:", src.count("get_style_descriptor"))
print("ImageUseCase uses:", src.count("ImageUseCase"))
PY`
這些名稱在 helper 內仍各有區域 import 與使用，所以**模組整體**計數 > 1 是正常。重點是 `_hydrate_images` **函式體**內若不再直接使用它們，移除該函式頂部對應的區域 import（`from app.services.flux_prompt_rewriter import derive_flux_prompt`、`from app.services.flux_style import get_style_descriptor, StyleDescriptor`、以及 `from app.schemas.studio import ImageUseCase` 中未再用到的部分）。**保留** `import base64`、`from app.services.diagram_renderer import render_dot_to_png`（Path 2 仍用）。若不確定是否仍被引用，保守保留，交由 code review 處理——勿誤刪仍在使用的 import。

- [ ] **Step 6: 跑測試 + import 驗證**

Run: `.venv/bin/python -m pytest tests/test_studio_illustration_routing.py tests/test_flux_style.py tests/test_flux_quality_gate.py -q`
Expected: 全 PASS。
Run: `.venv/bin/python -c "import app.api.studio"` → 無輸出、exit 0。

- [ ] **Step 7: Commit**

```bash
git add app/api/studio.py tests/test_studio_illustration_routing.py
git commit -m "feat(studio-flux): 內文/章節插圖統一走 rewriter+閘門路由 + per-deck 上限 (spec §7)"
```

---

## Task 5: 驗收（spec §10，rebuild 後、user 觸發）

**前置**：Task 1-4 已 commit。rebuild 需 user 授權/觸發：
`docker compose -f docker-compose-dev.yml up -d --build csp`

- [ ] **驗收 1**：跑含封面 + 多章節扉頁 + 內文插圖的 deck → log 顯示三種 use_case（cover_hero / section_band / content_illustration）都經 gate，圖無文字、套同一 deck_style。
- [ ] **驗收 2**：含 `image_kind="diagram"` 的 slide → 仍走 graphviz、不受影響。
- [ ] **驗收 3**：章節扉頁 BAND 圖在 renderer（pptx）正常全幅顯示、標題可讀（驗證 server.js `renderSectionBreak` 無需改）。
- [ ] **驗收 4**：人為造 > 15 張圖需求的 deck → 第 16 張起 log 有 `per-deck image cap` warning、走 fallback、job 正常完成。
- [ ] **驗收 5**：記錄單 deck 端到端時間（≤10 分為目標、不阻擋）。

實機觸發一律 user 從 frontend 點，本機 read-only 撈 log。

---

## Self-Review

**Spec coverage：**
- §3.1 `_infer_image_use_case`（順序）→ Task 1。
- §3.2 helper（rewriter→gate→write，concept_en 沿用，回 bool）→ Task 3（採 helper 失敗只回 False、不寫 meta 的版本，與 §4 `if not ok: _apply_illustration_fallback` 對齊）。
- §4 routing（wants_illustration 訊號、use_case 先算、上限、Path1/2 互斥 continue 鏈）→ Task 4 Step 4。
- §5 上限常數 + 計數語意 → Task 4 Step 3 + routing。
- §6 fallback 對映表 → Task 2。
- §7 renderer 驗證 → Task 5 驗收 3。
- §9 測試（infer 4 / fallback 3 / helper 4 含「用 title+bullets 非 image_prompt」/ routing 上限 + 觸發）→ Task 1-4 測試。
- §10 驗收 → Task 5。
- 鐵則：不動 validator（計畫多處明示）、helper 不讀 image_prompt（Task 3 測試斷言）、concept_en 沿用（Task 3 docstring + 實作）。

**Placeholder scan：** Task 3 Step 1 有一個明確標示要刪除的 placeholder 行（已在步驟內指示刪除）；其餘無 TBD/TODO。

**Type consistency：** `_infer_image_use_case(idx, slide) -> ImageUseCase`、`_apply_illustration_fallback(slide, use_case)`、`_generate_slide_illustration(slide, *, idx, use_case, deck_style, flux_provider, deck_base_seed, llm) -> bool`、`MAX_GENERATED_IMAGES_PER_DECK` 在實作、測試、routing 三處一致；fallback label 字串（solid_theme_cover / theme_section_break / text_only）在 Task 2 實作與 Task 4 測試一致；helper 對 `_gated_generate` 的呼叫參數與既有簽名（`flux_provider, flux_prompt, *, use_case, seed, style_id, concept_en, vlm`）一致。
