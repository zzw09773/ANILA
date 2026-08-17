# ANILA LM Studio — FLUX 語意對齊圖像生成規格

> **新分支**：`feature/studio-flux`（從 main 開）。
>
> 把 FLUX.2-dev 從「零星亂用、每次產出垃圾」變成「**封面 hero / 章節 band / 概念插圖** 三個抽象用途的可靠視覺資產」。核心洞見來自研究報告 + ANILA 前幾輪實測:
>
> **之前缺的不是 model,是 pipeline。** FLUX 本身沒壞,壞在「LLM 直接吐 prompt → 單張生成 → 無把關 → 塞上投影片」這條沒有任何品質閘門的路。研究文獻一致指向三層架構:
>
> ```
> Layer A  LLM rewriter   投影片 title+bullets → 英文、無文字、house-style 的 image prompt
> Layer B  Generation     FLUX.2-dev 固定 seed 生 N 張候選
> Layer C  Quality gate   CLIPScore + VLM + striping 檢查 → 接受 / 重生 / fallback
> ```
>
> **本 spec 策略(依你指示)**:Stage 1 只做最小可行(封面 hero、固定 seed、單一 house style、先不做完整 gating),但 **Stage 2-4 的設計在第 3 節「跨階段鎖定合約」一次定死**,確保 Stage 1 寫的 contract / cache key / schema 不會在後面被迫重寫。

---

## 1. 既有元件盤點(別重造)

| 元件 | 路徑 | 現況 | 本 spec 動它嗎 |
|---|---|---|---|
| FLUX 推論服務 | `models/flux2-dev/server.py` | `/generate {prompt, aspect_ratio}` → PNG，無 seed / 無多候選 / 5 種 aspect | ✅ 擴 contract |
| OpenAI-compat shim | `models/flux2-dev-agent/` | 含 `prompt_translator.py`（需確認職責） | ⚠️ 視 translator 內容 |
| Backend provider | `myCSPPlatform/backend/app/services/flux_image_provider.py` | cache（SHA256 prompt+aspect）+ semaphore 併發 | ✅ 擴 signature + cache key |
| Hydration | `myCSPPlatform/backend/app/api/studio.py` `_hydrate_images()` | image_ref / diagram→graphviz / image_prompt→FLUX 三路徑 | ✅ 插入 rewriter + gate |
| Schema | `myCSPPlatform/backend/app/schemas/studio.py` | image_prompt / image_kind / diagram_dot + 互斥 validator | ✅ 加 use_case + audit 欄位 |
| Diagram 路徑 | graphviz `render_dot_to_png` | 正常運作 | ❌ 完全不動 |

**前置步驟 0（必做）**：`view models/flux2-dev-agent/app/prompt_translator.py`。如果它已經在做「中文→英文 prompt 翻譯」，Layer A 的 rewriter 應該擴充它、而非另寫一份。如果它只是 OpenAI API 格式轉換，Layer A 獨立新增。

---

## 2. 目標架構(完整資料流)

```
 deck 生成階段（既有 gemma planner）
        │  emits: Slide{ title, bullets, image_intent? }
        ▼
 ┌─────────────────────────────────────────────────────────┐
 │ Layer A — LLM Prompt Rewriter（新增，每張需要圖的 slide）   │
 │   in : slide.title + bullets + use_case + brand.style     │
 │   out: 英文 image prompt（40-80 字，無文字，house suffix）   │
 │        或 "USE_GRAPHVIZ"（結構性內容 → 走既有 diagram 路徑） │
 └─────────────────────────────────────────────────────────┘
        │  flux_prompt
        ▼
 ┌─────────────────────────────────────────────────────────┐
 │ Layer B — Generation（擴充既有 FluxImageProvider）         │
 │   seed = deck_base_seed + slide_index（決定性）            │
 │   N 候選（Stage 1: N=1；Stage 2+: N=2）                    │
 │   aspect: hero 16:9 / band 3:1 / content 1:1              │
 └─────────────────────────────────────────────────────────┘
        │  [png_candidate × N]
        ▼
 ┌─────────────────────────────────────────────────────────┐
 │ Layer C — Quality Gate（Stage 2 新增；Stage 1 直接 pass）   │
 │   1. CLIPScore(prompt, image) ≥ threshold                 │
 │   2. VLM: {match, has_text, reason}                       │
 │   3. striping check: FFT/Laplacian high-freq on flat area │
 │   pass → 用；fail → 換 seed 重生（≤3 次）→ 仍 fail → fallback│
 └─────────────────────────────────────────────────────────┘
        │  accepted png
        ▼
 既有 hydration：base64 inline 進 spec → pptx renderer
```

