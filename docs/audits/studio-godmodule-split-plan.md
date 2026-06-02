# studio.py god-module 拆分計畫 — 2026-06-02

`anila-studio/app/api/studio.py` 原 3102 行。目標:拆成多個 cohesive 模組,
studio.py 只留 endpoints + `_run_pipeline` 編排(目標 ~300-500 行)。

**原則**:純程式碼搬移(behavior-preserving,不改邏輯)、import alias 保持呼叫點不變、
每步本地 venv `py_compile` + `import` + 功能 sanity 驗證,理想上配 dev stack 跑完整 pipeline。

**驗證管道**:anila-studio 有本地 `.venv`,`.venv/bin/python -c "import app.api.studio"`
可抓 import/module 級錯誤。⚠️ 但函式體內的 NameError(漏 import 常數)只在 runtime 浮現 →
每次抽常數/helper 必須**完整 import-back 全部名稱**,並 grep 確認無遺漏。

## 依賴順序(必須照順序,否則 circular import)

### ✅ Step 1（已完成 + committed)— `services/llm_json.py`
`_extract_json_object` / `_loads_lenient`(純函式,零 studio 依賴)。3102 → 3020 行。
studio.py import alias。同時是 datatables/infographics/mindmaps 去重可收斂的 canonical。

### ✅ Step 2（已完成 + committed)— `services/studio_config.py`(foundation,先做)
搬 Tunables 15 常數:`STUDIO_TOP_K/MIN_SCORE/CONTENT_LIMIT_CHARS`、`STUDIO_IMAGE_TOP_K/MIN_SCORE`、
`SCHEMA_CORRECTION_PASSES`、`VISUAL_QA_PASSES`、`RENDERER_BASE_URL`、`SLIDES_LLM_MODEL`、
`VISION_LLM_MODEL`、`FLUX_GATE_*`、`MAX_GENERATED_IMAGES_PER_DECK`、`CONTENT_ILLUSTRATION_MAX_BULLETS`。
3020 → 2985 行。studio.py re-import 全部(mindmaps 取 SLIDES_LLM_MODEL、測試取 MAX_GENERATED_IMAGES_PER_DECK)。

### ✅ Step 3（已完成 + committed)— `services/studio_llm.py`(foundation,layout/vision 的前置)
`_count_hint`(私有)、`build_generation_prompt`、`call_llm_chat`、`StudioLLMAdapter`、`Gemma4VlmGate`。
依賴:csp_client(`proxy_chat_completions`)、llm_json、studio_config(schemas 實際未用到)。2985 → 2470 行。
⚠️ 踩雷:blanket rename `_count_hint`→`count_hint` 與區域變數 `count_hint` 碰撞造成 UnboundLocalError,
故 `_count_hint`/`_PRESET_COUNT` 保持私有。

### ✅ Step 4（已完成 + committed)— `services/studio_retrieval.py`
`retrieve_chunks`、`retrieve_images`、`_build_chunk_dicts`(私有)。依賴 csp_client + studio_config。2470 → 2367 行。

### ✅ Step 5（已完成 + committed)— `services/studio_render.py`
`get_flux_provider`、`_gated_generate`、`_infer_image_use_case`、`_apply_illustration_fallback`、
`_generate_slide_illustration`、`_hydrate_images`、`_render_pptx` + `_FLUX_PROVIDER` 單例。保留底線名(零改名)。
2367 → 1865 行。⚠️ 測試交叉 monkeypatch:patch target 從 `app.api.studio` 改到 `app.services.studio_render`
(patch where looked up);singleton 搬家故測試改 reload `studio_render`;`fetch_image_blob` patch 改 studio_render。

### ✅ Step 6（已完成 + committed)— `services/studio_vision_qa.py`
`visual_qa`、`fix_spec_with_defects`(公開,re-export)+ `_capture_screenshots`、`_inspect_slide_visually`、
`_geometric_to_visual`、`_merge_defects`(私有)。依賴 geometric_qa、studio_llm、llm_json、studio_config。無測試耦合。1865 → 1638 行。

### ✅ Step 7（已完成 + committed)— `services/studio_layout.py`(最大塊)
`LayoutViolation`、`_apply_theme_title_override`、`_audit_layout_distribution`、`_should_rebalance`、
`_select_rebalance_candidates`、`_build_rebalance_prompt`、`_call_llm_for_rebalance`、
`_apply_rebalance_change`、`_rebalance_layouts` + LAYOUT_*/正則常數。保留底線名。依賴 schemas、studio_llm、studio_config、llm_json。
1638 → 917 行。測試改從 studio_layout import + patch `_call_llm_for_rebalance` 於 studio_layout。

### ✅ 收尾完成 — studio.py 留下(913 行)
endpoints(`create/get/get_pptx/cancel_slides_job`)+ `_run_pipeline` 編排 + `_generate_validated_spec`
+ `_saturate_spec_dict` + `_build_fallback_spec`,全部 import 上述 7 個 services 模組。
(原訂 300-500 行;實際 913,因 `_saturate_spec_dict`/`_build_fallback_spec`/`_generate_validated_spec`/`_run_pipeline`
這四個按計畫留存的函式本身就佔 ~750 行 —— 它們是「編排 + spec 安全網」邏輯,屬 studio.py 本職。)

## 完整驗證(每步 + 最後)
1. ✅ 每步:venv py_compile + `import app.api.studio` + 完整 import-back/re-export identity + runtime-invoke
   helper + grep 無遺漏/死 import。全程全套測試 **495 passed**(3 個 test_hydrate_images FLUX 失敗為 baseline 既有,
   與本拆分無關,stash 比對確認)。
2. ✅ `import app.main` 走完 router 註冊鏈,7 模組 import graph 全綠,studio 4 endpoints 健在。
3. ⏳ **待 user**:dev stack 重 build anila-studio image + 從前端跑一次完整 slides pipeline
   (POST → 輪詢 → 下載 pptx)確認 e2e 不變(rebuild 需授權;觸發由 user 從前端,我只 read-only 撈 log)。
