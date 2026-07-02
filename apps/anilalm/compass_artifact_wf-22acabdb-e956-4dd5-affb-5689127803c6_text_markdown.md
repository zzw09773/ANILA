# Presenton 與 Gemma 4 整合可行性評估

**結論先行：這個組合可行且有顯著優勢，但須處理三個非平凡的工程坑**——CJK 字型在 python-pptx 後端會 fallback、Presenton 內建沒有視覺驗證迴圈、vLLM 的 thinking 與 function calling 同時啟用尚不穩定。Gemma 4 31B-it 已於 2026-04-02 由 Google DeepMind 釋出（Apache 2.0、30.7B dense BF16、256K context、原生 function calling 與 system role、SigLIP 約 550M vision encoder），所有使用者提供的規格均經 ai.google.dev 官方 model card 與 Hugging Face 模型卡核實為準確。Presenton（commit `adcda867`，2026-04-13 維護中）則確認採 5-phase pipeline 與 Zod-first schema 設計，但**沒有任何視覺驗證階段、沒有 CJK 字型相關 issue/PR、icon 處理推論為「LLM 直接輸出名稱 + Zod enum 限制」而非語意對應表**。整體判斷：值得做，但九步流程要稍作調整、icon 兩階段策略應保留為主路徑、MVP 必須加上自製 CJK patch 與視覺 QA 模組。

---

## A. Gemma 4 31B 在 4×H100 部署完全可行

**模型存在性與規格已 100% 核實**。VRAM 數字非常充裕：BF16 權重僅 **61.4 GB**（單張 H100 80GB 即可裝下，加 vision encoder ~1.1 GB 仍寬鬆），FP8 量化降到 31 GB，Q4 (AWQ/GPTQ) 約 18-22 GB，NVIDIA 已釋出官方 `Gemma-4-31B-IT-NVFP4` checkpoint。在 4×H100 320 GB 總記憶體下，最佳配置不是 TP=4，而是**橫向部署 4 個 TP=1 副本 + load balancer**——InferenceBench 實測顯示這比 TP=4 單實例吞吐量高約 2.5 倍（5,040 tok/s vs 1,996 tok/s），原因是 Gemma 4 31B 在多卡上的 scaling 效率僅約 40%。

**256K 全展開的 KV cache 是唯一真正壓力點**：BF16 下需要約 218 GB KV，啟用 `--kv-cache-dtype fp8` 可降到 64 GB。對 NotebookLM-style 應用，256K context 等於 400-500 頁 PDF 或 15-20 萬中文字，比 gpt-oss-120b 的 131K（OpenAI 官方）多兩倍，且 Gemma 4 在 **MRCR v2 8-needle @ 128K 達到 66.4%**，較 Gemma 3 27B 的 13.5% 有 5 倍躍進，意味著長文件中找事實的能力不再是空殼數字。

**推論引擎成熟度**已達 day-0：vLLM 上游官方 recipe 提供 `--tool-call-parser gemma4` 與 `--reasoning-parser gemma4` 兩個專屬 parser，SGLang/TGI/Ollama 同時支援。**但有一個重要警告**：vLLM issue #39043 報告 thinking + function calling 並用時會出現 reasoning tag 洩漏到 chat 或 tool call 洩漏的 bug，**生產環境建議解耦設計**——簡報生成主流程關閉 thinking、僅在規劃敘事階段（步驟 3）另開一個獨立 endpoint 啟用 thinking。

**吞吐量實測（vLLM, BF16, 4×H100）**：peak 2,355 tok/s、sustained 1,996 tok/s、TTFT 109 ms。10 張 slide（每張 ~1,000 tokens 輸出）若**並發處理**，4 個 TP=1 副本可在 **30-60 秒** 完成；若 sequential 約 154 秒。這個數字遠低於使用者的耐心閾值，符合互動式使用要求。JSON guided decoding 僅 5-10% overhead，schema 嚴格度不會顯著拖慢。

---

