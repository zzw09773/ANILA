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

### Step 2 — `services/studio_config.py`(foundation,先做)
搬 Tunables ~15 常數:`STUDIO_TOP_K/MIN_SCORE/CONTENT_LIMIT_CHARS`、`STUDIO_IMAGE_TOP_K/MIN_SCORE`、
`SCHEMA_CORRECTION_PASSES`、`VISUAL_QA_PASSES`、`RENDERER_BASE_URL`、`SLIDES_LLM_MODEL`、
`VISION_LLM_MODEL`、`FLUX_GATE_*`、`MAX_GENERATED_IMAGES_PER_DECK` 等。studio.py `from ... import (全部)`。
⚠️ 散用各處 → import 必須完整;grep 每個常數名確認都 import 到。

### Step 3 — `services/studio_llm.py`(foundation,layout/vision 的前置)
`_count_hint`、`_build_generation_prompt`、`_call_llm_chat`、`_StudioLLMAdapter`、`_Gemma4VlmGate`。
依賴:csp_client(`proxy_chat_completions`)、schemas、llm_json、studio_config。
**這步做完才能抽 layout/vision**(它們呼叫 `_call_llm_chat`,否則循環依賴)。

### Step 4 — `services/studio_retrieval.py`
`_retrieve_chunks`、`_build_chunk_dicts`、`_retrieve_images`(用 csp_client + studio_config 常數)。單一 caller `_run_pipeline`。

### Step 5 — `services/studio_render.py`
`get_flux_provider`、`_gated_generate`、`_infer_image_use_case`、`_apply_illustration_fallback`、
`_generate_slide_illustration`、`_hydrate_images`、`_render_pptx`。依賴 flux_provider、csp_client、studio_config。

### Step 6 — `services/studio_vision_qa.py`
`_capture_screenshots`、`_inspect_slide_visually`、`_geometric_to_visual`、`_merge_defects`、
`_visual_qa`、`_fix_spec_with_defects`。依賴 geometric_qa、studio_llm、studio_config。

### Step 7 — `services/studio_layout.py`(最大塊 ~730 行)
`LayoutViolation`、`_apply_theme_title_override`、`_audit_layout_distribution`、`_should_rebalance`、
`_select_rebalance_candidates`、`_build_rebalance_prompt`、`_call_llm_for_rebalance`、
`_apply_rebalance_change`、`_rebalance_layouts`。依賴 schemas、studio_llm、studio_config。

### 收尾 — studio.py 留下
endpoints(`create/get/get_pptx/cancel_slides_job`)+ `_run_pipeline` 編排 + `_generate_validated_spec`
+ `_saturate_spec_dict` + `_build_fallback_spec`,全部 import 上述模組。目標 ~300-500 行。

## 完整驗證(每步 + 最後)
1. 每步:venv py_compile + import + 該模組功能 sanity。
2. 最後:**dev stack 重 build anila-studio image + 跑一次完整 slides 生成 pipeline**(POST /api/studio/slides/jobs → 輪詢 → 下載 pptx)確認 e2e 不變。
