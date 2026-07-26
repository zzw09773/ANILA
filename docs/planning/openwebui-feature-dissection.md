# Open WebUI v0.10.2 功能解剖報告(供 ANILA 重建參考)

> 產出日期 2026-07-26。方法:十路平行讀碼(commit `ecd48e2` / v0.10.2),每條宣稱附 `file:line`。
> **本報告不含 Open WebUI 程式碼**(全文引用 0 行),一律以 pseudo-schema 與散文描述結構——因其授權條款禁止移除品牌且我們只學設計不引入實作。
> 不含資安分析(依指派範圍)。

---


---

# Open WebUI v0.10.2 完整功能解剖報告

> 標的:`commit ecd48e2`(`0.10.2`)。路徑皆相對於 scratchpad 內的 `openwebui/`。所有宣稱附 `file:line`;推測一律標明。**本報告不含資安分析**,且刻意不貼可直接複製的實作片段(全文引用程式碼 0 行,一律以 pseudo-schema 與散文描述結構)。
>
> ⚠ **先講一個會影響你怎麼讀二手資料的事實**:v0.10 是一次大重構。網路上絕大多數 Open WebUI 架構文章描述的 `PersistentConfig` / `AppConfig` / `app.state.config` 機制,在這份原始碼裡**搜尋結果為零**;各資源的 `access_control` JSON 欄位也已被實際 DROP。取代它們的是 per-key 的 `config` 表(migration `3ff2c63645b8_reshape_config_to_per_key_rows.py`)與統一的 `access_grant` 表(migration `f1e2d3c4b5a6_add_access_grant_table.py`)。**照舊文章設計 ANILA 會抄到一個已經被作者自己淘汰的架構。**

---

## 一、功能哲學:五句話

1. **一切都是同一棵樹、同一條管線**——編輯、重新生成、多模型並排、Arena、排程自動化、頻道裡 @ 模型,全部收斂成「訊息樹上長出一個兄弟節點」+「呼叫同一個 chat completion handler」,新功能因此幾乎不需要新機制。
2. **權限是聯集,不是交集**——群組只能「解鎖」不能「收緊」(`utils/access_control/__init__.py:41-67` 的合併運算子只有 `or`),ACL 是「owner OR grant」;整個系統沒有任何減法語意,換來管理員永遠不會被兩個群組打架搞糊塗。
3. **把危險的東西推到行程外或瀏覽器內**——Python 執行預設在使用者瀏覽器的 Pyodide、終端機是對 admin 註冊之外部 server 的反向代理(`routers/terminals.py:1-6`)、工具伺服器是遠端 OpenAPI/MCP;主行程盡量只做編排。
4. **模型從「被餵資料」變成「自己去拿」**——v0.10.0 正式把 native function calling 設為預設,知識庫不再自動注入,改由模型用 `ls`/`grep`/`cat` 語意的工具自己查(`CHANGELOG.md:262`)。
5. **設定不是文件而是產品**——500+ 設定項全部可在 UI 熱改、立即生效,並用十種漸進式揭露手法讓這個龐大表面仍然可用;代價是幾乎沒有自動化測試(backend 測試檔 0 個,CI 只跑 `ruff format --check` 與窄化的 `ruff check --select=F`,`.github/workflows/backend.yaml:36-42`)。

---

## 二、逐面向拆解

### A. 對話核心

#### A-1 訊息樹(parentId / childrenIds)
- **使用者看到**:只看到「從根到 `currentId` 的一條線」;其他分支要按 `< 1/3 >` 才出現。
- **怎麼運作**:整棵樹是**扁平物件 + 雙向指標**,存在 `Chat.svelte` 的 `history = { messages: {}, currentId }`(`src/lib/components/chat/Chat.svelte:224-227`)。線性化演算法(從 `currentId` 一路取 `parentId` 到 `null` 再反轉)有三份實作:前端共用 `createMessagesList`(`src/lib/utils/index.ts:1378-1392`)、`Messages.svelte` 內帶分頁截斷與循環偵測的 `buildMessages()`(`src/lib/components/chat/Messages.svelte:85-132`,用 `requestAnimationFrame` 節流)、以及後端鏡像 `get_message_list`(`backend/open_webui/utils/misc.py:110-140`)。
- **資料模型**:`Message{id, role, content, parentId, childrenIds[], timestamp, done, model, modelIdx, models[](user), merged, statusHistory, files, output}`;根節點 `parentId === null`,允許多根。
- **設計洞察**:`childrenIds` 是刻意冗餘的反向索引,讓「取某節點所有分支」變 O(1),代價是任何刪除/嫁接都得修兩邊——所以存在 `sanitizeHistory()`(`src/lib/utils/index.ts:233-300`)在載入時用反查表補回孤兒節點的 `parentId`。**「有修復函式」本身就是這個設計的成本收據。**

#### A-2 編輯 → 分支 / 重新生成:同一個原語
- 編輯並提交時**不改原節點**,而是產生新 UUID、沿用**原節點的 parentId**、push 進父節點 `childrenIds`(`Messages.svelte:339-374`);只編輯不重送則就地改 `content`(`Messages.svelte:376-380`)。
- 重新生成則以「原回答的 parent(即使用者訊息)」為 parent 建新節點(`Chat.svelte:2798-2829` → `2264-2288`),於是新舊回答成為兄弟,共用同一套 `< n/m >` 導覽器(`Message.svelte:62-66, 83`)。
- **引導式重新生成**(「更詳細/更簡短」)不是改 system prompt,而是把引導語當成一則額外 `user` 訊息附加到陣列尾端(`utils/middleware.py:2204-2257`)。
- **貫穿全系統的單一慣例**:切換到任何兄弟後,一律沿 `childrenIds.at(-1)` 貪心下鑽到葉節點(`Chat.svelte:516-552`、`Messages.svelte:191-328`、`MultiResponseMessages.svelte:76-146`)。**「切到舊分支就自動跟到該分支最新的一條路徑」是整個分支系統最重要的單一設計決定。**

#### A-3 多模型並排 + Merge(MoA)
- N 個模型 = 同一個 `userMessage.id` 底下 N 個子節點,各帶 `modelIdx`(陣列索引);但**只發一次 HTTP 請求**,帶 `messageIdsList: [{model_id, message_id}]` 讓後端 fan-out 成多個背景任務(`Chat.svelte:2257-2293, 2353`)。
- 渲染分流只靠一個廉價旗標:父訊息 `models.length === 1` 走單欄,否則走多欄(`Message.svelte:76`)。多欄元件以 `modelIdx` 分組,**每欄各自維護自己的版本指標** `groupedMessageIdsIdx[modelIdx]`(`MultiResponseMessages.svelte:148-197`)。
- 下一輪只延續 `history.currentId` 那一支(`Chat.svelte:2095`),其餘 N-1 支保留可回看但不再延伸。
- **Merge**:蒐集各欄當前版本內容,打 `POST /api/v1/tasks/moa/completions`,套 `DEFAULT_MOA_GENERATION_PROMPT_TEMPLATE`(`config.py:2368-2372`)的 `{{prompt}}`/`{{responses}}`(`utils/task.py:372-389`)。**執行合併的模型 = 當前作用中那一欄的模型**,不是另設的合併模型(`Chat.svelte:2857-2909`)。
- **資料模型上最聰明的一點**:合併結果掛成當前節點的旁生欄位 `message.merged = {status, content}`,**不是新樹節點**——因為它邏輯上綜合了 N 個兄弟,當不了任何人的兒子;這樣也不會污染 `siblings.length` 的版本計數。

#### A-4 續寫 / 訊息佇列
- **續寫**是唯一「原地改既有葉節點」的操作:沿用同一個 `responseMessageId`,帶 `assistant_message_id`,後端把該訊息已有內容當作一則 `assistant` 訊息附加到陣列尾端(assistant-prefix continuation,`utils/middleware.py:2215-2227`),前端 `content +=` 累加。刻意不走分支機制,因為語意是「同一則回答的延續」而非「新一次嘗試」。
- **佇列**:串流中送出的訊息不打斷、不併發,而是 push 進 `chatRequestQueues` store(依 chatId 分桶,`src/lib/stores/index.ts:87-89`);生成結束時 `processNextInQueue` 把所有排隊項目以 `\n\n` **串接成單一則訊息**送出(`Chat.svelte:1736-1756`)。UI 層仍視為多筆(可個別編輯/刪除/立即送出),只在真正送出時才折疊——避免樹裡長出一串瑣碎單輪節點。純前端、不持久化。

#### A-5 持久化:JSON blob 與正規化表雙寫
| | JSON blob | 正規化表 |
|---|---|---|
| 位置 | `chat.chat` 欄位(`models/chats.py:50`) | `chat_message` 表(`models/chat_messages.py:80-123`) |
| 存什麼 | 整包 `{history:{messages,currentId}, models, params, files, title}` | 每則一列,含 `parent_id`;**不存 `childrenIds`** |
| 誰讀 | **前端渲染唯一來源** | 後端組 context 的快速路徑 + 所有分析聚合 |
- `childrenIds` 在讀取時由所有列的 `parent_id` 動態反推(`chat_messages.py:265-326`)。
- 雙寫時機包含**每一個串流 chunk**(`socket/main.py:939-965` → `chats.py:763-772`),正規化表寫入全包在 try/except 只 log warning。
- 讀取權威:`get_messages_map_by_chat_id()` 優先讀正規化表,偵測到 `parent_id` 指向不存在的列(`get_unresolved_parent_ids`,`chats.py:543`)才退回 JSON blob 補齊並回寫(自我修復,`chats.py:644-693`)。
- **洞察**:文件型儲存(一次讀寫還原完整 UI 狀態)+ 正規化索引(SQL 聚合 token 用量/每日訊息數,免解析巨大 JSON)的折衷。代價是寫入放大,但用「blob 永遠優先、正規化表容錯降級 + 自我修復」把不一致的影響限制在一次額外查詢。

#### A-6 串流:HTTP 觸發、socket.io 傳遞
- 生成請求 `POST /chat/completions` 直接 `res.json()` 回傳 `task_id`/`task_ids`(`src/lib/apis/openai/index.ts:228-258`)——**不是要逐字讀的 stream**。
- 內容增量走 socket.io 通用通道:`sio.emit('events', {chat_id, message_id, data}, room=f'user:{user_id}')`(`socket/main.py:919-937`),粒度是**每使用者一個房間**,前端自行以 `chat_id` 過濾、以 `message_id` 定位樹節點(`Chat.svelte:613-617`)。
- **為什麼這樣**:生成在伺服器背景進行,不依賴發起請求的 HTTP 連線是否還活著——切分頁、重新整理、換裝置都能接回進行中的訊息(`Chat.svelte:629-634, 898-909`)。真正走 SSE-over-fetch 的只有「一次性、單一消費者」的任務型請求(MoA、標題生成),因為它們不需要跨裝置同步(`src/lib/apis/streaming/index.ts`)。

#### A-7 Reasoning 呈現與「思考時間」怎麼算(高價值細節)
- 標籤表 `DEFAULT_REASONING_TAGS` 含 `<think> <thinking> <reason> <reasoning> <thought> <Thought> <|begin_of_thought|>` 及全形 `◁think▷`,可被 `metadata.params.reasoning_tags` 覆寫(`utils/middleware.py:140-149, 3901-3929`)。
- 串流狀態機 `tag_output_handler`(`utils/middleware.py:3636-3858`)逐字元搜尋起始標籤,命中就切斷當前 `message` output item、插入一個 `type:'reasoning'` item;provider 原生欄位(`reasoning_content`/`reasoning`/`thinking`)走另一條路徑但邏輯相同(`middleware.py:2967-2994, 4300-4369`),第一個正式 `content` delta 即代表思考結束。
- **耗時完全在伺服器算**:建立 item 時記 `started_at = time.time()`,結束時記 `ended_at` 並算 `duration = int(ended_at - started_at)`(`middleware.py:3814-3816, 2993-2994`),寫死進 item 欄位並隨每個 SSE event 持久化(`middleware.py:3941-3947`)。
- **reload 後不重算**:前端純粹依 `attributes.duration` 渲染(`Collapsible.svelte:90-107`)。**這一招直接避開「前端計時器 + 重整/多分頁不同步」這個經典坑。**
- 新舊 schema 並存:有 `output` 陣列走 `StructuredOutputRenderer`,沒有則退回用自訂 marked `details` tokenizer 解析內嵌的 `<details type="reasoning" duration>` HTML(`src/lib/utils/marked/extension.ts:29-94`)。0.10.0 CHANGELOG 明講這次把 reasoning/tool call/server-side tool step 改成**由結構化輸出在瀏覽器渲染,而非在伺服器壓平進訊息文字**。

#### A-8 程式碼執行、Artifacts、Mermaid/LaTeX/Vega
- **兩套執行引擎**:`$config.code.engine === 'jupyter'` 走後端 `JupyterCodeExecuter`(REST 建 kernel + WebSocket 送 `execute_request`,`utils/code_interpreter.py`);否則走瀏覽器 Pyodide worker,用 regex 掃 import 決定 `micropip.install` 哪些套件、掛 IDBFS 到 `/mnt` 讓上傳檔案持久化、若含 matplotlib 就 monkeypatch `plt.show()` 吐 base64 PNG(`CodeBlock.svelte:223-356`、`src/lib/workers/pyodide.worker.ts:49-63, 194-219`)。
- **最有意思的耦合**:native 模式下 `execute_code` 是個註冊的 tool(`backend/open_webui/tools/builtin.py:431-448`),但實際執行是後端透過 socket.io 反向 RPC 呼叫**使用者瀏覽器**(`__event_call__({type:'execute:python'})`,`builtin.py:494-504` → `src/routes/+layout.svelte:521-525`),結果再序列化回 tool result。伺服器發起、瀏覽器執行。
- **Artifacts 的「版本」不是版本鏈**:每次 `history` 變動就重新掃描**整個對話**所有助手訊息的程式碼區塊(`Chat.svelte:1284-1337` → `src/lib/utils/index.ts:2031-2089`),css/js 區塊會附掛到最近一個 html 分組;掃描前先 `removeAllDetails` 剝掉思考區塊避免誤判。所以「Version X of Y」= 對話中依序找到的第 X 段可渲染內容,**無法命名、無法知道兩個版本是不是同一個 artifact 的疊代**。純前端衍生狀態、不持久化。
- 自動開側欄的觸發極早:串流中只要偵測到 token 語言是 `html`/`svg`,程式碼還沒輸出完就 `showArtifacts.set(true)`(`ContentRenderer.svelte:132-147`)。沙盒為 `iframe sandbox="allow-scripts allow-downloads"` + `srcdoc` + 把 admin 設定的 `ui.iframe_csp` 注入成 `<meta http-equiv>` 放在 `<head>` 最前面(`Artifacts.svelte:244-258`、`src/lib/utils/csp.ts`)。
- **Markdown 管線**:marked + 模組層級一次性註冊 katex/details/citation/footnote/colonFence/disableSingleTilde 與 `@`/`#`/`$` 三種 mention(`Markdown.svelte:12-29`)。KaTeX 定界符表含 `$…$`、`$$…$$`、`\(…\)`、`\[…\]`、`\ce{}`、`\pu{}`、`\begin{equation}`,並用含 CJK 標點的 `ALLOWED_SURROUNDING_CHARS` 判斷邊界避免誤判(`src/lib/utils/marked/katex-extension.ts:1-20`,註解記載 regex 編譯曾佔渲染時間 87%,故預編譯)。
- **串流中不完整 markdown 的策略**:不做補全,而是每個動畫影格對「目前累積的完整字串」整段重新 lex,靠 marked 自身容錯(`Markdown.svelte:66-97`);但會產生視覺副作用的 `mermaid`/`vega`/`vega-lite` 區塊,**必須偵測到收尾的三個反引號才嘗試渲染**(`CodeBlock.svelte:366-389, 378-384, 440`)。順帶發現:**模型吐 `vega-lite` JSON 區塊會直接渲染成圖表**,和 mermaid 同級待遇。

