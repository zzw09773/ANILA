# ANILA UI/UX 對標盤點:claude.ai × ChatGPT × Open WebUI

> 盤點日期 **2026-07-25**。目的:ANILA 將作為院內正式使用的平台,使用者體驗與延展功能不可明顯落後市面主流。
> 資料來源:Open WebUI **v0.10.2 源碼**(clone 實讀)+ claude.ai / ChatGPT 官方文件與媒體報導(web)+ ANILA 自身 code survey。
> ⚠ **有效期約 1–2 個月**:2026 年 7 月正逢 OpenAI 密集改版(Canvas 被移除又擴大、訊息版本 pager 消失、Chat/Work/Codex 三分桌面 app、GPT-Live 取代 Advanced Voice、Universal Search 上線),對標前請重新核對。凡標「未確認」者僅有二手來源佐證。

---

## 0. 結論先行

1. **ANILA 的對話核心已經接近三家水準**,不是落後很多:訊息編輯、重新生成(含 guided:更詳細/更簡潔/換個說法)、分支版本 pager、停止生成、複製、Markdown+語法高亮+複製鈕、LaTeX、Mermaid、表格、**續寫(Continue)**、thinking 摘要、訊息評分——這些都已實作。ANILA 甚至有三家都沒有的**多 Agent 並排比較 + Handoff Timeline 決策鏈可視化**。
2. **真正的落差集中在三塊**:①**輸入與導覽的「手感」**(無 `/` 斜線指令、無 `Cmd+K` 命令面板、無快捷鍵面板)②**對話組織**(無封存、無標籤)③**知識庫與主聊天割裂**(RAG 在另一個 SPA `anilalm`,主聊天 `anila-shell` 完全沒有 collection 概念)。第 ③ 項是與 claude.ai Projects / ChatGPT Projects / Open WebUI「資料夾即專案」體驗差距最大的一點,但它需要**產品決策**(見 §4)。
4. **治理面 ANILA 遠勝三家**:五級分類單向 latch、PKI 卡登、22 個 admin view、部門/稽核/用量、air-gapped 全離線 bundle。對標時**不可為了追功能而弱化這些**。
5. 最該向 Open WebUI 學的**不是功能清單,而是兩個架構決策**:統一 AccessGrant 授權表(一張表管所有資源的 ACL)與 Preview Access 稽核視圖(輸入使用者 → 列出他能讀到的所有資源)。後者對 ANILA 的資安稽核(CLAUDE.md §5 的機敏資料 No-Go)有直接價值。

---

## 1. 功能對照矩陣

圖例:✅ 完整 ・ 🟡 部分/僅後端 ・ ❌ 無 ・ ⭐ ANILA 獨有

### 1.1 對話核心

| 功能 | claude.ai | ChatGPT | Open WebUI | **ANILA** | 備註 |
|---|---|---|---|---|---|
| 訊息編輯 | ✅ | ✅(2026 改為覆蓋式,版本箭頭已移除¹) | ✅(訊息樹) | ✅ | ANILA 後端持久化 + 前端截斷重跑 |
| 分支版本切換 | ✅(箭頭) | 🟡(改為 Branch in new chat) | ✅(parentId/childrenIds 樹) | ✅ `< N/M >` pager | ANILA 保留舊分支 tail 可還原 |
| 重新生成 | ✅ | ✅(+Add details/More concise) | ✅ | ✅ + guided steer | |
| 續寫截斷回覆 | ❌ | 🟡 | ✅ Continue | ✅ ⭐附加不取代 | |
| 停止生成 | ✅ | ✅ | ✅ | ✅ | |
| 程式碼高亮/複製 | ✅ | ✅(+可編輯/Preview) | ✅(+執行) | ✅ | ANILA 表格也有複製(還原 Markdown) |
| 程式碼執行 | ✅ | ✅ | ✅ Pyodide/Jupyter/Terminal | ❌ | air-gapped 下 Pyodide 可行(需 bundle) |
| LaTeX / Mermaid / 表格 | ✅ | ✅ | ✅ | ✅ | ANILA 全部離線 bundle ⭐ |
| Artifacts(對話內產出物) | ✅ 高亮就地編輯+版本 | 🟡 Canvas 變動中¹ | ✅ 自動偵測 html/svg | 🟡 Studio 5 種但在另一 SPA | 見 §3 項目 4 |
| thinking/reasoning 呈現 | ✅ | ✅ | ✅ | ✅ | |
| 訊息評分 | ✅ | ✅ | ✅ + Arena/ELO | ✅ | |
| 訊息佇列(生成中可續打) | ❌ | ❌ | ✅ | ❌ | 小而有感 |
| 多模型並排比較 | ❌ | ❌ | ✅ + Merge 合併 | ✅ ⭐ + Handoff Timeline | ANILA 更透明 |

