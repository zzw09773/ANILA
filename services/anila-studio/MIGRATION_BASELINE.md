# anila-studio Migration Baseline

**Date**: 2026-05-23
**Branch**: `feature/anila-studio-extract`
**Source commits**: main @ `fcc6832`
**Phase**: 0 (baseline + skeleton)

本文件鎖定 Studio 從 myCSPPlatform/backend 抽出前的「綠燈基線」,後續 phase 必須維持或改善這個 baseline,不允許新增紅燈(已知 3 個紅燈例外)。

---

## Studio 相關 test 完整清單(20 個檔)

從 Phase 0 grep audit 出來的全部 studio-coupled test 檔,Phase 4 必須全部搬到 `anila-studio/tests/`:

```
tests/test_studio_saturation_schema.py
tests/test_studio_saturation_round2.py
tests/test_studio_saturation_degrade.py
tests/test_studio_flux_e2e.py
tests/test_studio_illustration_routing.py
tests/test_studio_text_normalizer.py
tests/test_studio_diagram_schema.py
tests/test_studio_theme_schema.py
tests/test_studio_theme_override.py
tests/test_layout_audit.py
tests/test_layout_rebalance.py
tests/test_theme_override.py
tests/test_slide_image_prompt.py
tests/test_hydrate_images.py
tests/test_flux_image_provider.py
tests/test_flux_quality_gate.py
tests/test_flux_style.py
tests/test_geometric_qa.py             ← v2 plan 漏列
tests/test_diagram_renderer.py         ← v2 plan 漏列
tests/test_flux_provider_wiring.py     ← v2 plan 漏列
```

Plan v2 原列 17 檔,實際 20 檔(+3)。

## Baseline 跑測結果

`pytest <20 個檔> --tb=no -q` 於 `feature/anila-studio-extract` 分支(尚未動任何 Studio 程式):

| 統計 | 數量 |
|---|---|
| **passed** | 198 |
| **failed** | 3 |
| **total** | 201 |
| **warnings** | 11(deprecated regex / Pydantic v1 config / async mock,不影響行為) |

### 已知紅燈(pre-existing,非本次 refactor 造成)

`tests/test_hydrate_images.py`:
1. `test_hydrate_image_prompt_calls_flux`
2. `test_cover_hero_path_generates_via_rewriter`
3. `test_mixed_slides_all_resolved`

**錯誤類型**: `AttributeError: 'coroutine' object has no attribute 'png_bytes'`(async 處理缺 await)+ `AssertionError: assert 'image_data' in {...}`(slide 沒注入 image_data)

**判定**: 既有問題,可能與最近 Stage 4 FLUX 改動 await 訊號路徑有關。這次 refactor **不修**,僅記錄為 baseline 例外。Phase 4 搬遷後若紅燈 ≤ 3 即合格;若新增其他紅燈需追溯到此次 refactor 並修正。

---

## Studio module 外部 import audit

grep `(from|import) app\.(api|services|schemas)\.(studio|flux_|studio_|diagram_renderer|geometric_qa)`:

### 外部 caller(非 studio 自家)

僅 **1 處** application code:
- `myCSPPlatform/backend/app/api/router.py:16` — `from app.api.studio import router as studio_router`
- **Phase 7(v2 順序)** 統一移除

### 內部 caller(studio 自家、跟著搬)

- 20 個 test 檔(已列上方)
- `app/services/flux_quality_gate.py:41` → `flux_image_provider`(同搬)
- `app/services/studio_text_normalizer.py:46` → `schemas/studio`(同搬)
- `app/services/flux_image_provider.py:35` → `schemas/studio`(同搬)
- `app/services/studio_job_service.py:41` → `schemas/studio`(同搬)
- `app/services/flux_prompt_rewriter.py:29-30` → `schemas/studio`、`flux_style`(同搬)
- `app/services/diagram_renderer.py:24` → `studio_text_normalizer`(同搬)
- `app/api/studio.py:79+94+97 + 1268+1314+1360+1378+1413-1414+1522-1523+2862` → flux/diagram/normalizer/schemas(同搬)

### 結論

**Studio 模組與 csp 其他子系統零交叉依賴**。Phase 7 刪檔風險可控:除 `router.py` 一行外,csp 端**沒有任何模組 import Studio**。

---

## 服務檔搬遷清單(10 個,Phase 3)

```
myCSPPlatform/backend/app/api/studio.py                       # 主檔,需改造 A→I
myCSPPlatform/backend/app/services/studio_job_service.py      # 純搬
myCSPPlatform/backend/app/services/studio_text_normalizer.py  # 純搬
myCSPPlatform/backend/app/services/diagram_renderer.py        # 純搬
myCSPPlatform/backend/app/services/geometric_qa.py            # 純搬
myCSPPlatform/backend/app/services/flux_image_provider.py     # 純搬
myCSPPlatform/backend/app/services/flux_prompt_rewriter.py    # 純搬
myCSPPlatform/backend/app/services/flux_quality_gate.py       # 純搬
myCSPPlatform/backend/app/services/flux_style.py              # 純搬
myCSPPlatform/backend/app/schemas/studio.py                   # 純搬
```

---

## 後續 Phase 對齊點

- **Phase 4 acceptance**: `pytest anila-studio/tests/` 紅燈集合 ⊆ 上述 3 個既有紅燈(即 `test_hydrate_images.py` 三個 case)。若 anila-studio 端搬完跑出新的紅燈,**必須修到綠**或證明是 Phase 5+ contract 變更導致,不能默默吞掉。
- **Phase 6 E2E**: user 從 browser 跑完整 deck → 跑出 PPT。
- **Phase 7 切換**: `app/api/router.py:16` 移除 import + 註冊。csp full pytest(扣 studio)綠。