#### A-9 create_tasks 與時間感知注入
- **`create_tasks` / `update_task`**(`tools/builtin.py:2917-3014`,狀態限 `pending|in_progress|completed|cancelled`)寫進 **chat 層級**的 `chat.tasks` JSON 欄位(`models/chats.py:62, 2052-2059`),而非某則訊息的 output。因此模型可以在第 5 輪把第 1 輪建的任務標記完成;代價是沒有版本歷史,且並行分支會共用同一份清單狀態。透過 socket 事件 `chat:message:tasks` 即時同步,只在有 pending/in_progress 項目時顯示(`ResponseMessage/TaskList.svelte`)。
- **模板變數完整清單**(這是可以直接抄的資產):
  - 後端 `utils/task.py:36-105`:`{{CURRENT_DATE}} {{CURRENT_TIME}} {{CURRENT_DATETIME}} {{CURRENT_WEEKDAY}} {{USER_NAME}} {{USER_EMAIL}} {{USER_BIO}} {{USER_GENDER}} {{USER_BIRTH_DATE}} {{USER_AGE}} {{USER_LOCATION}} {{USER_GROUPS}}`(`USER_GROUPS` 只在模板真的含此變數時才查 DB——懶查詢優化)。
  - 截斷語法:`{{prompt:start:N}} {{prompt:end:N}} {{prompt:middletruncate:N}}`(`task.py:108-133`);`{{MESSAGES}}` 及 `:START:N`/`:END:N`/`:MIDDLETRUNCATE:N` 變體並可疊 `|filter`(`task.py:197-257`);RAG 用 `{{CONTEXT}} {{QUERY}}`;另有 `{{TYPE}} {{responses}} {{TOOLS}}`。
  - 前端 `src/lib/utils/index.ts:1172-1184` 額外提供 `{{CURRENT_TIMEZONE}}`(後端完全沒有)與 `{{USER_LANGUAGE}}`,且用瀏覽器 `Intl` 取真實時區。
  - **優先序**:前端把 `metadata.variables` 隨請求送出,後端 `resolve_system_prompt` **先**套前端值(使用者時區)、**後**用後端 `prompt_template` 補漏(註解直書 `# Legacy (API Usage)`,`utils/payload.py:13-30`)。
  - ⚠ **落差**:所有任務型 prompt(標題/標籤/follow-up/query 生成)一律走後端伺服器時間且無時區變數(`task.py:304-369`)。伺服器與使用者不同時區時,背景任務的日期認知會與主回覆不一致。重建時應把使用者時區存進 user profile,讓所有替換都查同一來源。
- **一個沒被指派、我自己挖到的功能**:`src/lib/components/chat/Overview.svelte` + `Overview/{Flow,Node,View}.svelte` 用 `@xyflow/svelte` 把整棵訊息樹畫成**節點圖總覽**。對「編輯出一堆分支後迷路」這個問題,這是比 `< 1/3 >` 更根本的答案。

---

### B. 模型與連線

#### B-1 Connections:平行陣列 + 索引字典
- 形狀是 `OPENAI_API_BASE_URLS: string[]` / `OPENAI_API_KEYS: string[]`(等長同序)/ `OPENAI_API_CONFIGS: {"0": {...}}`(以**陣列索引字串**為 key),皆由 `;` 分隔字串解析(`config.py:325-349`);Ollama 對應 `OLLAMA_BASE_URLS` / `OLLAMA_API_CONFIGS`(key 存在 config 內,`config.py:286-302`)。保留以 URL 當 key 的舊格式 fallback:`api_configs.get(str(idx), api_configs.get(url, {}))`(`routers/openai.py:296-301`)。
- 每連線 config 欄位:`enable, tags[], prefix_id, model_ids[], connection_type(local|external), provider, azure, api_version, api_type, auth_type, headers`(`src/lib/components/AddConnectionModal.svelte:183-198`);Azure 強制要求 `api_version` + 至少一個 `model_ids`(因為 Azure 用 deployment name 取代 `/models`,`AddConnectionModal.svelte:145-165`)。
- **設計洞察(反面教材)**:索引即隱性主鍵,刪一個連線就要手動平移所有索引鍵(`Connections.svelte:278-282`)。重建時直接用一張 `connections` 表配 UUID 主鍵。

#### B-2 模型探索完整流程
1. `/api/models` → `utils/models.py:get_all_models(request, refresh, user)`,先看 `app.state.BASE_MODELS` 快取(由 `models.base_models_cache` 開關控制,預設關)(`utils/models.py:66-90`)。
2. `get_all_base_models` 用 `asyncio.gather(openai, ollama, function)` 三路並行,provider 關閉就用 `asyncio.sleep(0, result=[])` 佔位(`utils/models.py:55-63`)。
3. 同 provider 內跨連線也並行:每個 idx 一個 coroutine 一起 gather(`routers/openai.py:442-477`);單連線 timeout 由 `AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST`(預設 10s,`env.py:587-589`)控制,失敗只回 `None`(`routers/openai.py:113-116`)——**故障隔離乾淨**。
4. `model_ids` 白名單短路:OpenAI 側**完全不打上游**,直接用白名單捏造清單(`routers/openai.py:452-473`);Ollama 側是打完 `/api/tags` 再過濾(`routers/ollama.py:404-405`)。
5. `prefix_id` 改寫 id 成 `{prefix}.{原id}`(`routers/openai.py:502-503`、`routers/ollama.py:408-409`)——天然避免撞名。
6. **去重規則兩邊不對稱(重要)**:OpenAI 側依連線順序迭代,`if model_id not in models` → **先到者(低 idx)贏**,每筆帶唯一 `urlIdx`(`routers/openai.py:580-618`);Ollama 側則保留全部來源,同名模型累積 `entry['urls'].append(idx)` → **多主機共享語意**(`routers/ollama.py:335-350`)。**不能用同一套 dedupe 函式。**
7. 兩層快取:`aiocache` 的 `@cached(ttl=MODELS_CACHE_TTL)`,TTL 預設 **1 秒**、key 依 user 分(`routers/openai.py:543-547`)——這是「同一秒內去抖動」而非長期快取;長期快取是上層 `app.state.BASE_MODELS`,`?refresh=true` 才失效。⚠ 多 worker 下這層是各 worker 獨立的 in-process dict,短暫不一致是既有取捨而非 bug(`utils/models.py:66-90`)。

#### B-3 Model Builder(base_model_id 疊加)
- `Model` 表:`id, user_id, base_model_id(nullable), name, params(ModelParams, extra='allow'), meta(ModelMeta, extra='allow'), is_active`(`models/models.py:75-104`)。`knowledge`/`toolIds`/`filterIds`/`actionIds`/`skillIds` 都靠 `extra='allow'` 塞進 `meta`,空清單直接 `delete` 該欄位而非存空陣列(`ModelEditor.svelte:196-240`)。
- **system prompt 不是獨立欄位**,而是 `Model.params.system`,走跟 temperature 完全一樣的 tri-state 語意,只是最後被 `remove_open_webui_params` 摘出交給 `apply_system_prompt_to_body` 處理(`utils/payload.py:72-95, 380-431`)。
- 請求時把 `payload['model']` 換成 `base_model_id`(`routers/openai.py:1130-1136`)——**自訂 id 只存在於 Open WebUI 這一層,到 provider 邊界就消失**。
- base model 不見了:`ENABLE_CUSTOM_MODEL_FALLBACK`(預設 False)開啟時退用 `ui.default_models` 第一個,否則丟 `Model not found`(`main.py:1056-1072`)。
- `has_base_model_access` 沿 `base_model_id` 鏈往上逐層驗證 read 權限,任一環失敗整條不可存取(`utils/access_control/__init__.py:259-297`)。
- **洞察(可改進)**:`base_model_id is None` 同時代表「這是根模型」與「這是覆蓋既有 base model 的偽裝」,導致 `get_all_models` 要用一個 if/else 分岔兩種完全不同的合併策略(就地改寫 vs 新增節點,`utils/models.py:145-223`)。重建時拆成明確的 `kind: 'override' | 'derived'`。

#### B-4 三層參數繼承與 tri-state(最需要精確的一節)
- **tri-state 語意**:前端 `defaultParams` 每個鍵初值 `null` = 繼承上層;按鈕在 `null` 與具體值之間切換(`AdvancedParams.svelte:15-49, 404`)。後端只有 `value is not None` 才寫回(`utils/payload.py:56-69`)。**因此 `0` / `''` / `False` 都算「已設定」,與「未設定」在資料層可區分**——這正是用 `null` 而非額外 `isSet` 布林的關鍵好處。
- **合併點**在 `/api/chat/completions`(`main.py:1043-1054`),順序:`{**global(models.default_params), **model_info.params}` → 再 `{**那個, **request_params(過濾 None)}`,即 **全域 < 模型 < 對話**。
- ⚠ **但實際生效優先序不是這樣**:payload 傳到 provider router 後,`generate_chat_completion` 會**再查一次** `Models.get_model_by_id` 取**未合併的原始** `model_info.params`,用 `apply_model_params_to_body_*` **再蓋一次**(`routers/openai.py:1130-1150`、`routers/ollama.py:1103-1117`)。淨效果:**凡是模型層明確設值(非 null)的鍵,最終會覆蓋掉使用者本次對話的手動調整**,真實優先序是 **模型 > 對話 > 全域**。這只有對照原始碼才看得出來。重建時要決定「這是刻意的管理員鎖定,還是該修的重複套用」,並且**只在一個地方做最終合併**。

#### B-5 Arena、權限、Capabilities
- Arena 以 `owned_by == 'arena'` / `model['arena'] = True` 標記(`utils/models.py:92-126`),候選池來自 `info.meta.model_ids`(`filter_mode == 'exclude'` 則反向排除)。**隨機挑選延後到請求時**才 `random.choice`,並把 `metadata['selected_model_id']` 傳下去讓知識庫/工具/參數都用真實模型設定(`utils/middleware.py:2172-2201`);背景任務另有 fallback 重抽(`utils/chat.py:206-221`)。
  - **匿名邊界很乾淨**:一般訊息氣泡完全不揭露模型;只有使用者主動打開評分面板時 `RateComment.svelte` 才查真名並跳 toast(`RateComment.svelte:74-82, 106-112`)。延後隨機 + 延後揭露,比在清單階段就固定挑選公平得多(否則快取期間會永遠打到同一顆)。
- 權限:`is_active`(主表布林,軟停用)與 `AccessGrant`(獨立表,可見性)**分成兩套機制**,各自可索引化查詢;`get_filtered_models` 用批次查詢而非逐筆 `has_access`(`routers/openai.py:518-540`、`utils/models.py:418-472`)。排序/釘選走 `ui.default_models` / `ui.default_pinned_models` / `ui.model_order_list`(`config.py:1625-1667`)。
- **Capabilities 完整清單**(`src/lib/constants.ts:100-112`):`file_context, vision, file_upload, web_search, image_generation, code_interpreter, terminal, citations, status_updates, usage, builtin_tools`。全域 `models.default_metadata.capabilities` 當基底,個別模型 `{**全域, **個別}` 覆蓋(`utils/models.py:307-310`)。
  - ⚠ 前後端**各自硬編碼預設值**(缺鍵一律當 `true`,唯 `usage` 當 `false`),容易對新旗標不一致。重建時把預設集中定義一次或由後端回傳完整旗標值。

---

### C. 知識庫與 RAG

#### C-1 集合、巢狀目錄、與索引的解耦(最值得抄的一點)
- `KnowledgeDirectory` 是**真正的鄰接表樹**(`parent_id` 自我外鍵 + `UniqueConstraint(knowledge_id, parent_id, name)`,`models/knowledge.py:61-77`),不是路徑字串;`KnowledgeFile.directory_id` 可為 NULL 表根目錄(`models/knowledge.py:97-113`)。麵包屑往上走 parent 組出(`:927-951`)、移動目錄有循環偵測(`:970-1001`)、刪目錄可選「檔案上移」或「連檔一起刪」且遞迴處理子樹(`:1022-1088`)。
- **關鍵事實**:無論檔案在哪個子目錄,向量 collection 名永遠是 `knowledge.id`(`routers/knowledge.py:1420`)。**資料夾只服務人類瀏覽,完全不參與檢索**。
- **洞察**:這個解耦讓「搬資料夾」永遠不需要重建索引,也沒有「資料夾內搜尋 vs 全庫搜尋」的一致性問題。強烈建議 ANILA 照抄。

#### C-2 攝取管線
- 內容擷取引擎(`retrieval/loaders/main.py:411-673`)由 `CONTENT_EXTRACTION_ENGINE` 分派:預設 langchain 各格式 loader、`external`(自架解析服務)、`tika`、`datalab_marker`、`docling`、`document_intelligence`(Azure)、`mineru`、`mistral_ocr`、`paddleocr_vl`。文字檔做 **CJK 感知編碼偵測**避免中日韓亂碼(`:277-370`)——對繁中環境直接有用。
- 切塊(`routers/retrieval.py:1676-1705`):`character` / `token`(tiktoken)/ `token_transformers`(HF tokenizer 當長度函式);可疊 `ENABLE_MARKDOWN_HEADER_TEXT_SPLITTER` 先按 H1–H6 切,再依 `CHUNK_MIN_SIZE_TARGET` 把過小片段前後合併(`:1520-1554`)。**Loader 軸與 Splitter 軸完全正交**——加一種解析引擎不必碰切塊邏輯。
- Embedding(`retrieval/utils.py:1073-1161`):本地 SentenceTransformer(丟 thread pool)或 ollama/openai/azure REST;依 `embedding_batch_size` 切批,`ENABLE_ASYNC_EMBEDDING` 開啟時 `asyncio.gather` 並用 `RAG_EMBEDDING_CONCURRENT_REQUESTS` + Semaphore 限流。**批次內全有全無、批次間互不影響**。
- 逐檔失敗:批次端點回傳 `results`/`errors` 兩陣列各帶 `file_id/status/error`(`routers/retrieval.py:2945-3065`);`reindex` 逐檔 try/except 記進 `failed_files` 繼續下一個(`routers/knowledge.py:349-391`)。前端靠 `GET /knowledge/{id}/files/pending?stream=true` 的 SSE 每 3 秒輪詢,**重整頁面仍看得到處理中狀態**(`routers/knowledge.py:1224-1281`)。

#### C-3 向量庫抽象 + 外部直連
- `VectorDBBase` 強制實作 `has_collection/delete_collection/insert/upsert/search/query/get/delete/reset`;`hybrid_search` 是可選 override,預設回 `None` 代表不支援(`retrieval/vector/main.py:24-101`)。內建後端:milvus(含 multitenancy)、mariadb-vector、qdrant(含 multitenancy)、chroma、pinecone、elasticsearch、opensearch、pgvector、oracle23ai、s3vector、weaviate、opengauss、valkey(`retrieval/vector/factory.py:10-89`);另有 `ASYNC_VECTOR_DB_CLIENT` 用 `asyncio.to_thread` 包住同步 driver(`retrieval/vector/async_client.py:1-40`)。
- **collection 命名同時是存取控制邊界**:`knowledge.id` / `file-{file_id}` / 裸 `{file_id}`(legacy)/ `knowledge-bases`(KB 中繼資料語意搜尋)/ `web-search-*`;`filter_accessible_collections` 依 prefix 決定用哪種權限檢查(`retrieval/utils.py:1241-1304`)。
- **外部向量庫直連**(`retrieval/external.py`)確實可以**指向既有外部索引、完全不 re-ingest**:KB 的 `meta.external = {connection_id, source:{type,name,config}}`,查詢時只做 embedding 再打過去,支援 qdrant / milvus / pgvector 三種,各自寫死一份查詢函式,再用同一個 `_normalize_result` 映射成 `{content, metadata, distance}` 並標 `external: True`(`external.py:33-288`)。外部 KB 全部寫入操作一律 400(`routers/knowledge.py:122-126`)。
- ⚠ **洞察**:這**不是** `VectorDBBase` 的另一個實作,是獨立的**唯讀 adapter** 路徑。理解這點才不會誤以為外部索引支援完整 CRUD。

#### C-4 增量同步 / diff(三段式架構)
- **比對鍵是 `(相對路徑, 檔名)`,判斷變更靠客戶端算的 SHA-256**,不是 file_id 也不是 mtime(前端 `buildDirectoryManifest`,`KnowledgeBase.svelte:562-572`;瀏覽器用 `showDirectoryPicker()` 或退回 `<input webkitdirectory>`,`:492-555`)。
- `POST /knowledge/{id}/sync/diff`(`routers/knowledge.py:1815-1907`):伺服器重建「(path, filename) → {file_id, checksum}」索引(checksum 取自 `file.meta['file_hash']`,由上傳時前端帶入,`routers/files.py:363-367`),然後判定 `added` / `modified`(附 `stale_file_id`)/ `unmodified_count` / `deleted`,並從 manifest 路徑反推需要的目錄集合算出 `mkdir`(淺層先建)與 `rmdir`。
- 前端依序執行:`POST /sync/cleanup` 刪舊向量+File+實體(同時清 `file_id` 與 `hash` 兩種殘留)並倒序刪空目錄 → 建目錄 → 只上傳 `added`+`modified`(`KnowledgeBase.svelte:701-724`)。
- **洞察**:伺服器是純 **stateless diff 計算器**,不持有本機檔案系統知識、無背景輪詢,驅動權完全在瀏覽器。代價:(a) 移動/改名 = 刪除+新增,無法辨識搬移;(b) 破壞性與新增操作分成兩個獨立 API,無交易保護,中途失敗會不一致。ANILA 若要做,建議改用 content-hash 全域比對以偵測搬移。

