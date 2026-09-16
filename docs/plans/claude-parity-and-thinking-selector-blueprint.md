# ANILA 向 claude.ai 靠齊——差距盤點與「使用者自選思考程度」規劃書

> 2026-09-15 草稿。狀態：**待擁有者過目**，尚未排進 `PLAN.md`。
> 本檔只寫「還差什麼、怎麼補、先後順序」；現況事實以程式碼錨點為準，
> 標 ⚠ 的是我沒在活體上驗過、或需要擁有者拍板的事。

---

## 0. 這一週已經落地的（不再列為差距）

| 項目 | 錨點 |
|---|---|
| 使用者不調 `max_tokens`；Router 預設 32768，`finish_reason=length` 且已有正文時同一回合自動續寫最多 3 輪 | `packages/anila-core/src/anila_core/prompts/sampling.py`、`router_server.py` `LENGTH_AUTO_CONTINUE_ROUNDS` |
| 截斷不再顯示「LLM 無法回應」，保留半成品＋「繼續產生」 | `router_server.py` `_LENGTH_FALLBACK`、`apps/anila-shell/src/runtime/reservedTurn.js` |
| HTML／SVG 產物右側預覽、可拉寬、程式碼區獨立捲動 | `apps/anila-shell/src/artifact.jsx`、`markdown.jsx` |
| 預覽走同源 `artifact-frame.html`，Three.js r128 內網 vendor，CDN 自動改寫 | `public/artifact-frame.html`、`runtime/artifactVendor.js`、`infra/nginx/anila.conf` |
| Auto compact 接進 Router 聊天路徑：接近輸入窗先摘要舊回合、失敗硬截、上游回 context overflow 再縮一次重試；QueryEngine `_pre_process` 同步接上 | `compact/openai_history.py`、`router_server.py` `_auto_compact_routing_messages`、`engine/query_engine.py` |
| CSP `router-primary` 回傳 `context_window`，Router 拿來算 compact 門檻（空值後援 128k） | `services/csp/app/api/models.py` `get_router_primary` |

---

## 1. 與 claude.ai 的差距矩陣

分四類：**✅ 已有**、**🟡 有一半**、**❌ 沒有**、**🔵 要擁有者決定要不要**。
只列一般使用者感受得到的；治理面不在本檔。

### 1.1 對話核心

| claude.ai 行為 | ANILA 現況 | 差距 | 類別 |
|---|---|---|---|
| 使用者選模型 | 每則對話可選（`router_model_id`＋樂觀鎖版本），授權走 grants | — | ✅ |
| **使用者開關／調整 extended thinking** | 只有 admin 在治理中心對**整個模型**設 `thinking_effort`；使用者看不到也改不了 | 見 §2，本檔主題 | ❌ |
| 思考過程可展開 | `anila.reasoning` 串流＋「N 字思考」折疊 | — | ✅ |
| 長對話自動摘要、使用者無感 | 這週接上；但摘要**不寫回對話庫**，前端每次重送全史，同段舊話可能重摘 | 補 compact boundary 持久化 | 🟡 |
| 思考與正文額度分開 | vLLM／litellm 系後端只有 `reasoning_effort` 等級，沒有獨立 thinking budget；現以大 `max_tokens`＋續寫補 | 等後端支援；先用等級 | 🟡 |
| 精準 token 計數 | 字數／4 粗估；估低了靠 PTL 重試救 | 接 tokenizer 或 `/tokenize` 端點 | 🟡 |
| 停止產生後保留半成品 | 有（`reservedTurn` 把停掉的答案標 incomplete） | — | ✅ |
| 編輯重問／重新產生成分支 | OW-1 訊息樹已關板 | — | ✅ |
| 追問建議 chips | 有 | — | ✅ |

### 1.2 記憶與個人化