**鐵則(研究 + 前幾輪實測雙重佐證)**:
- **CJK 文字永遠不進 image 像素**。所有中文一律 PPTX text box。Rewriter 必須先把概念翻成英文再給 FLUX。
- **結構性內容(架構/流程/比較表)永遠走 graphviz**。Rewriter 偵測到就回 `"USE_GRAPHVIZ"`,不生圖。AutoPresent 實測 raster 對結構內容比 vector code 差 ~3 倍。
- **FLUX 不吃 negative prompt**(guidance-distilled)。「不要文字」用正面措辭塞進 prompt,不靠 negative。

---

## 3. 跨階段鎖定合約(★ 最重要 — 先定死,後面才不會走歪)

這節定義所有 stage 共用、且**一旦 Stage 1 寫下去就不該改**的介面。Stage 1 只用到一部分,但全部先設計好。

### 3.1 use_case enum(決定 aspect / prompt 風格 / 是否啟用)

```python
# schemas/studio.py
class ImageUseCase(str, Enum):
    COVER_HERO = "cover_hero"            # 封面背景意象，16:9 全幅
    SECTION_BAND = "section_band"        # 章節扉頁裝飾帶，3:1 letterbox
    CONTENT_ILLUSTRATION = "content_illustration"  # 內容頁概念插圖，1:1 或 4:3

# Stage 1 只啟用 COVER_HERO；其餘在 schema 內定義但 hydration 暫不處理。
```

### 3.2 FLUX 服務 `/generate` 新合約（`models/flux2-dev/server.py`）

**現在**：`{prompt, aspect_ratio}` → 單張 PNG。
**新合約**（一次擴足，Stage 1 只送 prompt/aspect/seed）：

```python
class GenerateRequest(BaseModel):
    prompt: str
    aspect_ratio: Literal["1:1", "16:9", "9:16", "4:3", "3:4", "3:1"] = "16:9"  # +3:1
    seed: int | None = None                          # None = random（不建議）
    num_candidates: int = Field(default=1, ge=1, le=4)
    num_inference_steps: int | None = None           # None = env default
    guidance_scale: float | None = None              # None = env default

class GenerateResponse(BaseModel):
    # 一律回 list（即使 N=1），避免 Stage 2 改回傳型別
    images: list[str]   # base64 PNG, len == num_candidates
    seed: int           # 實際用的 seed（random 時回傳真值供 audit）
    meta: dict          # {steps, guidance, width, height, model_sha}
```

> **為什麼 Stage 1 就要把回傳改成 list**:Stage 2 要 N 候選。如果 Stage 1 回單張、Stage 2 再改成 list,provider + hydration 全要重寫。**現在就回 list[1]**。

新增 3:1 aspect 到 `_ASPECT_RATIOS`:

```python
_ASPECT_RATIOS = {
    "1:1":  (1024, 1024),
    "16:9": (1344, 768),
    "9:16": (768, 1344),
    "4:3":  (1216, 896),
    "3:4":  (896, 1216),
    "3:1":  (1536, 512),   # section band letterbox（總 px ≤ 0.8MP，FLUX 穩定區）
}
```

seed 接進 pipeline:

```python
import torch
generator = (
    torch.Generator(device="cuda").manual_seed(req.seed)
    if req.seed is not None else None
)
imgs = []
for i in range(req.num_candidates):
    g = torch.Generator(device="cuda").manual_seed(req.seed + i) if req.seed is not None else None
    out = pipeline(
        prompt=req.prompt, width=w, height=h,
        num_inference_steps=req.num_inference_steps or int(os.environ.get("FLUX_NUM_STEPS", "28")),
        guidance_scale=req.guidance_scale or float(os.environ.get("FLUX_GUIDANCE_SCALE", "4.0")),
        generator=g,
    )
    imgs.append(out.images[0])
```