#### C-5 混合檢索 + rerank
- `rag.enable_hybrid_search` 開啟走 `query_collection_with_hybrid_search`,兩層 fallback:
  1. **原生**:問後端有無 `hybrid_search` / `supports_hybrid_search`(`retrieval/utils.py:378-382`),`hybrid_bm25_weight` 直接交給後端融合。**目前只有 pgvector 實作**(0.10.0 CHANGELOG 明講改成在 DB 內原生執行、不再把整個 collection 載進記憶體)。
  2. **Python ensemble**:langchain `BM25Retriever.from_texts` **對取回的全部文件現場建 BM25 索引** + `VectorSearchRetriever` 組 `EnsembleRetriever`,權重 `[bm25_w, 1-bm25_w]`,用 `CHUNK_HASH_KEY` 當 `id_key` 做 RRF 去重(`retrieval/utils.py:448-576`)。
- Rerank(`retrieval/utils.py:1696-1738`):有 reranker 就用,沒有則 fallback 成 query/doc embedding 的 cosine similarity;先用 `RELEVANCE_THRESHOLD` 過濾、再取前 `TOP_K_RERANKER`,分數寫回 `metadata['score']`。三種實作:`jinaai/jina-colbert-v2` 走 ColBERT late-interaction、`engine == 'external'` 走相容 `/v1/rerank` API、其餘走本地 `CrossEncoder`(可選 sigmoid)(`routers/retrieval.py:162-237`)。**本地與外部都支援**。
- `TOP_K` 與 `TOP_K_RERANKER` 是**兩段式漏斗**(先寬後窄),讓 reranker 從更大候選集挑(`retrieval/utils.py:429-436, 557-564`)。
- ⚠ 洞察:ensemble fallback 每次查詢都現場建 BM25 索引(無持久化倒排索引),是「正確性優先、犧牲大 collection 效能」;只有 pgvector 原生路徑能避開。

#### C-6 Focused vs Full Context
- 三個訊號來源:全域 `rag.full_context` / `rag.bypass_embedding_and_retrieval`;**每個附件各自的 `context: 'full'`**(聊天上傳 toggle `MessageInput.svelte:1666`、模型附掛知識時的 per-file toggle `workspace/Models/Knowledge.svelte:27-45`);網頁/YouTube 擷取內容預設就是 `full`(`Chat.svelte:1197`)。
- 外層粗判斷:`all_full_context = all(item.context == 'full')`——**只有全員 full 才跳過 query 生成**(`utils/middleware.py:1769`);最終傳下去的是 `all_full_context or Config('rag.full_context')`(`:1835`)。
- 真正決策在 **per-item 層級**:`get_sources_from_items` 內對每個 item 各自檢查,`type=='file'` 且 full 就直接塞整份 `file_object.data['content']`、完全不查向量庫(`retrieval/utils.py:1409-1444`);`type=='collection'` 同理把 KB 內每個檔案全文塞入(`:1493-1521`)。
- ⚠ **沒有 per-knowledge-base 的持久化開關**——「per-KB」實際上是「每次附加時」的效果。
- **洞察**:用 `all(...) or global` 而非讓其中一個完全覆蓋,是刻意允許**同一次請求內混用**(附一份要精讀的短文件 + 一個要模糊檢索的大 KB)。這個「item-level 決策 + 外層粗判斷是否需要 query 生成」的兩段式結構值得直接複製。

#### C-7 `#` 引用端到端
- 前端 200ms debounce 並行打三支 API(folders 只載一次、`searchKnowledgeBases`、`searchKnowledgeFiles`),分三段標題列出 Folders/Collections/Files,若輸入是網址再加 YouTube/Web 選項(`chat/MessageInput/Commands/Knowledge.svelte:73-261`)。
- 選中後**不呼叫任何上傳 API**,直接把 item 原樣塞進本地 `files` 陣列並標 `status:'processed'`(`MessageInput.svelte:1097-1117`)——因為那是「引用」不是「上傳」。送出時歷史各則的 `files` 扁平化去重成請求 body 的 `files`(`Chat.svelte:2400-2438`)。
- **洞察**:`#` 引用與「上傳新檔案」共用**同一個前端 `files` 陣列與同一個後端 `get_sources_from_items` 入口**,唯一差別是元素的 `type`/`context` 欄位。單一 polymorphic 附件陣列取代兩條管線,大幅簡化下游(送出、渲染附件列、citation 溯源)。**這是 C 面向最該抄的抽象層次。**

#### C-8 Citations:相關度、分組、與一個跨端隱性契約
- payload 形狀:`sources: [{source, document[], metadata[], distances[]}]`,三個陣列以 index 對齊,一個 source 可含同一文件的多個 chunk(`retrieval/utils.py:1602-1618`)。
- **編號分配**:`get_source_context`(`utils/middleware.py:753-776`)以 `metadata.source`(或 `source.source.id`)當去重鍵,**依首次出現順序**分配 1-based 整數,組成 `<source id="N" name=...>` 塞進 prompt;`DEFAULT_RAG_TEMPLATE`(`config.py:1045-1069`)要求模型只在有 id 時才寫 `[N]`。
- **百分比公式**:`distance < 0 → 0%`;`> 1 → 100%`;否則 `round(distance*10000)/100`(`CitationModal.svelte:25-30`)。是否顯示百分比由 `shouldShowPercentage`(所有 distances 是否都在 [-1,1])與 `calculateShowRelevance`(處理單一極端值)共同決定,否則退回顯示 4 位小數原始 distance(`Citations.svelte:74-96`)。色階 綠≥80 / 黃≥60 / 橘≥40 / 紅<40(`CitationModal.svelte:32-40`)。
- **同文件多 chunk 合併**:以同一去重鍵 reduce 成單一 citation 物件的 `document[]`/`metadata[]`/`distances[]`,Modal 內再依 distance 由高到低排(`Citations.svelte:98-138`、`CitationModal.svelte:52-56`)。inline `[1][2,3][4#foo]` 由自訂 marked extension 解析(`src/lib/utils/marked/citation-extension.ts:1-64`)。
- ⚠ **最重要的警告**:citation 編號一致性完全依賴後端 `get_source_context` 與前端 `getSourceIds`(`ContentRenderer.svelte:102-122`)**各自獨立實作卻採用相同去重規則**,沒有共用型別保證。**重建時務必把「去重鍵 + 編號分配」做成後端單一函式,並把 `id→source` 映射表隨 response 回傳**,別讓兩端各刷一次同樣的演算法。另外百分比公式隱含「分數已正規化到 [0,1]」的假設,接上會吐負值的 reranker 會直接失真。

---

### C-bis. Native Function Calling 下的架構轉向(最高價值:知識庫從「被注入」變成「被查詢」)

#### 分歧點在哪一行
單一 request 參數 `metadata['params']['function_calling']`,值域 `null` / `'native'` / `'legacy'`:
- `utils/middleware.py:2363` — 資料夾附掛的知識:只有 `legacy` 才塞進 `form_data['files']`,否則只把 `allowed_files` 放進 `metadata['folder_knowledge']`(`:2369-2371`)。
- `utils/middleware.py:2377` — 模型層 `model.info.meta.knowledge`:只有 `legacy` 才轉成 files 走傳統檢索注入(`:2389-2413`)。
- `utils/middleware.py:2518-2522` — 核心開關 `use_builtin_tools` = 有 `session_id`(代表來自 UI 而非純 API)**且** `function_calling != 'legacy'` **且** 模型 capability `builtin_tools` 未關。
- `utils/middleware.py:2763-2779` — 最終分歧:非 legacy 就把 `tools_dict` 轉成 `form_data['tools']` 交給模型;否則走 `chat_completion_tools_handler`(`:1085`)。
- `null` 就是隱式的 native——沒有 resolve-default 函式,靠布林短路;前端 tooltip 直書「Native mode (default) … Legacy mode works with a wider range of models by calling tools once before execution via prompt injection」(`AdvancedParams.svelte:199-217`)。

#### Legacy 模式怎麼模擬工具呼叫
用 task model 做**一次**非串流呼叫,套 `DEFAULT_TOOLS_FUNCTION_CALLING_PROMPT_TEMPLATE`(`config.py:2340`)要求回傳 `{"tool_calls":[...]}`,解析後逐一執行(`utils/middleware.py:1085-1281`)。而**知識庫走的是完全獨立的另一條管線**:`generate_queries` → `get_sources_from_items` → `rag_template` 把 `<context>` 整段塞進 system message。兩條都在模型開始生成前決定完內容,模型**沒有主動權,不可能 multi-hop**。

#### Native 模式:兩種形態
**形態 A(v0.10.2 出廠實際預設,因 `ENABLE_KB_EXEC` 預設 False,`env.py:857`)——離散多工具家族**
`get_builtin_tools`(`utils/tools.py:465`)依情況注入:
- 有掛 KB:`list_knowledge`(docstring 直書「Use this first to discover what knowledge is available」,`tools/builtin.py:2351`)、`search_knowledge_files`、`grep_knowledge_files`(精確/regex,`:1928`)、`query_knowledge_files`(語意搜尋,`:2493`),再依 knowledge type 加 `view_file`/`view_knowledge_file`(支援 `offset`/`max_chars` 預設 10000/`start_line`/`end_line` 分頁,`:2205`)/`view_note`(`utils/tools.py:535-544`)。
- 沒掛任何 KB(讓模型自己找):再加 `list_knowledge_bases`、`search_knowledge_bases`、`query_knowledge_bases`(`:545-556`)。
回傳**結構化 JSON**。

**形態 B(opt-in)——`kb_exec`,檔案系統式 shell**
- 單一工具、單一參數 `command: str`,docstring 本身就是使用手冊(`tools/knowledge_fs.py:1083-1133`)。
- 支援 10 個命令 + pipe:`ls cat head tail grep find wc stat sed tree`(`COMMAND_MAP`,`:1037-1048`);quote-aware tokenizer 依 `|` 拆段再 `shlex.split`(`:77-115`),只有 `head/tail/grep/wc/sed` 吃 `piped_input`(`:1070`)。
- **不是真檔案系統**:把 KB 的目錄與檔案在**記憶體中**組成虛擬目錄樹(`_build_directory_tree`,`:152-210`);檔案可用完整路徑、唯一檔名、或內部 `file_id` 三種方式定址,檔名重複回「Ambiguous filename」要求消歧(`_resolve_file`,`:389-459`)。存取範圍受 `_get_accessible_kb_ids`(`:248-354`)與 `model_knowledge` 限制。
- **截斷是設計核心**:`cat` 硬上限 `MAX_CAT_CHARS=100_000` 且超過就提示改用 head/tail/sed/grep(`:570-596`);`head`/`tail` 預設 10 行;`grep` 檔案數超過 `MAX_GREP_FILES=200` 直接拒絕並要求縮小範圍(`:741-742`),命中超過 `MAX_GREP_MATCHES=50` 只顯示前 50 並註明「showing N of M」(`:792-793`);`sed -n 'M,Np'` 精準取行區間(`:914-961`)。
- `is_regex_pattern`/`build_matcher`(`:35-69`)自動判斷 pattern 像不像 regex,與形態 A 的 `grep_knowledge_files` 共用。
- 回傳是**純文字**(`ls`/`tree` 帶 📁 的人類可讀表格、`cat -n` 逐行編號、`grep` 為 `file_id filename:line: text`)。

#### 行為差異與代價
- Multi-hop 真的成立:native 的 tool-calling 是 `while` loop,上限 `CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS` **預設 256**(`-1` 為無限,`env.py:960-975`)。
- 每次工具結果要塞回 message history(`function_call_output`,`utils/middleware.py:4791-4798`),context 隨呼叫次數線性成長——上述所有截斷常數與「建議改用 head/tail」的錯誤訊息就是為了壓住這個成長(這個因果關係是推測,但 0.10.0 CHANGELOG 對應條目明講「returns output in bounded, paginated chunks with a default and a hard cap … sharply reducing token usage」)。

#### ⚠ Citation 歸屬有一個真實落差
白名單寫死在 `utils/middleware.py:4768-4777`:只有 `search_web`、`fetch_url`、`view_file`、`view_knowledge_file`、`query_knowledge_files` 會呼叫 `get_citation_source_from_tool_result`。**`kb_exec`、`grep_knowledge_files`、`list_knowledge`、`search_knowledge_files` 全部不在清單內**,因為它們回傳自由文字、不符 `_EXPECTS_LIST`/`_EXPECTS_DICT` 的形狀。使用者的體感是:同樣讀了知識庫,有些答案有可點擊來源、有些完全沒有。
- 有進白名單的 source,會先 emit 給前端,再用 `get_source_context(..., include_content=False)` 只把 `<source id="N" name=...>` **空殼標籤**塞回 system prompt(`:753-776, 4881-4907`)——內容已透過 tool message 給過模型了,這裡只補編號讓它能寫 `[N]`,不重複耗 token。**這一手很巧妙,值得抄。**
- **對 ANILA 的直接教訓**:走 filesystem-tool 路線就必須強制工具回傳時夾帶 `file_id` + line range 讓上層可解析,否則會失去可稽核的引用鏈——而在涉密環境,**引用鏈就是稽核鏈,不能是選配**。

#### 演進脈絡(作者自己的說法)
| 版本 | CHANGELOG 行 | 內容 |
|---|---|---|
| 0.5.8 | `3419` | Native Tool Calling(實驗),動機原文「reducing query latency and improving contextual responses」 |
| 0.7.0 | `1542,1544` | 知識庫變工具:「search their knowledge bases and retrieve documents **without manually attaching files**」 |
| 0.9.6 | `276` | `ENABLE_KB_EXEC` 檔案系統工具首度登場,明示 opt-in |
| 0.10.0 | `262` | **Native 成為預設**,舊行為改名 Legacy 成為顯式 opt-out(breaking change) |
| 0.10.2 | `204` | 修 bug:native + knowledge 時 model system prompt 不再被丟掉 |
歷時約 1 年 5 個月、每一步都保留舊行為當 fallback。**這個「用 env var 漸進 rollout → 觀察 → 才扶正為預設」的節奏,比那個功能本身更值得學。**

---

### D. 擴充機制

#### D-1 Tools:schema 從型別註記 + docstring 逆向組裝
- `Tools` 類別內每個不以 `_` 開頭的 callable 即一個工具(`utils/tools.py:823-832`)。
- 兩段式管線:`get_type_hints` + `inspect.signature` 取型別/預設值,自訂 regex parser `parse_docstring`(以 `:param x:` 抓,`:689-741`)取描述 → `pydantic.create_model` 動態組 model(`:744-780`)→ langchain `convert_to_openai_function` 轉 OpenAI spec → `clean_openai_tool_schema` 清掉 `anyOf`/`null` 提升跨 provider 相容(`:786-820`)。**不是直接用 Pydantic 的 `model_json_schema()`。**
- 特殊參數注入:`get_async_tool_function_and_apply_extra_params`(`:179-237`)只綁定該函式**有宣告**的 dunder(`__user__ __event_emitter__ __event_call__ __request__ __id__ __metadata__ __files__ __model__ __messages__ __oauth_token__`),用 `functools.partial` 綁定後**重寫 `inspect.Signature` 讓這些參數從對外簽章消失**——避免 provider SDK 用簽章反推 schema 時把內部參數當工具參數。一律包成 async 對外(`:220-229`)。
- **洞察**:降低作者學習成本(寫法接近普通 Python 函式),代價是 docstring 格式成為隱性契約——寫錯只是描述消失、不報錯,容錯高但除錯線索少。

#### D-2 四種 Function 型別
共用入口 `load_function_module_by_id`(`utils/plugin.py:253`),依模組內定義的 class 名稱(`Pipe`/`Filter`/`Action`/`Event`)決定 `function.type`(`:286-295`)。

| 型別 | 掛載點 | 執行時機 | 關鍵細節 |
|---|---|---|---|
| **Pipe** | `functions.py:71` `get_function_models()` | 被選為模型時 → `generate_function_chat_completion`(`:147`) | 有 `pipes` 屬性就展開成 `{pipe.id}.{p.id}` 的 manifold 多模型(`:83-121`);回傳可為 str/Generator/AsyncGenerator/BaseModel/StreamingResponse |
| **Filter** | `utils/filter.py:66` `process_filter_functions` | `inlet` → `middleware.py:2428`;`stream` → 逐 chunk `:4035,5332,5344`;`outlet` → `:3377` | **priority 是一個 valve 欄位**,不是 DB 欄位;`filter_ids.sort(key=(priority, fid))` 升冪(`filter.py:59`)。0.10.0 起 outlet 也對直接 API 呼叫生效(預設開) |
| **Action** | `utils/models.py:225-355` 寫進 `model['actions']` | 使用者點訊息下方按鈕 → `POST /api/chat/actions/{id}` → `utils/actions.py:19` | 可有 `actions` list 一檔多按鈕;可回傳新 `data` 直接覆寫 `history.messages` |
| **Event** | `events.py:1084` `EVENT_SINKS` → `dispatch_event_functions`(`:1027`) | 任何 `publish_event()`(`:1087`) | **0.10.0 新增,第一個掛進「系統本身」而非對話的原語** |

