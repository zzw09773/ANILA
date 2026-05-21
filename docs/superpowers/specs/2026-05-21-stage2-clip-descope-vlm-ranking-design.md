# Stage 2 Quality Gate — 去除 CLIP 閘、VLM 打分排序

**Date:** 2026-05-21
**Branch:** feature/studio-flux
**Spec context:** `docs/superpowers/plans/ANILA_Studio_FLUX_Spec.md` §5（Stage 2 — Quality Gate / Layer C）
**Status:** 設計已核准，待寫實作計畫

---

## 1. 背景與決策

Stage 2 品質閘門（Layer C）原設計三道閘門（spec 5.1）：CLIPScore → striping → VLM。
其中：

- **striping** 已於 2026-05-21 完成校準（`HF_ENERGY_THRESH=0.861`，commit `a1cbaef`）。
- **VLM**（gemma4）已部署並接線，做語意 `match` + `has_text` 檢查。
- **CLIP** 整道仍是 stub：`stub_clip_scorer` 永遠回 `100.0`，不擋任何候選。

部署真 CLIP（`openai/clip-vit-base-patch16` / 多語 SigLIP）需從零建 infra：backend venv 無
`torch/torchmetrics/transformers`、機器無 clip/siglip 權重快取、無現成 CLIP 服務。

**決策**：**去除 CLIP 閘**，語意把關交給已部署的 gemma4 VLM、pixel artifact 交給 striping。
理由：gemma4 VLM 已涵蓋 CLIP 的核心職責（圖文語意是否相符）；CLIP 多出的價值（便宜數值預篩、
N 候選排序分數）以「讓 VLM 多回一個排序分數」替代即可，不值得為此建模型服務。

**N 候選排序**：原本 `best = max(clip_score)`。CLIP 拿掉後，改由 **gemma4 VLM 回傳 0–1 品質分數**排序。

**已知限制**：gemma4 自評 `score` 是粗略、未校準的 tiebreaker。N=2 時足夠；若日後需要更準的排序/
預篩，再回頭部署真 CLIP（保留的合約欄位讓這條路不需重寫，見 §3）。

---

## 2. 範圍

**In scope**
- `_Gemma4VlmGate.check`（`app/api/studio.py`）：prompt + 解析加 `score`（0–1）。
- `app/services/flux_quality_gate.py`：移除 CLIP；閘門順序改 striping → VLM；排序用 VLM score。
- `_gated_generate`（`app/api/studio.py`）：移除 `clip_scorer` 注入與 import。
- 測試（`tests/test_flux_quality_gate.py`）：移除 CLIP 測試、改寫 gate 簽名相關測試、新增排序/短路/fallback 測試。
- spec 5.6 驗收。

**Out of scope**
- 真 CLIP / SigLIP 部署（明確 de-scope）。
- 改動 `GeneratedImage.clip_score` 的 schema 欄位（3.5 鎖死合約，保留欄位、恆 None）。
- Stage 3 / Stage 4。

---

## 3. 鎖死合約的處理（spec 3.5）

`GeneratedImage.clip_score`（與 Slide audit schema 的 `clip_score`）是 spec 3.5 一次定死的欄位。
**保留欄位、值恆為 `None`**，不破壞 audit table / 前端「重新生成」/ cache 除錯。排序改讀
`vlm_verdict["score"]`，不再依賴 `clip_score`。如此「日後要上真 CLIP」時欄位仍在、可直接回填。

---

## 4. 元件設計

### 4.1 VLM gate 加分數（`_Gemma4VlmGate.check`）

回傳 dict 新增 `score`：

```
回傳: {"match": bool, "has_text": bool, "score": float(0..1), "reason": str}
```

- prompt 要求 gemma4 多回一個 `score`：在「乾淨、無文字、貼切呈現該概念」的前提下，這張圖有多好（0–1）。
- 解析：`score` clamp 到 [0,1]；缺值或非數值 → `0.0`。
- unparseable（沿用現有 fail-closed）：`{"match": False, "has_text": True, "score": 0.0, "reason": "unparseable"}`。