> **guidance 預設 3.5 → 4.0**:研究報告 FLUX.2-dev 推薦值。

### 3.3 Cache key（`flux_image_provider.py`）— ★ 一次定死

**現在**：`SHA256(prompt + NUL + aspect_ratio)`。
**新**：必須納入 seed + style_id + steps + guidance，否則 Stage 3 換 style / 換 seed 時 cache 會回舊圖。

```python
def _cache_key(self, prompt, aspect_ratio, seed, style_id, steps, guidance):
    h = hashlib.sha256()
    for part in (prompt, aspect_ratio, str(seed), style_id, str(steps), str(guidance)):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()
```

> Stage 1 的 `style_id` 固定 `"default"`、`steps/guidance` 固定預設,但**參數現在就進 key**。

### 3.4 Provider signature — ★ 一次定死

```python
async def get_or_generate(
    self,
    prompt: str,
    *,
    use_case: ImageUseCase,
    seed: int,
    style_id: str = "default",
    num_candidates: int = 1,
    steps: int | None = None,
    guidance: float | None = None,
) -> list[GeneratedImage]:   # 一律回 list
    ...

@dataclass
class GeneratedImage:
    png_bytes: bytes
    seed: int
    clip_score: float | None = None      # Stage 2 填
    vlm_verdict: dict | None = None       # Stage 2 填
    accepted: bool = True                 # Stage 2 gate 結果
```

use_case → aspect 對映集中一處:

```python
_USE_CASE_ASPECT = {
    ImageUseCase.COVER_HERO: "16:9",
    ImageUseCase.SECTION_BAND: "3:1",
    ImageUseCase.CONTENT_ILLUSTRATION: "4:3",
}
```

### 3.5 Schema audit 欄位（`schemas/studio.py` Slide）— ★ 一次定死

```python
# 生成後回填，供 audit + 前端「重新生成」按鈕 + 快取除錯
image_gen_meta: dict | None = Field(default=None)
# 結構：{ use_case, flux_prompt, seed, style_id, steps, guidance,
#         clip_score, vlm_verdict, retry_count, model_sha, image_sha }
```

每次生成寫一筆到既有 usage / audit table(跟 LLM call 同一張),欄位:`(job_id, slide_index, use_case, flux_prompt, seed, style_id, clip_score, vlm_match, vlm_has_text, retry_count, model_sha, image_sha, accepted)`。

### 3.6 brand.style 來源 — ★ 一次定死介面（實作分階段）

Layer A 需要一個 house-style suffix。**介面現在定**,Stage 1 hardcode、Stage 3 才接 `brand.yaml`:

```python
def get_style_descriptor(brand_id: str | None) -> StyleDescriptor: ...

@dataclass
class StyleDescriptor:
    style_id: str                 # 進 cache key
    suffix: str                   # 附在每個 flux prompt 尾巴
    lora_path: str | None = None  # Stage 3+
    redux_ref_path: str | None = None  # Stage 3+

# Stage 1 實作：
_DEFAULT_STYLE = StyleDescriptor(
    style_id="default",
    suffix=(
        "flat editorial illustration, isometric perspective, "
        "muted teal and warm gray palette, soft diffused lighting, "
        "generous negative space, clean unmarked surfaces, "
        "no text, no letters, no symbols, no signage"
    ),
)
def get_style_descriptor(brand_id=None):
    return _DEFAULT_STYLE  # Stage 3 改成讀 brand.yaml
```

---

## 4. Stage 1 — 最小可行(封面 hero)

**目標**:單張封面 hero 圖,驗證「LLM rewriter + 固定 seed + house style」這條路產出的品質。**不做 gating**(Layer C 先 pass-through)。

### 4.1 Layer A rewriter（新增 `services/flux_prompt_rewriter.py`）