- **Event 是廣播式、沒有訂閱過濾**:載入所有 active event function,每個都呼叫並帶 `__event_name__`,篩選要在 handler 內自己做。事件目錄 `EventDefinitions`(`events.py:42-97+`)涵蓋 `system.startup.* config.* auth.* user.* chat.* function.*` 等數十種。0.10.0 CHANGELOG 描述其價值:「from onboarding and access control to auditing, lifecycle automation, and external integrations」。
- ⚠ **兩套 filter 目前並存疊加**,不是互斥:Pipelines 的 inlet/outlet(`middleware.py:2420, 3360`)與 Function filter 一起跑。

#### D-3 Valves vs UserValves(高價值模式)
| | Valves(admin) | UserValves(每使用者) |
|---|---|---|
| 宣告 | 類別內巢狀 Pydantic `BaseModel` | 同上,類名 `UserValves` |
| 儲存 | `Tool.valves` / `Function.valves` JSON 欄位,全站共用一份(`models/tools.py:30`、`models/functions.py:28`) | **借用 `User.settings` 的巢狀路徑** `settings['tools'\|'functions']['valves'][id]`(`models/tools.py:254-293`) |
| 執行時 | `module.valves = module.Valves(**v)`,模組物件層級 | 實例化後塞進 `__user__['valves']`,只影響本次呼叫 |
- **不是欄位級合併**:模組讀 `self.valves.x`,函式讀 `__user__['valves'].x`,由作者自己決定何時用哪份(`utils/tools.py:298-304`、`utils/filter.py:107-116`、`functions.py:196-202`)。
- **UI 完全由 schema 自動生成**:後端提供 `.../valves/spec` 端點回傳 `ValvesClass.schema()`,並用 `resolve_valves_schema_options`(`utils/valves.py:26-147`)解析動態選單(`options` 可指向一個接收 `__user__` 的 classmethod);前端 `common/Valves.svelte:69-201` 依 `type`/`enum`/`input.type` 動態切換 Switch / SensitiveInput(password)/ NativeSelect / color picker / **MapSelector(地圖選點,`common/Valves/MapSelector.svelte`)** / textarea。**新增一個 valve 欄位不需要改任何前端程式碼。** 0.10.0 另加了 markdown 描述與 dropdown options。
- 加密:`ENABLE_VALVE_ENCRYPTION` 開啟時用衍生自 `WEBUI_SECRET_KEY` 的 Fernet 對**整包** valves 加密落地,解密失敗容錯回 `{}`(`utils/valves.py:21-41`)。⚠ 整包而非欄位級,schema 變更時可能靜默丟資料。

#### D-4 載入與失效
`utils/plugin.py:205/253`:`replace_imports` 重寫舊 import 路徑並寫回 DB(相容舊外掛,`:186`)→ 解析 frontmatter(以 `"""` 開頭、`^\s*([a-z_]+):\s*(.*)$` 抓 `title/author/version/requirements`,`:150-183`)→ `requirements` 用 `subprocess` pip install(丟 `asyncio.to_thread`,全域 `_installed_requirements` 去重,可由 `ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS`/`OFFLINE_MODE` 關閉,`:411`)→ `types.ModuleType(f'tool_{id}')` 登記進 `sys.modules` → 寫臨時檔只為讓 `__file__` 有合理值 → `exec(content, module.__dict__)`。Function 載入失敗會自動把 `is_active` 設回 False(`:301`)。
- **失效兩層**:create/update 端點主動重載並覆寫 `app.state.TOOLS`/`FUNCTIONS`(`routers/functions.py:209-217`);被動則每次查 DB 拿最新 `content` 與快取的**原始碼字串逐字元比對**,不同才重 exec(`:329-364`)。串流階段的 filter 刻意傳 `load_from_db=False` 走純快取(每 chunk 查 DB 太貴)。
- ⚠ 快取在 `app.state`,行程內單例,多 worker 各自一份。

#### D-5 Pipelines(legacy)與 D-6 OpenAPI / MCP
- **Pipelines** 是 HTTP 反向代理協定,複用 OpenAI 連線機制:外部 server 的 `/models` 回傳含 `pipeline: {type, priority, pipelines}` 的模型物件,filter 則 `POST {base}/{id}/filter/inlet|outlet` 交換 `{user, body}`(`routers/pipelines.py:41-126`)。**Functions 是它的原地取代品**——同一套 inlet/outlet 概念,從「HTTP 呼叫外部進程」改為「in-process exec」(`CHANGELOG.md:4530-4535` vs `:4332`)。Function 的 valves API 幾乎照抄 Pipelines 的介面(`pipelines.py:198-565`)。
- **OpenAPI → 工具**:`convert_openapi_to_tool_payload`(`utils/tools.py:897`)逐一走訪 paths,`operationId` 當工具名,合併 path-level 與 operation-level parameters(以 `(name, in)` 去重),`requestBody` 的 schema 用遞迴 `resolve_schema` 沿 `$ref` 展開並用 set 防循環(`:851`);`enum` 值會附加進 description 文字。spec 併發抓取後快取進 `app.state.TOOL_SERVERS` 並鏡射一份到 Redis 供多 worker 共享(`:996, 1315`)。
- **auth 模式**(`utils/tools.py:115-174`,MCP 共用):`bearer` / `session`(轉送使用者 session)/ `system_oauth` / `oauth_2.1` / `oauth_2.1_static`。
- **direct 模式**很特別:使用者權限 `features.direct_tool_servers` 開啟時,驗證由**瀏覽器直接對 tool server 發請求**(`AddToolServerModal.svelte:157-168`),執行階段則透過 `__event_call__({type:'execute:tool'})` 讓瀏覽器代為執行(`middleware.py:1192-1204, 4720-4732`)——伺服器只轉發。**這讓「使用者自己網路環境才能到達的工具」變可能。**
- **MCP**:只支援 **Streamable HTTP**(`utils/mcp/client.py:10,67`),`inputSchema` 本身即 JSON Schema 免轉換(`:88`);`outputSchema` 讀出但標 TODO 未用;**Resources API 存在但聊天流程未串接,Prompts 完全未支援**(推測)。生命週期是**每輪對話建新連線、回合結束才斷**(`middleware.py:2112, 2735` → `main.py:1567-1570` 以 reversed LIFO 順序 disconnect),註解明載不可用 `asyncio.shield`,因 MCP SDK 的 TaskGroup 必須在同一 asyncio task 內 LIFO 關閉(`client.py:149-181`)。

#### D-7 Skills:漸進式揭露(Claude Skills 同款)
- Skill **不是程式碼**,是純文字/Markdown 內容 + name/description/tags(`models/skills.py:27`),`routers/skills.py` 只有 CRUD + toggle + access grant,完全不碰 `utils/plugin.py`。
- **兩段式載入**(`utils/middleware.py:2509-2557`):
  1. 被 `<$skillId|label>` **明確 @mention** 的(regex `SKILL_MENTION_RE`,`:2075`),或當前不支援 builtin tools 時 → **整份 content 直接塞 system message**(`<skill name=...>`)。
  2. 其餘「選取但未提及」的 → 只把 `id/name/description` 組成精簡 `<available_skills>` manifest,並註冊一個 `view_skill(id)` builtin 工具(`utils/tools.py:636-637`、`tools/builtin.py:2817` 內含存取檢查)→ **模型看了 manifest 覺得需要才自己拉全文**。
  3. `strip_skill_mentions`(`:2097`)最後把標記換成純文字 label,不讓模型看到內部語法。
- 三種來源可疊:模型 `meta.skillIds`、請求 `skill_ids`、訊息內 @mention。
- **與 Tool / Prompt 的差別**:Tool 有參數 schema 與回傳值;Prompt 是使用者主動用 `/` 插進自己輸入的模板;Skill 是**可由模型自主延遲載入、且有群組層級 ACL 與啟用開關的「行為指令/知識」**。

#### D-8 Event Emitter 協定(完整型別表)
`get_event_emitter`(單向)/ `get_event_call`(雙向)皆走 socket.io(`socket/main.py:919 / 1039`),前端統一入口 `chatEventHandler`(`Chat.svelte:610`):

| type | UI 行為 | 落地 DB |
|---|---|---|
| `status` | 推入 `statusHistory` 顯示階段狀態列 | ✔ |
| `message` / `chat:message:delta` | 累加 `content` | ✔ |
| `replace` / `chat:message` | 整段覆寫 | ✔ |
| `files` / `chat:message:files` | 設定附加檔案 | ✔ |
| `embeds` / `chat:message:embeds` | Rich UI(HTML iframe)並自動捲動 | ✔ |
| `source` / `citation` | 附加引用;`data.type==='code_execution'` 走程式碼結果卡片 | ✔ |
| `notification` | toast(success/error/warning/info) | ✘ |
| **`confirmation`** | 彈確認框,選擇經 callback **回傳後端** | ✘ |
| **`input`** | 同上但帶輸入框(支援 `input.type`/`options`)→ **讓後端工具「問使用者一個問題並等回覆」** | ✘ |
| **`execute`** | 後端要求前端 `new Function(...)` 執行 JS 並回傳結果 | ✘ |
| `chat:completion` `chat:tasks:cancel` `chat:active` `chat:title` `chat:tags` `chat:message:tasks` `chat:message:error` `chat:message:follow_ups` `chat:message:favorite` `chat:outlet` `context_compaction` `terminal:*` | 各種狀態同步 | 視類型 |
只有 `confirmation`/`input`/`execute` 是 `sio.call(...)` 的雙向語意(有 `WEBSOCKET_EVENT_CALLER_TIMEOUT`,`socket/main.py:1050`)。
- ⚠ **洞察**:這組型別不是設計出來的枚舉,是逐版累加的「字串 + 向下相容別名」,前端用長串 if/else 比對。重建時應該一開始就用集中式事件表,每列帶「是否落地 DB」「是否需雙向 ack」兩個布林。

---

### E. 設定的資訊架構

#### E-1 config 機制:v0.10.2 已重寫(最需要更新認知的一節)
- **舊的 `PersistentConfig` / `AppConfig` / `app.state.config` 在此版本搜尋命中為零**(我親自驗證);取代者是 `models/config.py:97-104` 的 `Config` 表:`key TEXT PK` / `value JSON` / `updated_at BIGINT`,**每個設定鍵獨立一列,無版本欄位、無 revision history**。migration 為 `3ff2c63645b8_reshape_config_to_per_key_rows.py`(檔內 `BLOB_PATH_TO_KEY` 字典可當「舊 blob path → 新 key」對照表)。
- **沒有記憶體常駐設定物件**:`Config.get()/get_many()/get_namespace()/get_all()/upsert()` 每次都即時查/寫 DB(`models/config.py:136-234`)。
- **三層優先序的真相**:
  - 冷啟動:`DEFAULT_CONFIG`(所有 env 值,dotted key,`config.py:2745-2929`)由 `seed_registered_defaults()`(`main.py:317`)灌入 DB。此時 env 贏——**但其實是「灌入」成為 DB 初值**。
  - 之後每次啟動:`seed_defaults` **只對 DB 裡不存在的 key 寫入**(`models/config.py:249-250`),已存在的列完全不動。
  - ⚠ **結論(與多數人的印象相反)**:**一旦某個 key 被寫進 DB(含 UI 編輯過的),之後改 env var 永遠不再生效。DB 永遠贏。**
- 兩個逃生閥:`ENABLE_PERSISTENT_CONFIG=False`(預設 True)讓 `Config.get()` 一律回傳 env 值、完全略過 DB(`models/config.py:128-134`、`config.py:3130`)——**這是 GitOps/IaC 場景的正解**;`oauth.*` 命名空間再多一層 `ENABLE_OAUTH_PERSISTENT_CONFIG`(預設 **False**),否則 OAuth secret 永遠只讀 env。
- **沒有 cross-worker cache invalidation 問題,因為壓根沒有 cache**;`config.py:15` 的 `import redis` 是死 import。真正用 Redis 的是衍生資料:`socket/main.py:110` 的 `MODELS = RedisDict(...)` vs `:152` 的 plain dict。
- 遺留自我修復:少數巢狀 dict 設定被 per-key 拆得過細,`repair_flattened_dict_configs()`(`models/config.py:294-360` + `DICT_CONFIG_KEY_ALIASES` `:24-37`)在啟動時重組回 dict。
- **純 env-only(共 202 個 `os.getenv`,多數不進 `DEFAULT_CONFIG`)**:`DATABASE_URL`(`env.py:261`)、`WEBUI_SECRET_KEY`(`:679`)、`WEBUI_AUTH`(`:668`)、`REDIS_URL`/`REDIS_CLUSTER`/`REDIS_SENTINEL_*`(`:368-413`)、`WEBSOCKET_MANAGER`(`:441`)、`DATA_DIR`、`ENABLE_DB_MIGRATIONS`、`RESET_CONFIG_ON_START`(`:359`)、各 `AIOHTTP_POOL_*`。推測理由:雞生蛋(`DATABASE_URL` 不可能存在它要連的 DB 裡)、行程級一次性初始化、以及避免根本性開關能被 UI 竄改。

#### E-2 使用者設定分頁
`chat/SettingsModal.svelte:52+`,可見性由 `getAvailableSettings`(`:478-497`)動態決定:

| tab | 可見條件 | 內容重點 |
|---|---|---|
| General | 一律 | 語言、主題、System Prompt、通知、Advanced Params 折疊區 |
| Interface | admin 或 `settings.interface` | 排版、氣泡、自動標題/追問、圖片壓縮 |
| Connections | `features.enable_direct_connections` | 使用者自帶 API endpoint |
| Integrations | admin 或 `features.direct_tool_servers` | Tool Server / Open Terminal |
| Personalization | `features.enable_memories` + 權限 | Memory、自訂人設 |
| Audio | 一律 | STT/TTS 引擎、語速、語音 |
| Data Controls | 一律 | 封存/刪除/匯出匯入全部對話、檔案管理 |
| Account | 一律 | 密碼、個人資料、API Key、JWT、Webhook URL |
| About | 一律 | 版本、檢查更新 |

- **儲存**:`saveSettings()` 同時更新前端 store 與 `updateUserSettings(token, {ui: $settings})`(`:544-548`)。**載入以後端為主**:先 `getUserSettings(token)`,只有失敗才 fallback 讀 `localStorage.settings`(`src/routes/(app)/+layout.svelte:88-104`)——**localStorage 是離線容錯,不是主要持久層**。
- 資料模型:`UserSettings{ui: dict}` 且 `ConfigDict(extra='allow')`(`models/users.py:40-43`),存在 `User.settings` 單一 JSON 欄位(`:72`)。
- **洞察**:「使用者可自訂但對系統無風險」的東西走寬鬆 schema JSON(換前端迭代速度);「後端要拿去做授權判斷」的東西(permissions)走嚴格 Pydantic + 專用欄位。這個分工很清楚,值得照抄。

#### E-3 管理員設定分頁
路由白名單在 `admin/Settings.svelte:36-52`:`general / authentication / connections / models / evaluations / integrations / documents / web / code-execution / interface / audio / images / pipelines / db`。另有獨立的 `/admin/users`(`overview` + `groups`)、`/admin/analytics`、`/admin/evaluations`、`/admin/functions`。
- 值得注意的項目:General 內嵌 Webhook 事件設定(`Events.svelte`,`Settings.svelte:390`);Integrations 內嵌 `ExternalKnowledge.svelte`;Pipelines 頁面 UI 直接寫警語「Pipelines are a plugin system with arbitrary code execution」;Database 頁提供 **Config Import/Export**、Export Users(CSV)、Export All Chats、Download Database。
- 後端骨幹 `routers/configs.py`:`get_config_values()`/`config_updates()`(`:72-78`)把「REST 表單欄位名」映射到 dotted config key(如 `CONNECTIONS_CONFIG_KEYS` `:42-45`、`MODELS_CONFIG_KEYS` `:63-69`),每個 POST 存檔後 `publish_event(EVENTS.CONFIG_XXX_UPDATED)`(`:143-150`)。
- ⚠ **洞察**:新增一個設定項要同時改四處(env 定義 → `DEFAULT_CONFIG` → `*_CONFIG_KEYS` 映射 → 前端表單),**無自動生成**。React 重建應該用一份 schema 同時驅動後端驗證與前端表單渲染。

