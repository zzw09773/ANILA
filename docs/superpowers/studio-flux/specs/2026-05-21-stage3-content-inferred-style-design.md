# Stage 3 — 依內容自動推斷 deck 風格

**Date:** 2026-05-21
**Branch:** feature/studio-flux
**Spec context:** `docs/superpowers/studio-flux/ANILA_Studio_FLUX_Spec.md` §6（Stage 3 — Style Consistency）、§3.6（StyleDescriptor 鎖定合約）
**Supersedes:** `2026-05-21-stage3-brand-yaml-design.md`（部門/brand.yaml 方案已放棄）
**Status:** 設計已核准，待寫實作計畫

---

## 1. 背景與決策

Stage 1 把 house style 寫死在 `flux_style.py` 的 `_DEFAULT_STYLE`。Stage 3 要讓每份簡報的視覺風格**自動貼合該專案的內容**，整份 deck 共用同一風格 → 視覺一致。

**演進**：原先考慮「依使用者部門 + brand.yaml」，但這要 ops 維護設定檔、且部門名要對得上，複雜。改為：**由 LLM 看專案內容自動推斷一段風格描述，無需任何設定**。

**決策**：
- 風格來源 = LLM 看「deck 標題 + 一段原始文件內容」**自由生成**一段 house-style suffix。
- 整份 deck **只推斷一次**（deck 層級），所有 slide 共用 → 一致性。
- `style_id` = 由 suffix 內容 hash 而來 → 同 suffix 同 id，FLUX provider cache（§3.3）穩定、重跑可命中。
- 任何失敗 → 退 `_DEFAULT_STYLE`，絕不讓風格推斷弄垮 job。

**已知限制（誠實聲明）**：
- 本階段讓風格**貼內容**，但**不保證 FLUX 服從**該風格（Stage 2 5.6 已見 FLUX 無視 suffix 畫成寫實風）；服從度屬 §6.1③（Redux / LoRA），不在本階段。
- LLM 自由生成 → 重跑若字句飄移，`style_id` 變 → cache 偶爾 miss。以**低溫**推斷盡量壓低；可接受（user 已知並接受彈性換取貼合度）。

---

## 2. 範圍

**In scope**
- `app/services/flux_style.py` 新增 `infer_deck_style(*, title, content_sample, llm) -> StyleDescriptor`。
- Job pipeline（`studio.py`，chunks 可用處）：組內容樣本、呼叫一次推斷、把 `deck_style` 往下傳。
- 串接：`_render_pptx` 與 `_hydrate_images` 各加一個 `deck_style: StyleDescriptor | None` 參數；`_hydrate_images` 用它取代 `get_style_descriptor(brand_id=None)`。
- 單元測試（`tests/test_flux_style.py` 新增）。

**Out of scope**
- 部門 / brand.yaml（放棄）。
- LoRA / Redux（服從度）；`StyleDescriptor.lora_path` / `redux_ref_path` 欄位保留、不實作。
- frontend 選風格、DB 管理介面、per-project 持久化風格（重跑靠低溫+hash，不另存）。
- 改動 FLUX `/generate` 服務合約、改動 Layer A rewriter 介面（`derive_flux_prompt` 仍吃 `StyleDescriptor`，不變）。

---

## 3. `infer_deck_style`

簽名：`async def infer_deck_style(*, title: str, content_sample: str, llm) -> StyleDescriptor`

流程：
1. 組 prompt：要 LLM 依「標題 + 內容樣本」產出一段**簡潔的視覺 house-style 描述**（palette、插畫風格、打光、構圖），**只輸出風格片語**、不要解釋。system 指示輸出純風格字串。
2. 低溫呼叫（`temperature` 取低值，例 0.2，求重跑穩定）。
3. 取回字串 → `_normalize_suffix`：strip、塌掉換行/多餘空白、長度上限（例 ≤ 400 字）。
4. **no-text 護欄**：若正規化後的 suffix 不含 no-text 字樣，**強制附加** `", no text, no letters, no symbols, no signage"`（FLUX 不理負向 prompt，這道一定要在）。
5. 防呆：若 LLM 回空、太短（例 < 10 字）或呼叫拋例外 → log warning、回 `_DEFAULT_STYLE`。
6. 成功 → `StyleDescriptor(style_id=f"auto-{_hash8(suffix)}", suffix=suffix)`，`_hash8 = sha1(suffix.encode()).hexdigest()[:8]`。

`StyleDescriptor` 資料形狀不變（沿用 §3.6）。`lora_path`/`redux_ref_path` 留空。

---