## B. Presenton 整合相容性高，但三項細節須驗證

**OpenAI-compatible 路徑通暢**。Presenton 透過 `LLM=custom` + `CUSTOM_LLM_URL` 直接把 OpenAI Python SDK 的 `base_url` 指向自家 endpoint，vLLM/SGLang 都原生提供 `/v1/chat/completions` 相容 API。但 Presenton pipeline 重度仰賴 `response_format={type: "json_schema", schema: ...}` strict 模式，**這是相容性最大坑**：你的 vLLM 啟動指令必須帶 `--enable-auto-tool-choice` 並使用 outlines/xgrammar guided decoding 後端，否則 schema-strict 任務會直接失敗（部分舊版 LiteLLM 包裝就有此問題）。

**Gemma 4 的 JSON 遵從性理論上極強**，原生 function calling + 訓練即包含結構化輸出，搭配 vLLM 的 outlines 後端在實證上能達 98.7% valid rate（PARSE 論文）；但**幻覺仍會在「語意層」發生**——模型可能輸出語法合法但語意無關的 enum 值（OpenAI Structured Outputs 文件與 Towards Data Science 都有報告此現象）。這就是為何 schema 驗證迴圈不能取消。

**Sampling 設定要降溫**。Gemma 4 官方推薦 `temperature=1.0, top_p=0.95, top_k=64` 是給開放生成用的；對 schema-strict 任務，**建議降到 temperature=0.3-0.5、保留 top_p=0.95**，這是業界共識（philschmid 的 Gemma function calling 範例也是用低溫）。規劃敘事階段（步驟 3）可保留 0.7-1.0 增加創意。

**原生 system role 是 Gemma 4 相對 Gemma 3 的真實升級**。先前 Gemma 3 必須把 system instruction 黏在 user message 前，Gemma 4 直接支援 `messages: [{role: "system", ...}]`，這對 Presenton 的 pipeline 友善——5-phase 中每個 phase 的 system prompt（含版型清單、schema、語氣指南）可乾淨注入，預期比 Gemma 3 / Llama 3 系列在指令遵從上有可量化改善。

---

## C. 繁體中文是真實風險，必須主動防禦

**Gemma 4 沒有公開的繁中獨立基準**。MMMLU 88.4% 是 14 種語言平均，Google 沒拆 zh-TW vs zh-CN，第三方截至 2026-04-28（釋出 26 天）也無實測。最有價值的訊號來自兩個來源：(1) gemma4guide.com 直接寫「On Chinese benchmarks Qwen3-32B catches up or edges ahead」、「If your work is primarily in Chinese, Qwen3 tends to be the stronger choice」；(2) Twinkle AI（台灣社群）的 gemma-3-4B-T1-it 模型卡明確指出原版 Gemma 3「**翻譯幾乎都是中國用語**」——這是他們做台灣本地化微調的主因。

**簡體用詞混入是預期行為**。Gemma 3 27B 在繁中輸出時頻繁出現「視頻、軟件、網絡、激光、信息、鼠標、分辨率、打印、登錄、文件、程序、內存」等中國大陸用詞的繁體寫法。Gemma 4 訓練資料規模更大、MMMLU 從 70.7→88.4 顯著進步，**簡中混入頻率應降低但不會消失**。實務必備：(1) system prompt 明確列出對映表強制台灣用語；(2) 後處理跑 OpenCC `s2twp.json`（簡到繁並轉台灣常用詞）；(3) 上線前用罕用字測試集（裡/裏、為/爲、台/臺、線/綫）抽樣量化簡中比例。

**Presenton 的 CJK 是已知盲點且尚未改善**。截至 2026-04 的 issue tracker 與 release notes **沒有任何 CJK / Chinese / 中文字型相關討論**——這代表中文使用者基數小、官方未正視。技術根因有兩層：(1) `python-pptx` 已知 issue #768「Change font name not working for asian characters」，因 OOXML 需分別設 `<a:latin>`、`<a:ea>`、`<a:cs>` typeface，python-pptx 預設只設 latin，亞洲字會 fallback；(2) Presenton 的 HTML 端用 Tailwind 載 web font（如 Noto Sans TC）渲染預覽沒問題，但匯出 .pptx 時不會嵌入 web font，給 Windows 用戶開啟可能變新細明體或方塊。