#### E-4 權限樹(完整鍵層級)
定義在 `config.py:1898-1976` 的 `DEFAULT_USER_PERMISSIONS`,每個葉節點對應一個 `USER_PERMISSIONS_*` env(如 `:1858`),六大分支:
- `workspace.*`:models / knowledge / prompts / tools / skills,各含 `_import`/`_export`
- `sharing.*`:models / public_models / knowledge / public_knowledge / prompts / tools / skills / notes / public_notes / folders / public_chats / public_calendars
- `access_grants.allow_users`:能否分享給「特定使用者」(而非只能群組/公開)
- `chat.*`:controls / valves / system_prompt / params / file_upload / web_upload / delete / delete_message / continue_response / regenerate_response / rate_response / edit / share / export / import / stt / tts / call / multiple_models / temporary / temporary_enforced
- `features.*`:api_keys / notes / folders / channels / direct_tool_servers / web_search / image_generation / code_interpreter / memories / automations / calendar / webhooks
- `settings.interface`
- 系統預設存 `config` 表的 `user.permissions` key(`routers/users.py:262-286` 讀寫);群組覆寫存 `Group.permissions` 稀疏 JSON(`models/groups.py:49`)。合併是 **deep-copy 預設 → 逐群組 `permissions[key] = permissions[key] or value`**(`utils/access_control/__init__.py:41-67`),`fill_missing_permissions` 補齊結構。登入時算一次寫進回應(`routers/auths.py:185,267`)。
- **沒有 per-user 覆寫**——個人差異只能靠加入/移出群組。
- 型別以 Pydantic model 定義(`routers/users.py:170-256`),同時驅動請求驗證與 OpenAPI schema。

#### E-5 立即生效 vs 需重啟
| 類別 | 需重啟? | 依據 |
|---|---|---|
| 一般設定(Connections/Models default/Banners/Code Execution…) | **不需要**,下個 request 查 DB 就拿到 | `routers/configs.py` 各 POST 存完即回;全庫找不到「please restart」字串 |
| Base Model List 快取 | 需**手動刷新** | 前端存 Connections 後主動帶 `refresh=true`(`admin/Settings/Models.svelte:190`) |
| OAuth Provider 註冊 | **需重啟** | `load_oauth_providers()` 在模組層級只執行一次(`config.py:2705`),之後存 `oauth.*` 不會重呼叫 |
| 純 env-only 基礎設施 | **需重啟** | 只在 `env.py` import 時讀一次 |
| 跨 worker `MODELS` 查找表 | 有 `REDIS_URL` 才自動同步 | `socket/main.py:110` vs `:152` |

#### E-6 漸進式揭露:十種具體手法(全部有元件依據)
1. **搜尋設定**:每個分頁預綁英文關鍵字陣列,100ms debounce 過濾分頁清單並跳到第一個吻合(`chat/SettingsModal.svelte:52-472, 501-524`;Admin 版同構於 `admin/Settings.svelte:74+`)。⚠ **兩份幾乎相同的獨立實作**。
2. **折疊 Advanced**:一般使用者預設看不到約 30 個模型參數,按 Show/Hide 才展開,帶 `aria-expanded`(`chat/Settings/General.svelte:25, 322-330`)。
3. **Default / Custom 三態**:每個參數不是開/關,而是「跟隨預設」vs「自訂」,Custom 才展開輸入框——避免使用者以為空白 = 0(`AdvancedParams.svelte`,同 pattern 重複三十餘次)。
4. **權限樹條件式揭露**:父權限(`workspace.tools`)開啟才顯示子權限(import/export)(`Groups/Permissions.svelte`)。
5. **用文案彌補模型限制**:群組編輯畫面對已被預設鎖開的權限顯示灰字「This is a default user permission and will remain enabled」,而非給一個按了無效的開關(`Permissions.svelte:191-195` 附近)。**這是全報告我最喜歡的一個 UX 決定。**
6. **Tabs-in-Tabs**:`/admin/users` 的 overview/groups → 群組 Edit Modal 內再有 General/Users/Permissions(`EditGroupModal.svelte`);`ModelSettingsModal.svelte:41,197-373` 的 defaults/display。
7. **Drill-down Modal 取代就地展開**:連線清單只顯示摘要一行,點擊才開完整表單(`admin/Settings/Connections.svelte:22,27,99`)。
8. **逐項 Info Tooltip,密度與複雜度正相關**:`Documents.svelte` 用了 61 次、`Interface.svelte` 27 次、`Images.svelte` 23 次。
9. **Danger Zone 視覺隔離**:`Documents.svelte` 把「Bypass Embedding」等破壞性選項獨立分區。
10. **權限驅動的整頁不渲染**(而非灰掉):非管理員根本看不到 connections/tools/interface/personalization 分頁(`SettingsModal.svelte:478-497`)。
- ⚠ 這十種 pattern 是各 feature 各自手刻的,沒有共用元件。重建時應抽成 `<SettingsSearch>` `<DefaultOrCustomField>` `<PermissionRow requiresParent>` `<CollapsibleAdvanced>`。

#### E-7 Config Import/Export
- **舊 `config.json` bootstrap**:`import_legacy_config_json()`(`config.py:82-88`)啟動時若 `{DATA_DIR}/config.json` 存在就整包 upsert 並改名成 `old_config.json`;在 `main.py:316` 呼叫,**刻意早於** `seed_registered_defaults()`(`:317`)——先讓舊設定進 DB,再用 env 補「DB 裡沒有的」,確保升級不被 env 覆蓋。
- Export = `Config.get_all()` 整張表倒成 dotted-key JSON(`routers/configs.py:110-112`);Import = 整包 upsert(`:92-102`),語意是**部分覆寫**(檔案裡沒列的 key 維持原值),但**沒有 diff 預覽也沒有回滾**。
- `RESET_CONFIG_ON_START`(env-only)為 True 時啟動先 `Config.clear()` 整表 DELETE 再重灌(`config.py:46-47`、`models/config.py:229-234`)——容器化/一次性環境用(推測)。

---

### F. 組織與搜尋

#### F-1 Folder = Project
- `Folder` 表:`id, parent_id, user_id, name, items, meta, data, is_expanded`(`models/folders.py:22-33`)。**沒有獨立的 system_prompt / model 欄位**——都塞進 `data` JSON,實際只用 `data.system_prompt` 與 `data.files`(型別為 `file`/`collection`);**沒有 per-folder 預設模型**(推測尚未支援)。巢狀靠 `parent_id`,前後端皆遞迴且**無深度上限**(`RecursiveFolder.svelte:690` 用 `<svelte:self>`;`models/folders.py:193-214, 251-268`)。
- **completion-time 注入路徑**(`utils/middleware.py:2342-2371`):① 先用**輕量欄位查詢** `get_chat_folder_id`(`models/chats.py:1289-1300`)只取 folder_id,不載入整個 chat JSON(註解明講是效能考量);② 有 `data.system_prompt` 就 `apply_system_prompt_to_body`;③ 有 `data.files` 先用 `get_accessible_folder_files`(`utils/access_control/files.py:101-133`)逐一過濾權限,再依 function_calling 模式決定進 `form_data['files']`(legacy)或 `metadata['folder_knowledge']`(native);④ 暫時對話退回 `metadata.folder_id`。
- 0.10.0 新增**資料夾共享**:可分享給使用者/群組/所有人、read 或 write,非擁有者以唯讀檢視開啟,並有一個**預設關閉**的「Folders Sharing」管理員權限(`CHANGELOG.md` 0.10.0 首條)。拖進共享資料夾的子資料夾會自動歸屬該資料夾擁有者以維持樹一致(`routers/folders.py:122-148`)。
- 拖放與匯入共用同一入口:`dataTransfer` 的 `text/plain` JSON `{type, id, item}`,`folder`→改 parent、`chat`→改 folder_id、外部拖進來的 JSON 檔→`importChats`(`RecursiveFolder.svelte:132-208`)。0.10.0 另加「從側欄把資料夾/筆記/模型拖進聊天輸入框」。

#### F-2 Tags / Archive / Pin
- `Tag` 表複合主鍵 `(id, user_id)`,`id = name.replace(' ','_').lower()`(`models/tags.py:18-31, 61`)——**tag_id 是正規化後的名稱,天然去重**;但 chat↔tag **不是 join table**,而是存在 `Chat.meta['tags']` JSON 陣列(`models/chats.py:509-530`)。查詢用 dialect 分支:SQLite `json_each`,PostgreSQL `json_array_elements_text`(`:1495-1569`)。移除時 `delete_orphan_tags_for_user` 清孤兒(`:1790-1813`)。
- ⚠ **AI 自動標籤存在但用途不同**:`/tasks/tags/completions` 產出的 tags 寫進 `message.annotation.tags`,**只在使用者按讚/倒讚時觸發**(`ResponseMessage.svelte:537-554`),是「這則回應好在哪個面向」的評分標籤;側欄分類用的 `chat.meta.tags` 是使用者手動編的。**兩套不同機制,別搞混。**
- `archived` / `pinned` 是 Chat 表布林欄位並各自建索引(`models/chats.py:56-57, 70-71`)。一般清單預設 `archived=False`;pinned 有專屬查詢(`:1357-1364`);**資料夾內清單一律排除 pinned 與 archived**——pinned 對話從資料夾清單「浮出」到置頂區(`:1592-1666`,推測)。搜尋預設排除封存,但可用 `archived:true` 主動搜(`:1428-1432, 1457-1458`)。

#### F-3 Cmd+K:實際支援的 filter 前綴(我原本的假設被推翻)
- 元件 `layout/SearchModal.svelte`,keybinding `Shortcut.SEARCH: ['mod','K']`(`src/lib/shortcuts.ts:78-82`),觸發於 `src/routes/(app)/+layout.svelte:249-252`。輸入框元件與側欄搜尋框共用,**同一套前綴引擎**。
- **搜尋範圍只有「對話」**(title + 訊息內容 + snippet)加兩個固定 Actions(開新對話、建新筆記)(`SearchModal.svelte:256-266, 508-525`)。**不搜 models / prompts / knowledge**——那些是各自獨立的選擇器。debounce 500ms(`:391-399`)。
- **完整前綴清單**(前端 `Sidebar/SearchInput.svelte:25-46` 與 `SearchModal.svelte:285` 各宣告一份;後端解析 `models/chats.py:1410-1450`):
  - `tag:<名稱>`(特殊值 `tag:none` = 未標籤)
  - `folder:<名稱>`(含子資料夾,後端展開子孫 folder_id)
  - `pinned:true|false`
  - `archived:true|false`
  - `shared:true|false`(依 `share_id` 是否為 NULL)
  - **就這五個。沒有 `model:` / `before:` / `after:` / `is:`。**
- 後端**不是全文檢索**:`Chat.title.ilike('%text%')` + 對 JSON 訊息陣列做 `json_each`(SQLite)/ `json_array_elements`(PG)子查詢配 `LIKE '%text%'`(`models/chats.py:1476-1571`),SQL 層 offset/limit 預設 60。
- ⚠ 洞察:前綴解析前後端各自實作一份(`word.startswith('tag:')` 之類),隱性耦合;`ILIKE %…%` 掃 JSON 沒用到資料庫原生全文檢索能力,是明確可改善點(改 `tsvector` + GIN)。Cmd+K 兼任導航面板但動作清單寫死兩三項,不是可擴充的 command registry。

#### F-4 匯入匯出
- **匯出全在前端本地產生**:JSON(全部/單一)、TXT(攤平成 `### ROLE\ncontent`)、**PDF 用 jsPDF + html2canvas-pro 把渲染好的 DOM 截圖分頁貼進去**(`ChatMenu.svelte:61-249`)——無伺服器端 PDF 服務。**沒有 Markdown 匯出。**
- **格式偵測是啟發式**:陣列中任一項含 `mapping` key 就判定 ChatGPT 格式(`src/lib/utils/index.ts:756-764`)。**沒有 version 欄位或 schema 版本號。**
- **ChatGPT 轉換是有損的**(`index.ts:789-941`):ChatGPT 的 node-graph 帶 `parent`/`children` 支援多分支,但轉換**不依原始 parent 重建樹**,而是用 `for (const message_id in mapping)` 的**物件鍵序**依序走訪,強制把每則的 `parentId` 設為「上一個被加入的訊息」(`:836-853`)——**分支結構被壓平成單一鏈**。`role === 'system'` 與 `'tool'` 節點整個跳過(`:826-829`),圖片/DALL-E 等非字串 part 丟棄(`:789-802`)。失敗只 `console.log`,不擋其餘對話。
- 後端匯入一律重發 `uuid.uuid4()`,**無任何去重**——同一份檔案匯入兩次就有兩份(`models/chats.py:393, 409-456`)。0.10.0 新增「Allow Chat Import」權限。
- ⚠ 對 ANILA 的意義:壓平分支換來的是「可以直接複用既有訊息鏈 UI」,但使用者在 ChatGPT 端的重新生成歷史會消失。若 ANILA 要做匯入,**自家格式一定要加顯式 `version` 欄位**。

#### F-5 集中檔案管理與清單效能
- `File` 表:`id, user_id, hash, filename, path, data, meta`(`models/files.py:18-31`)。**file→chat 的關聯是隱性的**(chat JSON blob 內引用 id,非外鍵);**file→knowledge 有明確 join table** `KnowledgeFile`(`models/knowledge.py:97-116`)。
- 儲存後端 4 種:Local / S3 / GCS / Azure(`storage/provider.py:40-272`)。
- ⚠ **有算 hash 但沒去重**:上傳時算 `sha256` 存進 `meta.file_hash`,程式碼註解明講是「for incremental sync diffing」(`routers/files.py:363-367`)——**上傳前不查有無相同 hash**,同檔上傳 N 次就有 N 份實體。
- ⚠ **沒有孤兒清理**:刪除聊天完全不觸碰 `File` 表(`routers/chats.py` 全文無 `Files.` 呼叫),也沒有排程清理。只有手動刪單一檔案才會連帶清 KB 關聯、向量與實體(`routers/files.py:970-1024`)。
- 清單效能做得對:所有清單端點的 `response_model` 都是 `ChatTitleIdResponse`,**只有 `id, title, updated_at, created_at, last_read_at, snippet`**,SQL 也只 SELECT 這幾欄(`routers/chats.py:131-132`、`models/chats.py:177-183, 1027-1029`);完整 blob 只在單一對話詳情才回。
- ⚠ 但前端**沒有虛擬滾動**:`Loader.svelte:10-33` 用 IntersectionObserver 當哨兵做無限捲動,已載入項目全部留在 DOM,節點數線性成長。

---

### G. 協作與治理

#### G-1 統一 AccessGrant 表(全報告最優雅的資料模型)
- 表 `access_grant`:`id, resource_type, resource_id, principal_type(user|group), principal_id, permission(read|write), created_at`(`utils/access_control/__init__.py:20-40`、migration `f1e2d3c4b5a6_add_access_grant_table.py:33-60`)。
- 唯一約束 `uq_access_grant_grant` 涵蓋五欄(天然去重、天然支援多筆授權);**兩個索引正好對應兩個查詢方向**:`idx_access_grant_resource(resource_type, resource_id)` 答「這個資源誰能看」、`idx_access_grant_principal(principal_type, principal_id)` 答「這個人能看什麼」。
- **public 用萬用字元 principal 表示**(`principal_type='user', principal_id='*'`),省掉一個 `is_public` 欄位,也讓 public 與特定 user 共用同一組 `or_()` 條件。
- **乾淨遷移,不是雙軌並存**:`models/models.py`、`knowledge.py`、`prompts.py`、`tools.py` 內 grep `access_control` **已完全零命中**;migration 三段式:建表建索引 → 逐表把舊 JSON 依語意回填(`None`→public read,但 file 的 `None` 是 private;`{}`→private;結構化 dict→展開多筆)→ `batch_alter_table` **實際 DROP 欄位**(`:62-223`),且 `downgrade()` 可反向重建(`:226-348`)。應用層仍留 `access_control_to_grants` / `grants_to_access_control`(`access_control/__init__.py:78-142, 232-274`)但**只為 API 向後相容輸出**。
- 各資源 Pydantic model 的 `access_grants` 是**讀取時才組裝的虛擬欄位**,ORM class 本身沒這個 column(`models/models.py:100` vs `:75-91`)。
- **兩種查詢模式,都不是 N+1**:
  - 單一 JOIN(主要列表):`has_permission_filter()` 把 owner 條件與一個 `EXISTS(SELECT ... FROM access_grant ...)` 相關子查詢用 `or_()` 組合,直接掛在原查詢的 `.filter()` 上,一次搞定分頁+過濾(`access_control/__init__.py:658-765`;`models/knowledge.py:302,366,404` 三處在用)。另有 `_has_read_only_permission_filter`(`:767-877`)專篩「有讀無寫」。
  - 批次 IN(已知候選 id 時):`get_accessible_resource_ids`(`:557-611`),函式註解自己寫「This replaces calling has_access() in a loop (N+1) with a single query」。
- **admin bypass 是命令式的,不是資料驅動的**:沒有在表裡塞「admin 萬用授權」,而是每個檢查點顯式判斷角色(`check_model_access` 的 `if user.role != 'admin'`,`:325-343`);連線類資源另有 `BYPASS_ADMIN_ACCESS_CONTROL` 旗標(`config.py:2029`)。刻意把 admin 語意留在程式碼而非資料,換「一眼看出這是管理端點」的可讀性。