### 1.2 知識庫 / RAG

| 功能 | claude.ai | ChatGPT | Open WebUI | **ANILA** |
|---|---|---|---|---|
| 主聊天內直接用知識庫 | ✅ Projects | ✅ Projects | ✅ `#` 引用 + 資料夾綁知識庫 | ❌ **在另一個 SPA** |
| 集合/知識庫管理 | ✅ | ✅ | ✅ 巢狀目錄+增量同步 | ✅(anilalm) |
| 引用來源呈現 | ✅ | ✅ 行內引用 | ✅ **相關度% + 同檔分片分組** | 🟡 有 citations,無相關度 |
| 混合檢索+重排 | 未公開 | 未公開 | ✅ BM25+向量+CrossEncoder | 🟡 需查證 |
| 檢索參數可調 | ❌ | ❌ | ✅ | ❌ 前端寫死常數 |
| 外部向量庫直連 | ❌ | ❌ | ✅ 9 種 | ❌ pgvector 自建 |

### 1.3 組織 / 搜尋

| 功能 | claude.ai | ChatGPT | Open WebUI | **ANILA** |
|---|---|---|---|---|
| 對話搜尋 | ✅ 自然語言 | ✅ Universal Search(跨對話/檔案/圖片) | ✅ `Cmd+K` + `tag:`/`folder:` 前綴 | ✅ 伺服器端搜尋(無面板) |
| 命令面板 `Cmd+K` | 🟡 `Cmd+/` 快捷鍵面板 | ✅ | ✅ | ❌ |
| 資料夾 | ❌(Projects 代替) | ❌(Archive+Projects 代替) | ✅ **資料夾=專案**(可綁系統提示+知識庫) | 🟡 UI 設定層 folder |
| 標籤 | ❌ | ❌ | ✅ | ❌ |
| 封存 Archive | 🟡 未確認 | ✅ | ✅ | ❌ |
| 釘選 | ✅ | ✅ | ✅ | ✅ star |
| 分享連結 | ✅ **快照脫敏**(不含附件/工具原始資料) | ✅(無精細權限,可被匯入² ) | ✅ 站內/社群三級 | ✅ 匿名唯讀 + classified 禁分享 ⭐ |
| 匯出 | ✅ 全量 ZIP | ✅ | ✅ JSON(含訊息樹) | ✅ JSON/Markdown |

### 1.4 輸入體驗

| 功能 | claude.ai | ChatGPT | Open WebUI | **ANILA** |
|---|---|---|---|---|
| `/` 斜線指令 | ✅ | ✅ `/agent` `/plan` `/goal` | ✅ + Prompt 庫(16 種輸入型別、版本歷史) | ❌ |
| `@` 提及 | 🟡(第三方擴充³) | ✅ 檔案 | ✅ 模型 | ✅ agent ⭐ 可 bypass router |
| `#` 引用知識 | ❌ | ❌ | ✅ | ❌ |
| 快捷鍵面板 | ✅ `Cmd+/` | ✅ `Ctrl+/` 可自訂 | ✅ ShortcutsModal + registry | ❌ 零星快捷鍵 |
| 草稿保存 | 未確認 | 未確認 | ✅ | ✅ per-conversation |
| 語音輸入 | ✅ 口述+Voice Mode | ✅ GPT-Live 全雙工 | ✅ STT+TTS+通話 | ✅ ASR 串流 |
| 附件拖放/貼上 | ✅ | ✅ | ✅ | ✅ |

### 1.5 延展與治理