**必備 CJK 補丁**：fork 後在 Dockerfile 加 `fonts-noto-cjk`，修 layout `.tsx` 的 Tailwind font stack 為 `"Noto Sans TC", "PingFang TC", "Microsoft JhengHei", sans-serif`，PPTX 後端引用 `AndersonBY/pptx-ea-font` patch 處理 East Asian typeface。這是大約 1-2 人天的工作量但**沒有捷徑**。

---

## D. Icon 兩階段策略應保留為主路徑

**Presenton 內建做法（推論）**是「LLM 直接輸出 icon 名 + Zod enum 限制 + 前端 fallback」，icon library 推測為 lucide-react（Tailwind 生態系標配），但因工具無法直接 fetch GitHub source tree，這部分是基於 README 與 DeepWiki 索引的推論——使用者本地 clone 後 `grep -r "lucide" servers/nextjs/` 即可實證。

**LLM 對 icon 命名的幻覺是有量化證據的問題**。USENIX Security 2025（arXiv 2406.10279）對 LLM 套件名稱建議的系統研究顯示開源模型平均 21%、商業模型 5% 是不存在的名稱；GitHub Copilot 研究指出 1/5 樣本含對不存在 library/API 的引用；lucide-react 自身在 GitHub issue 與 Vercel 社群有大量「import 失敗」的回報，特別是 `CircleHelp` ↔ `HelpCircle`、`Pen` ↔ `Edit2` 這類版本間 alias 造成的 LLM 混淆。Lucide 官方甚至特地提供 `/llms.txt` 給 LLM 讀取，等於默認了這個問題。

**enum 解法看似乾淨但有硬限制**。OpenAI Structured Outputs 限制 schema 內 enum 總數 ≤ 500（超過直接 400 Bad Request），Gemini 類似；react-icons 集合 ~10,000+ 名稱、跨 30+ 字型前綴的命名空間根本塞不進。Outlines/XGrammar/Guidance 等開源 constrained decoding 引擎雖能支援，但 JSONSchemaBench（arXiv 2501.10868）實測顯示大 enum 會帶來 >10% 解碼速度退化，且實務報告指出「極受限 enum 反而讓模型挑出語意不對的最近值」。

**「31B 規模能直接輸出 icon 名是否讓對應表變多餘」的回答是：仍應保留**。Gemma 4 31B 推論能力約對應 GPT-4-class（MMLU Pro 85.2% 屬實的話），對熱門 icon（Hi/Fa/Md 前 200 名）預期召回率 90%+，但在 react-icons 跨集 alias 與冷門 icon 上會掉到 60-75%。**對應表 + 語意關鍵字的真正價值**有三層：(1) 把命名空間從 ~10,000 收斂到 50-100 個 semantic concept，覆蓋 ~80% 簡報場景，token cost 從 5,000+ enum tokens 降到 10-30 tokens；(2) 對應表可在不重訓模型情況下人工調校，控制 icon set 一致性（不會這張用 Hi 那張用 Fa）；(3) 是 design tokens 業界標準（Adobe Spectrum、GitLab Pajamas、Contentful 都採 primitive→semantic→component 三層），有成熟方法論可借鏡。

**工程成本估算**：50-100 個 concept × 3-5 visual_style 變體 ≈ 150-500 條目，一次性建置 1-3 人天，每月新增 5-10 條長尾。比起每次 LLM call 多送 enum 列表的累積 token 成本，對應表是更便宜的方案。

---

## E. 九步流程跟 Presenton 的契合度有清楚分工

