# Runbook — 思考／fast（nothink）模型變體路由

部署常把同一底座拆成兩個 registry 名稱（例：`gemma26`＝思考版、
`gemma26-nothink`＝fast）。活體量測見
`docs/designs/ncsist-prompt-localization-and-harness.md` §9b：輔助任務用
fast 熱機約 0.8s、品質不輸；思考版卻可能燒 400–1300 reasoning tokens
還拖多秒。**變體名稱因站而異，靠環境變數對應，不要寫死在程式常數。**

契約程式：`packages/anila-core/src/anila_core/prompts/model_routing.py`
（`TASK_CLASS`＋`resolve_model`）。未設 env 時呼叫端 `default` 原樣回傳——
零行為變化。

---

## 1. 在 model_registry 註冊兩個變體（治理中心）

1. 開治理中心 → 模型／Model registry。
2. 新增（或確認已有）**思考版**一筆：`name` 填實際 gateway 模型名
   （例 `gemma26`），`model_type=llm`，`endpoint_url` 指向 gateway FQDN，
   設為 active；若這顆是主對話預設，可勾 router primary。
3. 再新增 **fast／nothink** 一筆：`name` 填對應變體名
   （例 `gemma26-nothink`），同一 endpoint（或該站的 nothink 端點），
   active。**兩筆都要能被 `/v1/chat/completions` 以各自 `model` 名稱打到。**
4. 用治理中心「測試連線」或對兩名稱各打一發非串流 completion 確認
   Content-Type 為 JSON、有 content（思考版還可能有 `reasoning_content`，
   前端不應渲染該欄）。

名稱只是字串契約：下一步 env 填的值必須與 registry `name` **完全一致**。

---

## 2. 環境變數要設在哪

根目錄 `.env`（compose 插值）＋對應服務的 `environment:`。套用後一律
`docker compose -p <project> up -d` recreate，不要只 `docker restart`。

| 變數 | 服務 | 用途 |
|---|---|---|
| `ANILA_MODEL_ANALYSIS` | 跑 anila-core 路由契約的服務（接線後：`anila-core-router`／csp 內嵌呼叫端；寫在根 `.env` 並傳到該容器） | `rag_qa`／`chat` 等 analysis class |
| `ANILA_MODEL_FAST` | 同上 | `chips`／`title`／`json_gen` 等 fast class |
| `ANILA_STUDIO_SLIDES_MODEL` | `anila-studio` | 投影片主 LLM（analysis；預設 `gemma4`） |
| `ANILA_STUDIO_VISION_MODEL` | `anila-studio` | Vision／VLM gate（analysis；預設 `gemma4`） |

> 🔴 **`ANILA_MODEL_FAST` 與 `ANILA_MODEL_ANALYSIS` 目前沒有任何程式在讀（2026-08-05 查證）。**
> 設下去**不會有任何效果**,不會報錯,也不會有任何訊號告訴你它沒生效。
> **自動標題實際上用的是「回答那一輪對話的同一顆模型」**
> (`apps/anila-shell/src/app.jsx:1210-1253` 送 `model: effectiveTarget`),
> 也就是治理中心指定的主路由。
> **擁有者 2026-08-05 裁定不接這條線**——理由是零設定、自我維護:模型陣容換了會自動跟著
> 治理中心走,而實質開關本來就存在。所以下面這個範例**是保留給未來的形狀,不是現在能用的設定**。
> 詳見 `docs/FAKE-CONTROLS.md` #54 與 `docs/OWNER-QUESTIONS.md` Q35。

範例（名稱依你站 registry 為準；⚠ 上面兩個變數目前無效）：

```bash
# 根 .env — 輔助任務走 nothink；分析留思考版
# ⚠ 以下兩行目前不生效（無人讀取），保留為未來接線時的形狀
ANILA_MODEL_ANALYSIS=gemma26
ANILA_MODEL_FAST=gemma26-nothink

# Studio 投影片品質優先，預設維持思考／分析級模型名
ANILA_STUDIO_SLIDES_MODEL=gemma26
ANILA_STUDIO_VISION_MODEL=gemma26
```

compose 側把 Studio 兩個變數加進 `anila-studio.environment`
（`infra/compose/platform.yml`／`dev.yml`），例如：

```yaml
ANILA_STUDIO_SLIDES_MODEL: ${ANILA_STUDIO_SLIDES_MODEL:-gemma4}
ANILA_STUDIO_VISION_MODEL: ${ANILA_STUDIO_VISION_MODEL:-gemma4}
```

`ANILA_MODEL_*` 則傳到實際會 `import resolve_model` 的容器（合併接線後
以該 PR 的 compose diff 為準）。**未設＝行為與今日相同。**

---

## 3. 親和性警告（必讀）

§9b-4 實測：同一請求鏈裡在 thinking／nothink 之間交替，會觸發
**10–16 秒換模冷啟**。實務規則：

- 主對話／RAG QA 整段黏 analysis（思考版）。
- chips、標題、JSON 小工具整段黏 fast；不要在主回答串流中途插一發
  fast 再立刻回到 thinking（寧可 chips 延後、或與主模型同顆）。
- Studio 投影片／VLM 是 analysis-class：**不要圖一時延遲隨手改指
  nothink**，會傷 deck 品質。

### 3a. Router 本身是 analysis-class（2026-08-02 活體探針，不可指到 fast）

對改版後的 Router 模板做四情境探針（gemma26 vs gemma26-nothink）：

- 思考版：明確派工題輸出一字不差的 `DISPATCH:軍規助手:...`；模糊題照規
  矩列選項＋澄清。
- **nothink 版在同一明確派工題「漏派工」**——改回澄清清單而不是
  `DISPATCH:` 行。派工判斷需要推理，fast 變體會讓自動分派變笨。
- 另一發現：思考版遇閒聊題可能把 max_tokens 全燒在 reasoning、正文全空
  ——**Router 呼叫端的 max_tokens 要 ≥2048**（空回覆守則見 §9b-2）。

結論：`ANILA_MODEL_ANALYSIS` 才是 Router 該用的 class；
把 Router 指到 `ANILA_MODEL_FAST` 是配置錯誤。

---

## 4. 接線狀態表

| 呼叫端 | 狀態 | 檔案指標 |
|---|---|---|
| Studio 投影片／Vision 模型常數 | **程式碼側已完成**；compose environment 傳遞待接（依 §2 範例加入 `anila-studio` 區塊後才生效；未設＝`gemma4`） | `services/anila-studio/app/services/studio_config.py`（`SLIDES_LLM_MODEL`／`VISION_LLM_MODEL`） |
| 任務→class 契約＋`resolve_model` | **已完成**（函式庫；呼叫端尚未全接） | `packages/anila-core/src/anila_core/prompts/model_routing.py` |
| 追問 chips hook | **待接**（合併時） | 接線位置待定——尋找建立 QueryEngine 並呼叫 `add_post_turn_hook(...)` 的 composition root；hook 本體在 `post_turn/prompt_suggestion.py`。接線時把 `model=` 改走 `resolve_model("chips", default=...)` |
| 對話標題產生器 | **待接**（合併時） | `apps/anila-shell/src/app.jsx`（`generateConversationTitle`，約 :1013；目前 `model: effectiveTarget`） |

`services/csp/app/services/prompt_gen_service.py` 的
`_resolve_primary_llm` 仍走 registry primary，**本包不改**；若日後要把
prompt-gen 也納入 class 路由，另開包。