## 4. 內容樣本與呼叫點（`studio.py` job pipeline）

- 在 job pipeline 內、`_render_pptx` 之前（`spec` 已成形、`chunks_str` 已存在、`flux_llm` 可用處）做一次推斷。
- `content_sample` = `spec.title` + 換行 + `chunks_str` 截斷至上限（例 1500 字）。
- 僅在 FLUX cover-hero 路徑會啟用時才推斷（`flux_provider` + `deck_base_seed` + `flux_llm` 皆備）；否則不必推斷，傳 `deck_style=None`。
- 推斷失敗已在 `infer_deck_style` 內降級為 `_DEFAULT_STYLE`，pipeline 端不需額外 try。

---

## 5. 串接（threading）

- `_render_pptx(spec, images_lookup, *, deck_base_seed, llm, deck_style: StyleDescriptor | None = None)`：把 `deck_style` 原樣傳給 `_hydrate_images`。
- `_hydrate_images(..., deck_style: StyleDescriptor | None = None)`：
  - cover-hero 區塊內，將 `style = get_style_descriptor(brand_id=None)` 改為
    `style = deck_style or get_style_descriptor()`（`get_style_descriptor()` 仍回 `_DEFAULT_STYLE`，作為無內容時的保底）。
  - 其餘行為不變。
- `get_style_descriptor` 保留（無內容/未接 FLUX 時的 fallback 來源），但不再是主路徑。

---

## 6. 元件邊界

- `flux_style.py`：新增 `infer_deck_style` + 私有 `_normalize_suffix` / `_hash8`；`StyleDescriptor`、`get_style_descriptor`、`_DEFAULT_STYLE` 不變。
- `studio.py`：(a) job pipeline 新增一次推斷 + 組樣本；(b) `_render_pptx`、`_hydrate_images` 各加一參數並透傳。
- 風格「內容」由 LLM 即時產生（無設定檔）；「邏輯與保底」在 `flux_style.py`。

---

## 7. 錯誤處理

- LLM 例外 / 空輸出 / 過短 → `infer_deck_style` 回 `_DEFAULT_STYLE` + warning。
- no-text 護欄確保即使 LLM 漏寫，suffix 仍含禁字字句。
- pipeline 取 `chunks_str` 為空（純標題知識庫）→ `content_sample` 僅標題，仍可推斷；推不出有意義結果則照樣降級 default。

---

## 8. 測試計畫（TDD：先紅後綠）

新檔 `tests/test_flux_style.py`，用 mock LLM（async callable / 物件，回固定字串）：

1. LLM 回正常風格句 → `infer_deck_style` 回 StyleDescriptor，`suffix` 含該句、`style_id` 以 `auto-` 開頭。
2. **決定性**：同一 LLM 輸出 → 兩次呼叫得相同 `style_id`（hash 穩定）。
3. **no-text 護欄**：LLM 回不含 no-text 的句子 → 結果 suffix 仍含 `no text`。
4. LLM 回空字串 / 過短 → 回 `_DEFAULT_STYLE`。
5. LLM 呼叫拋例外 → 回 `_DEFAULT_STYLE`（不外拋）。
6. `_normalize_suffix`：多行/前後空白 → 單行、修剪；超長 → 截斷至上限。
7. 不同 LLM 輸出 → 不同 `style_id`（避免不同風格撞 cache key）。

`studio.py` 的接線（組樣本 + 透傳參數）不另寫單元測試，由實機驗收（§9）涵蓋。

---

## 9. 驗收（對應 spec §6.4，按本階段範圍調整）

1. 同一專案內容觸發 job → log 顯示推斷出的 `style_id`（`auto-xxxxxxxx`），cover flux_prompt 尾巴帶推斷出的 suffix（且含 no-text）。
2. 同一專案重跑（低溫）→ 多數情況得相同 `style_id`、命中 cache（可由 log/cache 命中觀察）。
3. 不同主題的兩個專案 → 推斷出的 suffix 明顯不同（風格貼內容）。
4. 故意讓推斷失敗（例如 LLM 不可用）→ job 仍完成、退 `_DEFAULT_STYLE`、log 有 warning。

（實機觸發一律 user 從 frontend 點，本機 read-only 撈 log。）

---

## 10. 風險

- LLM 自由生成風格不穩 → 重跑 cache 偶爾 miss（低溫緩解；可接受）。
- 推斷出的風格仍可能被 FLUX 無視（服從度未解，見 §1 限制）。
- 內容樣本截斷可能切掉關鍵主題資訊 → 風格判斷偏差；以「標題優先 + 內容補充」降低影響。