| claude.ai 行為 | ANILA 現況 | 差距 | 類別 |
|---|---|---|---|
| 跨對話記憶、使用者可看可刪 | CSP `memory_service` 有 writer／recall／密等隔離（P4.5）；SYSTEM-MAP §5 要求「偏好＋自動萃取、使用者自管」 | ⚠ 待活體驗：萃取是否每輪跑、shell 是否有「我的記憶」管理頁 | 🟡 |
| 自訂回覆風格（Styles） | 「### 使用者偏好」段由 memory 注入，Router 規則 7 依偏好調語氣 | 沒有 UI 讓使用者直接寫偏好；只能靠系統萃取 | 🟡 |
| Projects：一組對話共用指示＋知識檔 | 有 agent 綁定的專案知識庫、ANILALM 個人知識庫；**沒有**使用者層級的「專案＝自訂指示＋檔案＋多則對話」 | 新概念，牽動密等與分享 | 🔵 |

### 1.3 產物（Artifacts）

| claude.ai 行為 | ANILA 現況 | 差距 | 類別 |
|---|---|---|---|
| 右側預覽、可切原始碼、可複製 | 有 | — | ✅ |
| 產物版本歷史（v1/v2 切換） | PLAN 已併入 D3，隨 Studio 延後 | 維持延後 | 🔵 |
| 下載成檔（.html/.svg/.md） | 只有複製 | 一顆按鈕，`Blob` 下載；密等對話沿用 copy 拒絕規則 | ❌ |
| 「改這一段」局部修訂 | 原始碼選字→可見新 turn→同 kind 完整回覆換右側；無 v1／v2 | — | ✅ |
| React／JSX 產物 | 只認 HTML／SVG／Markdown | 內網要先擺 React UMD＋Babel standalone vendor，和 Three.js 同一套路 | 🔵 |

### 1.4 附件與檢索

| claude.ai 行為 | ANILA 現況 | 差距 | 類別 |
|---|---|---|---|
| 丟 PDF 直接讀 | 有，附件預算 70% 窗 | — | ✅ |
| 太大時明講「用檢索、可能漏」 | SYSTEM-MAP §5 有寫；⚠ 待驗 shell 是否真的顯示 | 補提示 | 🟡 |
| 圖片附件 | VLM caption 走 ingestion；聊天內直接貼圖 ⚠ 待驗 | — | 🟡 |
| Compact 時先剝掉 base64／圖 | sliding window 不區分，大 data URL 會被整輪保留或整輪丟 | `stripImagesFromMessages`（runtime_logic P0 未 port） | ❌ |

### 1.5 用量與透明度

| claude.ai 行為 | ANILA 現況 | 差距 | 類別 |
|---|---|---|---|
| 使用者看得到自己的用量 | SYSTEM-MAP §1 說「應該直接放進 ANILA」；`token_usage` 在 CSP | ⚠ shell 有沒有用量頁待驗 | 🟡 |
| 每則回覆顯示思考 token／正文 token | 只有「N 字思考」 | 把 `usage.reasoning_tokens` 帶到 `anila.meta` | ❌ |
| 手動「/compact 整理對話」 | `POST /sessions/{id}/compact` 仍 501 | 接到 `_auto_compact_routing_messages(force=True)` | ❌ |

---

## 2. 使用者自選思考程度——設計

### 2.1 現況（錨點）

- 等級存在 `model_registry.thinking_effort`（`none/low/medium/high/xhigh/max`，`schemas/model_registry.py`），**admin 存檔時**只探測「admin 填的那一個等級」接不接受（`services/thinking_probe.py`），不會把整組支援等級記下來。
- 套用點在 CSP proxy：`app/services/proxy/sampling.py` `apply_model_sampling_overrides` → `_apply_thinking_effort`。規則：**呼叫端 body 已有 `reasoning_effort` 就不動**；否則依模型列補 `reasoning_effort`＋`chat_template_kwargs.enable_thinking`。
- Router 送上游時**從不帶** `reasoning_effort`（`router_server.py` `_sampling_payload` 只帶 temperature／max_tokens），所以現在永遠是模型列的值。
- 後端行為不一（2026-09-03 實測，寫在 `sampling.py` 檔頭）：Qwen/litellm 只收 `low/medium/xhigh`，其餘 400；gemma 無視 `reasoning_effort`，只看 `enable_thinking`。GLM ⚠ 未探測。
- 模型選擇已有完整的「每則對話一個值＋版本鎖＋Router 向 CSP resolve」的樣板：`conversations.router_model_id` / `router_selection_version`、`PUT /api/conversations/{id}/router-model`、`POST /api/router-models/resolve`、shell `RouterModelPicker.jsx`。