```python
_REWRITER_SYSTEM = """\
You convert a slide's title and bullet points into a single English
image-generation prompt for FLUX.2-dev. Output ONE paragraph, 40-80 words.

REQUIREMENTS
- Translate any non-English content to English first.
- Abstract the slide's core concept into ONE visual metaphor or scene.
- NO text, words, characters, letters, digits, logos, brand names,
  signage, charts, graphs, arrows, UI elements, or screenshots.
- Specify: lighting (quality/direction/color), materials, color palette,
  mood, composition, render style.
- If the slide is structural (architecture / flow / sequence / comparison
  table / org chart), output ONLY the string "USE_GRAPHVIZ".

OUTPUT: one paragraph only, no preamble, no JSON, no quotes.
"""

async def derive_flux_prompt(
    *, title: str, bullets: list[str], use_case: ImageUseCase,
    style: StyleDescriptor, llm,
) -> str | None:
    """Return a FLUX prompt, or None if the slide should use graphviz."""
    user = f"USE_CASE: {use_case.value}\nTITLE: {title}\nBULLETS:\n" + \
           "\n".join(f"- {b}" for b in bullets)
    raw = (await llm.complete(system=_REWRITER_SYSTEM, user=user)).strip()
    if "USE_GRAPHVIZ" in raw:
        return None
    # 防線：萬一 LLM 仍吐 CJK / 引號文字，砍掉
    raw = _strip_cjk_and_quoted(raw)
    return f"{raw} {style.suffix}".strip()
```

> `_strip_cjk_and_quoted`:正則移除任何 CJK 字元與引號包住的字串(双重保險,因為文獻指出 prompt 內出現引號文字會誘發 FLUX 渲染它)。

### 4.2 deck base seed

```python
# studio.py，job 開始時算一次
deck_base_seed = int(hashlib.sha256(job_id.encode()).hexdigest()[:8], 16)
# 每張 slide: seed = deck_base_seed + slide_index
```

### 4.3 hydration 接 Layer A（studio.py `_hydrate_images` Path 3）

Stage 1 只處理封面(slide index 0 / layout_kind=="cover")的 hero：

```python
# Path 3 改寫：image_prompt 不再由 LLM 直接給，而是 rewriter 產生
if slide.get("layout_kind") == "cover" and flux_provider is not None:
    style = get_style_descriptor(brand_id=None)
    flux_prompt = await derive_flux_prompt(
        title=slide.get("title", ""), bullets=slide.get("bullets", []),
        use_case=ImageUseCase.COVER_HERO, style=style, llm=llm,
    )
    if flux_prompt:  # None → rewriter said USE_GRAPHVIZ，封面不該發生但防一手
        results = await flux_provider.get_or_generate(
            flux_prompt, use_case=ImageUseCase.COVER_HERO,
            seed=deck_base_seed, style_id=style.style_id, num_candidates=1,
        )
        img = results[0]
        slide["image_data"] = "data:image/png;base64," + base64.b64encode(img.png_bytes).decode()
        slide["image_gen_meta"] = {
            "use_case": "cover_hero", "flux_prompt": flux_prompt,
            "seed": img.seed, "style_id": style.style_id,
        }
```

> **既有 Path 3(LLM 直接吐 image_prompt)在 Stage 1 保留不動**——只在 cover 多插一條 rewriter 路徑。等 Stage 4 才把 content_illustration 全面切到 rewriter。

### 4.4 Stage 1 驗收

1. 跑任一 deck,封面出現一張 16:9 hero 意象圖、**無任何文字/亂碼/條碼**
2. 同一個 job_id 重跑 → seed 相同 → **完全一樣的圖**(決定性驗證,看 image_sha)
3. rewriter 對「架構/流程」標題的封面(若有)回 `USE_GRAPHVIZ`、不生圖
4. FLUX 服務 `/generate` 回 `{images: [...], seed, meta}` 格式
5. cache key 含 seed + style_id(看兩個不同 seed 不會撞同一 cache 檔)
6. 既有 image_ref / diagram / 既有 image_prompt 路徑零退化

---

## 5. Stage 2 — Quality Gate(Layer C)

**目標**:N=2 候選 + CLIPScore + VLM + striping 檢查 + 重試。這是「之前缺的把關」。

### 5.1 三道閘門（新增 `services/flux_quality_gate.py`）