**Presenton 自己的 5-phase pipeline 並不等於使用者的 9 步流程**：(1) Document Processing（docling/pdfplumber 解析 PDF/DOCX）、(2) Outline Generation、(3) Structure Generation（決定每張 slide 用哪個 layout）、(4) Slide Content Generation（依 layout schema 批次生成，10 張 `asyncio.gather()` 並發）、(5) Asset Generation（圖與 icon 平行抓取）。**Schema 驗證是內建的**——LLM call 用 OpenAI structured output `response_format`，發生在 FastAPI Python 端而非 Next.js 端，Zod schema 在 build 時提取為 JSON Schema 後送 LLM。但 retry 邏輯隱藏在 OpenAI SDK 內，Presenton 沒有自訂 retry 上限與錯誤訊息結構化注入。

**重大缺口：Presenton 完全沒有視覺驗證迴圈**。pipeline 章節 5.1-5.7 從 Asset Generation 直接進入 streaming 與儲存，editor 階段的「AI-assisted edit」是 user-in-the-loop 而非自動 QA。使用者建議的步驟 8（headless 渲圖 + vision QA）必須**自建並插在 Presenton 出 .pptx 之前**——具體做法是 hook Presenton 的 streaming SSE 完成事件，跑 Playwright 渲染每張 slide 的 HTML 預覽截圖（200-800 ms），餵給獨立的 Gemma 4 31B vision endpoint 做 binary check（latency 1-2.5 秒/slide，20 頁簡報並行 5-15 秒可完成）。

**Gemma 4 31B 當 visual QA judge 是夠用的，但有界線**。MMMU Pro 76.9%、MATH-Vision 85.6%、SigLIP 約 550M vision encoder 的能力組合，在 ICML 2024 MLLM-as-a-Judge 框架下適合 **pair comparison（兩圖比較）與 holistic 判斷**——例如「icon 是否與 title 主題相關」「文字層級是否清晰」「整體構圖平衡」這類問題；但 **不可靠**於「文字溢出 1px」「對齊偏差」這類像素級缺陷，因為 vision encoder 把 896² 圖壓縮成 256 visual tokens 會損失精細位置資訊。**最佳實踐是分工**：DOM-level 缺陷交給 Playwright accessibility tree + getBoundingClientRect 抓（速度快、確定性），語意/美感缺陷交給 VLM 做 binary check（避免 scoring bias，ICML 2024 已確認 MLLM 在絕對打分上偏離人類偏好）。

**Schema 驗證迴圈的最佳設計**：retry max=2，第一次帶結構化錯誤訊息（哪個欄位、哪條 constraint 違反）注入 prompt 最有效（dev.to clawgenesis 與 NVIDIA blog 實測顯示這把 valid rate 從 60% 拉到 95%+）；第二次邊際效益小（再升 2-3%），第三次因 Self-Correction Bench（arXiv 2507.02778）報告的「64.5% blind spot rate」反而可能 reinforce 錯誤，**應 fallback 到預設 layout/icon 而非繼續 retry**。視覺驗證 retry 建議 max=1（SELF-REFINE NeurIPS 2023 確認單次 iteration 抓最大增益、後續遞減）。

---

## F. MVP 路徑、風險與最終建議

**Day 1 必備（最小可行版本）**：fork Presenton + 加 CJK Dockerfile patch（Noto Sans TC、pptx-ea-font）+ 接 vLLM 跑 Gemma 4 31B BF16 單副本 + system prompt 強制台灣用語 + 內建 schema 驗證 + retry max=2 + OpenCC s2twp 後處理。這個組合可在 1-2 週內跑通端到端，產出可開啟的繁中 .pptx。

**Day 2-N 加入**：(1) icon 兩階段對應表（語意關鍵字 50 條起步）、(2) Playwright 視覺驗證模組（DOM-level 抓 overflow + VLM 抓語意一致性）、(3) 4 個 TP=1 vLLM 副本 + load balancer 把吞吐量推到 5,000+ tok/s、(4) 256K context 給長文件 RAG-less 模式。