**結論**：後端已經有「呼叫端優先」的縫，缺的是（a）使用者的選擇存在哪、（b）怎麼知道這個模型能選哪些、（c）UI。

### 2.2 使用者看到的

模型選單旁多一顆「思考」選單，四檔：

| 使用者看到 | 意義 | 對映後端 |
|---|---|---|
| 依模型預設 | 不干預，走 admin 設的模型列 | 不送 `reasoning_effort` |
| 關閉 | 直接回答 | `enable_thinking=false`，不送等級 |
| 標準 | 日常 | 該模型支援集中的 `medium`（沒有就取最接近） |
| 深入 | 寫程式、長文、推導 | 該模型支援集中最高的一檔（`xhigh`／`max`／`high` 擇一存在者） |

- 存的是**檔位**（`default/off/standard/deep`），不是後端字串。使用者中途換模型，檔位不變，由 CSP 依新模型重新對映。
- 只顯示這個模型**真的接受**的檔位；不支援的變灰並附「此模型不支援分級」。gemma 類只會有「預設／關閉／開啟」。
- 深入模式下，畫面提示「思考會用掉較多時間與額度」；思考 token 數顯示在回覆折疊列（§1.5）。
- 單則覆寫：送出列長按／右鍵「這一題深入想」，只影響本回合。

### 2.3 資料與 API

**Alembic（一支 migration，接在現行 head 之後）**

```
conversations.thinking_tier        VARCHAR(16) NULL   -- default|off|standard|deep；NULL 視同 default
model_registry.thinking_levels_supported  JSON NULL    -- 例 ["none","low","medium","xhigh"]；NULL=未探測
model_registry.thinking_user_selectable   BOOLEAN NOT NULL DEFAULT TRUE
```

**CSP**

- `PUT /api/conversations/{id}/thinking` `{thinking_tier, expected_version}`：擁有者本人、CSRF、樂觀鎖沿用 `router_selection_version`（同一把版本號，避免兩個欄位各自鎖）；版本不符 409，detail 為純字串（與 `/router-model` 一致）。
- ⚠ 不變式：`thinking_levels_supported` 後端**永不回 `[]`**（可達必含 `none`，全不可達存 NULL）；shell 與治理中心一律把 `null` 與 `[]` 都當「未探測」。
- proxy 依 `X-ANILA-Conversation-Id` 撈對話檔位時**比對擁有者**（`conv.user_id == caller`），不符回 None——admin 能過 access gate，不能借別人對話的檔位。
- `GET /api/router-models` 每列多回 `thinking_levels_supported`、`thinking_user_selectable`、`thinking_effort`（模型預設）。
- **支援等級在註冊模型時自動探測引入**，不另設按鈕、不靠 admin 記得去按：
  - 觸發點：`POST /api/models`（單筆新增）、`/v1/models` 整批帶入（`bulk import`）、以及 `PUT /api/models/{id}` 改到 `endpoint_url`／`api_version`／`api_key` 時。三條路徑都收斂到同一個 `discover_thinking_levels(model_like)`。
  - 做法：對 `none/low/medium/high/xhigh/max` 各送一顆 token 的探測（沿用 `thinking_probe.py` 的請求、出向 guard 與錯句抽取），回 2xx 的等級進 `thinking_levels_supported`；400 且提到 reasoning effort 的排除；整批 `unreachable` 就留 NULL 並在回應標 `thinking_probe: unprobed`。
  - 若端點對所有等級都回 2xx 但明顯無視（gemma 類），仍記成全支援——真正有沒有想，由回覆的 reasoning token 決定，UI 不預先判斷。
  - 探測結果不阻擋存檔；只有 admin 明確填的 `thinking_effort` 被拒才維持現行 422。
  - 整批帶入時探測並行、每模型最多 6 次小呼叫，逾時 20 秒放棄該模型；治理中心模型列顯示「已探測／未探測」。
  - 保留 `POST /api/models/{id}/probe-thinking` 只當維運補救（端點升級後重探），不是主要路徑。