#### G-2 Groups 與加法制權限
- `Group`(id, user_id=建立者, name, description, data, meta, permissions JSON) + `GroupMember` 多對多,**無角色欄位**(成員身份二元,沒有「群組管理員」層級,推測)(`models/groups.py:37-92`)。`Group.data.config.share` 字串旗標(`true`/`members`/`false`)控制誰能把這個群組當分享對象(`:135-143, 200-226`)。
- 合併純 OR(見 E-4),**沒有任何減法路徑**。**整個系統貫穿「聯集,無交集無差集」的哲學**:好處是加群組只會變寬鬆,不會出現兩群組打架導致意外變嚴;代價是無法表達「群組 A 覆寫群組 B 使其更嚴格」。
- Provisioning 三條路:手動;**OAuth claim 對應**(`utils/oauth.py:1493-1603`,`OAUTH_GROUPS_CLAIM` + `OAUTH_GROUPS_SEPARATOR`,`ENABLE_OAUTH_GROUP_CREATION` 自動建群並套 `OAUTH_GROUP_DEFAULT_SHARE`,`ENABLE_OAUTH_GROUP_MANAGEMENT` 每次登入做**差異同步**——不在 claim 裡的群組會被移除,另有 `OAUTH_BLOCKED_GROUPS` 黑名單);**SCIM v2**(`routers/scim.py`,1136 行,`/Users` `/Groups` 端點 + `group_to_scim`/`user_to_scim` 轉換,`:330-400`)。OAuth 是 pull-on-login、SCIM 是 push,**互補而非取代**(推測)。

#### G-3 Preview Access:同一個引擎的「假設性稽核」
- 群組視角 `GET /groups/id/{id}/preview`(`routers/groups.py:338-402`):撈出全部 active models / knowledge / tools,再用 `get_accessible_resource_ids(user_id='', user_group_ids={group.id})`——**刻意把 user_id 傳空字串**,等於「假裝我是一個只屬於這個群組、沒有任何個人授權的虛擬使用者」。
- 使用者視角 `GET /users/{user_id}/preview`(`routers/users.py:780-845`)同構,先取該使用者所屬全部群組再跑三次,回應附上 `groups` 清單。
- **洞察**:Preview **沒有另建一套權限計算邏輯**,重用同一個原語。這代表授權引擎是**無狀態純函式**——輸入 `(user_id, group_ids, resource_ids, permission)` 即可求值,天然可被拿去做假設性稽核。**這是統一 ACL 表帶來的直接紅利,也是 ANILA 最該學的一點。**

#### G-4 Chat 分享:快照與授權正交
- `SharedChat` 表:`id`(= share token,即 `/s/{id}` 路徑)、`chat_id`(FK cascade)、`user_id`、`title`、**`chat` JSON 快照**(`models/shared_chats.py:18-43`)。
- **是複本不是指標**:分享當下複製整段對話 JSON;之後原對話繼續也不會自動更新,除非再按分享觸發 re-snapshot(`:60-112`;`routers/chats.py:1705-1716`)。
- **兩層閘門**:`GET /chats/share/{share_id}` 要求已登入且非 pending(`routers/chats.py:1046-1079`),分享者本人一定可看,否則必須在 `access_grant` 對 `resource_type='shared_chat'` 有 read。**建立分享時預設不插入任何授權**——光有連結打不開,必須另外呼叫 `POST /chats/shared/{id}/access/update`(`:1774-1802`),且該更新會經 `filter_allowed_access_grants(..., 'sharing.public_chats')` 檢查呼叫者權限。
- **洞察**:「內容快照」與「誰能開」是兩個獨立關注點,同一把鑰匙可動態調整開鎖對象而不需重新產生連結或重拍快照;`shared_chat` 只是又一種 resource_type,**沒有為它寫任何特例程式碼**。

#### G-5 Channels:AI 是完整管線的另一個入口
- `Channel`(type dm/channel, name, access_grants, data, meta)+ `ChannelMember` + `ChannelFile` + `ChannelWebhook`;`Message` 含 `channel_id`、`reply_to_id`(直接回覆)、`parent_id`(討論串根)、`is_pinned/pinned_by/pinned_at`;`MessageReaction` 獨立表(`models/channels.py:37-224`、`models/messages.py:20-97`)。
- 權限完全走 `AccessGrants.has_access(resource_type='channel')`(`routers/channels.py:70-93`),沒有頻道專用邏輯。
- 即時:各操作 `sio.emit` 到 `channel:{id}` room(`:1160,1375,1511,1586,1661,1739`)。打字指示器與已讀游標**共用同一事件通道** `events:channel`,依 `event_type` 分流成「純廣播不落地」(`typing`)與「落地不廣播」(`last_read_at`)(`socket/main.py:486-518`)。
- **@mention 模型**:語法 `<@{TYPE}:{id}|{label}>`(`utils/channels.py:4-31`,`M` = model)。`model_response_handler()`(`routers/channels.py:947-1111`)兩種觸發:訊息含 `<@M:...>`,**或這則訊息是在回覆一則由模型發出的訊息**(`:956-962`,即「回覆 AI」等同「@它」,不必每次手動標註)。接著建佔位訊息 → 把整個 thread 歷史組成上下文 → 包上 system prompt → 組出與前端同構的 `form_data`(`chat_id` 設成 `f'channel:{id}'` 讓下游走 channel emit 路徑)→ **丟進與一般聊天完全相同的 `CHAT_COMPLETION_HANDLER`**(`:1106`)。
- **洞察**:不是另開 bot 框架,而是把「被 @ 的模型」重新包裝成標準 chat completion 借道既有管線——工具、RAG、filter 全部原封不動可用。`reply_to` 隱式觸發是很細膩的 UX。

#### G-6~G-8 Notes / Calendar / Analytics
- **Notes**:`Note`(data JSON 正文, meta, access_grants)+ **`PinnedNote` 獨立 user×note 表**(而非 Note 上一個布林,允許每人各自釘選)(`models/notes.py:20-63`)。音訊擷取/上傳 → 帶 `$settings.audio.stt.language` 轉錄(`NoteEditor.svelte:445-452`);AI 輔助的系統提示語意是「用逐字稿或上傳檔案的內容強化既有筆記」(`:649`)——**改寫擴充而非附加**。可在輸入框選為聊天上下文,標 `type:'note'`(`InputMenu/Notes.svelte:32-59`)。**協作編輯是真的 CRDT**:`tiptap`/`prosemirror` + `yjs` + `y-prosemirror` + `prosemirror-collab`(`src/lib/components/common/RichTextInput/Collaboration.ts`)。
- **Calendar**:多行事曆 + 事件 + 與會者/RSVP(`models/calendar.py:36-103`)。**grep `caldav|google.*calendar|\.ics` 全無命中——是原生內建,不是外部行事曆鏡射。** 提醒走自動化排程器 `_check_calendar_alerts()`(`utils/automations.py:557-649`),去重靠 DB 欄位 `meta.alerted_at` 而非記憶體。**且暴露為模型可呼叫工具**:`search_calendar_events / create_calendar_event / update_calendar_event / delete_calendar_event`(`tools/builtin.py:3407-3747`,由 `utils/tools.py:654-657` 在 `calendar.enable` + `features.calendar` 權限下注入)。
- **Analytics**:全端點限 admin。聚合來源是**正規化的 `chat_message` 表**(`models/chat_messages.py:80-123`,`chat_message_model_created_idx`/`chat_message_user_created_idx` 兩個索引),不解析 chat JSON。⚠ **時間分桶在 Python 做,不是 SQL `date_trunc`**:撈出範圍內原始列後用 `strftime('%Y-%m-%d')` 迴圈累加,**最後補齊區間內無資料的日期為 0**(`:639-686`)——這個補零是刻意的,讓前端折線圖不斷點。群組過濾統一用 `GroupMember` 子查詢 + `user_id.in_(...)`(九處,如 `:662-663`)。0.10.0 新增日期範圍選擇器與可排序欄位。

#### G-9 Evaluation:Arena 是 regenerate 的自然延伸
- 前端 `feedbackHandler()`(`ResponseMessage.svelte:446-495`):無論單純讚踩,還是同一 parentId 底下有多個子回覆時互相評比,都送**同一種** `type:'rating'` feedback,差別只在後者附上 `sibling_model_ids`(`:474-479`)。**Arena 比較不是獨立模式,而是「regenerate / 多模型並排」情境下讚踩互動的自然延伸。**
- `Feedback` 表:`id, user_id, type, data JSON, meta JSON, snapshot JSON`;`data{rating: 1|-1, model_id, sibling_model_ids[], reason, comment}`、`meta{arena, chat_id, message_id, tags[]}`(`models/feedbacks.py:20-105`)。
- **ELO 實作**(`routers/evaluations.py:81-128`):起始分 1000、**K=32 固定**(無依比賽數動態調整)、標準公式 `expected = 1/(1+10^((opp-self)/400))`。`rating` 不是 `'1'`/`'-1'` 就整筆跳過(`:107`)——**平手沒有被建模**(UI 只有讚/踩)。**N 方比較被拆解成 N-1 場一對一 Elo**(對每個 sibling 各跑一次,`:112-119`),不是 Bradley-Terry 多方模型(推測為刻意簡化以重用雙人公式)。
- **主題加權**:`_compute_similarities()`(`:158-200`)用 embedding 算搜尋字串與各 feedback 標籤的 cosine 相似度當 `weight` 乘入 Elo 更新量,**在同一份資料上動態產生「哪個模型最擅長寫程式」之類的主題排行榜**,不需另存分類排行榜。順帶一提,這裡用的是 Dockerfile build arg `USE_AUXILIARY_EMBEDDING_MODEL=TaylorAI/bge-micro-v2` 這顆小模型(`routers/evaluations.py:65`)。
- 排行榜另附 `_get_top_tags()`(`:131-155`,每模型取前 5 常用標籤)當「這模型常被用在什麼主題」的標籤雲;`/leaderboard/{model_id}/history` 回每日勝敗計數。

#### G-10 Banners 與 Webhooks
- **Banner** 是純設定值不是資料表:`BannerModel{id, type(info|success|warning|error), title, content, dismissible, timestamp}` 陣列存在 `ui.banners`(`config.py:2094-2110, 3030`),可由 `WEBUI_BANNERS` env JSON 初始化。⚠ **關閉狀態存在 `localStorage.dismissedBannerIds`**(`Navbar.svelte:63-69`)——換裝置或清 cache 會重新看到(推測為刻意輕量化)。
- **Webhook(0.10.0 大改)**:`publish_event()` 產生結構化 Event(schema/id/event/resource/operation/actor/subject/data,`events.py:939-987`),雙重扇出到 `EventFunctionSink`(觸發 Event Function)與 `WebhookEventSink`(`:1084`)。`event_webhook_matches()`(`:787-791`)同時檢查事件名是否在訂閱清單/萬用字元內,**以及事件涉及的使用者是否落在該 webhook 的目標群組內**——webhook 也能做群組範圍過濾。多組具名 webhook 存在 `events.webhooks`,舊的單一 `webhook_url` 由 `migrate_legacy_webhook_config()`(`:809`)自動包成 `id='default'`。
- **payload 依目的地自動變形**(`utils/webhook.py:30-89`):`hooks.slack.com`/`chat.googleapis.com` → `{"text":...}`;`discord.com/api/webhooks` → `{"content":...}` 且截斷 2000 字元;`webhook.office.com` → 完整 MessageCard schema;其餘 → 通用 JSON。**呼叫端完全不需要知道對方是哪種平台。**

---

### H. 自動化與其他

#### H-1 Automations:用資料庫做分散式鎖(最該抄的一段工程)
- **本質不是輕量執行器,而是「排程觸發的完整 chat pipeline」**:到期時用擁有者身份建立一個**真正的新 chat**,把 prompt 當第一則使用者訊息送進與 `/api/chat/completions` 完全相同的管線(filter、RAG、工具、參數、DB 落地全走一遍),輸出永遠是一個可點進去看的 chat(`utils/automations.py:360-538`)。
- **RRULE**:`dateutil.rrule` 的 `rrulestr`(`:26`),前端組字串(`ScheduleDropdown.svelte:69, 86-104`,一次性排程用 `DTSTART` + `FREQ=DAILY;COUNT=1`)。`_parse_rule` 對 MINUTELY/HOURLY **強制用固定 epoch DTSTART(2000-01-01)** 讓區間對齊整點整分,避免因「建立時間」漂移(`:68-81`)。時區吃 `user.timezone` 用 `zoneinfo`,無效時區 fallback 伺服器本地並記警告而非丟例外(`:52-133`)。建立時擋「無未來執行時間」與「間隔過短」(`routers/automations.py:71-98`、`constants.py:117-120`)。
- **排程器**:**不是 APScheduler / Celery / 外部 worker**,是 FastAPI lifespan 裡 `asyncio.create_task(scheduler_worker_loop(app))` 的單一 in-process 迴圈,**每個 app 實例都跑**(`main.py:351-353`、`utils/automations.py:166-202`),每 `SCHEDULER_POLL_INTERVAL`(預設 10s)+ `random.uniform(0,2)` jitter 輪詢(jitter 是分散尖峰,不是防重複)。
- ⭐ **避免多副本重複執行靠 DB 原子搶佔,不是 Redis 鎖**:`Automations.claim_due()` 在 PostgreSQL 用 `SELECT ... FOR UPDATE SKIP LOCKED`,並**在同一交易內立即把 `next_run_at` 往後推才 commit**,所以同一列不可能被兩個實例同時搶到;SQLite 因單行程本來就無此問題(`models/automations.py:256-299`)。**這是一個乾淨到可以直接搬的分散式排他鎖範式。**
- **執行身份**:重新載入擁有者並**重新檢查權限**(被降級/停權/撤銷 `features.automations` 就直接失敗記錄,不用舊快取身份)(`:368-395`);現簽一個短效 JWT(預設 1 小時)塞進手刻的最小 ASGI Request(`_build_request`,`:210-239, 504-515`)讓下游需要 bearer 的工具伺服器認得是誰。0.10.0 CHANGELOG 明列「Scheduled automations stop for deactivated accounts」。
- ⚠ 後端**必須手動補齊前端原本會做的事**:model 綁的 `toolIds`、`defaultFeatureIds`、`defaultFilterIds`、`terminalId`,因為 chat_completion handler 本身不解析這些;**`code_interpreter` 被明確排除於 headless 執行**(需要前端事件發射器)(`:242-305`)。
- 資料模型:`Automation{id, user_id, name, data{prompt, model_id, rrule, terminal?}, meta, is_active, last_run_at, next_run_at(索引)}` + `AutomationRun{id, automation_id, chat_id, status, error, created_at}`(兩個複合索引);列表用 `get_latest_batch` 一次查完所有最新執行紀錄避免 N+1(`models/automations.py:19-50, 341-366`)。限制:`automations.max_count`、`automations.min_interval`。

#### H-2 Task model 與八個輔助任務(完整核實)
`get_task_model_id`:對話模型是 `connection_type=='local'` 就用 `task.model.default`,否則用 `task.model.external`(`utils/task.py:16-27`)——**本地與外部分開設定,因為成本結構不同**。

| 端點 | Prompt 模板 | 何時觸發 | 失敗降級 |
|---|---|---|---|
| `/title/completions` | `config.py:2135` | **僅新 chat 第一輪**(`!_chatId && parentId===null`) | JSON 解析失敗→退回訊息前 100 字;整體關閉→直接用第一則訊息 |
| `/tags/completions` | `:2161` | 同上,`autoTags` 控制 | try/except 吞掉,tags 留空 |
| `/follow_up/completions` | `:2204` | **每次**回覆完成後,`autoFollowUps` 控制 | 吞掉,不顯示 |
| `/image_prompt/completions` | `:2181` | 生圖前,`image_generation.prompt.enable` | **任何例外都 fallback 用原始訊息去生圖**(`middleware.py:1692-1697`) |
| `/queries/completions` | `:2234` | web_search / RAG 每則訊息,`type` 區分兩個開關 | 例外→用原始訊息當 query |
| `/auto/completions` | `:2265` | 輸入框打字即時(ghost-text),受 `AUTOCOMPLETE_GENERATION_INPUT_MAX_LENGTH` 限制 | 500,前端靜默 |
| `/emoji/completions` | `:2364` | **僅語音 Call 模式**驅動頭像表情,`max_tokens=4` | 400,表情維持中性 |
| `/moa/completions` | `:2368` | 多模型 Merge | 400 |
`FUNCTION_CALLING`(`:2340`)在 `TASKS` enum 內但**不是獨立端點**。**`moderation` 任務不存在**——`routers`/`utils` 全域搜尋皆無,v0.10.2 沒有內容審核任務。
- **洞察**:title/tags 綁死第一輪而非每輪重算是刻意的成本控制;image_prompt / query_generation 的降級是「退回原始輸入」而非中斷——「輔助任務失敗不能拖垮主流程」。
- 0.10.0 另加了三個自訂 header 變數 `{{USER_MESSAGE_ID}}`、`{{USER_MESSAGE_PARENT_ID}}`、`{{TASK}}`,**讓上游服務能分辨真正的使用者訊息與這些背景任務請求**——對需要逐筆稽核與計費歸屬的內網環境,這個小設計價值很高。