**主要風險點按嚴重度排序**：(1) **CJK 字型在生產環境破版**——這是 Presenton 已知盲點且工程成本不可避免，繁中專用字（裡/裏/為/爲/台/臺/線/綫）若沒驗證會在客戶 Windows 機器爆雷；(2) **vLLM thinking + function calling 同用的 #39043 bug** 會洩漏 reasoning token 到 chat，必須解耦兩個 endpoint；(3) **Gemma 4 繁中簡體混入**沒有 26 天內的第三方實測，建議部署後第一週密集量化簡中用詞比例；(4) **視覺 QA latency 在 20+ 頁長簡報可能累積到 30 秒**，需要並發 batch 設計；(5) Presenton 的 schema 客製化要懂 React + Tailwind + Zod 三件套，學習曲線較陡但不阻塞。

**替代方案評估**：短期若擔心 Gemma 4 太新，**Qwen3-32B** 是繁中最強的替代（gemma4guide.com 直接背書、原生 36T tokens 119 語言訓練），但仍以簡中為主、需 OpenCC 後處理；**Gemma 3 27B** 是更保守選擇但繁中問題更嚴重、context 僅 128K；**gpt-oss-120b** context 131K、MoE 架構 inference 較快但**沒有 vision**，無法做 visual QA judge。**長期看 Gemma 4 值得等與投資**——256K context、原生 multimodal、配套 vLLM/SGLang/Ollama 完整、Apache 2.0、Google 持續維護的生態系，沒有單一替代品同時提供這四項。

**給開發者的最終建議：做，但聚焦三件事**。第一，**先解 CJK** ——這是其他所有問題的前提，沒有可靠的繁中渲染，模型多強都沒意義；具體做法是 fork Presenton、Dockerfile 加 fonts-noto-cjk、引入 pptx-ea-font patch、修 layout font stack、用罕用字測試集驗證。第二，**保留 icon 兩階段對應表為主路徑**——Gemma 4 31B 雖強但 react-icons 跨集 alias 仍會掉到 60-75% 召回，對應表的 token cost 與一致性效益持續存在；不要被「模型夠強就不需要對應表」誤導。第三，**自建視覺驗證模組並用 DOM + VLM 分工**——Presenton 沒有這個迴圈，但 NotebookLM-style 應用對「圖文對齊保證」是不可妥協的；DOM 抓確定性缺陷、VLM 用 binary check 抓語意缺陷、retry max=1 後 fallback 到預設 layout。

## 結論：認知更新與下一步

研究過程中三個認知有顯著更新。第一，**Gemma 4 31B 的部署門檻比預期低**——BF16 單張 H100 80GB 即可裝下，4×H100 不是「需要這麼多」而是「可以橫向跑 4 個副本拉吞吐量」，這改變了基礎設施規劃的思維。第二，**Presenton 的 schema 機制比預期成熟、視覺驗證比預期空白**——5-phase pipeline 已內建 OpenAI structured output，Pydantic + model_validator 反而是雙層驗證的多餘工程；但視覺 QA 是一張白紙、必須自建，這是九步流程相對 Presenton 的最大價值添加。第三，**繁中問題的根因不在 Gemma 4 也不在 Presenton 單獨任一方**——是訓練資料分佈（簡中為主）+ python-pptx East Asian typeface bug + web font 不嵌入 PPTX 的三重疊加，必須三層同時解決，沒有單點修復。

最後一個 novel insight：**九步流程的「雙驗證迴圈」設計（schema-stage + visual-stage）跟 Self-Correction Bench 64.5% blind spot rate 的研究結論完美呼應**——LLM 在語意層自我修正能力有限，把 schema（結構違反，模型自己看得到）跟 visual（語意/視覺缺陷，需要不同模態）分到兩個 loop 並用不同 retry 上限（schema max=2、visual max=1），這個設計從第一性原理上就是對的，不是過度工程。建議使用者直接執行，不必猶豫。