- `apply_model_sampling_overrides` 多一個輸入：**本請求的有效檔位**。優先序：

  1. 呼叫端 body 自帶 `reasoning_effort`／`chat_template_kwargs.enable_thinking`（SDK 使用者、Router 摘要呼叫）→ 原樣。
  2. body 帶 `anila_thinking_tier`（單則覆寫）→ 對映後套用；此欄位**pop 掉**不上游。
  3. `X-ANILA-Conversation-Id` 對應對話的 `thinking_tier` 非 NULL／非 default → 對映後套用。
  4. 模型列 `thinking_effort`（現行行為）。

  對映函式 `resolve_thinking_level(tier, supported, model_default)` 放在 `sampling.py` 旁，純函式，補真值表測試。`thinking_user_selectable=False` 時 2、3 直接忽略並在回應 `anila_meta` 註記 `thinking_locked=true`。

**Router**

- 不需要懂檔位：`X-ANILA-Conversation-Id` 本來就轉給 CSP。單則覆寫走 body 欄位，Router 把 `anila_thinking_tier` 原樣放進上游 payload（和 `router_model` 一樣在 `chat_completions` 入口處理）。
- 自動 compact 的摘要呼叫已明確送 `reasoning_effort=none`，不受影響。

**Shell**

- `ThinkingPicker.jsx` 複製 `RouterModelPicker.jsx` 的結構（浮層、鎖定、錯誤列）。
- 對話切換時同步 `thinkingTier`；建立對話時可帶初值（沿用使用者上一則的選擇，存 `localStorage`）。
- 回覆折疊列顯示「思考 N tokens · 深入」。

### 2.4 治理與稽核

- 預設 `thinking_user_selectable=TRUE`；admin 可對特定模型鎖住（例如共用顯卡吃緊時）。
- 深入檔位是否要落 audit：**不落**。理由：等級不是密等也不是資料存取；用量已在 `token_usage`。⚠ 擁有者若要看「誰一直開深入」，改為每日用量報表加一欄即可。
- **不以「把模型思考調低」當作讓功能過關的手段**（擁有者 2026-09-15 明示）。GLM 現在 `thinking_effort=max` 就讓它 max；截斷、逾時、續寫都必須在這個設定下成立。已註冊模型的 `thinking_levels_supported` 在下次改端點欄位時自動補探，或由維運手動重探。

### 2.5 驗收

| # | 情境 | 期望 |
|---|---|---|
| 1 | 對話選「關閉」，問一題 | 上游 body 含 `enable_thinking=false`、無 `reasoning_effort`；回覆無思考折疊 |
| 2 | 對話選「深入」，模型支援集 `[low,medium,xhigh]` | 上游 `reasoning_effort=xhigh` |
| 3 | 同上，中途換成只支援 on/off 的模型 | 上游只帶 `enable_thinking=true`；UI 檔位顯示「開啟」 |
| 4 | SDK 直接帶 `reasoning_effort=low`，對話設「深入」 | 上游仍是 `low`（呼叫端優先） |
| 5 | admin 鎖住該模型 | 使用者選單灰掉；既存對話的檔位被忽略，`anila_meta.thinking_locked=true` |
| 6 | 兩個分頁同時改檔位 | 第二個拿 409，重新拉對話後同步 |
| 7 | 新增模型，端點對 `high/max` 回 400、其餘 2xx | 存檔成功，`thinking_levels_supported=[none,low,medium,xhigh]`，使用者選單只出現對應檔位 |
| 7b | 整批帶入 3 個模型，其中一個端點逾時 | 三個都建立；逾時者 `thinking_levels_supported=NULL`、列上標「未探測」，選單只給「依模型預設」 |
| 8 | 自動 compact 摘要呼叫 | 不受對話檔位影響（`reasoning_effort=none`） |

---

## 3. 分期