#### H-3~H-4 圖片生成與語音
- **圖片**:4 種引擎 `openai` / `gemini`(`:predict` 或 `:generateContent`)/ `comfyui` / `automatic1111`(空字串亦視為 A1111),Edit 另有獨立引擎設定(`routers/images.py:87-99, 616-829`)。
  - **ComfyUI 參數化**很漂亮:管理員上傳完整 workflow JSON,另定義一組 `ComfyUINodeInput{type, node_ids[], key, value}`,`type` 可為 `model|prompt|negative_prompt|image|width|height|n|steps|seed`,執行時用 `node_ids` 對照進 workflow dict 的 `inputs[key]` 做 in-place 覆寫;`type` 為空則是固定值覆寫(`utils/images/comfyui.py:122-186`)。**schema-free workflow + 型別化節點映射,能適配任意自訂圖。**
  - **圖片落地走事件旁路,不是把 `![](url)` 塞進文字**:用 `event_emitter({'type':'files', ...})` 以附件形式送前端,**同時往 LLM 訊息序列注入一段 `<context>` 告訴模型「圖片已生成且正顯示給使用者」**,讓它下一輪能自然回應這件事;失敗時同樣注入告知錯誤的 context,不中斷 pipeline(`utils/middleware.py:1710-1753`)。
- **TTS 5 種**:openai / elevenlabs / azure / transformers(本地)/ mistral;`engine == ''` 時 `/speech` 回 404,**由前端 fallback 到瀏覽器原生 `speechSynthesis`**(`routers/audio.py:546-612`、`ResponseMessage.svelte:258-272`)。另有**完全在瀏覽器跑的 Kokoro TTS**(`browser-kokoro`,經 `KokoroWorker` 用 WASM/WebGPU,`src/lib/workers/kokoro.worker.ts`、`ResponseMessage.svelte:299-328`)——**完全不打後端、不需要 GPU 伺服器**。
- **STT 5 種**:本地 faster-whisper(空字串)/ openai / deepgram / azure / mistral(`routers/audio.py:889-911`)。長音檔先 `compress_audio`(16kHz 單聲道)再 `split_audio` 切塊,平行送出後用 `asyncio.as_completed` 收集串接(`:1048-1103`)。TTS 端則**依標點斷句逐句合成**,結果 push 進 `$audioQueue` 無縫接續播放,不等整段合成完(`ResponseMessage.svelte:284-349`)。
- **Call 模式 VAD 全在瀏覽器自製**,無外部 VAD 函式庫:`AudioContext` + `AnalyserNode`,`MIN_DECIBELS = -55` 判斷有無聲音,**連續 2 秒無聲**即視為講完停止錄音(`CallOverlay.svelte:154, 289-379`)。**Barge-in 預設關閉**——AI 播放時直接把麥克風分析器參數調到讀不到聲音,必須開 `voiceInterruption` 才允許打斷(`:323-340`)。turn-taking 是純前端狀態機:錄音停 → 整段 Blob 打 `/api/audio/transcriptions` → **呼叫與文字聊天完全相同的 `submitPrompt()`** → TTS 播放 → 立刻重新 `startRecording()`(`:157-231`)。**Call 模式沒有任何獨立後端路由。**

#### H-5~H-7 PWA、i18n、Web Search
- ⚠ **PWA 已經不是 PWA**:`static/manifest.json` 內容是空物件 `{}`,找不到任何 service worker,前端反而**主動 `unregisterServiceWorkers()`** 並在版本更新時清掉舊註冊(`src/routes/+layout.svelte:84-104`)。桌面通知走瀏覽器原生 `Notification` API(非 SW push),由 socket 事件驅動,僅在分頁不在前景(Electron 版另問 `window:isFocused`)且使用者開啟 `notificationEnabled` 時才跳(`:472-513, 639-753`)。**多分頁去重用 `BroadcastChannel('active-tab-channel')` + `isLastActiveTab`,只有最後互動的分頁會跳通知**(`:108, 1038-1048`)。
  - 順帶:`svelte.config.js` 用 `adapter-static` + `fallback: index.html`(純 SPA,靜態檔案),並用 git HEAD 當 version、**每 60 秒輪詢一次以觸發重載提示**——對內網部署很友善的兩個決定。
- **i18n**:**實際 62 個語系目錄**(63 個項目扣掉 `languages.json`)。`i18next` + `browser-languagedetector`(querystring → localStorage → navigator)+ `resources-to-backend` 動態 `import()` **懶載入不打包進主 bundle**(`src/lib/i18n/index.ts:1, 44-69`)。抽取用 `i18next-parser`,locale 清單讀 `languages.json` 動態產生,**`keepRemoved: false` 讓掃不到的舊 key 被清掉**(`i18next-parser.config.ts:1-33`)。
  - **RTL 不是整頁鏡像,而是逐訊息** `dir={$settings.chatDirection}`(LTR/RTL/auto,預設 auto 交給瀏覽器 bidi)(`UserMessage.svelte:132,207,387`、`ResponseMessage.svelte:654,701`);側欄與設定頁不隨語言翻轉。
  - **zh-TW 完整度**(我親自統計):en-US 2543 key、zh-TW 2532 key(缺 11),另有約 201 個 key 值為空字串 → 實際填值 2331,**約 92% 有效覆蓋率**。
- **Web Search 30 家 provider**(`retrieval/web/` 扣掉 `main.py`/`utils.py`):ollama_cloud, perplexity_search, searxng, yacy, google_pse, brave, brave_llm_context, kagi, mojeek, bocha, serpstack, serper, serphouse, serply, duckduckgo, tavily, exa, searchapi, serpapi, jina, bing, azure, perplexity, microsoft_web_iq, sougou, firecrawl, external(自架), yandex, youcom, linkup;分派是 `routers/retrieval.py:2168-2513` 一長串 elif。迴圈:query 生成 → `asyncio.Semaphore` 限流併發搜尋(`WEB_SEARCH_CONCURRENT_REQUESTS`,設 1 可應付免費版限流)→ URL 去重 → `get_web_loader` 爬全文(或 `BYPASS_WEB_SEARCH_WEB_LOADER` 只用 snippet)→ 走一般 RAG(`:2516-2615`)。0.10.0 新增「管理員可要求使用者在搜尋執行前確認」。

#### H-8 其他值得知道的
- **Memory(0.10.0 重做)**:不是單一 embedding 庫,而是**「路徑階層 + 向量檢索」混合**——memory 有 `path` 欄位像檔案系統路徑,每次請求前組出 `[User Memory] / [Memory Neighborhood] / [Relevant Context]` 三段注入,各有字數上限(`utils/memory.py:290-405`)。**背景記憶審查**:每滿 `memories.review_interval_turns`(預設 10)輪,用 `asyncio.create_task` **非阻塞**讓**目前對話所用的模型本身(不是 task model)**審視最近 16 則訊息,產出 `add/replace/move/remove` 結構化操作(`:408-463, 505-517`)。**刻意與 context compaction 用便宜 task model 形成對比——記憶審查需要更好的判斷力。**
- **Context Compaction(0.10.0 新增,預設關)**:超過 token 門檻時找壓縮邊界,把舊訊息交給 **task model** 摘要,存成該訊息的 `contextSummary` 當 checkpoint,下次請求先套最新 checkpoint 再判斷是否再壓(`utils/context_compaction.py:42-141, 257-315`)。可設門檻、自訂摘要 prompt、per-model 降低門檻。
- **Rate Limiting**:Redis rolling-window(bucket 化 `mget` 加總)為主,**Redis 不可用時自動退化成行程內記憶體字典,兩套 API 完全對稱**(`utils/rate_limit.py:47-137`)。
- **Audit Logging**:純 ASGI middleware,依 `AuditLevel`(NONE / METADATA / REQUEST / REQUEST_RESPONSE)決定是否連 body 一起記,body 有 `MAX_BODY_LOG_SIZE` 上限,底層用 loguru `bind(auditable=True)` 過濾(`utils/audit.py:36-134`)。
- **OTel**:`TracerProvider` + 依 `OTEL_OTLP_SPAN_EXPORTER` 選 gRPC/HTTP exporter,對 FastAPI app 與 SQLAlchemy engine 做 instrument(`utils/telemetry/setup.py:27-53`)。
- **Anthropic 雙向相容**(沒人指派、我自己找到的):既能**消費** Anthropic 原生上游(`is_anthropic_url`、`get_anthropic_models`、`convert_anthropic_to_openai_payload`,`utils/anthropic.py:16-112`),也能**提供**一個 Anthropic 相容的 `/messages` 端點(`main.py:1679-1726`,inbound Anthropic → 內部 OpenAI 管線 → 轉回 Anthropic,含串流事件翻譯)。**設計原則:單一內部標準格式(OpenAI chat completions)+ 兩端各自的 adapter。** 0.10.2 還修了它的並行 tool call 與 prompt caching marker 保留。
- **`FileNav` 系列**(沒人指派、我自己找到的):`chat/FileNav.svelte` + `FileNav/` 底下有 `JsonTreeView`、`NotebookView`(.ipynb)、`SqliteView`(用 `sql.js` 在瀏覽器開 SQLite)、`FileCodeEditor`、`CellEditor`、`BulkActionBar`,以及 **`PortList`/`PortPreview`——每 5 秒輪詢沙盒的 listening ports 並讓你在對話裡預覽跑起來的 web app**(`FileNav/PortList.svelte:11-40`)。另有 `PyodideFileNav.svelte` 對應瀏覽器端虛擬檔案系統。0.10.0 另加「終端機檔案瀏覽器可鎖在指定 root/home 目錄內」。

---

## 三、十個最值得學的設計決策

| # | 洞察 | 為什麼有效 | 移植到 React+FastAPI 的難度 |
|---|---|---|---|
| 1 | **一張 `access_grant` 多型表管所有資源 ACL**,public 用 `principal_id='*'` 表示,owner 走 `OR` 而非塞進表裡,admin bypass 留在程式碼 | 新增資源類型 = 加一個字串,零 schema 變更;兩個索引正好對應兩種查詢方向;授權引擎變成無狀態純函式,因此 Preview Access 幾乎免費得到 | **低**。純資料模型 + 兩個查詢 helper(`EXISTS` 子查詢版與批次 `IN` 版)。SQLAlchemy 直接可寫。**這是整份報告投報率最高的一項** |
| 2 | **訊息樹一個原語打通編輯/重生/多模型/Arena**,加上「切到兄弟後沿 `childrenIds.at(-1)` 貪心下鑽」這條單一慣例 | 四個看起來不同的功能共用同一套儲存、同一套版本導覽 UI;`modelIdx` 只是多一維分組鍵 | **中**。資料結構簡單,但要在 DB 就把 `parent_id` 建成一等欄位(別像 ANILA 現在只有扁平 list),並統一「currentId 該指向哪」的規則 |
| 3 | **reasoning「思考時間」在伺服器算完寫死進持久化欄位** | 一舉解決前端計時器 + 重整/多分頁/多裝置不同步;重新載入不需要重算也不會漂 | **低**。就是在 output item 上多存 `started_at`/`ended_at`/`duration` |
| 4 | **native 模式不注入知識庫,改給模型檔案系統語意的工具,並用截斷常數當第一公民**(`MAX_CAT_CHARS`/`MAX_GREP_FILES`/`MAX_GREP_MATCHES` + 錯誤訊息直接教模型改用 head/grep) | 讓模型 multi-hop 自己找,而不是靠一次 top-k 賭中;截斷把 agentic 檢索的 context 爆炸壓住;錯誤訊息本身就是 few-shot 教學 | **中高**。工具實作不難,難在 (a) 引用鏈設計(見下)與 (b) 需要一顆 tool-calling 夠好的模型 |
| 5 | **`get_source_context(include_content=False)`——把內容給模型一次,只把 `<source id="N">` 空殼標籤塞回 system prompt 補編號** | 模型能寫 `[N]` 而完全不重複耗 token 貼全文。這是「引用」與「內容」正交化的漂亮解法 | **低**。純 prompt 組裝技巧 |
| 6 | **Automations 用 `SELECT ... FOR UPDATE SKIP LOCKED` + 同交易內推進 `next_run_at`** 做多副本互斥,取代 Redis 鎖/leader election | 零額外基礎設施,正確性由資料庫交易保證;`asyncio` in-process 迴圈 + jitter 就夠了 | **低**。PostgreSQL 一行 SQL 語意 |
| 7 | **Valves 的 UI 完全由 Pydantic schema 自動生成**,含 `json_schema_extra.input.type` 指定 select/password/color/map 等 widget,`options` 可指向接收 `__user__` 的 classmethod | 新增一個參數欄位不需要碰任何前端程式碼;admin 層與 per-user 層清楚分離(一個存資源表、一個借 `User.settings`) | **中**。React 需要一個 JSON-Schema-driven form renderer(生態很成熟:rjsf 等) |
| 8 | **Skills 的兩段式漸進揭露**:預設只給 `<available_skills>` 的 id/name/description manifest,模型覺得需要才呼叫 `view_skill(id)` 拉全文;被明確 @mention 的才直接全文注入 | 可以掛 50 個 skill 而不炸 system prompt;而且「使用者明確要求」與「模型自主判斷」兩條路徑語意分明 | **低**。就是 manifest + 一個 view 工具 + 一個 mention 正則 |
| 9 | **`#` 引用與檔案上傳共用同一個 polymorphic 附件陣列與同一個後端入口**,靠元素的 `type`/`context` 欄位分流;`context: 'full'` 的決策在 **per-item** 而非 per-request 層級 | 送出、渲染、citation 溯源全部只有一條程式碼路徑;而且天然支援「同一則訊息裡一份精讀全文 + 一個模糊檢索的大 KB」 | **低**。純介面設計決定,越早定越省 |
| 10 | **用文案彌補模型限制**:群組權限若已被系統預設鎖開,直接顯示「This is a default user permission and will remain enabled」,而不是給一個按了無效的開關 | 加法制權限的限制無法用 UI 藏掉,但可以用一句話讓管理員當場理解,不必去翻文件 | **極低**。但需要有人願意寫這句話——這是紀律問題不是技術問題 |

**額外三個榮譽提名**(不佔前十但很便宜):Analytics 時間分桶**補零**讓折線圖不斷點;webhook payload **依目的地網域自動變形**成 Slack/Discord/Teams/通用四種格式;自訂 header 變數 `{{TASK}}` / `{{USER_MESSAGE_ID}}` 讓上游能分辨真人訊息與背景任務。

---

## 四、它刻意不做的事(從程式碼與註解推斷)