| 功能 | claude.ai | ChatGPT | Open WebUI | **ANILA** |
|---|---|---|---|---|
| 自訂模型/角色 | ✅ Styles | ✅ 自訂 GPT + GPT Store | ✅ Model Builder(綁知識庫/工具/`{{USER_GROUPS}}`) | 🟡 agent 註冊(admin) |
| 記憶 | ✅ 分類清單+白話編輯+暫停/重置 | ✅ 可直接改摘要+變更通知 | ✅ | ✅ 檢視+刪除(**不能編輯**) |
| 排程任務 | ❌ | ✅ Tasks(集中管理頁) | ✅ Automations(RRULE) | ❌ |
| MCP / 工具生態 | ✅ Connectors+Skills | ✅ MCP+Connectors | ✅ MCP(admin only)+OpenAPI tool server | 🟡 agent 機制 |
| RBAC | 🟡 Team/Ent | ✅ | ✅ **統一 AccessGrant 表**+群組+加法制 | ✅ user/developer/admin + 部門 |
| 存取稽核視圖 | ❌ | ❌ | ✅ **Preview Access**(逐人列出可讀資源) | ❌ |
| 用量分析 | ✅ 角色分層(Owner 看 Spend) | ✅ | ✅ 可依群組篩選 | ✅ 依模型/部門/使用者 |
| SSO / SCIM | ✅ Ent | ✅ | ✅ LDAP/OAuth/SCIM 2.0 | ✅ OIDC + **PKI 卡登** ⭐ |
| 分類分級 | ❌ | ❌ | ❌ | ✅ **五級單向 latch** ⭐ |
| air-gapped | ❌ | ❌ | ✅ 明文支援 | ✅ 全離線 bundle ⭐ |
| i18n | ✅ | ✅ | ✅ 61–63 語言 | ❌ 硬編繁中 |
| PWA / 行動版 | ✅ 原生 App | ✅ 原生 App | ✅ PWA(可白牌化) | ❌ 桌面單一佈局 |

¹ 社群回報為主,官方無對應說明頁,對標前需實機核對。
² ChatGPT 分享連結的瀏覽者可把整段對話匯入自己帳號,原作者刪連結後副本仍存在——**這是反面教材,ANILA 不要抄**。
³ claude.ai 的「@提及過去對話」實為第三方瀏覽器擴充,非官方功能。

---

## 2. 缺口排序(價值 × 可行性)

| # | 缺口 | 價值 | 風險/工程量 | 判斷 |
|---|---|---|---|---|
| 1 | `/` 斜線指令 | 高(三家都有,天天用) | 低(純前端,已有 `@` 基礎) | **今晚做** |
| 2 | `Cmd+K` 命令面板 + 快捷鍵面板/registry | 高 | 低(純前端,搜尋 API 已有) | **今晚做** |
| 3 | 對話封存 + 標籤 | 中高(長期使用者側欄會爆) | 低(跟隨 folders 的 UI 設定層) | **今晚做** |
| 4 | Artifact 版本歷史 UI | 高(claude.ai 核心設計) | **極低**(後端 `ArtifactVersion` 已存在,前端沒接) | **今晚做** |
| 5 | Citations 相關度 + 同檔分片分組 | 高(RAG 可信度) | 低(後端已有 score) | **今晚做** |
| 6 | **主聊天整合知識庫** | **最高** | 中(前端為主)**但需產品決策** | **見 §4,不擅自動** |
| 7 | Preview Access 稽核視圖 | 高(資安稽核直接需求) | 中(需後端彙總 API) | 建議下一批 |
| 8 | 資料夾升級為「專案」(綁系統提示+知識庫) | 高 | 中 | 依 #6 的決策而定 |
| 9 | 記憶可**編輯**(現在只能刪) | 中 | 低 | 建議下一批 |
| 10 | 排程任務(Automation) | 中高(定期報告機器人) | 中(需 scheduler) | 建議下一批 |
| 11 | 訊息佇列(生成中續打) | 中 | 低 | 建議下一批 |
| 12 | 檢索參數可調 | 中 | 低 | 建議下一批 |
| 13 | 圖片生成專屬入口 | 中 | 低 | 建議下一批 |
| 14 | 響應式 / 行動版 | 中高 | **大**(JSX 全 inline style,無斷點) | 需專案級規劃 |
| 15 | i18n | 中(內網場景較低) | 大(架構級) | 需專案級規劃 |
| 16 | 統一 AccessGrant 表 | 高(長期治理) | **大且高風險**(動治理核心) | **絕不夜間做** |
| 17 | 程式碼執行(Pyodide) | 中 | 中(需 bundle WASM) | 需資安評估 |
| 18 | 訊息樹重構(取代 revisions) | 中 | 大 | 現有 pager 已夠用,低優先 |

---

## 3. 今晚實作範圍(本 PR)