```python
async def gate_candidates(
    candidates: list[GeneratedImage], *, flux_prompt: str, concept_en: str,
    clip_scorer, vlm,
) -> GeneratedImage | None:
    """Return best accepted candidate, or None if all fail."""
    scored = []
    for c in candidates:
        # 1. CLIPScore（torchmetrics CLIPScore, openai/clip-vit-base-patch16）
        c.clip_score = clip_scorer(c.png_bytes, flux_prompt)
        if c.clip_score < CLIP_THRESHOLD:        # 校準值，初設 27（cosine×100）
            continue
        # 3. striping / barcode 檢查（FFT 高頻能量 on flat region）
        if _has_striping_artifact(c.png_bytes):
            continue
        # 2. VLM 語意 + 文字檢查
        v = await vlm.check(c.png_bytes, concept=concept_en)
        c.vlm_verdict = v
        if not v["match"] or v["has_text"]:
            continue
        c.accepted = True
        scored.append(c)
    return max(scored, key=lambda x: x.clip_score) if scored else None
```

VLM prompt（structured JSON 回傳）:

```
Does this image depict an abstract, text-free illustration of: {concept}?
Does it contain ANY letters, characters, digits, logos, or readable signage?
Answer JSON only: {"match": bool, "has_text": bool, "reason": "<short>"}
```

striping 檢查(VLM 抓不到 pixel artifact,文獻 AlignGemini 證實要另一支):

```python
def _has_striping_artifact(png_bytes, flat_region_frac=0.3, hf_energy_thresh=...):
    # 取影像中變異最小的 30% 區域（理應是平滑背景），
    # 算其 FFT 高頻能量；條碼/striping 會在平滑區出現異常高頻。
    # 閾值用 Stage 2 校準集定。
```

### 5.2 重試迴圈（provider 內）

```python
for attempt in range(MAX_RETRIES + 1):   # MAX_RETRIES=3
    cands = await self._generate_n(prompt, seed=base_seed + attempt*1024, ...)
    best = await gate_candidates(cands, ...)
    if best: return [best]
# 全 fail → fallback（見 5.3）
```

### 5.3 fallback 策略

3 次重試全 fail → 依 use_case:
- COVER_HERO / SECTION_BAND → 用 brand 的純色/漸層底(theme palette),不放圖
- CONTENT_ILLUSTRATION → 退回該 slide 為純文字 layout(既有 image_focus→standard fallback)

### 5.4 模型選擇(air-gapped,排除中國模型)

- CLIP scorer:`openai/clip-vit-base-patch16`(torchmetrics 直接支援)。若要對「原始中文 concept」打分,用多語 SigLIP。
- VLM:**LLaVA-NeXT-Mistral-7B** 或 **gemma-3-vision**(Western-origin,符合你的 no-Chinese-model 約束)。**不要 Qwen-VL**。

### 5.5 校準(Stage 2 上線前必做)

人工標 50 張 slide 的「好/壞」→ 校 `CLIP_THRESHOLD` 跟 `hf_energy_thresh`。研究報告:強匹配對 CLIP cosine 通常落 0.27-0.35,torchmetrics scale ×100 → 初設 27,看 ROC 調。

### 5.6 Stage 2 驗收

1. 故意餵會產生文字的 prompt → VLM `has_text=true` → 重生或 fallback
2. CLIP 分數記進 audit table
3. 4 GPU 上 N=2 候選 + gate 的單張總延遲 ≤ 30s
4. 全 fail 時正確 fallback、不會塞壞圖上去

---

## 6. Stage 3 — Style Consistency(整份 deck 視覺一致)

**目標**:同一份 deck 的封面 + 章節 band + 插圖看起來是「同一套視覺語言」。

### 6.1 三層手段(由便宜到貴,研究報告順序)

1. **固定 seed family + 同一 style suffix**(Stage 1 已具雛形):`seed = deck_base_seed + slide_index`,全 deck 同一個 `style.suffix`。最便宜、最可重現。
2. **Flux Redux / FLUX.2 native multi-reference**:挑一張參考圖當 conditioning,全 deck 繼承其 palette/mood。需要 FLUX 服務支援傳 reference image。
3. **Style LoRA**:對 30-50 張 in-house 參考圖訓一顆 LoRA(rank 64、strength 0.8,用 AI-Toolkit/Kohya)。最強一致性、可做品牌風格。FLUX.2 LoRA 生態仍在成熟,若要 today 就上 LoRA 可考慮 FLUX.1-dev。