### 4.2 `gate_candidates`（`flux_quality_gate.py`）

- 簽名移除 `clip_scorer: ClipScorer`。
- 同時刪除 `ClipScorer` Protocol、`stub_clip_scorer`、`CLIP_THRESHOLD` 常數。
- 每個候選的閘門順序：**(1) striping（純 CV，便宜，先短路) → (2) VLM**。
- 接受條件：striping 未觸發 且 VLM `match` 為真 且 `has_text` 為否。
- 排序：`best = max(accepted, key=lambda c: c.vlm_verdict.get("score", 0.0))`。
- `c.clip_score` 不再設值（恆 None）。audit 欄位 `c.vlm_verdict` 照舊回填（現含 score）。

### 4.3 wiring（`_gated_generate`）

- `from app.services.flux_quality_gate import gate_candidates`（移除 `stub_clip_scorer`）。
- `gate_candidates(...)` 呼叫移除 `clip_scorer=` 引數。

---

## 5. 資料流（改後）

```
generate_candidates(N=2)
   └─ for each candidate:
        striping check ── 觸發 ─→ skip（不呼叫 VLM）
            │ 未觸發
        VLM check ── match=False 或 has_text=True ─→ skip
            │ 通過
        accepted（記 vlm_verdict 含 score）
   └─ best = 最高 vlm score；無 accepted → None（重試 ≤3 → fallback）
```

---

## 6. 錯誤處理

- VLM 例外 / unparseable：fail-closed（該候選 reject），交給重試迴圈 / fallback。
- striping 對無法解碼圖：沿用 fail-open（不因解碼怪圖誤殺，§flux_quality_gate 既有行為）。
- 全候選 reject：`gate_candidates` 回 `None`；`_gated_generate` 換 seed 重試，耗盡後 fallback（不塞壞圖）。

---

## 7. 測試計畫（TDD：先紅後綠）

**移除**
- `test_stub_clip_scorer_passes_threshold` 及任何傳 `clip_scorer=` 的 gate 測試。

**新增 / 改寫**
- 兩候選都過閘 → 回傳 VLM `score` 較高者。
- striping 觸發的候選 → 不呼叫 VLM（用 spy/mock 斷言 VLM 未被呼叫）→ 被 skip。
- VLM `has_text=True` 或 `match=False` → 該候選 reject。
- VLM unparseable → reject；全 reject → `gate_candidates` 回 None。
- `gate_candidates` 新簽名（無 clip_scorer）可正常呼叫。
- `clip_score` 在 accepted 候選上維持 None。

**既有 striping 測試**保留（`_has_striping_artifact` / `striping_energy`）。

---

## 8. spec 5.6 驗收（改 CLIP 那項）

| # | 驗收項 | 方式 |
|---|--------|------|
| 1 | 餵會產生文字的 prompt → VLM `has_text=true` → 重生或 fallback | user 觸發 job，讀 log |
| 2 | ~~CLIP 分數記進 audit~~ → **VLM verdict（含 score）記進 `image_gen_meta`** | 驗 audit 欄位 |
| 3 | 4 GPU 上 N=2 候選 + gate 單張總延遲 ≤ 30s | user 觸發，讀 log 算延遲 |
| 4 | 全 fail 時正確 fallback、不塞壞圖 | 單元測試 + user 實機看 deck |

實機觸發一律由 user 從 frontend 點，本機只 read-only 撈 log（依既有約定）。

---

## 9. 風險

- gemma4 自評 score 噪音大 → 排序可能不穩。緩解：N=2 時影響有限；保留 clip_score 欄位供日後上真 CLIP。
- 改動 VLM prompt 可能影響既有 `match`/`has_text` 判定 → 測試覆蓋 + 5.6 實機驗收把關。
- rebuild 後才生效（backend 原始碼 COPY 進 image，非掛載）；rebuild 需 user 授權、user 觸發。