1. **`/` 斜線指令**:重構 `@` mention 為「依觸發字元分派」的統一 command 架構(參考 Open WebUI `CommandSuggestionList`),新增 `/翻譯` `/摘要` `/公文` `/清空` `/快捷鍵` `/搜尋`(重用既有快捷動作,不重造)
2. **`Cmd+K` 命令面板**(搜尋對話 + 跳轉動作 + `folder:`/`starred:`/`archived:`/`tag:` 前綴過濾)+ **`Cmd+/` 快捷鍵面板** + **集中式 shortcuts registry**
3. **對話封存 + 標籤**(持久化跟隨 folders 的 UI 設定層;classified 限制不弱化)
4. **Artifact 版本歷史 UI**(接既有後端 `ArtifactVersion`)
5. **Citations 相關度 + 同檔分片分組 + 分類徽章**

實作紀律:純前端、不動後端契約、**不引入新 npm 依賴**(air-gapped)、零 CDN、`npm run build` 為驗收門檻。

---

## 4. 需要 user 決策的事項

### 4.1 主聊天要不要接知識庫?(最高價值的缺口)

**現狀**:後端其實已經備好——`conversation.collection_id` 欄位存在、`retrieve_and_seal` 已接在主 chat 流程、`anila-shell` 連 citations 抽屜都做好了。**唯一的阻擋是後端的明文契約**:

> `collection_id`: REQUIRED when `origin='anilalm'` … **MUST be None for any other origin (anila-ui has no collection concept)**. The endpoint enforces this contract; clients passing the wrong combination get 400.
> — `services/csp/app/api/conversations.py:31-35`

放寬它等於決定「兩個 SPA 的對話要不要合流」,會影響 anilalm sidebar 的過濾語意。三個方案:

| 方案 | 做法 | 優點 | 代價 |
|---|---|---|---|
| **A. 合流** | 允許 `anila-ui` 帶 `collection_id`,主聊天加 collection 選擇器 | 體驗最接近 Projects;一套聊天 UI | anilalm sidebar 過濾要改;兩 app 的對話混在一起 |
| **B. 平行** | 主聊天新增自己的「知識庫模式」,用新 origin 值區隔 | 不動 anilalm 語意 | 資料模型多一種狀態 |
| **C. 收斂 UI** | 把 anilalm 的工作區「嵌入」主 shell(iframe 或路由整合),不動資料模型 | 零後端風險 | 只是視覺整合,仍是兩套狀態 |

我的建議:**A**,但需要你確認「兩個 SPA 未來的定位」——若 anilalm 長期是獨立的 NotebookLM 式產品,則 B 更安全。

### 4.2 其他待決

- **記憶可編輯**:目前只能刪除。claude.ai/ChatGPT 都允許編輯。這牽涉「使用者可否改寫 AI 對自己的認知」的治理立場,涉密環境可能刻意不給。
- **程式碼執行**:Pyodide 是瀏覽器端 WASM,air-gapped 可行,但等於在使用者瀏覽器內給模型執行環境,需資安評估。
- **統一 AccessGrant**:長期治理最值得的重構,但會動到分類分級與部門權限的核心,建議獨立專案 + 完整測試,不可夾帶。

---

## 5. ANILA 的獨有優勢(對標時不可弱化)

1. **五級分類單向 latch**(`runtime/classified.js`)+ 連動禁複製/編輯/分享
2. **PKI 自然人憑證卡登入**(含 CRL)
3. **22 個治理 admin view**(部門、Trusted Hosts、Service Clients、分類盤點、Evaluator)
4. **air-gapped 全離線**:mermaid/highlight.js/字體全 bundle,零 CDN
5. **多 Agent 並排比較 + Handoff Timeline**:把路由決策鏈攤開,比三家的黑盒路由透明
6. **續寫(附加不取代)**
7. **Studio 主題精靈**(受眾/語氣/格式/長度四問 → 推薦視覺主題)
8. **記憶治理**:facts / chunks 分離刪除、逐筆帶分類等級與 compartment

---

## 6. 方法論與可信度

- **Open WebUI**:讀 v0.10.2 源碼(30 個 backend router、45 個前端路由頁面),證據為 file:line,可信度最高。
- **claude.ai**:support.claude.com 官方文件為主;程式碼區塊複製鈕/行號、表格樣式、原生 `@` 提及、完整斜線指令清單、草稿保存、封存功能**皆未確認**。
- **ChatGPT**:help.openai.com 對自動抓取回 403,改用搜尋摘要 + 一手科技媒體交叉比對;Canvas 現況、訊息版本 pager 是否真的移除、GPT-5.6 上線狀態**皆未確認**,且 2026-07 是密集改版期。
- **ANILA**:自身 code survey,file:line 為證。

建議:對標前對「未確認」項目以實機帳號覆核,尤其 ChatGPT 部分。