### 6.2 brand.yaml(接 3.6 的 StyleDescriptor 介面)

```yaml
# 每個組織/部門一份
brand_id: 資通所
style_id: rd-institute-v1
suffix: "flat editorial illustration, deep navy and amber palette, ..."
lora_path: /var/anila/loras/rd-institute-v1.safetensors   # 選填
redux_ref_path: /var/anila/refs/rd-institute-hero.png      # 選填
```

`get_style_descriptor(brand_id)` 從 Stage 1 的 hardcode 改成讀這個。**因為 style_id 早在 3.3 就進了 cache key,這裡換 style 不會回舊圖。**

### 6.3 FLUX 服務再擴(支援 LoRA / Redux)

```python
class GenerateRequest(BaseModel):
    # ... 既有 ...
    lora_path: str | None = None       # 服務端 load + fuse
    reference_image: str | None = None  # base64, Redux/multi-ref conditioning
```

### 6.4 Stage 3 驗收

同一 deck 的 3 種 use_case 圖放一起,人工目測「同一套視覺」;換 brand_id → 整份風格切換、cache 不撞舊。

---

## 7. Stage 4 — 全用途上線(section band + content illustration)

**目標**:把 SECTION_BAND(3:1)跟 CONTENT_ILLUSTRATION 全面接上 rewriter + gate。

### 7.1 hydration 全面切換

Stage 1 只有 cover 走 rewriter。Stage 4 把既有「LLM 直接吐 image_prompt」那條 Path 3 **完全換掉**,改成:

```python
# 任何 image_kind=="illustration" 的 slide 都走 rewriter + gate
if slide.get("image_kind") == "illustration":
    use_case = _infer_use_case(slide)   # cover→HERO, section_break→BAND, else→CONTENT
    flux_prompt = await derive_flux_prompt(..., use_case=use_case, ...)
    if flux_prompt is None:
        # rewriter 判定該走 graphviz — 但 illustration 不該如此，記 warning 後 fallback
        ...
    else:
        results = await flux_provider.get_or_generate(flux_prompt, use_case=use_case,
                   seed=deck_base_seed+idx, style_id=style.style_id, num_candidates=2)
        best = await gate_candidates(results, ...)
        ...
```

### 7.2 section_break band 的特殊版面

3:1 band 放在章節扉頁底部 1/3 或全幅當背景(標題疊在上面,白/深字看 theme)。renderer(`server.js` renderSectionBreak)要支援「band 圖 + 文字 overlay」版面。

### 7.3 prompt 數量爆炸的快取效益

一份 deck 可能 1 hero + 3 band + 6 插圖 = 10 張 × 2 候選 = 20 次生成。content-addressable cache(3.3)在第二、三份 deck 起大幅省:通用概念(「成長」「資料流」「韌性」)同 prompt+seed → 同圖。

### 7.4 前端「重新生成這張圖」按鈕

audit 欄位(3.5)存了 flux_prompt + seed。前端給一顆按鈕,改 seed offset 重生單張、不重跑整個 deck。**這是處理「視覺一致性是部分主觀」的人工逃生口**。

### 7.5 Stage 4 驗收

跑一份含封面+多章節+多插圖的 deck,三種 use_case 圖都語意對齊、無文字、風格一致;graphviz 結構圖不受影響;單 deck 端到端 ≤ 10 分鐘(4 GPU)。

---

## 8. 硬體(H100×4)規劃

| GPU | 角色 | 模式 |
|---|---|---|
| GPU0, GPU1 | FLUX.2-dev worker ×2(並行候選) | **BF16**(64GB 權重,H100 80GB 裝得下;VRAM 不緊就別省畫質) |
| GPU2 | VLM(LLaVA-NeXT / gemma-3-vision) | — |
| GPU3 | gemma(rewriter LLM)+ CLIP scorer + headroom | CLIP ViT-B 很小 |