1. **不做自動化測試**。backend 測試檔 **0 個**(`find backend -name "test*"` = 0),`pytest` 只是宣告的相依;CI 只跑 `ruff format --check` 與窄化的 `ruff check --select=F --ignore=F401,F403,...`(`.github/workflows/backend.yaml:36-42`),`lint-backend.yaml`/`lint-frontend.yaml`/`codespell.yaml` 三個 workflow 都是 `.disabled`。**這不是疏忽,是拿測試換發布速度的明確交易**——0.10.0 一個版本就上了 100+ 條變更。⚠ **ANILA 有 1594 個 CSP 測試;請只抄它的產品設計,絕對不要抄它的工程流程。**
2. **不做多租戶**。`organization`/`workspace_id` 全庫零命中,`tenant` 只出現在 qdrant 連接器內部的 collection→tenant 映射(`retrieval/vector/dbs/qdrant_multitenancy.py`)。隔離單位是 group + AccessGrant,不是 tenant。推測:單一組織自架是目標場景,多租戶會污染每一個查詢。
3. **不做配額 / 計費 / 用量上限**。`quota` 只出現在 errno 字串與 pinecone 錯誤字串;`billing` 零命中。有 token 用量**統計**(`chat_message.usage` + analytics)但**沒有任何 enforcement**。推測:自架者的成本控制在上游 gateway 做,不在應用層。
4. **不做資料分級 / 保留政策 / 稽核工作流**。`classification` 1 個無關命中,`retention`/`approval_workflow` 零命中,`soft_delete` 零命中,`deleted_at` 只有 channel 有(`models/channels.py:64`)——**聊天是硬刪除,沒有垃圾桶、沒有復原**。這正是 ANILA 已經做完而 Open WebUI 完全沒做的一整塊。
5. **不做 per-user 權限覆寫**。權限樹只有「系統預設 + 群組聯集」,個人差異只能靠加入/移出群組(`routers/users.py:170-256` 沒有 user-level permissions 欄位)。刻意保持心智模型單純。
6. **不做減法權限**。合併運算子只有 `or`(`utils/access_control/__init__.py:41-67`),整個系統沒有交集/差集語意。這是明確取捨:換來「加群組只會變寬鬆」的可預測性,放棄「群組 A 收緊群組 B」的表達力。
7. **不做具名 artifact 版本鏈**。artifact「版本」是每次從整個對話重掃出來的衍生清單,不持久化、不能命名、無法知道兩版是不是同一個 artifact 的疊代(`src/lib/utils/index.ts:2031-2089`)。推測:為了完全不新增資料模型。
8. **放棄 PWA**。manifest 是空的 `{}`,並主動 `unregisterServiceWorkers()`(`src/routes/+layout.svelte:84-104`)。推測:SW 快取與「每 60 秒輪詢 git HEAD 觸發重載」的部署模型直接衝突,選了後者。
9. **不做全文檢索索引**。搜尋是 `ILIKE '%…%'` 掃 JSON 訊息陣列(`models/chats.py:1476-1571`),沒用 tsvector/GIN。推測:要同時支援 SQLite 與 PostgreSQL,全文檢索沒有共同抽象。同理 tag 用 JSON containment 而非 join table。
10. **不做檔案去重與孤兒回收**。算了 sha256 但註解明講只給 sync diff 用,上傳前不查重;刪除聊天完全不碰 `File` 表(`routers/chats.py` 無任何 `Files.` 呼叫)。推測:隱性的 chat→file 引用(藏在 JSON blob 裡)讓「還有誰在用這個檔案」無法查詢,所以自動回收做不了。**這是「早期為了省一張 join table,後來付不出去的技術債」的教科書案例。**
11. **Pipelines 沒有被刪,而是被放著**。Functions 已完全取代它的能力,但兩套 filter 目前並存疊加執行(`middleware.py:2420, 3360`),舊使用者不會壞。**「取代但不移除」也是一種產品決定。**
12. **MCP 只做一半**。只支援 Streamable HTTP(無 stdio);`outputSchema` 讀出但標 TODO;Resources API 寫好了但聊天流程沒串;Prompts 完全沒做(`utils/mcp/client.py:88-147`)。推測:先接上工具這條主線,其餘等需求。

---

## 五、對 air-gapped 涉密環境不適用的部分,與替代方向

| 不適用項 | 問題在哪(file:line) | 替代方向 |
|---|---|---|
| **DB 值永遠贏過 env var** | 一旦 key 落地 DB,改 env 永遠無效(`models/config.py:249-250`)。這與 ANILA 的「不可變簽章 release artifact 才是部署身分」方向**正面衝突**——設定漂移將無法被 release 收斂 | **設 `ENABLE_PERSISTENT_CONFIG=False`**(`config.py:3130`)讓所有設定回歸 env-only,或做**分層**:安全關鍵設定(分級門檻、egress、信任錨)env-only 且啟動時斷言;純體驗設定才允許 DB 熱改。**這正是 ANILA 該採的混合制** |
| **在行程內 `exec()` 使用者貼上的 Python(Tools/Functions)+ 自動 pip install** | `utils/plugin.py:411`(subprocess pip)與 `:253`(exec);`OFFLINE_MODE` 可關 pip 但 exec 本身不可關 | ANILA 現有的 `agents` 表(`services/csp/app/models/agent.py:35-81`,帶 `manifest_url`/`endpoint_url`/`input_schema`/`allowed_tool_ids`/`runtime_type`)已經是**更安全的同構解**:out-of-process、manifest 註冊、schema 宣告。**只要補上 Open WebUI 的 valves-from-schema 自動 UI 生成,就能得到同等開發者體驗而不引入 in-process exec** |
| **瀏覽器 Pyodide 執行 + socket.io 反向 RPC 讓伺服器叫瀏覽器跑程式碼** | `builtin.py:494-504` → `+layout.svelte:521-525` | Pyodide 本身在 air-gapped 反而是優點(`npm run pyodide:fetch` 預先抓好、離線可用、不佔伺服器)。但**反向 RPC 這條路徑要砍**——它讓「伺服器發起的動作在使用者端執行」,稽核歸屬會斷。改成純前端功能(使用者按 Run),不要讓它成為模型可呼叫的 tool |
| **`direct_tool_servers`:瀏覽器直連使用者自填的工具伺服器** | `AddToolServerModal.svelte:157-168` 驗證、`middleware.py:4720-4732` 執行皆繞過後端 | 直接關閉 `features.direct_tool_servers` 權限;所有工具伺服器一律 admin 註冊 + 走 ANILA 既有的 `trusted_hosts` / `service_access_grants` / egress policy |
| **30 家外部 web search + 外部 rerank / 外部文件解析引擎** | `routers/retrieval.py:2168-2513`;`retrieval/models/external.py` | 全部關閉,只留 `external`(自架)這一種形態並指向內網服務。**注意 0.10.0 有個很對味的設計可以抄:管理員可要求使用者在搜尋執行前確認**,搭配 banner 明示——這在需要「出網動作必須有人為確認紀錄」的環境正好用得上 |
| **Terminals / PortPreview:對外部終端機伺服器的反向代理 + 在對話裡預覽跑起來的服務** | `routers/terminals.py:65-113, 256-286`;`FileNav/PortList.svelte` | 架構本身(把沙盒推到行程外、admin 註冊)是對的,0.10.0 的「檔案瀏覽器鎖在指定 root/home」也對。但在涉密網要先回答「沙盒內產出的檔案屬於什麼分級」——**在 ANILA,這必須先接上 classification latch 才能開** |
| **hard delete、無保留政策、無 legal hold** | 聊天硬刪除;`retention`/`soft_delete` 零命中 | **不需要替代——ANILA 這塊已經領先**:`ingestion_documents` 已有 `lifecycle_state`/`archive_due_at`/`erase_due_at`/`legal_hold`/`legal_hold_reason`(`services/csp/app/models/ingestion.py:261-269`)。反過來說,**若照抄 Open WebUI 的訊息樹,務必把「舊分支永久保留」納入保留政策計算**——編輯分支會讓對話單調成長,而每個分支都可能帶分級 |
| **kb_exec 等工具不進 citation 白名單** | `middleware.py:4768-4777` | **在 ANILA 這是硬需求不是選配**:所有知識庫工具的回傳一律強制結構化(至少 `document_id` + line range),並讓 citation 由後端單一函式產生 `id→source` 映射隨 response 回傳。ANILA 已有 `citations` 表與 `source_snapshots`(`services/csp/app/models/source_snapshot.py:85`),接上即可 |
| **localStorage 存 banner 已讀、前端算 PDF、前端算 checksum** | `Navbar.svelte:63-69`;`ChatMenu.svelte:86-249`;`KnowledgeBase.svelte:562-572` | banner 已讀改存後端(涉密公告需要「誰看過」的紀錄);PDF 匯出改後端產生(前端截圖無法嵌入分級水印與稽核 ID——ANILA 已有 `export_records` 表可掛);checksum 前端算沒問題但後端必須複驗 |
| **授權品牌條款** | `LICENSE` 第 4 條:禁止移除/遮蔽 Open WebUI 品牌,例外之一是「滾動 30 日內端使用者不超過 **50** 人」 | ANILA 目標是院內數百人,**該例外不成立**。這再次確認既定方針正確:**只學設計、不引入程式碼、不部署其本體** |

---

## 六、ANILA 最該補的功能(排序 + 理由)

先講 ANILA 現況的三個關鍵事實(我親自查證,非推測):
- **`messages` 表是扁平 list**:`id(autoincrement), conversation_id, role, content, model_name, metadata_, rating, classification_*, created_at`——**沒有 `parent_id`,沒有樹**(`services/csp/app/models/message.py:11-40`)。
- **編輯是破壞性截斷**:端點 docstring 直書「Rewrite a user message and **drop everything after it**」(`services/csp/app/api/conversations.py:401-415`)。
- **資料夾 / 標籤 / 封存全部只是 `users.ui_settings` 裡的一個 per-user JSON blob**(`apps/anila-shell/src/app.jsx:323-406` 的 `convMeta` → `services/csp/app/api/users.py:513-551`),**伺服器無法查詢、無法分享、無法治理**。

而 ANILA 明顯領先的地方也要講清楚,免得搞錯投資方向:分級 latch 與不可降級不變式、`legal_hold`/`erase_due_at` 生命週期、`embedding_fingerprint` 與 re-embedding generations、`artifacts`/`artifact_versions` 真正的版本鏈與抹除追蹤、`model_governance_receipts`/`policy_decisions`/`trace_spans`、以及 out-of-process 的 agent 註冊模型——**這些 Open WebUI 全部沒有**。所以下面的排序只針對「該有而沒有」。

| 排序 | 該補什麼 | 為什麼是這個順位 |
|---|---|---|
| **1** | **訊息樹:`messages` 加 `parent_id`(+索引),編輯與重新生成改成長出兄弟節點而非截斷** | 這是**唯一會造成使用者資料實際遺失**的缺口(`conversations.py:401-415` 的 "drop everything after it")。也是一切上層功能的地基:多模型並排、Arena、ELO 評測、A/B 比較全部建在「同 parent 多 children」之上,現在一個都做不了。且它是**破壞性最小的 migration**(加一個 nullable 欄位 + 回填 `parent_id` 為前一筆 id)。⚠ 補的同時要一併決定舊分支的保留政策——每個分支都可能帶分級 |
| **2** | **設定的 DB 化:一張 per-key `config` 表 + 分層優先序** | ANILA 現在沒有任何 config 表(全表列表無 config/settings),改設定 = 編 `.env` + `up -d`(`CLAUDE.md` §4 自己列為 footgun,且 `START-HERE.sh`/`intranet-deploy.sh` 每跑一次就蓋掉手動修正)。**院內數百人上線後,「改一個提示詞模板要重啟服務」不可持續。** 但務必**不要照抄「DB 永遠贏」**——採分層:安全關鍵(分級門檻/egress/信任錨)env-only 且啟動斷言,體驗類才允許 UI 熱改。學 Open WebUI 的 `ENABLE_PERSISTENT_CONFIG` 逃生閥 |
| **3** | **統一 `access_grants` 表(一張表管所有資源 ACL)+ Preview Access 稽核視圖** | ANILA 目前是**每種資源一張授權表**:`collection_access_grants`、`service_access_grants`、`user_model_permissions`、`api_key_model_permissions`、`user_agent_permissions`、`api_key_agent_permissions`、`execution_grants`、`clearance_grants`…。每加一種資源就要加一張表 + 一套查詢 + 一套 UI。收斂成 `(resource_type, resource_id, principal_type, principal_id, permission)` 一張表後,**Preview Access(「這個人到底能看到什麼」)幾乎免費得到**——而在需要對稽核人員證明權限邊界的涉密環境,這個視圖的價值遠高於一般企業。⚠ 分級/compartment 檢查**必須留在 grant 之外當獨立的 AND 條件**,不能被折進聯集邏輯(Open WebUI 全 OR 的哲學在這裡不適用) |
| **4** | **資料夾/標籤/封存/釘選落地成伺服器端可查詢的結構** | 現在存在 per-user JSON blob 裡:換裝置不同步、伺服器無法「列出所有帶標籤 X 的對話」、無法共享資料夾、無法對「專案」綁系統提示與知識庫、也**無法治理**(稽核者查不到某個分級的對話被歸在哪)。做法直接抄 F-1:`conversations.folder_id` + `folders` 表(`parent_id` + `data.system_prompt` + `data.collection_ids`),`archived`/`pinned` 各一個帶索引的布林欄位。**標籤建議一開始就用 join table**——別學 Open WebUI 的 JSON containment,ANILA 用 PostgreSQL 沒有相容性包袱 |
| **5** | **知識庫的巢狀目錄 + 增量同步 diff** | ANILA 的 `ingestion_documents` 是集合底下的平坦清單。院內數百人、每人幾十份文件後,沒有目錄就不可用。抄 C-1 的關鍵洞察:**目錄樹只服務瀏覽,collection 仍是唯一向量命名空間**——搬資料夾永不重建索引。同步 diff(C-4)則直接解決「文件更新後要不要整批重上傳」;比對鍵建議用 content-hash 而非 (path, filename),這樣能偵測搬移。ANILA 已有 `sha256` 欄位(`ingestion.py:246`)可直接用 |
| **6** | **Native function calling 下的知識庫工具化 + 強制結構化引用** | 這是 Open WebUI 花 17 個月走完的路,ANILA 可以跳過中間步驟直接到終點。但**必須反過來做一件 Open WebUI 沒做的事**:所有知識庫工具的回傳一律結構化(`document_id` + line range),citation 由後端單一函式產生 `id→source` 映射隨 response 回傳(見 C-8 與 C-bis 的白名單落差)。**在涉密環境,引用鏈就是稽核鏈**——不能像 Open WebUI 那樣「有些工具有來源、有些沒有」 |
| **7** | **Valves 式的 schema-driven 參數 UI(admin 層 / per-user 層分離)** | ANILA 的 `agents` 表已有 `input_schema`/`manifest_json`/`runtime_config`(`agent.py:74-174`),**離「從 schema 自動生成設定表單」只差一個渲染器**。補上之後,新增一個 agent 參數不需要動前端——這對「未來要接多個院內單位自帶的 agent」是決定性的維護成本差異 |
| **8** | **三層參數繼承 + tri-state(全域 → 模型/agent → 每次對話)** | ANILA 的 `model_registry` 沒有 params 欄位,`agents` 有 `runtime_config` 但無繼承。抄 B-4 的 `null` = 繼承語意(別用額外布林旗標),**但務必只在一個地方做最終合併**——Open WebUI 自己就踩到 provider router 二次覆蓋導致真實優先序變成「模型 > 對話」的坑 |
| **9** | **Cmd+K 搜尋的 filter 前綴 + 伺服器端全文檢索** | ANILA 已有 `CommandPalette.jsx`/`paletteQuery.js` 與 `/conversations/search` 端點,基礎在了。補五個前綴(`tag:` `folder:` `pinned:` `archived:` `shared:`)並**直接用 PostgreSQL `tsvector` + GIN**——Open WebUI 因為要相容 SQLite 才只能 `ILIKE`,ANILA 沒有這個包袱,可以一步做對 |
| **10** | **Automations(RRULE 排程)+ 用 `FOR UPDATE SKIP LOCKED` 做多副本互斥** | ANILA 已有 `tasks`/`task_runs`/`session_events`/`retention_reaper_leases`(已經在用 lease 概念了),補 RRULE 排程與「排程觸發完整管線」的模式即可。**執行身份的處理要照抄 H-1**:每次執行前重新載入使用者並重新檢查權限/分級 clearance,而非用建立時的快照——這在涉密環境是硬要求(人員調職、clearance 撤銷後排程必須立刻停) |

**明確建議「先不要做」的**:Channels(需要先有訊息樹與 ACL 收斂,否則會再長出一套平行的授權邏輯)、Notes 的 CRDT 協作(yjs 是好東西,但涉密環境的「多人同時編輯同一份分級文件」需要先有政策而非先有技術)、圖片生成 / TTS / Call 模式(對「院內數百人查文件」這個核心場景的邊際效益低於前 10 項;唯一例外是 **browser-kokoro 那種完全在瀏覽器跑的 TTS**,若有無障礙需求,它是 air-gapped 環境下成本最低的解)。

---

## 附:方法與可信度說明

- 十路平行深挖(A 拆成 2 路、C 拆成 2 路,其餘 B/D/E/F/G/H 各 1 路),各路先獨立讀碼再寫結論;我自己另外獨立查證了 ANILA 側基線、授權條款、Dockerfile/build 姿態、i18n 統計,以及三個沒有指派給任何一路的surface(`Overview` 訊息樹圖、`FileNav`/`PortPreview`、Anthropic 雙向相容)。
- **兩個與通行說法衝突的宣稱我親自復驗**:`PersistentConfig`/`AppConfig`/`app.state.config` 在 `backend/open_webui/` 命中為零;`3ff2c63645b8_reshape_config_to_per_key_rows.py` 與 `f1e2d3c4b5a6_add_access_grant_table.py` 兩個 migration 確實存在。
- 有一路報告開頭懷疑這份原始碼「相較公開版有大量客製化」(因為看到 access_grants/events/session_pool/RedisDict)。**經上述 migration 與 0.10.0 CHANGELOG 條目交叉比對,這些都是 v0.10.x 的真實變更,不是 fork。** 該路的技術結論仍成立,只有那句 caveat 應予撤回。
- 全文引用 Open WebUI 程式碼 **0 行**(遠低於 40 行上限),結構一律以 pseudo-schema、欄位名清單與散文描述表達。
- 推測處皆已標「推測」。最需要注意的三個「只有讀碼才看得出來」的落差:模型參數被 provider router 二次覆蓋(B-4)、citation 編號是前後端各自實作的隱性契約(C-8)、`kb_exec` 類工具不進 citation 白名單(C-bis)。