| 期 | 內容 | 依賴 | 估工 |
|---|---|---|---|
| **A** | §2 思考選單全套（migration、CSP 對映、註冊時自動探測、Router 透傳、shell picker、9 條驗收） | 無 | ✅ 2026-09-15 合入 main（四分支：csp `3b0c0c96`、router `2e01dfa2`、gov-ui `3f9364fa`、shell `1909f851`；獨立審查通過）。待活體驗收 §2.5。2026-09-16：Router `_call_llm_non_stream`／`_stream_llm_sse` 思考檔位改 opt-in（主模型呼叫才帶）；Shell「重試」重放 `thinking_applied.source=turn` 的深入覆寫 |
| **B** | 思考／正文 token 帶到 `anila.meta`；折疊列顯示；用量頁若無則先做每對話小計 | A | ✅ 2026-09-15 合入 main（csp `b5f22b85`、shell `c9297cd0`）。契約定案：`token_usage.reasoning_tokens`（r1_0041）；`anila_meta.usage.reasoning_tokens` + `reasoning_tokens_source: reported\|estimated\|null`（null 僅當 tokens 為 null）；`anila_meta.thinking_applied{tier,level,source}`；串流所有 named `anila.meta` 暫存到 usage 收齊後合成一則終端 meta，`usage_complete: bool`（中斷 flush 為 false，關閉路徑不 yield）；agent 中斷 `usage_source="unavailable"`；`GET /api/usage/me`（`by_kind[].kind`）、`GET /api/conversations/{id}/usage`（僅擁有者）；shell「我的用量」為平台入口 Modal，總數＝prompt+completion，思考另計 |
| **C** | Compact boundary 寫回對話（`messages` 加一則 `role=system, kind=compact_summary`，前端重送時從 boundary 之後開始）；手動「整理對話」按鈕接 `force=True` | D | 2 天 |
| **D** | `stripImagesFromMessages`：compact 前先把 data URL／base64 換成佈位；PTL 重試順序改為「剝圖→摘要→硬截」 | — | ✅ 2026-09-15 合入 main（`2f094f35`）。剝圖保留回合數跟隨呼叫端 `keep_recent_turns`（回合前 4、PTL 2）；每張 data URL 圖估 800–2000 tokens；PTL 最多兩次重試 |
| **E** | 產物下載成檔；React/JSX 產物 vendor（React UMD＋Babel standalone 放 `public/vendor/`，`artifactVendor.js` 加改寫規則） | — | 1–2 天 |
| **F** | 使用者可寫的「回覆偏好」頁＋「我的記憶」管理頁（列、刪、來源對話）；活體驗每輪萃取有沒有跑 | 先驗現況 | 3 天 |
| **G** | 精準計數：模型端 `/tokenize` 或 tiktoken 近似；compact 門檻改用真值 | — | 1 天 |
| 🔵 | Projects、產物版本歷史（v1／v2） | 擁有者拍板 | 另開藍圖。局部修訂 ✅ 2026-09-16 合入 main（shell `b8e430d7`；獨立審查 r5 PASS；不做 v1／v2） |

建議順序 **A → B → C → D**，因為 A 是使用者點名要的，B 讓 A 的效果看得見，C／D 是這週 compact 的收尾。E 便宜可插隊。

---

## 4. 風險與待拍板

1. **GLM 可以拿來驗，但不准為了過關把它的思考調低**。A 期可對線上 glm-5.3-flash 跑探測取得支援集；驗收情境全部在它現行 `max` 設定下跑。若某條驗收在 max 下不過，那是功能沒做好，不是設定問題。
2. **同一把版本鎖**（`router_selection_version` 共用）：模型與思考兩個欄位互相會撞 409。可接受——兩個都是「這則對話的設定」，撞到就重拉。若擁有者要各自獨立，改成兩把鎖多一欄。
3. **深入模式吃顯卡**：多人同開會拉長排隊。B 期把 token 顯示出來後再看要不要限流；先不做。
4. 🔵 **Projects 要不要做**：這是 claude.ai 最大的結構差異，但會碰密等繼承（專案內檔案的密等要傳到每則對話）與分享範圍。建議等 G9（密等標記介面）有結論再開。
5. 🔵 **產物版本歷史**已和 D3 綁在一起，本檔不解綁。
6. 本檔所有 ⚠ 項目在排期前要先各花半小時在活體上確認，避免把已有的功能再做一次。

---

## 5. 不做的事

- 不讓使用者直接輸入數字（`budget_tokens`、`max_tokens`）。claude.ai 也不給。
- 不把後端字串（`xhigh`／`max`）暴露給一般使用者。
- 不為思考等級新增 audit action。