- 4 顆 H100 → VRAM 充裕,**FLUX 走 BF16 不必 FP8**(畫質優先)。若要多塞 worker 或同卡共置 VLM,再降 FP8(~32GB,40% VRAM 省、畫質損失小)。
- 延遲:研究報告 FLUX.1-dev H100 BF16 ~8s/張、FP8+compile ~3.5s。FLUX.2-dev 較大、估 10-20s/張 BF16。2 個 worker 並行,20 次生成 ≈ 3-5 分鐘 + gate。
- 推論後端:production 用 **diffusers `Flux2Pipeline`**(乾淨 Python API、好包 FastAPI、`enable_model_cpu_offload()` 備用)。ComfyUI 只當原型工作台試 LoRA/Redux,不進 production。
- 權重在連網工作站下載一次 → SHA256 鎖定 → rsync 進 air-gapped 網。

---

## 9. 授權雷區（★ Stage 1 開工前先確認）

- **FLUX.2-dev 是 Non-Commercial License**。內部商用算灰色地帶。**開工前請法務確認**「公司內部工具」是否落在授權範圍。
- **FLUX.2-klein-4B 是 Apache-2.0**——法律最乾淨的本地選項,畫質略降。若法務認定 dev 版內部商用有疑慮,**主力換 klein-4B**(順帶 sub-second 延遲、4 GPU 綽綽有餘)。
- FLUX.2-klein-9B 與 dev 同 Non-Commercial 授權,不解決問題。
- 這條我無法替你判斷——**請務必在投入 Stage 2 前釐清**,否則整條 pipeline 建在不能商用的 model 上。

---

## 10. 實作順序總表

| Stage | 範圍 | 關鍵檔案 | 預估 |
|---|---|---|---|
| **0** | 確認 `prompt_translator.py` 職責 + 法務確認授權 | — | 0.5 天 |
| **1** | FLUX 服務 contract 擴充(seed/list/3:1)+ rewriter + cover hero + 固定 style + cache key 定死 | `flux2-dev/server.py`, `flux_prompt_rewriter.py`, `flux_image_provider.py`, `studio.py`, `schemas/studio.py` | 3-4 天 |
| **2** | Quality gate(CLIPScore + VLM + striping + 重試 + fallback)+ 校準 | `flux_quality_gate.py`, VLM/CLIP 部署 | 1 週 |
| **3** | Style consistency(brand.yaml + Redux/LoRA)| `brand.yaml` 載入, FLUX 服務 LoRA/ref 支援 | 1-1.5 週 |
| **4** | 全用途(band 3:1 + content illustration)+ 前端重生按鈕 | `studio.py` hydration 全切, `server.js` band 版面, 前端 | 1 週 |

**3.x 的合約全部在 Stage 1 落地**——這是你要求的「後面 stage 先完整、避免走歪」的具體做法:contract / cache key / provider signature / schema audit / use_case enum / style 介面,六樣東西 Stage 1 一次定死,Stage 2-4 只填血肉、不改骨架。

---

## 11. 不在本次範圍

- 取代 graphviz 做任何結構圖(研究明確反對,守住 vector/raster 邊界)
- CJK 文字進 image 像素(Western 開源模型無解,文字一律 PPTX text box)
- 換掉 gemma 當 rewriter(用既有 LLM 即可)
- 雲端 API 任何方案(全程 air-gapped、無 API)
- 影片/動畫生成

---

**Spec 產出時間**：2026-05-20
**研究依據**：附帶的 FLUX/diffusion 簡報插圖工程實踐研究報告（BFL 官方 prompting guide、HF diffusers FLUX-2 整合、arXiv 2503.03595 local generation bias、AutoPresent SLIDESBENCH、DOC2PPT、MuLan/VL-DNP VLM-feedback、CLIPScore）
**程式碼依據**：分支 main 的 `flux_image_provider.py` / `studio.py` `_hydrate_images` / `schemas/studio.py` / `models/flux2-dev/server.py` 實際比對
**核心主張**：之前缺的是 pipeline 不是 model。三層(rewriter → 生成 → 把關)+ 六個跨階段合約先定死 = Stage 1 小步上線、Stage 2-4 不走歪。
