# ANILA 開發引路路牌

> **這份文件是 ANILA 後續開發的單一路線圖 SSOT。** 它綜合了四份分析報告、一輪獨立實證驗證，以及一輪外部審查（GPT-5.6）與對該審查的逐點查證，回答三個問題：現在站在哪裡、下一步往哪走、什麼絕對不能先做。
>
> | 欄位 | 內容 |
> |---|---|
> | 版本 | **v1.1.1**（v1.0 → v1.1 → v1.1.1 的變更見 §10） |
> | 日期 | 2026-07-10 |
> | 程式基線 | `main@f661f5e8` ＝ `origin/prod-intranet-card@2d1bf08a`（**兩者程式碼位元組相同**，只差 `.env.example`；見 §2.4） |
> | 定位 | 路線圖與決策路牌。**不重複** `AGENTS.md`（架構／分支／測試矩陣）與 `CLAUDE.md`（內網運維知識）的內容 |
> | 現況判定 | **機敏資料 production：No-Go。** 「何時變成 Go」由 **Gate 6（Production Acceptance）** 定義——這是 v1.1 補上的最大缺口 |

---

## 2026-07-17 執行 checkpoint（Gate 5 已完成；Gate 6 維持 NO-GO）

> 本節是目前可驗證狀態；下方較早日期的數字與判定保留作歷史證據。若有衝突，以本節與最新 handoff 為準。

- Gate 5 已完成並 merge（base `bca5af0…`）。Gate 6 最新 engineering work 仍在本地未提交；PR [#32](https://github.com/zzw09773/ANILA/pull/32) 的 remote head 仍為 `5c5a1b2…`，狀態為 `OPEN / Draft / MERGEABLE`。舊 head 的 23 個 checks 雖為綠燈，**不涵蓋本地未提交 diff**；本地 CI 與 exact final Sol／Fable review 均不得宣稱已完成。
- Provider authority v2 採 Option C：支援 direct `external_governed`、`internal_shim`＋external upstream、`internal_isolated` 等 locality；canonical target 僅接受精確 `host:port` 或 Domain/FQDN。external gRPC embedding 必須是 `host:port`，含糊的 numeric IP 一律拒絕。runtime admission、frozen registry snapshot、receipt/replay 與 `image-primary` 已接線；這仍是工程證據，不是 P9 production acceptance。
- donkernet Revision D：沒有任何 Compose project 擁有 `anila-models-net`，所有 consumer 都宣告 `external: true`；helper 負責建立 bridge＋`Internal=true` 網路、read-back 與 fail-closed 檢查，且永不自動刪除／重建。此次 canonical live network read-back 為 `Driver=bridge, Internal=false, Containers={}`，因此是 deployment blocker；禁止 auto-fix 或 delete。
- P9 exporter v2 已具 provider-authority snapshot、redaction 與 fail-closed；但 production packet capture／deny、usage reconciliation 與五方 sign-off 仍缺，`gate6_pass=false`。
- 模型 live smoke：port `7000` 的 `/v1/models` 為 `gpt-oss-20b`，chat 回傳 `ANILA_GATE6_OK`；port `9001` Triton gRPC ready，1×4096 embedding 為 finite。未來以 external model 為 primary，donkernet 只承載明確列出的少量 internal model。
- CHT synthetic focused suite 為 **66 passed**；實體卡／reader／vendor HiPKI 與正式矩陣仍是 P8 blocker。

### 目前工程驗證摘要（不等於 P0–P9 acceptance）

| 範圍 | 最新結果 |
|---|---|
| policy／deployment／Gate suites | **384 passed / 2 skipped / 88 subtests** |
| CSP | **1594 passed / 40 skipped** |
| ingestion-worker | **252 passed / 10 skipped** |
| anila-core | **903 passed / 10 skipped** |
| anila-security／Studio | **95 passed / 1 skipped**；**587 passed / 5 skipped** |
| Flux／Flux-agent／embedding proxy | **15 passed**；**71 passed**；**49 passed** |
| true PostgreSQL migration／receipt／session／resume | head `r1_0031`；`0031` downgrade→upgrade PASS；**3 receipts、5 sessions、13 resumes** |
| Ruff／py_compile／Bash | **63 Python**；**63**；**7 shell scripts `bash -n` PASS** |
| Compose／overlay | dev、models、example 與 prod synthetic locked-digest parse PASS；external overlay verify PASS |

**Gate 6 仍是 NO-GO**：P0–P9 的 production／external evidence、P0 signed profile、P1 restore、P2 fault drill、P3 七日觀測、P4 release envelope、P5 全鏈 matrix、P6 獨立具名人類覆核、P7 法務授權、P8 實體卡，以及 P9 production packet／deny／usage／五方簽核，均不可由上述工程測試取代。

## 怎麼用這份文件

| 你是 | 讀這幾節 |
|---|---|
| 決策者（要不要上線？要投多少？） | §1 一句話 → §3 現況判定 → §5.1 Gate 總表 → §6.1 三級資料門檻 → §8 人力現實 |
| 開發者（我今天該做什麼？） | §5 我在哪個 Gate → §6 不可倒置的排序規則 → §7 別再撿的假警報 |
| 未來的 AI session | 全部，特別是 §6、§7、§9（未驗證項）、§10（外部審查裁決）。動手前先讀 `AGENTS.md` 與相關子專案程式碼 |

**更新規則**：每個 Gate 通過後更新 §3 與 §5 的狀態欄。新發現的高危項先進 §3，經驗證後才排進 Gate。**不要在沒有實測證據的情況下往 §3 加東西。**

**§7 的維護規則（v1.1 新增，來自一次真實教訓）**：把一條宣稱放進「假警報」之前，先問「**我的驗證涵蓋了所有呼叫路徑嗎？**」v1.0 曾把 `document_ids` recall bug 判為不可達，理由是「互動搜尋不帶 `document_ids`」——但沒查 Studio 的 job 提交路徑，而那四條路徑全部都帶。**一條錯的假警報比沒有假警報清單更危險**，因為它會讓未來的人不再檢查。詳見 §7 表下的說明。

---

## 1. 一句話

> **ANILA 最不缺的是功能想法，最缺的是「任何人都繞不過的共同交易」。**
>
> 而在補那條交易之前，得先承認兩件事：**今天在內網跑的東西，還不能安全地放機敏資料**；而且在 v1.0 之前，**沒有人定義過「什麼時候才可以」**。

四份報告裡有三份在回答「怎麼把 agentic 未來蓋好」，只有一份在回答「現在跑的東西安不安全」。**第二個問題的答案是 No，而它必須先被回答。** v1.1 補上第三個問題：**No 要怎麼變成 Yes**（§5 Gate 6）。

---

## 2. 證據基礎

### 2.1 報告血緣

```
2026-07-06  Router 分析（5 份）        範圍：只有 anila-core / Router
                    │                  主張：決策結構化 + 治理內建化 + 執行可靠化
                    ▼
2026-07-10  系統變更計畫書              範圍：Router + CSP + Agent + Shell + Ingestion
                    │                  主張：契約先行、逐段切換（WS0–WS8）
                    │
2026-07-10  prod-intranet-card 分析     範圍：整個分支的營運安全
                    │                  主張：No-Go；縮核、立約、接線
                    ▼
2026-07-10  審查報告（本團隊）           範圍：驗證上述三者 + 獨立深度檢視
                    │                  主張：同意 No-Go；修正誇大；裁決架構分歧
                    ▼
2026-07-10  roadmap v1.0 → GPT-5.6 外部審查 → 四路獨立查證 → **v1.1**
                                       產出：新增缺陷、修正誤判、逐元件裁決模型路徑
2026-07-10  v1.1 一致性覆核 → **v1.1.1**
                                       產出：修正 Gate 相依、Agent 模型合規、客觀 Go 條件
```

### 2.2 報告清單與驗證結論

| 報告 | 路徑 | 範圍 | 驗證結論 |
|---|---|---|---|
| Router 分析（5 份） | `docs/gpt-report/2026-07-06-anila-core-agent-router-analysis/` | anila-core / Router 大腦 | **診斷正確**。三主線（RouteDecision／PolicyGate／StreamBridge）是後續計畫書的骨架 |
| 系統變更計畫書 | `docs/gpt-report/ANILA_系統變更計畫書.md` | 平台級「怎麼蓋」 | **有條件核准**。40+ 條技術宣稱絕大多數成立；三套測試基線一字不差重現。需修訂 8 項 |
| prod-intranet-card 分析 | `docs/gpt-report/ANILA_prod-intranet-card_完整專案分析與決策建議.md` | 分支級「安不安全」 | **同意 No-Go**。每個可量測數字都重現了。但有 9 處誇大／誤判，且**低估**了最嚴重的一條 |
| 審查報告（本團隊） | `docs/gpt-report/ANILA_系統變更計畫書_審查報告.md` | 驗證 + 獨立檢視 | 本路線圖 v1.0 的直接依據 |
| GPT-5.6 對 v1.0 的審查 | （對話內，未落檔） | 路線圖本身的結構 | 依 top-level 與子項逐條裁決；多數採納，部分修正事實或處方。逐點裁決見 §10 |

### 2.3 驗證方法（為什麼可以信這份路線圖）

- **10 路平行領域檢視**：每路先獨立審查程式碼找缺陷，**之後才**讀對照報告給判定（避免錨定）。
- **對抗式驗證**：對每條 CRITICAL/HIGH 派一個「預設它是錯的」的驗證者盡力推翻；逐條結果保留在 §7，不再使用容易因子項重疊而失真的數量摘要。`document_ids` 一條在 v1.1 被外部審查翻回；存活的唯一 CRITICAL 經三重確認。
- **四套乾淨 venv 實跑**：anila-core、anila-agent、ingestion-worker、CSP。Windows 11 / Python 3.11.9 / pytest 9.1.1 / ruff 0.15.21 / mypy 2.2.0。
- **可量測數字全部重現**：core ruff 22 errors、core strict mypy 98/26、ingestion 146 passed、CSP card 子集 12 failed、version triple `0.14.0/0.7.0/0.1.0`、規模數字（services 552／packages 319／apps 230／infra 92／291 測試檔／14 服務／CSP 54 tables＋54 migrations）。
- **v1.1／v1.1.1 追加**：對 GPT-5.6 的每一條**新事實宣稱**派獨立查證代理回程式碼求證（nginx 認證面／share 讀取端／memory 旗標｜`document_ids` 呼叫路徑／瀏覽器端 RAG｜Task 快照與身分模型｜Router 與官方 Agent 的模型路徑）。**不接受任何未經 file:line 佐證的宣稱，包括 GPT 的與我自己的；也不把操作指南當成 runtime enforcement。**

### 2.4 一個必須先講清楚的事實：分支模型的認知已過時

**實測（2026-07-10）：六條 downstream 分支中，五條與 `main` 的差異只有 `.env.example` 一個檔案，所有程式碼位元組相同。** 只有刪減型的 `trial-military` 有真實程式碼差異（30 檔）。

| Branch | 與 `main` 的非文件差異 |
|---|---|
| `dev-public`／`prod-public-passwd`／`dev-military`／`prod-military-passwd`／`prod-intranet-card` | **只有 `.env.example`** |
| `trial-military` | 30 檔（刪除 `DeveloperAgentsView.vue`、`DeveloperGuideView.vue`、`OutputsPage.tsx`、`MindmapTree.tsx`、`anila-ops.sh`） |

- `AGENTS.md` §3 描述的「card/SSO 永久 fork 熱區」**已不成立**（且其清單裡的 `api/auth.py` 與 `AuthProvidersView.vue` 根本不存在）。card/SSO 程式碼已收進 `main`，由旗標切換。→ **已於 2026-07-10 更正 `AGENTS.md` §3 與 `CLAUDE.md`。**
- 常被引用的「12/11 divergence」是 **commit 圖差異**（同語意不同 SHA 的 cherry-pick），**不是內容差異**。porting 負擔目前極低。
- **⚠ 順帶查出的交付缺口**：`AGENTS.md` 舊表宣稱 `prod-public-passwd` 已移除 code-server、`dev-military` 已移除 code-server/n8n/GitLab、`prod-military-passwd` 已移除 n8n/GitLab。**實測：七條分支的 `platform.yml` 全部都含這三個服務。** 那些是**未落實的交付要求**，不是現況。這直接放大 §3.1 的 C1——那個暴露分類 DB dump 的 code-server，出貨在**每一條**分支上，包括文件明文要求移除它的軍用分支。
- **真正的風險已經轉移：正式部署身分由可變的環境設定決定，而不是由不可變的簽章 release artifact 決定。**
  > **v1.1 修正**：v1.0 把這條的解法排在 Gate 6 條件式能力（「之後」）。**那是不一致的**——你不能一邊說「這是真正的風險」，一邊把它排在最後一個條件式 Gate。posture assertion 已提前到 **Gate 1（F6）**，完整簽章 release 進 **Gate 6 acceptance**。

---

## 3. 現況判定：高危清單

> 全部經對抗式驗證。嚴重度是**裁決後**的等級，不是初始評級。標 🆕 者為 v1.1 或 v1.1.1 新增；每列會寫明來源。

### 3.1 CRITICAL（1）

| # | 問題 | 證據 | 為什麼是 CRITICAL |
|---|---|---|---|
| C1 | **Code Server 工作區暴露整庫分類資料的 superuser dump** | `platform.yml:506-508`（RW 掛 repo root，只遮 `.env` 與 `server.key`）＋ `anila-ops.sh:42,239,244,248`（`BACKUP_DIR` 預設落在工作區內；`pg_dump -U csp` 繞過 RLS；另複製 `.env` 與 `secrets/*.pem`）＋ `intranet-deploy.sh:131`（第二份未遮蔽 `.env` 副本）＋ `runbooks/intranet-zero-to-prod-guide.md:174-177`（**官方 runbook 要求每日 02:30 cron 備份**）＋ `nginx/anila.conf:311-334,697-720`（兩個 server block 皆無 `auth_request`）＋ `platform.yml:489`（SSO gating 是**未做的 TODO**，服務卻已解凍）＋ 無 `profiles:`（預設啟動） | **這不是誤配置，是照著 runbook 部署的預設狀態。** 任何取得 `CODESERVER_PASSWORD` 者可用瀏覽器下載整個五級分類知識庫的 dump（完全繞過 RLS），並從 `env.bak` 取得 postgres superuser 密碼＋`SECRET_KEY`＋`CSP_SERVICE_TOKEN`。`secrets/` 目錄由 UID 1001（＝codeserver）擁有且可寫 → 可替換 JWT keypair → 簽發任意 owner JWT。`.gitignore` 有 `/backups/`，但**容器掛載不看 gitignore** |

### 3.2 HIGH（7）

| # | 問題 | 證據 |
|---|---|---|
| H1 | **營業秘密對話可繞過分享封鎖，未登入即可讀全文** | 五級定序 `無機密(0) < 營業秘密(1) < 機密(2) < 極機密(3) < 絕對機密(4)`（`schemas/contracts/classification.py:39-43`，「排序不可變」是契約）；鏡射規則 `classified = level >= 機密`（`modules/policy/service.py:214-226`）→ 營業秘密的 `classified=False`；`create_share` 只擋 `conv.classified`（`conversation_service.py:384-388`）；**讀取端 `public_share.py:58` 同樣只讀 `conv.classified` boolean**；`GET /public_share/{token}` 註解明寫 "No auth required"（`public_share.py:58-67`）。`ENABLE_PUBLIC_SHARE` 預設 `True`（`config.py:18`），compose 從未關閉。**附帶**：`expires_at` 可為 NULL（`models/conversation.py:77`）→ 可建永久連結；撤銷靠硬刪 row（`conversation_service.py:423-429`），無 `revoked` 欄位 |
| H2 | **Memory 分類洗白 ＋ stored prompt injection** | `UserFact` 無分類欄位（`user_memory.py:41-90`，對照 `:129` 的 chunk **有**）；`persist_turn` 每 turn 無條件抽 fact；`_format_block` 無條件注入 system prompt；`enforce_model_ceiling` 對注入的 fact 分類全盲。chunk 通道有 no-read-down latch，**facts 通道完全沒有**。延伸：`_extract_facts`／`_embed` 直呼固定模型（`memory_service.py:200,433`），**完全不經 ceiling** → 絕對機密內容每 turn 被送去 gemma。**🆕 且無停用旗標**：`config.py` 全檔無 `ENABLE_MEMORY`；`proxy.py:519` 無條件呼叫 `_inject_memory`，`proxy.py:654,720,825,859` 四個出口無條件 `_schedule_memory_write`。要止血只能改 code |
| H3 | **Governance UI 建立服務必 422** | UI 送 `url`（`PlatformLinksView.vue:380-398`），backend 必填 `entry_url`（`schemas/registered_service.py:49-65`）；`registryMode` 在 prod 必為 true（`router.py:53` 無條件掛載） |
| H4 | **多數專案入口卡片 launch 恆 400** | auto-seed 6 張卡片有 5 張是 relative URL（`platform.yml:135-139`，字面 YAML、`.env` 無法覆寫）；`auto_seed.py:56,78` 逐字存不正規化；`services.py:110-113` 對非 http(s) scheme 直接 400，且 `:379` 的 URL 驗證在 `:383` 權限檢查**之前**（未授權者收到 400 而非 403） |
| H5 | **Air-gap bundle 缺 image closure，全新內網主機必然部署失敗** | 匯出腳本只 build/save 7 個 image（`build-and-export-for-intranet.sh:54,80-88`），`flux2-dev-agent` 只有 `build:`、無 `image:`、無 `profiles:`，而 `intranet-deploy.sh:258` 跑 `--no-build`。根因：commit `4674e70` 新增服務時未同步更新匯出清單 |
| 🆕 H6 | **`/uploads/` 之下（除 `ingestion/` 外）全部未認證對外** | `anila.conf:453-458`（443）與 `792-797`（4443）：`location /uploads/ { alias ...; try_files $uri =404; }`，**無 `auth_request`**。唯一 deny 是 `/uploads/ingestion/ → return 404`（`449-451`、`788-790`）——設計者知道要擋 ingestion，但 **`/uploads/flux/` 沒擋**。生成圖寫入鏈：`flux2-dev-agent/app/image_store.py:50-53`（`{uuid4().hex}.{ext}`）→ `platform.yml:426`（`../../share/uploads/flux:/share/flux`）→ nginx `platform.yml:293` 掛同一 host 目錄。**唯一屏障是 uuid4 檔名（~122-bit），無登入、無過期、無撤銷**——URL 一旦流入 log／referer／貼文即永久可讀。**適用範圍**：凡部署 `flux2-dev-agent` 或 `anila-studio` 圖像生成的環境。H5 只能證明新 air-gap bundle 缺 image，**不能證明既有內網沒有 image；現有內網實際暴露狀態未知** |
| 🆕 H7 | **anilalm 正式聊天是瀏覽器端 RAG：檢索內容的分類從不進入 server 的 latch** | `WSChat.tsx:287-296` 瀏覽器直呼 `POST /api/ingestion/collections/{id}/search` 取 chunks → `WSChat.tsx:171-226,319-323` **在瀏覽器把 chunk 內容拼進 system prompt** → `chat.ts:87` 打 `/v1/chat/completions`。此路**不建立 Task**（`chat.ts:19-27,36-43` 無 taskId；`task_link.py:119-121` 讀不到 `X-ANILA-Task-Id` 即回 `None`）。**精確表述（修正 GPT 的誇大）**：`enforce_model_ceiling` **仍然執行**（`proxy.py:789-795`，註解自承涵蓋 legacy traffic），但等級來源退化為 conversation latch（`ceiling.py:44-67`），且模型未設 `classification_ceiling` 時整段 no-op（`ceiling.py:82-84`）。**缺口不是「沒 gate」，是「gate 的輸入看不到瀏覽器注入了什麼」**——server 從未看到那批 chunk，因此不會 latch、不會升級 |

### 3.3 MEDIUM（13）

| # | 問題 | 說明 |
|---|---|---|
| M1 | Task 永不收斂終態（`BLOCKED_BY_POLICY` 生產端完全不可達） | 治理帳完整性問題，未繞過 auth 或 classification ceiling |
| M2 | Studio status 回報 `done` 但 download 404（五個 pipeline 全中；不需 restart，正常 prune／FIFO 驅逐即觸發） | v1.0 未指派 Gate。**v1.1 已排入 Gate 3** |
| M3 | anilalm artifact 跨使用者殘留（`OutputsPage.tsx:116-124` 跨 collection 攤平，下一位卡片使用者**不需 devtools、直接在 UI 就看到**前一位的產出標題） | 殘留的是 metadata/titles，非機敏內文。v1.0 未指派 Gate。**v1.1 已排入 Gate 3** |
| M4 | alembic 失敗被吞成 warning 後 `create_all()` 續啟；`/health` 仍回 200 | `create_all(checkfirst=True)` 使既有帶 RLS 的表被**跳過**而非脫除 |
| M5 | `CARD_DEV_SKIP_NONCE_BINDING` 缺 production startup hard gate | 該旗標不在 compose 的 csp `environment:` 區塊 → 在 `.env` 設它傳不進容器，投遞路徑不存在 |
| 🆕 M6 | **`document_ids` recall bug 在 Studio 四條路徑上可達**（**v1.0 誤判為假警報，見 §7**） | `search.py:507-511` 全域 top-k（SQL 不帶 document_ids）→ `search.py:517-519` app 層 post-filter（註解自承）。呼叫者：Report `generators.ts:243`→`studio.ts:451`→`report_runner.py:468-472`；Mindmap `:398`→`:492`→`mindmaps.py:498-503`；Infographic `:440`→`:568`→`infographics.py:511-516`；Datatable `:483`→`:613`→`datatables.py:445-450`。top_k 僅 8–15（`studio.ts:452/494/571/614`）→ 指定少數文件時極易被其他文件的 chunk 佔滿。**Report 空命中直接 `raise RuntimeError("找不到相關內容")`**（`report_runner.py:483-487`）。正解 `similarity_search_per_document`（`pgvector_store.py:239-287`，`WHERE document_id = ANY($2)` + `RANK() OVER (PARTITION BY document_id)`）**已存在但只被 relation 展開用到**。Slides 豁免（`studio_retrieval.py:57` 不帶 document_ids） |
| 🆕 M7 | **圖像路徑直連模型後端，不經 CSP、不寫 `token_usage`** | `flux_client.py:88-97` 直接 POST `{endpoint}/v1/images/generations`，`:64-68` 帶自己的 `FLUX_API_KEY`；endpoint 來自 `backend_resolver.py:35-45`（CSP `image-primary` 的 `endpoint_url`，或 env `FLUX_BACKEND_URL`＝`platform.yml:406` 寫死 `172.16.120.35:30010`）——**兩者都是裸模型後端 URL，CSP 只當登錄簿、不在資料路徑上**。`anila-studio` 同樣自帶 `FLUX_API_KEY` 直連（`platform.yml:355-359`）。這是 G8「GPU-分鐘成本在帳本上隱形」的**根因**：不是估算不準，是呼叫根本不經 CSP。對照組：LLM（`router_server.py:2005`）與 embedding（`platform.yml:220`）都已收斂到 `csp:8000/v1` |
| 🆕 M8 | **卡登 challenge nonce 無一次性消費，120 秒視窗內可重放** | `card_auth_service.py:64-83` 簽 stateless JWT（`exp=120s`）；`:86-105` `decode_card_challenge` **只驗簽章/exp/aud，無 store／consume／delete**；docstring `:5-8` 明示「stateless，不依賴 Redis / DB nonce store」。防護只靠 120s TTL ＋ CMS eContent 必須等於 nonce（`card_auth.py:186-187`）。**後果**：同一組 `(challenge_token, signature)` 在 120 秒內可重複送、重複成功 |
| 🆕 M9 | **session token 無 `amr`／`acr`：稽核上分不出卡登 vs 密碼 break-glass** | access token payload 只有 `sub/username/role/tv/exp/type`（`auth_service.py:56-61` + `security.py:208-219`），**無 `sid`／`jti`／`amr`／`acr`／`auth_time`／`iat`**。卡片（`api/auth/card.py:166`）、密碼（`password.py:161`）、OIDC（`oidc.py:179`）三路呼叫**同一個** `create_tokens(user)`，claims 逐字相同。撤銷粒度是 per-user `token_version`（`auth_service.py:87`）＋ `token_revocations` 事件表供跨服務同步——**可用，但無 per-token jti denylist**（因為沒有 jti） |
| 🆕 M10 | **官方 `anila-agent` starter 可配置成直連任意模型；CSP 只靠操作指南，沒有 runtime／admission／egress enforcement** | 現行 `anila-core-router` 的 LLM 路徑確實走 CSP；但這不能代表正式 Agent。`packages/anila-agent/configs/model.yaml:6` 預設 `http://gpt-oss-20b:8000/v1`，`config.py:77` 接受任意 `ANILA_BASE_URL`，`runtime/model.py:49-53` 直接建立 `AsyncOpenAI(base_url=cfg.base_url)`，測試 `test_model_airgap.py:43-49` 還明確驗證該直連端點。`DeveloperGuideView.vue:88-101` 要求人工作業改成 CSP URL，卻無程式強制；Agent approval／trace-test 也不驗實際模型 URL。**裁決**：未證明現行 MLSteam Agent 正在繞過，但架構上可繞過，屬 Silver conformance／部署網路政策缺口 |
| 🆕 M11 | **憑證鏈驗證刻意不做 CRL／OCSP 撤銷檢查，且沒有離線撤銷資料的新鮮度契約** | `card_auth.py:21-28` 會驗 CMS 簽章、信任鏈、效期與 nonce；但 `:30-31` 明文寫「撤銷檢查刻意不做」，以實體回收卡片＋`User.is_active` 作補償。這是已知設計，不是隱藏 bug；但對 card-only production，遺失／被竊／離職卡在帳號停用前仍缺少憑證層撤銷 enforcement。機密 production 必須有離線 CRL／等效院內撤銷來源、X.509 profile 與 freshness／fail-closed 契約；具名殘餘風險＋人工停用 SLA 只可作較低資料級 pilot 例外，不能成為 production Go |
| 🆕 M12 | **refresh token 可重複使用，沒有 token family／rotation reuse detection** | `/refresh` 驗證舊 refresh token 後直接發新 token（`api/auth/password.py:205-228`），不消費舊 token、沒有 family state；同一舊 refresh token 可重複刷新。即使 G14/G16 加 `sid/jti`，若沒有一次性 rotation 與 reuse detection，遭竊 refresh token 仍可持續換發 access token |
| 🆕 M13 | **背景推論呼叫面不只 Router／Agent／FLUX，仍有 CSP 與 Ingestion 旁路** | H2 已確認 Memory extraction／embedding 直呼（`memory_service.py:181,433`）；另有 prompt generator 直連 registry endpoint（`prompt_gen_service.py:107`）、Ingestion relation LLM 直連可配置端點（`llm_relations.py:188`）、Judge 讀使用者 credential 後把 chunk 送往該 endpoint（`judge.py:121,189`）。固定列四類服務會漏驗；Gate 2 G19 先收斂／關閉 pilot callsite，Gate 5 R7 再由 signed production profile 建立完整 inventory、egress 與 usage reconciliation |

### 3.4 結構性問題（沒有單一 file:line，但影響最深遠）

1. **RLS 在測試架構上不可能被驗證。** RLS 政策只存在於 4 個 migration 的 raw SQL（`0014/0019/0037/0039`），ORM metadata 零 RLS 認知；而 CSP 測試跑 **SQLite in-memory**（`tests/conftest.py:17`）。SQLite 沒有 RLS 概念。→ 分類隔離的骨幹，**沒有任何預設會跑的測試在保護它**。
2. **沒有 CI。** repo 內無 GitHub/GitLab/Jenkins workflow。CSP 全套測試目前 **38 failed / 681 passed**——經分類幾乎全是環境問題，**但沒有 CI，所以沒人知道哪些是真的**。`anilalm` 與 `csp-governance-ui` 連 test script 都沒有。
   > **⚠ 排序張力（v1.1 標註）**：Gate 0 的每一項都需要部署與回歸驗證，但 CI 在 Gate 1。這代表 **Gate 0 只能手動驗證**——這是 Gate 0 從「數天」修正為「2–3 週」的主因之一（§8）。
3. **Full Trace 在部署層是 no-op。** Router 只在 `ANILA_TRACE_ENDPOINT` 設定時輸出 span；`infra/compose/*.yml` **全文零 TRACE 字樣**。CSP 的 trace ingest 端點（`traces.py:100-173`）存在且路徑對齊，但沒人餵它。
4. **PUBLIC repo 內有真實在職人員的憑證身分。** `cht/` 測材鏈到正式 CSPKI Root G1，含真實姓名／員編／email／卡序與 NCSIST CRL/OCSP URL。（已確認：**無**明文私鑰或 API key 外洩。）
5. **🆕 `caller_clearance` 沒有任何資料來源。** v1.0 的 Gate 2 G2 要做 `data_classification <= caller_clearance`，但 `User` model 只有 `role` 與 `department_id`（`models/user.py:19-75`），**全 repo 含所有 migration 零個** clearance／security_level／compartment／need-to-know 欄位或模型。現存唯一「clearance-like」邏輯是 `access_control.py:76-84`，比的是**資源 vs 資源**（launch context level vs 服務 ceiling），不碰任何使用者屬性。→ **G2 有一個 v1.0 沒寫出來的前置：先定義使用者密級的資料模型。不能把 `role` 當 clearance。**
6. **🆕 SourceSnapshot 不是空殼，是「schema 建好、寫入面沒接線」。** `models/source_snapshot.py:43-46` 的 table 存在，欄位齊全：`document_ids:60`／`chunk_ids:61`／`document_versions:63`／`retrieval_queries:64`／`content_hash:66`／`payload_ref:68`。`Citation`（`:91-118`）**刻意不掛 live document 的 FK**（docstring `:5-9` 明載此規則），設計上已經是「引用指向不可變快照」。但 `create_task` 只寫 5 個欄位（`tasks/service.py:184-190`：`task_id`／`origin`／`source_scope`／`collection_ids`／`classification_level`），其餘全空；**citation 無任何 production 寫入面**（service docstring `:11` 自承）。
   > 這是 §11 主題的又一實例。**未來的人請不要重新設計這個 model——把寫入面接上去就好。**
7. **🆕 ingestion queue 是 volatile 的。** `platform.yml:201`：`redis-server --appendonly no`。redis 重啟即遺失所有排隊中的 job，而 `document.status` 仍停在中間態 → 對應 Gate 3 的 reaper 與 replay/dedupe 契約。
8. **🆕 沒有 production acceptance gate。** 整份 v1.0 的起點是「機敏 production No-Go」，卻**從未定義 Go 的條件**。§6 的排序表甚至寫著「任何 pilot 放機敏資料 → Gate 0 全部完成」——那句話會讓人在只關掉外部暴露之後就放機敏資料。**這是 v1.0 最危險的一句話，已於 v1.1 改寫（§6.1）。**

---

## 4. 設計不變式：任何變更都不可違反

> 這些是路牌的地基。違反其中任一條的 PR，不論多漂亮，都應該被擋下。

1. **分類只升不降，未知即 fail-closed。** `effective_classification = max(所有觀測到的來源)`。unknown／invalid classification 一律拒絕或視為最高等級，不得默默當成無機密。
2. **CSP 是治理權威，且是最終 enforcement point。** Router 的 gate 是早期攔截（防禦縱深），不是唯一防線；反過來，Router 也不得繞過 CSP 直連正式 Agent。
   > 補充事實：CSP **已經**在 dispatch 時驗證 permission／classification ceiling／approval（`proxy.py:379-394,608-614`），並使用 per-agent service token 而非轉交 caller bearer。這使 Router PolicyGate 的**安全緊迫性**低於變更計畫書的描述——它的真正價值在 routing 品質、可稽核性與 multi-agent 地基。
3. **air-gap 是定義，不是限制。** 不做 marketplace、不做 runtime 動態安裝外掛、不做外部 registry 拉取。
4. **UI 只反映真實事件，不做動畫。** 附圖式步驟必須由後端 `StepEvent` 驅動。「已完成 8」由終態事件計算，不由前端猜。
5. **UI 只顯示 safe summary；遮罩在 agent 端做，但 agent 的自我遮罩不是信任邊界。**（v1.1 修訂）
   > v1.0 寫「遮罩發生在 agent 端，不是前端」。對第一方 agent 成立，但對**第三方 agent** 就等於把安全邊界交給不受信任的一方——這與不變式 2（CSP 是最終 enforcement point）直接衝突。**正確規則**：agent 端先遮罩（責任在來源），CSP／StreamBridge 再做 schema 驗證、大小上限、secret pattern 掃描（防禦縱深）。raw chain-of-thought、完整工具參數、秘密、未遮罩機敏資料永不送到 Shell。
6. **每個 Task／Invocation／Step 只能進入一次終態。** 重試不得重複產生外部副作用。
7. **migration 一律 additive**（expand → backfill → switch → contract）。回滾不刪資料。
8. **回滾只能回到更保守的能力。** 安全 gate 啟用後，故障時的 fallback 是 deny／clarify／direct-answer，不是「暫時關掉 gate」。
9. **祕密零外洩**（PUBLIC repo）。`.env`／`*.pem`／`*.key`／`secrets/` 已 gitignore，不要加回追蹤。**容器掛載不看 gitignore。**
10. **🆕 模型與檢索呼叫一律經 CSP。** 任何服務直連模型後端即為缺陷（含圖像——見 M7）。CSP 是唯一持有 `MODEL_GATEWAY_API_KEY` 的服務（實測：兩個 compose 檔中該 env 只出現在 csp 一處），也是唯一寫 `token_usage` 的地方。
11. **🆕 prompt 的內容由 server 組裝。** 前端不得把檢索結果拼進 system prompt 再送出（見 H7）——否則 server 的分類 latch 永遠看不到它在處理什麼。
12. **繁體中文台灣用語**，禁簡體與大陸用語。

---

## 5. 路線圖

> **Gate 制，不是時程制。** 每個 Gate 有明確的退出條件；退出條件沒達成就不進下一個。以 1 人＋AI 的實際人力，Gate 之間是**串行**的——不要用「平行推進」來壓縮時程，那只會同時削弱契約、測試與治理。

### 5.1 Gate 總表

| Gate | 主題 | 規模 | 退出條件（摘要） | 狀態 |
|---|---|---|---|---|
| **0** | 止血與關閉外露 | **2–3 週**（v1.0 誤估「數天」） | 所有**可直接利用的外部暴露**已關閉 | ☐ |
| **1** | 地基 | **2–3 週** | CI 綠燈；RLS 有真 PG 測試；`anila-security`＋`anila-contracts v0` 已抽出；startup posture assertion | ☐ |
| **2** | 讓治理帳成立 | **9–13 週**（v1.1.1 加入卡片撤銷、refresh family 與 pilot inference closure） | 每個正式動作都能回答「誰做、用什麼來源、什麼分類、算誰的帳」 | ☐ |
| **3** | 讓資料與產物可靠 | **7–11 週**（v1.1.1 加入完整備份／還原設計） | accepted upload 100% 到終態；artifact 有不可變 SSOT 且下載驗分類 | ☐ |
| **4** | 唯讀 Agentic timeline（**Demo Lane**） | **2–3 週**（v1.1.1 加入 validator 與 cancel propagation） | 真實 tool/skill/retrieval 事件在 UI 可重現。**不計入 production readiness** | ☐ |
| **5** | 讓大腦正式化 | 8–12 週 | 100% dispatch 經 RouteDecision + PolicyGate；官方 Agent 達 Silver；durable HITL | ☐ |
| **6** | **Production Acceptance**（v1.1 新增，v1.1.1 客觀化） | **3–6 週** | **這裡才是 No-Go → Go。** signed acceptance profile、restore／故障／PKI／egress drill、SLO、簽章 release | ☐ |
| **7** | 條件式能力 | 之後 | 另行 Go/No-Go | ☐ |

**到「機密以上 production Go」的工程關鍵路徑 ＝ Gate 0＋1＋2＋3＋5＋6 ＝ 31–48 週（約 8–12 個月，1 人＋AI）。**
Gate 4（Demo Lane）不在關鍵路徑上。Gate 7 在 Go 之後。**這是工程估算，不是 production 日曆承諾**：PKI／法務／獨立資安覆核與正式簽核的外部等待時間另計。若這個量級不可接受，正確的反應是**縮小資料分級的野心**（例如只做不含真實營業秘密的 synthetic／去識別 pilot，或只完成 Gate 0–2），而不是壓縮 Gate 的退出條件。

**v1.1 的四個結構性變更**：
1. **資料可靠（原 Gate 4）提前到 Gate 3，Agentic UI（原 Gate 3）退到 Gate 4 並降格為 Demo Lane。** 理由：唯讀 timeline 不提升 production readiness；資料可靠會。Demo Lane 可與 Gate 3 並行，但**不得因它延後 Gate 3**。
2. **新增 Gate 6 Production Acceptance。** 這是 v1.0 最大的結構缺口——文件宣告 No-Go 卻沒定義 Go。
3. **`anila-contracts` 從 Gate 5 提前，分三階段抽取**（Gate 1 → Gate 2 → Gate 5），每階段只抽下一個 Gate 真的會用到的，不做投機抽象。
4. **Gate 0–3 的規模全部上修。** v1.1 往 Gate 1／2／3 各加了數項工作（F5/F6、G1b/G2a/G10–G16/G6b、I2b/I2c/I8/A1–A6），估計必須跟著動。
   > 這正是 v1.0 犯的錯的鏡像：**加工作而不加時間，等於偷偷削弱退出條件。** 任何人往某個 Gate 加工作項時，請同時修這張表——否則下一個讀它的人會以為範圍是免費的。

**v1.1.1 的追加修正**：Gate 2 增加憑證卡撤銷契約、refresh token family 與 pilot inference closure，估計由 6–10 週調為 9–13 週；Gate 3 增加完整備份／還原設計，估計由 6–10 週調為 7–11 週；Gate 4 增加 validator／cancel propagation，估計由 1–2 週調為 2–3 週；Gate 6 增加 signed profile、PKI 與 egress 驗收，估計由 2–4 週調為 3–6 週，關鍵路徑同步改為 31–48 週。

---

### Gate 0 — 止血與關閉外露

**進入條件**：無。現在就做。
**退出條件的定位（v1.1 修訂）**：v1.0 寫「CRITICAL/HIGH 全部關閉」，但 H2（Memory）的完整修復需要分類欄位＋migration，本質屬 Gate 2。**改寫為：關閉所有可直接利用的外部暴露，並對無法在此 Gate 完整修復者提供部署層 kill switch。**

| ID | 工作 | 對應 |
|---|---|---|
| S1 | `ANILA_BACKUP_DIR` 與 `.env.bak` 移出 repo root；codeserver 移出正式預設 stack，只保留明示 `developer-tools` profile，workspace 固定為 git-ignored 非機密 sandbox，registry 明標「按需」；啟用時只由 `code.ai.ncsist.org.tw` 的獨立 origin 提供，使用 code-server 原生強密碼，主平台 `/codeserver*` 固定 404 | **C1** |
| S1b | **依使用者決策，`prod-intranet-card` 保留 n8n／GitLab，但隔離瀏覽器來源。** n8n 與 GitLab 分別只由 `n8n.ai.ncsist.org.tw`、`gitlab.ai.ncsist.org.tw` 的 root canonical URL 提供，採各自原生帳號／PAT／runner token 認證；不得接受或轉送 CSP session cookie／`auth_request`，主平台 `/n8n*`、`/gitlab*` 固定 404。n8n 的 `webhook`／`webhook-test`／`webhook-waiting`（exact 與子路徑）一律 404；Git smart HTTP／LFS／API 走 GitLab 原生認證，Git CLI 亦可走明示綁定內網介面、以 SSH key 驗證的 GitLab Shell port | §2.4 |
| S1c | 落實 `AGENTS.md` §3.3 的服務交付矩陣（compose service ＋ nginx virtual host ＋ `AUTO_REGISTER_LINKS`）：不需要 n8n／GitLab 的 profile 必須移除；`prod-intranet-card` 是經決策保留的例外，signed deployment profile 必須明列三個工具 FQDN、原生認證與 GitLab SSH ingress。n8n machine ingress 未完成前維持 fail-closed，不得以「內網」取代身分驗證 | §2.4 |
| 🆕 S1d | nginx `/uploads/ingestion/` 維持 404；`/uploads/flux/` 與其餘 `/uploads/` 均以 CSP smart-card session 驗證，並回 `private, no-store`／`no-referrer`。這只關閉匿名暴露：已知 UUID 仍可能被其他已登入使用者讀取；**完整正解**仍是 Gate 3 A3 的 CSP artifact 下載端點，綁 owner／Task／classification 並可撤銷 | **H6** |
| S2 | compose 設 `ENABLE_PUBLIC_SHARE: "false"`；`create_share` 的 gate 由 legacy boolean 改為 `classification_level > 無機密`；**🆕 讀取端 `public_share.py:58` 的 `conv.classified` 一併改為 level 判斷** | **H1** |
| | ↳ 註：讀取端是**即時查 conversation 當下狀態**（非建立時快照），所以改完 level 判斷後，分類升級會讓既有 token **自動失效**——不需要另建「撤銷舊 token」機制。v1.0 只改了 `create_share`，等於營業秘密對話在修補前建立的連結仍可讀。**另**：`expires_at` 加強制上限（現可為 NULL ＝ 永久） | |
| 🆕 S2b | **加 `ENABLE_MEMORY` 旗標**（包住 `proxy.py:519` 的 `_inject_memory` 與 `proxy.py:251-279` 的 `_schedule_memory_write`），正式 profile 預設 `false`。實測：`config.py` 全檔無此旗標，今天**只能改 code 才能停用** | **H2 的部署層止血**（完整修復＝Gate 2 G3/G4） |
| S3 | `cht/` 測材以 synthetic CA ／假員編重建；掃描 current tree、全部 reachable history／remote refs，依結果完成 history 清理或建立不可回收 clone 的事件處置與憑證／身分風險紀錄 | §3.4-4 |
| S4 | air-gap 匯出清單補 `flux2-dev-agent`；建立「新服務必須進匯出清單」的 checklist | **H5** |
| S5 | Governance UI 送 `entry_url`；auto-seed 改 absolute URL；`_validate_launch_entry_url` 移到權限檢查**之後** | **H3, H4** |
| S6 | Router：caller system message 不得取代內部控制 prompt（`router_server.py:726-730`）；`anila_multi_turn` 加 server-side 上限（`:717`，初期 3） | 變更計畫書 P0-01 |
| S7 | alembic 失敗改 fail-stop，移除 `create_all` fallback；`/health` 反映 migration 狀態 | **M4** |
| S8 | `CARD_DEV_SKIP_NONCE_BINDING` 加 production startup hard gate | **M5** |
| S9 | upload 的 enqueue 失敗回滾（比照 `reprocess` `documents.py:387-397`）；空 chunks 路徑補 job 終態（`handlers.py:761-767`） | 變更計畫書 WS7 |
| S10 | compose 注入 `ANILA_TRACE_ENDPOINT`；正式 profile 缺 endpoint 時 readiness fail | §3.4-3 |
| S11 | `infra/docker/csp.Dockerfile` 加 `USER`（目前以 root 執行）；刪除死檔 `services/csp/Dockerfile` | 審查報告 Part 3 §3.6 |

**退出條件（v1.1 改寫）**
- 以 route inventory ＋匿名 contract/DAST negative tests 證明無任何**未登入即可讀取資料**的路徑；至少覆蓋 `public_share`（S2）、主平台 `/codeserver*`／`/n8n*`／`/gitlab*` 全為 404、三個工具獨立 origin 由原生認證保護、n8n `/webhook*` 全為 404（S1、S1b），以及 `/uploads/*`（S1d）
- 正式 profile 的 `docker compose config`／container mount inventory 證明 codeserver 不在 stack（或 workspace 只掛非機密子目錄且已通過明文核准）、repo root 不存在 RW 掛載、backup／`.env.bak`／key material 全部位於 repo 與 codeserver workspace 外並有最小權限 ACL（S1）
- 逐 deployment profile 對照 `AGENTS.md` §3.3：要求移除的 codeserver／n8n／GitLab 在 compose service、nginx virtual host、`AUTO_REGISTER_LINKS` 三處都不存在；`prod-intranet-card` 則須證明 links 指向三個獨立 HTTPS origin、主平台沒有工具代理、GitLab SSH 僅綁 `GITLAB_SSH_BIND_IP`；用 rendered compose／nginx config 驗證，不只看匿名 HTTP 結果（S1c）
- H2 有部署層 kill switch 且正式 profile 已關閉（S2b）——**完整修復記在 Gate 2，不在此處宣稱已關閉**
- 全新內網主機 `docker compose up -d --no-build` 可完整啟動（S4）
- Governance UI 建立／啟動服務的正反向測試通過；未授權者先得到 403、不是 URL oracle 的 400（S5）
- caller system message 無法取代控制 prompt，`anila_multi_turn` 超過 server-side 上限會被拒絕（S6）
- migration 失敗會停止部署，不會靜默 `create_all`（S7）
- 正式 profile 夾帶 `CARD_DEV_SKIP_NONCE_BINDING` 時拒絕啟動（S8）
- enqueue 失敗不留下孤兒 document/job；空 chunks、timeout、cancel 都會寫入唯一終態（S9）
- Full Trace 在正式 profile 下可回讀 span（S10）
- CSP 容器以非 root 執行，且 runtime 所需 volume 權限經 smoke test 驗證（S11）
- PUBLIC repo 的 current tree 與全部 reachable refs 無真實人員身分資料；若已公開 clone 無法回收，事件處置與風險接受已有書面 owner（S3）

**這個 Gate 之後可以做什麼**：無機密資料的受控 pilot。**不可以**放營業秘密以上的資料（見 §6.1）。

---

### Gate 1 — 地基

**進入條件**：Gate 0 全數通過。
**為什麼在這裡**：**沒有 CI 就不敢重構；沒有 RLS 測試就不知道重構有沒有把分類隔離弄壞。** 後面每一個 Gate 都要動 CSP，這是先付的保險費。

| ID | 工作 | 說明 |
|---|---|---|
| F1 | 最小 CI | backend pytest（core／agent／ingestion／CSP）＋ 三個前端 build/typecheck。**即使只有這樣，也終結了「38 個失敗但沒人知道哪些是真的」** |
| F2 | **真 PostgreSQL 的 RLS 整合測試** | 現況 CSP 測試跑 SQLite，**架構上就測不到 RLS**。這是分類隔離的骨幹 |
| F3 | 分類現有測試失敗 | 把 CSP 的 38 個失敗逐一歸類為 functional／platform／test-staleness，銷案；暫時 quarantine 者必須移出 required suite、綁 ticket／owner／到期日，且不得用永久 `xfail` 把紅燈偽裝成綠燈。**禁止新增未登錄偏差** |
| F4 | 抽出 `anila-security` | 656 行、2 個模組（`credential_crypto.py` + `url_guard.py`）、唯一第三方依賴是 `cryptography`、CSP 有 13 處在用。**天級工作量、近零風險** |
| 🆕 F5 | 抽出 `anila-contracts` **v0（最小集）** | 只放 **Classification**、**StepEvent**、**AgentError**。理由：Gate 2 的分類工作與 Gate 4 的事件工作都要用；第三方 agent 開發者需要一個薄套件來 import 事件型別，**不能為了 import 契約而繼承 `fastapi`＋`asyncpg`＋`pgvector`＋`sse-starlette` 全套**。v1.0 把 contracts 排在 Gate 5，會造成 Gate 4 先接一套、之後重做 |
| 🆕 F6 | **startup posture assertion** | 服務啟動時斷言自身旗標姿態符合宣告的 deployment profile（例：`prod-intranet-card` 必須 `ENABLE_PUBLIC_SHARE=false` ∧ `ENABLE_MEMORY=false` ∧ `ENABLE_CARD_LOGIN=true`），不符即拒啟。＋ compose image digest pinning。**理由見 §2.4**：既然正式部署身分目前只由可變 env 決定，至少讓它在啟動時被驗證。完整簽章 release／SBOM ＝ Gate 6 |
| F7 | 凍結新能力 | 不新增 agent 類型、artifact 類型、memory 功能、自由 multi-agent，直到 Gate 5 |

**退出條件**：CI 對 `main` 自動跑 build + unit + contract smoke，**所有 required checks 必須全綠**；quarantine 只允許有 ticket／owner／到期日的非 required case；RLS 有一個真 PG 測試會失敗（若把 policy 拿掉）；`anila-security` 與 `anila-contracts v0` 已是獨立套件且 CSP 從它們 import；正式 profile 啟動時會驗證 posture；Gate 5 前新增 agent/artifact/memory 類型的 PR 會被 required policy check 擋下或有具名架構 owner 的書面例外（F7）。

---

### Gate 2 — 讓治理帳成立

**進入條件**：Gate 1 通過。
**主題**：每個正式動作都能回答「誰做、用什麼來源、什麼分類、算誰的帳」。
**規模警告**：這是**工作項數最多**的 Gate（v1.0 估 2–4 週，v1.1 修正為 6–10 週，v1.1.1 再修正為 **9–13 週**）。它有四個區塊、23 個工作項，其中含**新資料模型＋migration**（G2a clearance、G14 claims／refresh family）、憑證撤銷來源（G17）、pilot inference closure（G19）與**前端聊天架構重寫**（G10 移除瀏覽器端 RAG）。1 人團隊應照下表順序做，不要平行。

**Gate 2 signed pilot profile**：在任何營業秘密 pilot 前，由 system owner、data owner、PKI owner 與資安先簽章固定 pilot 的資料上限、啟用功能、**machine-readable enabled inference callsite inventory**、revocation／lost-card SLA、PKI stale/offline policy、保存期限與撤回方式。這是 pilot 專用 profile，**不是** Gate 6 的 production acceptance P0，也不能沿用成 production Go。

#### 2A. 分類傳播（先做——後面的 gate 都依賴它）

| ID | 工作 | 問題 |
|---|---|---|
| G1 | worker 寫入 chunk 的 `classification_level` | 現為**雙向死欄位**：worker 不寫（`pgvector_store.py` 兩段 INSERT 皆不含此欄）、search 也不讀 |
| 🆕 G1b | **historical classification backfill ＋ 全量 reconciliation ＋ 語意抽樣** | v1.0 只在 §6 排序表提到 backfill，工作項漏排。沒有 backfill 就開 G2 的 gate ＝ 舊 chunk 全部 UNCLASSIFIED，等於沒開。**100% 完整率不能靠抽樣證明**：所有 classification-bearing row 的 NULL、五級合法集合以外字串、低於 document 或 collection effective classification 的數量都必須為 0，expected/actual row count 必須相等；非法字串進 search／artifact／trace 必須 fail-closed；抽樣只驗語意 |
| 🆕 G2a | **定義 clearance 資料模型**（`ClearanceGrant`／compartment／need-to-know） | **§3.4-5：這是 G2 的隱藏前置。** `User` 只有 `role`／`department_id`，`caller_clearance` 今天**沒有任何資料來源**。**不能把 `role` 當 clearance** |
| G2 | search clearance gate | 加 `data_classification <= caller_clearance`，並同時 enforce compartment／need-to-know／collection grant／owner／agent ceiling。必須測「同密級、不同 compartment」的橫向拒絕，以及 connection-pool 重用後授權 context 不殘留。**依賴 G1b + G2a** |
| G3 | `UserFact` 加分類欄位 + 注入前依 clearance 過濾 | **H2** 的根因（Gate 0 S2b 只是 kill switch） |
| G4 | memory 寫入側走 `enforce_model_ceiling` | `_extract_facts`／`_embed` 目前直呼固定模型 |
| G5 | chunk latch 不硬編「機密」 | `_latch_inherited_classification`（`proxy.py:125`）→ 絕對機密來源只 latch 到機密 |
| 🆕 F5→ | `anila-contracts` v1：加 `TaskContext`／`TraceContext`／`InvocationCommand`／`SourceSnapshot`／`SafeSummary` | 承 Gate 1 F5；**必須在 2B 前完成**，後續 G10–G13／G7 不得先用私有型別接線再重做 |

#### 2B. 來源交易（v1.1 大幅擴充）

| ID | 工作 | 問題 |
|---|---|---|
| 🆕 G10 | **Canonical server-side RetrievalService；移除瀏覽器端 RAG** | **H7。** `WSChat.tsx` 在瀏覽器組 system prompt → server 的 latch 永遠看不到檢索了什麼。正式 RAG 必須 server-side、必須建 Task。禁止 taskless／browser-side fallback（不變式 11） |
| 🆕 G11 | **接線既有的 `SourceSnapshot` 寫入面** | **§3.4-6：model 與 table 已存在且欄位齊全，`Citation` 也已刻意設計為指向不可變快照。缺的只是寫入。** `create_task` 補寫 `document_ids`／`chunk_ids`／`document_versions`／`retrieval_queries`／`content_hash`。**不要重新設計這個 model** |
| 🆕 G12 | retrieval 失敗與 zero-hit 分離；citation 寫入面 | 現況 Report 空命中直接 `RuntimeError`（見 M6），分不出「檢索壞了」與「真的沒有」 |
| 🆕 G13 | 修 `document_ids` post-filter → 接線 `similarity_search_per_document` | **M6**（`pgvector_store.py:239-287` 已存在，零呼叫點） |
| G9 | `delete_document()` 接線（或改 upsert） | migration `0016` 的 unique 約束假設「re-index 前會刪舊 chunk」，但該方法**全倉零呼叫點** |

#### 2C. 身分與 session（v1.1 新增）

| ID | 工作 | 問題 |
|---|---|---|
| 🆕 G14 | session token 加 `sid`／`jti`／`iat`／`iss`／`aud`／`amr`／`acr`／`auth_time`／`break_glass` | **M9。** 三路登入 claims 逐字相同，簽發／decode 也未要求 issuer/audience → 稽核分不出卡登與密碼 break-glass，錯 issuer/audience 也沒有邊界。驗收要逐路比對 claims 語意，且錯 `iss/aud` 必拒絕，不是只檢查欄位存在 |
| 🆕 G15 | 卡登 challenge one-time consume | **M8。** 現為 stateless JWT，120 秒內可重放。驗收：同一 `(challenge_token, signature)` 第一次成功、第二次必須拒絕並留下 audit event；跨 process／restart 仍不可重放；兩個 CSP instance 同時 consume 同一 challenge 時必須靠原子 compare-and-consume 保證**恰好一個成功** |
| 🆕 G16 | per-token 撤銷（有了 jti 才做得到） | 現況粒度是 per-user `token_version`；`token_revocations` 表已存在可延用。驗收：指定 `jti` 撤銷在 signed pilot profile 規定的傳播時間內對所有 CSP instance 生效，其他 session 不受影響 |
| 🆕 G17 | **卡片憑證撤銷來源與 X.509 profile／freshness 契約** | **M11。** 預設方案為 air-gap 可更新的離線 CRL／院內等效撤銷清單；必須驗 CRL signature／issuer，以 signer X.509 cert serial 或 fingerprint 查撤銷，**不得用前端傳入的 `card_serial`**；chain verifier 補 BasicConstraints／KeyUsage／EKU／pathLen／certificate policy negative tests；記錄 thisUpdate／nextUpdate、同步來源與 max age，stale／缺失時 production 一律 fail-closed。營業秘密 pilot 若只靠實體回收＋`User.is_active`，必須在 signed pilot profile 明列人工停用 SLA、lost-card 流程、監控與具名殘餘風險接受者；此例外不得沿用到機密 production |
| 🆕 G18 | **refresh token family＋一次性 rotation＋reuse detection** | **M12。** access／refresh 使用不同 `jti`；每次 refresh 原子消費舊 token 並發新一代。舊 refresh token 再使用時視為 theft signal，撤銷整個 `sid` family、寫 audit，且多 instance race 只能有一個 rotation 成功 |

#### 2D. 交易閉合與帳本

| ID | 工作 | 問題 |
|---|---|---|
| G6 | Task 終態交易式收斂；`BLOCKED_BY_POLICY` 可達 | **M1** |
| 🆕 G6b | **Policy decision／Audit／Artifact 寫入與 Task 狀態同一交易（或 outbox）** | v1.0 只給 Ingestion 排了 outbox（原 Gate 4 I1）。**Task／Policy／Audit／Artifact 沒有任何交易式閉合**——治理帳可以在崩潰時只寫一半 |
| G7 | `TraceSpan.classification_level` 落地；trace ingest 驗 ownership | 欄位存在但從不設定；`traces.py:100-173` 不驗 trace 歸屬 |
| G8 | 非串流 dispatch 補 `token_usage`；**🆕 圖像呼叫收斂到 CSP** | `proxy.py:684-685` 註解自承。**根因是 M7**：`flux_client.py:88-97` 直連模型後端，CSP 不在資料路徑上 → 不是估算不準，是根本沒經過。修法：比照 embedding（`platform.yml:220`）改走 CSP 代理 |
| 🆕 G19 | **pilot inference closure** | **M13。** 從 signed pilot profile 的 callsite inventory 逐一驗證所有啟用推論都經 CSP、受 classification ceiling、寫 usage/audit，並做裸模型 egress negative test；尚未收斂的 prompt generator、Ingestion relation LLM／Judge、第三方 Agent 或其他 callsite 必須在 pilot profile、registry、UI 與 worker config 四層關閉。R7 再建立完整 production model governance |

**退出條件**：分類傳播以全量 reconciliation 證明完整率 100%（NULL＝0、非法分類字串＝0、低於來源 effective classification＝0、expected/actual row count 相等；非法值進 search／artifact／trace 一律 fail-closed；抽樣只驗語意）；clearance／compartment／need-to-know／collection grant／owner／agent ceiling 任一不足都搜不到資料，且 connection-pool 不殘留前一請求 context；Memory fact 繼承分類、注入前過 clearance，fact extraction／embedding 也受 model ceiling，latch 永遠取所有來源最高等級；每個 Task 最終為 completed／failed／cancelled／blocked_by_policy 且與 policy/audit/artifact 同交易；trace classification 落地且 ingest 驗 ownership；signed pilot inventory 中每個 enabled inference callsite 都經 CSP、受 ceiling 且有 usage/audit，裸模型 egress negative tests 通過，未收斂 callsite 在 profile／registry／UI／worker config 四層不可達；正式 RAG 100% server-side且每次都物化 SourceSnapshot／Citation，zero-hit 與 retrieval failure可區分；`document_ids` 在 SQL／per-document ranking 生效，re-index delete/upsert 冪等；`anila-contracts v1` conformance tests 通過；三路登入的 `iat/iss/aud/amr/acr/auth_time/break_glass` 語意正確且錯 issuer/audience 必拒絕；challenge 的 sequential／multi-instance race 重放都只有一次成功；指定 access／refresh `jti` 撤銷依 signed pilot SLA 傳播且不誤傷其他 session；refresh token rotation 為一次性，reuse 會撤銷整個 family；被撤銷卡必拒絕，CRL/X.509 profile 驗證與 stale／缺失行為符合 signed pilot policy。

**這個 Gate 之後可以做什麼**：營業秘密級的受控 **RAG／chat-only** pilot，限 signed pilot profile 核准的最小資料集，具完整稽核、保存期限與撤回機制；在 Gate 3 前，pilot profile 必須關閉 Studio／Artifact／FLUX／export 與任何會落地產物的路徑。**不得以「可承受洩漏」替代安全控制**；若環境仍假設洩漏可承受，只能使用 synthetic／去識別化資料，不得標示為營業秘密。若 pilot 要啟用任何產物路徑，必須先完成 Gate 3。仍不可以放機密以上（見 §6.1）。

---

### Gate 3 — 讓資料與產物可靠

**進入條件**：Gate 2 通過（分類傳播已就位）。
**v1.1／v1.1.1 變更**：由原 Gate 4 提前；範圍由「Ingestion」擴為「**Ingestion ＋ Artifact／Studio ＋ 備份／還原設計**」，規模由 4–8 週上修為 6–10 週，再因 I9 修正為 **7–11 週**。理由：v1.0 把 M2／M3 列進 MEDIUM 卻**沒有任何 Gate 負責修**；而 Gate 6 要做 restore drill，就不能等到驗收階段才首次設計備份。

#### 3A. Ingestion durability

| ID | 工作 | 問題 |
|---|---|---|
| I1 | Transactional outbox | upload 依序做「commit document → Arq enqueue → commit job row」三段分離，無原子性；worker 可能在 job row commit 前跑完，`_update_job` 打到 0 rows **靜默略過** |
| I2 | 租約式 job 狀態機 + reaper + retryable/permanent taxonomy + **🆕 DLQ** | `main.py:70` 靜態 `max_tries=3`；`handlers.py:925-948` catch 後一律 raise，不看 `err.retryable` |
| 🆕 I2b | **Redis 持久化與 restart replay／dedupe 契約** | **§3.4-7：`platform.yml:201` 是 `--appendonly no`。** redis 重啟即遺失所有排隊 job，而 `document.status` 停在中間態 |
| 🆕 I2c | worker heartbeat／readiness／queue age 指標 | 沒有這些就不知道 reaper 該不該動 |
| I3 | Generation-level 冪等 + 原子啟用 | parent／leaf 分兩個獨立交易、皆無 `ON CONFLICT`；配合 G9 → 重試必撞唯一鍵 |
| I4 | stage 與文件可用性分離 | 單一 `document.status` 同時表示排程與可搜尋 |
| I5 | 同步 parse 移出 event loop；timeout/cancel 在 `finally` 寫終態 | `handlers.py:693-697` 同步呼叫 `extract_text` 無 executor；`ingest_document` 無 `finally`、不接 `CancelledError` |
| I6 | LLM relation 的 HTTP 呼叫移出 DB transaction | `llm_relations.py:226-274` 在 `conn.transaction()` 內做 httpx POST，timeout 120 秒 |
| I7 | similarity relations 改 debounce/dedupe job | `similarity_relations.py:28-49` 每次 ingest 都做 collection-wide centroid self-join（O(N²)）；`:82-88` 超限的 return 在 DELETE 之前 → 留下 stale edges |
| 🆕 I8 | embedding model fingerprint／dimension 契約 | 換 embedding 模型後舊向量無法比對，且沒有記錄哪些 chunk 用哪個模型產生 |
| 🆕 I9 | **完整備份／還原設計與自動化** | Gate 0 S1 只把危險備份移出 repo，沒有建立 production backup SSOT。需覆蓋 PostgreSQL、blob／artifact、active vector generation、必要 config／CA／key reference（私鑰依 key-management 規範處理）、retention、encryption、off-host copy、失敗告警與可重複 restore runbook；Gate 6 P1 只負責獨立驗收，不在驗收現場第一次發明備份 |

#### 3B. Artifact／Studio durability（v1.1 新增——v1.0 完全漏排）

| ID | 工作 | 問題 |
|---|---|---|
| 🆕 A1 | durable Studio queue／lease／checkpoint | Studio job 是 process-local；prune／FIFO 驅逐即觸發 **M2**（status `done` 但 download 404，五個 pipeline 全中） |
| 🆕 A2 | immutable blob store；Slides bytes 落地 | 現況只在 RAM |
| 🆕 A3 | **CSP Artifact／Version 作唯一 SSOT；下載端點驗 owner／Task／Snapshot／classification** | 這同時是 H6（`/uploads/` 未認證）的中期正解 |
| 🆕 A4 | 移除跨使用者 localStorage artifact | **M3**（`OutputsPage.tsx:116-124`）。Outputs 的 SSOT 必須是 CSP，不是 localStorage |
| 🆕 A5 | blob／image／counter／archive／retention lifecycle | 沒有 retention ＝ 機敏產出永久堆積 |
| 🆕 A6 | 上傳 MIME sniffing（air-gap 環境下 malware scan 優先度較低，但 USB 帶入仍是攻擊面） | — |

**退出條件**：任何 accepted upload 最終都能到 terminal state；retryable／permanent／DLQ 正反向測試通過，heartbeat／readiness／queue-age metrics 可抓取且 reaper 不會誤收仍有 lease 的 job；stuck nonterminal 超過 lease+grace＝0；worker crash/retry 不產生 duplicate active chunks；redis restart 後 queue 可 replay 且不重複；stage 與可搜尋 generation 分離，re-index 期間舊 active generation 持續可搜尋；同步 parse 不阻塞 event loop，timeout/cancel 都寫唯一終態；LLM relation HTTP 不在 DB transaction 內，similarity relation job 有 debounce/dedupe 且不留下 stale edge；每個 active generation 記錄 embedding model fingerprint／dimension，維度不相容會 fail-stop；Studio process crash/restart 後 job 可續跑，任何 `done` 都必須可下載；**每個 artifact 都有不可變 blob ＋ CSP 側 SSOT**，下載時同時驗 owner、Task／collection membership、classification、compartment、版本與撤銷狀態；登出／換使用者後 localStorage 不含 artifact metadata；同密級不同 compartment／owner 的正反向測試矩陣全數通過；retention／archive／erase lifecycle 與 MIME sniffing negative tests 通過；production backup job 覆蓋 DB／blob／artifact／active vector generation 與必要 release references，失敗會告警，且已有由自動化產物完成的預備 restore smoke（正式獨立 drill 在 Gate 6 P1）。

---

### Gate 4 — 唯讀 Agentic timeline（Demo Lane）

**進入條件**：Gate 2 通過（分類與遮罩規則已定）。**可與 Gate 3 並行，但不得延後 Gate 3。**
**v1.1 定位變更**：這個 Gate **不提升 production readiness**。它是 demo／體驗價值。v1.0 把它排在資料可靠之前是錯的排序。

**關鍵洞察**：**管線已有八成，缺的是接線，不是發明新協議。**

| 層 | 現況 | 證據 |
|---|---|---|
| 協議層 | **已存在** | `api/events.py:17-178` typed pydantic 事件（`tool_call_started`／`tool_call_finished`／`todos_updated`／`interrupt_requested`…） |
| 傳輸層 | **已存在** | `router_server.py:2219-2231` passthrough 白名單；`_make_event:337-338` 輸出 named SSE |
| 前端解析層 | **已存在** | `sse.js:198-287` 解析 10 種 `anila.*` 事件；`TodoChecklist`／`ToolExecutionWidget`／`InterruptCard` 元件皆備妥且有單測 |

斷掉的只有頭尾兩段：**agent 端不發事件**（`service_wrapper.py:356-362` 只轉發 text delta）、**Shell 不消費**（`app.jsx` 五條路徑只接 `onText/onTrace/onMeta/onReasoning`）。

| ID | 工作 | 說明 |
|---|---|---|
| T1 | anila-agent 在 RunHooks 發 tool/skill/retrieval 事件 | 掛點現成：`tracing.py` 已用同一組 `on_tool_start`／`on_tool_end` hooks 發 trace span |
| T2 | Shell 單一 execution reducer | 五條訊息路徑（`sendMessage`／`handleEditUser`／`continueMessage`／`regenerateMessage`／**`sendCompare`**——計畫書漏了最後一條）共用一套 callbacks |
| 🆕 T3 | **先建立薄型 `StreamValidator`／`BridgeCore`，驗 safe summary 與事件身分** | 不變式 5：strict event-name allowlist＋逐型別 schema；task/trace/agent/session ID 必須由可信 dispatch context 覆寫或核對；限制單事件大小、每秒與每 run 事件數；unknown／malformed／oversize event 丟棄並 audit；safe summary 再做 size／secret-pattern scan。第三方 agent 的自我遮罩不是信任邊界。negative test 必含第三方 Agent 偽造 task/agent ID、送 raw `anila.reasoning`／secret、event flood。此 Gate 只做同步驗證核心；Gate 5 R4 再擴成含 replay／idempotency 的完整 `StreamBridge` |
| T4 | 官方 LangChain adapter 範例 | 兌現「不用換框架」的承諾 |
| 🆕 T5 | **in-session cancel propagation** | Shell cancel → Router/CSP → Agent run 的取消訊號與終態必須接線；不得只把 UI 按鈕設成 cancelled。durable restart recovery 仍留 Gate 5 |

**Agent 協議的邊界**（給第三方開發者的 wire contract）：

- **必須統一**：① OpenAI 相容 request/response；② named SSE 事件集（不統一的話，Shell 的單一 parser 不可能講 N 種方言）；③ 治理欄位——trace 關聯 id、classification、safe summary 的遮罩規則寫在協議層（`anila-contracts`，Gate 1 F5）；④ 模型與檢索呼叫回頭走 CSP（不變式 10）。
- **不必統一**：內部框架、規劃方式、記憶體實作、程式語言。LangChain 的 `astream_events` hooks 與 ANILA 事件集幾乎一對一，adapter 約一頁程式碼。
- **漸進降級**：不發事件的 agent 照樣能跑（只是沒有步驟列表），發了才拿完整 timeline。

**這個 Gate 明確不做**（v1.1 修正 v1.0 的內部矛盾）：
> v1.0 的退出條件寫「pause/approve/deny/cancel E2E 通過」，同一節又寫「不做 EventStore／replay」。**這是矛盾的**——沒有 durable event store，approval 在 refresh／restart 後無法恢復，approve 一個已遺失的 pause 是未定義行為。
> **Gate 4 只做唯讀 timeline ＋ 純 in-session 的 cancel。** durable pause／approve／resume／replay／idempotency／restart recovery **全部移到 Gate 5（R4＋R5）**。

**退出條件**：真實 skill → tool → retrieval 流程在 UI 可重現；完成數與狀態完全來自 backend event（不由前端猜）；in-session cancel 會真正停止 downstream run 並只寫一次 cancelled 終態；strict event schema、可信 task/trace/agent/session binding、size/rate/run budget 與 audit negative tests 全數通過，偽造 ID、raw reasoning／secret、unknown event 與 event flood 不會到 Shell；官方 `anila-agent` 與 LangChain adapter 對同一 frozen event fixture 產生符合 `anila-contracts` 的等價 named SSE。完整 `StreamBridge`／replay／restart recovery 仍屬 Gate 5。

---

### Gate 5 — 讓大腦正式化

**進入條件**：Gate 1–3 通過（Gate 4 非必要前置）。
**架構裁決（本路線圖最重要的判斷）**：**先抽，後建。**

兩份 GPT 報告在此開了方向相反的藥方：變更計畫書要把 RouterRuntime、contracts、PolicyGate 都**放進** `anila-core`；prod-intranet-card 報告要**拆解** `anila-core`。

決定性事實（實測）：
- `services/csp` 在 **33 個檔案** import `anila_core`（`anila_core.security` ×13）
- `anila_core.security` 只有 **656 行、單一第三方依賴**
- `anila-agent` 對 `anila_core` 的真實 import 是 **0**
- `RuntimeConfigPoller` 的消費者只有它自己＋自己的測試 → GPT「無 production consumer」**成立**
- `build_app()` 建空 `ToolRegistry` → GPT 引為缺陷，但 docstring 明載這是**刻意設計** → 此條為 GPT **誤讀**

**裁決**：
- 拆解方向對，但應**排序**。`anila-security`（Gate 1 F4）與 `anila-contracts v0`（Gate 1 F5）是最便宜的兩刀。
- 把 `router/decision.py`、`policy.py`、`agents/contracts.py` 放進 `anila_core`，等於讓 CSP 為了 import 契約而繼承 `fastapi`＋`asyncpg`＋`pgvector`＋`aiosqlite`＋`sse-starlette` 全套。**契約必須是獨立的薄套件。**
- RouterRuntime 本身留在 `anila-core` 沒問題（只有 router 消費）。

| ID | 工作 | 對應報告 |
|---|---|---|
| R1 | `anila-contracts` v2：加 `RouteDecision`／`PolicyGateResult`／`AgentManifest`／`ExecutionGrant`（短效） | 變更計畫書 WS1（修正放置位置與時機） |
| R2 | CSP internal registry view + `ready_for_dispatch` = approved + health ready + manifest valid + Full Trace + trace-test passed + classification sufficient | 變更計畫書 WS2 |
| R3 | RouterRuntime：`RequestContextBuilder` → `CapabilityFilter` → `DecisionEngine`（structured output，非 regex）→ `PolicyGate` | Router 分析 P0-1/P0-2 |
| R4 | `AgentClient` + 完整 `StreamBridge` + **`SessionEventStore`（cursor/replay/idempotency）** | 若 Gate 4 已做，沿用其 `StreamValidator/BridgeCore`；**若 Gate 4 被跳過，R4 必須把 T3 validator 一併實作**，因此 Gate 5 不反向依賴 Demo Lane。承接 durable HITL |
| R5 | 官方 `anila-agent` 升 Silver（**單一 Agent／單一 Task**的 formal manifest、完整 history/session、StepEvent、durable pause/resume、cancel、idempotency、restart recovery；修 `PostgresSession` protocol、實作 `run_once_state()`、strict mypy 歸零；**模型端點必須經 CSP，不能只靠 Developer Guide**） | 變更計畫書 WS5 ＋ **M10**。startup/admission 驗 manifest 宣告與 CSP model binding；部署 egress allowlist 阻擋任意 `ANILA_BASE_URL` |
| R6 | frozen routing eval dataset + CI gate | Router 分析 P1；變更計畫書 WS8。資料集需 version／hash、固定樣本量、類別分布與 denominator；不得在看結果後改 test set |
| 🆕 R7 | **Model governance**：由 signed profile 產生所有 enabled inference callsite inventory；agent-scoped model invocation、`ModelArtifact`／`Deployment`、GPU topology、license／readiness gate、usage reconciliation、admission **與** egress policy | 承 M7／M10／M13；inventory 必含 Router、正式 Agent、embedding、Studio／FLUX、Memory extract/embed、prompt generator、Ingestion relation LLM／Judge 及未來新增 callsite。第三方 Agent 同時要求 admission 綁 CSP model 與部署網路拒絕裸模型網段。**FLUX.2-dev 授權必須先由法務確認** |

**`DISPATCH:` 的處置**：只保留為 shadow/legacy adapter，**不能是正式 authority**。目前的 regex 路徑已靠大量補丁維持（`_sanitize_leaked_thought`、中點 bullet 修復、CJK agent_id 容錯），這本身就是它脆弱的證據。

**退出條件**：`anila-contracts v2` conformance tests 通過；100% 正式 dispatch 都來自 ready-for-dispatch registry snapshot，具有有效 `RouteDecision` 與 allow 的 `PolicyGateResult`；invalid JSON／prompt injection／unknown agent／unhealthy agent／stale snapshot 造成零 downstream call；StreamBridge schema／size／secret scan、cursor replay、idempotency 與 duplicate-event tests 全數通過；在 frozen、versioned、hash-pinned 的 eval dataset（樣本量／類別分布／denominator 固定）上 routing top-1 ≥95%、false dispatch <1%、policy bypass＝0；官方 Agent 通過 Silver conformance，包含「任意非 CSP `ANILA_BASE_URL` 同時被 admission binding 與部署網路 egress policy 拒絕」的 negative test；每個 model invocation 綁定核准的 artifact/deployment/license/readiness 並有 usage；**pause／approve／resume 在 restart 後可恢復**。

---

### Gate 6 — Production Acceptance（v1.1 新增，v1.1.1 客觀化）

> **這是整份文件唯一回答「No-Go 什麼時候變成 Go」的地方。**
> v1.0 宣告了 No-Go，卻沒有定義 Go 的條件——那讓「Gate 0 做完了，可以放機敏資料了吧？」變成一個沒有文件能反駁的問題。

**進入條件**：Gate 0–3 ＋ Gate 5 通過（Gate 4 非必要）。開始任何 drill 前，P0 的 acceptance profile 必須先簽章凍結；**不得看完結果再降低門檻**。

| ID | 驗收項 | 客觀 pass／fail 判準 | Owner／證據 |
|---|---|---|---|
| P0 | **signed acceptance profile** | 先固定 production topology、enabled/disabled features、資料分級上限、RTO／RPO、各 SLO 數值、load profile、觀測窗、workflow matrix、P5 樣本數、**machine-readable enabled inference callsite inventory**、token／卡片撤銷傳播 SLA、PKI stale／offline policy、Sev-1／Sev-2 taxonomy、finding acceptance rule、revalidation impact matrix 的 version/hash、簽核角色。任一欄缺失＝No-Go | system owner＋data owner＋PKI owner＋資安＋維運共同簽章的 versioned profile；hash 納入 release evidence |
| P1 | **restore drill** | 由 production-equivalent 備份還原，在 P0 RTO/RPO 內恢復；DB／blob／artifact／vector generation 的 row count、checksum 與 referential integrity 對得上；RLS、compartment 與撤銷後讀取 negative tests 全數通過。**不是「備份有跑」，是「還原過」** | 維運 owner；restore log、計時、checksum／RLS 報告 |
| P2 | 故障演練 | 逐一殺 redis／worker／csp、製造磁碟滿與網路中斷；所有已接受工作在 lease/retry 後進唯一終態，資料損失不超過 RPO，服務在 RTO 內恢復，且任何 failure 都不得 fail-open 或繞過分類／授權 | 維運＋開發；fault script、事件時間線、前後 reconciliation |
| P3 | SLO 與可觀測性 | 在 P0 的 signed load profile 下至少連續觀測 7 日；ingestion p99、dispatch 成功率、queue age、stuck job、artifact download 與 auth error rate 全部達 P0 數值，觀測期間無未處置 Sev-1／Sev-2 | 維運＋system owner；dashboard export、原始 metrics、incident list |
| P4 | **簽章 release envelope** | 單一 code line ＋ image／model digest ＋ SBOM ＋ CA bundle hash ＋ deployment config／topology／enabled features／data ceiling ＋ bundle signature ＋ startup posture assertion；從 air-gap bundle 於乾淨主機部署成功，runtime envelope 與簽章完全相符。取代「用分支代表部署姿態」 | release owner；manifest、SBOM、signature verification、clean-host deploy log |
| P5 | 分類端到端稽核 | P0 先固定的 workflow matrix 必須覆蓋所有 enabled workflow／登入法／分類層級／compartment；每個組合至少有正向與負向 fixture，再依預先固定的 N 做 production-like 抽驗。upload → chunk → retrieval → citation → artifact → trace 每一段分類、owner、compartment、snapshot 都一致，錯一筆即 fail | data owner＋資安；fixture matrix、sample manifest、逐筆 trace evidence |
| P6 | 獨立滲透式覆核 | 對照 §3 每一條（含已關閉者）與匿名 route inventory 重驗；必須由不同於修補者的具名 reviewer 執行。**未結案 Critical／High＝fail；Medium 只有符合 P0 finding rule、具 owner／expiry／補償控制與簽核才可接受；auth／classification／egress bypass 不得風險接受。** 1 人＋AI 無法自行滿足此條，未取得獨立 reviewer＝No-Go | 資安 owner；signed review report、finding closure／risk-acceptance evidence |
| P7 | 法務／授權 | 每個 enabled model／dataset／第三方元件都有軍方環境適用的書面授權裁決；未核准者必須從 signed profile、bundle、model registry 與路由能力中排除，不能只寫「暫不使用」 | 法務／採購 owner；書面裁決與 P4 manifest 對照 |
| P8 | **憑證卡／session assurance** | 在 production-equivalent 用戶端矩陣驗證支援的 OS／browser／HiPKI localhost:16888／reader／卡型：有效卡成功；過期／撤銷／錯誤 chain／錯誤 signer／錯誤 PIN 拒絕；CRL signature/issuer 與 signer cert serial/fingerprint 綁定正確，BasicConstraints／KeyUsage／EKU／pathLen／certificate policy negative cases 全拒絕；CA rotation 與系統時間偏差符合 policy；離線撤銷資料 stale／缺失時 **production 一律 fail-closed，不接受例外**；challenge sequential／multi-instance race 只有一次成功；refresh reuse 撤銷整個 family；指定 `jti` 於 P0 SLA 內撤銷且不誤傷其他 session；break-glass claims 正確並有 audit | PKI owner＋資安；client／X.509 matrix、token claim dump（遮密）、revocation timing、lost-card／離職演練、audit records |
| P9 | **模型與 Agent egress** | 從 P0 signed profile 自動產生**全部 enabled inference callsite inventory**，逐項做 network deny／capture 與 usage reconciliation；至少涵蓋 Router、正式 Agent、embedding、Studio／FLUX、Memory extract/embed、prompt generator、Ingestion relation LLM／Judge。正式推論只能經 CSP 核准端點；第三方 Agent 必須同時通過 admission model binding **與**部署網路 egress deny，任意非 CSP `ANILA_BASE_URL`／裸模型網段／裸 T2I URL 都被拒絕；每次推論都有對應 usage／audit row | platform owner＋資安；machine-readable callsite inventory、egress policy、packet／proxy evidence、usage reconciliation |

**退出條件 ＝ Go 條件**：P0–P9 全數通過，證據包與 acceptance profile hash 一致，且由 **system owner、data owner、PKI owner、資安與維運五方的人員簽核**。任一條無 owner、無證據、門檻未達或使用事後調整的 profile，結論一律是 No-Go。這時、也只有這時，機密以上的資料可以進入 production。

**Go 只對該 signed release envelope 有效，不是永久授權。** code/image/model/CA/config/topology/enabled feature/data ceiling 任一變更，都必須依 impact matrix 重跑指定的 P1–P9；無法證明不受影響時，預設完整 re-acceptance。

#### Gate 6 分軌（2026-07-18 平台擁有者裁示）：開發者軌 vs 組織驗收軌

> 本專案由一人維護。P0–P9 的 Go 條件**一字不改**（上表仍是唯一的 Go 定義），但工作清單自此分成兩軌：
> 開發者的收尾清單只含軌一；軌二是移交清單，由組織權責方啟動，不再出現在開發者 backlog。

**軌一：平台開發者軌（工程可完成＝收尾清單）**

- P0–P9 的全部「工程備料」：profile schema／驗證器、drill 與 eval harness、frozen eval、mutation 證據、
  machine-readable inventory、證據打包——目標是讓進場驗收窗口縮到最短（進場後照清單執行與簽名，
  而不是進場後才開始查）。
- **分級與平台設計理由報告**：對長官／政策讀者說明平台為何強制三級資料門檻、fail-closed、簽章
  acceptance profile 與各 gate 控制；P6（獨立覆核）與 P7（授權裁決）的政策依據寫在此報告，
  供權責方裁決時引用。
- P7 的工程配套只到「enabled model／dataset／第三方元件 × 授權條款對照表」為止（隨 P4 manifest
  交付）；裁決本身屬軌二。

**軌二：組織驗收軌（不在開發者收尾清單；owner 見上表）**

| 項目 | 需要什麼 | 誰啟動 |
|---|---|---|
| 進內網驗收窗口 | P0 五方簽章凍結、P1 restore drill、P2 fault drill、P3 七日觀測、P8 實體卡／reader／HiPKI／正式 CA/CRL 用戶端矩陣、P9 production packet capture 與 usage reconciliation | 維運＋各 owner（於平台主機執行，開發者可隨行支援） |
| P6 獨立具名覆核 | 不同於修補者的人類 reviewer＋signed report（上表原文：1 人＋AI 無法自行滿足） | 長官指派（資安 owner） |
| P7 法務／授權裁決 | 每個 enabled model／dataset／元件的書面授權裁決（開發者提供對照表） | 法務／採購 owner |

**分軌不是降門檻**：機密以上 production 的 Go 條件仍是 P0–P9 全數通過＋五方簽核；軌二未啟動前，
可部署的資料分級依 §6.1 封頂（無機密 pilot／營業秘密 pilot 的條件不受本分軌影響）。

---

### Gate 7 — 條件式能力

**進入條件**：Gate 6 通過，且 single-agent 路徑穩定至少一個 release，且**另行 Go/No-Go**。

| ID | 工作 | 觸發條件（不是時程） |
|---|---|---|
| K1 | Gold Agent（跨日／跨工作階段、複數人工核准點、補償／回滾、完整 evidence package、高分類營運 SLA） | Silver 只保證單一 Agent／單一 Task 的 durable pause/resume；只有出現跨日、高保證工作流需求才升 Gold |
| K2 | 受控 multi-agent（步數／深度／併發／timeout／cost ceiling；只有 read-only step 可平行；每步各自過 PolicyGate；分類只升不降） | **預設 off。** single-agent 的 policy／cancel／idempotency／trace 完整 |
| K3 | 動態技能發現（限已核准集合內） | **當單一 agent 的工具/技能數多到 prompt 放不下（經驗值 20–30 個以上）才有價值。** 只有 5 個工具時硬加 discover 是浪費一步延遲 |

**關於動態 plugin——拆成兩件事**：

- **動態「安裝」**（runtime 拉進未審核能力）：**永遠不做。** air-gap 進不了外部 registry、分類環境不允許未審核程式碼執行。這不是保守，是平台定義（不變式 3）。
- **動態「發現」**（在已核准集合內由模型即時挑選）：**可以做。** Router 已有 agent 粒度的雛形（`_build_agent_list` 把清單塞進 prompt 讓模型挑）。工具粒度就是給模型一個 `discover_relevant_skill(query)` meta-tool，檢索範圍限定在 CSP 已核准集合內，每步發事件、可稽核。
- 前置條件：① Gate 4 的事件透明化先通；② `skills/loader.py`／`runtime/mcp.py`／`triggers/runner.py` **真正接進 runtime**（現況只有測試在 import）；③ Gate 0 的 S6（prompt 注入防護）先修，因為技能描述會進 prompt，檢索式發現會放大注入面。

> 學它的 UX 模式（discover 作為可稽核的一步出現在 timeline），不學它的供應鏈模式（runtime 裝新外掛）。

---

## 6. 不可倒置的排序規則（真正的「路牌」）

### 6.1 三級資料門檻（v1.1 改寫——**這是全文最重要的一張表**）

> v1.0 寫「任何 pilot 放機敏資料 → Gate 0 全部完成」。**那句話是錯的，而且是危險的錯。**
> Gate 0 只關閉外部暴露；它不處理瀏覽器端 RAG（H7）、chunk 分類（G1）、clearance（G2a）、SourceSnapshot（G11）、ingestion 冪等（I1–I3）、artifact durability（A1–A4）、session／PKI assurance（G14–G18）。

| 你想放什麼資料 | 必須完成 | 為什麼 |
|---|---|---|
| **無機密** 受控 pilot | **Gate 0** | 沒有未登入資料路徑即可 |
| **營業秘密** 受控 pilot（資料與資安權責人書面核准的最小資料集；有稽核、保存期限與撤回機制） | **RAG／chat-only：Gate 0 + 1 + 2；啟用 Studio／Artifact／FLUX／export：再加 Gate 3** | Gate 2 讓分類、clearance／compartment、SourceSnapshot 與 server-side RAG 成立；Gate 3 才修跨使用者 artifact metadata、不可變 blob 與下載授權。在 Gate 3 前 signed **pilot** profile 必須關閉所有產物路徑。**不得以「可承受洩漏」作為准入條件**；若只能接受洩漏假設，就只能放 synthetic／去識別資料 |
| **機密以上** production | **Gate 0 + 1 + 2 + 3 + 5，且 Gate 6 acceptance 簽核通過** | 需要資料不會遺失（I1–I3）、產物不會外洩（A3）、還原演練過（P1）、分類鏈端到端可稽核（P5） |

**Gate 4（Agentic timeline）不在任何一列。** 它是 demo 價值，不提升 production readiness。

### 6.2 技術前置順序

| 你想做 | 必須先有 |
|---|---|
| 重構 CSP／anila-core | CI（F1）＋ RLS 真 PG 測試（F2）。**沒有測試網的重構是賭博** |
| 把 contracts 放進某個套件 | 先確認那個套件的依賴不會被 CSP 繼承（§5 Gate 5 裁決） |
| Shell 顯示步驟 timeline | agent 端真的在發事件（T1）。**不做前端動畫** |
| Shell 顯示 resume／replay | `SessionEventStore`（R4）＋ agent 端 durable resume（R5）。**Gate 4 連 pause/approve 都不做**（v1.1 修正） |
| PolicyGate enforce | CSP formal registry（R2） |
| 把 `DISPATCH:` 拿掉 | v2 authority 已 canary 通過，且連續一個 release 正式流量為 0 |
| **開 search clearance gate** | ① 舊 chunk 分類已 backfill 且抽樣驗證（G1 → G1b）；② **clearance 資料模型已存在（G2a）**——今天 `caller_clearance` 無資料來源 |
| 移除瀏覽器端 RAG | server-side RetrievalService（G10）。**這是營業秘密 pilot 的前置** |
| 信任 agent 送來的 safe summary | StreamBridge 二次驗證（T3）。**agent 自我遮罩不是信任邊界** |
| 動態技能發現 | 事件透明化（Gate 4）＋ skills 真的接進 runtime ＋ prompt 注入防護（S6） |
| multi-agent | single-agent 的 policy／cancel／idempotency／trace 完整（Gate 5 退出） |
| 新增任何 agent 類型／artifact 類型／memory 功能 | Gate 5 之後（Gate 1 的 F7 凍結令） |
| **宣告 production Go** | **Gate 6 的 signed acceptance profile 與 P0–P9 證據全數通過，由 system owner、data owner、PKI owner、資安與維運五方簽核。工程師不能自己宣告 Go。** |

---

## 7. 已推翻的假警報——**不要再撿起來做**

> 對抗式驗證推翻或降級了多條宣稱；逐列裁決才是權威，不再維護容易因子項重疊而失真的數量摘要。把它們記在這裡，是為了避免未來有人讀了原始報告又重新立案。

| 曾被列為 HIGH 的宣稱 | 裁決 | 為什麼 |
|---|---|---|
| 「文件上傳不繼承 collection 分類 → 下游看到無機密來源」 | **推翻 → LOW，但⚠ scope 受限** | 執行期 ceiling 的 task 等級是從 **collection** 導出（`tasks/service.py:100-124`），不讀逐檔標籤，**沒有 fail-open**。**v1.1 補充的 scope**：這個結論**只在 task-linked 路徑成立**。anilalm 聊天（H7）根本不建 Task，ceiling 退化為 conversation-derived，且瀏覽器注入的 chunk 分類不進 latch。所以「分類保護沒問題」是過度概括——**原宣稱的具體技術內容仍是錯的，但它指向的擔憂在另一條路徑上是對的** |
| 「`CARD_DEV_SKIP_NONCE_BINDING` 可經 `.env` 誤開 → 未認證 owner 登入」 | **推翻 HIGH → MEDIUM（M5）** | 該旗標**不在 compose 的 csp `environment:` 區塊**，且 csp 無 `env_file:` → 在 `.env` 設它傳不進容器。**只需加 startup gate（S8），不需重寫卡登入** |
| 「`create_all` fallback → 新表無 RLS → 跨 collection 讀分類資料」 | **推翻 HIGH → MEDIUM（M4）** | `create_all(checkfirst=True)` 是預設值 → 既有帶 RLS 的分類表被**跳過**而非脫除。**仍需 fail-stop（S7），但不是資料外洩** |
| 「owner 密碼 break-glass、token 同時回 JSON body 是正式阻斷」 | **降級 LOW–MEDIUM** | 兩者皆為 code 內明示的刻意設計（含拍板日期註解），屬 hardening 議題 |
| 「anilalm token 殘留在 localStorage」 | **降級** | prod-intranet-card 下 `login()` unreachable，token 從不寫入 localStorage。真正殘留的是 artifact **metadata/titles**（M3） |
| 「FLUX alias 相撞是 live risk」 | **推翻** | `GROUP_INTRANET`（`model-serve.sh:27`）不含 flux。（但 GPT 相撞另有實據：`gemma4` 的 `device_ids:["3"]` 是**寫死**、不吃 env） |

### ⚠ 一條被翻回來的假警報（v1.1）——以及它的教訓

**v1.0 曾把「`document_ids` 過濾在全域 top-k 之後」列為假警報，理由是「互動搜尋根本不帶 `document_ids`，失敗情境不可達」。這個裁決是錯的。**

- 對抗式驗證者確實查了互動搜尋（`WSChat`）、直接 search API 與 Slides——**那三條路確實不帶 `document_ids`**。
- 但**沒有查 Studio 的 job 提交路徑**。而 Report／Mindmap／Infographic／Datatable **四條路徑全部都帶**（`generators.ts:243/398/440/483`），一路傳到 `search.py:517-519` 的 post-filter。
- 已升為 **M6**（見 §3.3）。

**教訓（已寫進本文件的「更新規則」）**：對抗式驗證的**範圍**和它的結論一樣重要。「我查了三條路徑，都不觸發」不等於「不可達」。把一條宣稱放進假警報表之前，先證明你列舉了**所有**呼叫者，而不是列舉了你想得到的呼叫者。**一條錯的假警報比沒有假警報清單更危險**——因為它會讓未來的人不再檢查。

**同樣地，這三條「疑似正式阻斷」經查證全是環境問題，不是缺陷**：`test_expired_token_rejected` 單獨跑會過（全套失敗是測試污染）；`test_single_head_in_r1_namespace` 失敗原因是 cp950 讀不了 `alembic.ini`；`test_swagger_ui_is_404_by_default` 是 `assert 401 == 404`（`/docs` 有擋，只是沒隱藏）。

---

## 8. 人力現實

計畫書假設「1 名技術負責人、3–4 名後端／Agent 工程師、1 名前端、1 名 QA/SRE」跑 9 個 Sprint。GPT-5.6 假設「2 backend、2 frontend、1 platform/SRE、1 QA/security」。

**實際是 1 人＋AI 協作。** 這改變三件事：

1. **Gate 之間串行，不平行。** 計畫書說「Phase 3 與 Phase 4 可平行」——那個前提是不同工程師。唯一的例外是 Gate 4（Demo Lane），它可以與 Gate 3 交錯，因為它不碰資料路徑。
2. **不要用壓縮 Gate 來換時程。** 退出條件是安全底線，不是可談判的範圍。
3. **優先做「便宜且高槓桿」的事。** 這是 Gate 0 與 Gate 1 F4 排在前面的原因。

**Gate 0 的時程修正（v1.1）**：v1.0 寫「數天，全是設定或一行級」。**那低估了兩件事**：

- **S1、S5、S7、S10、S11 都需要部署與回歸驗證**，而不只是編輯檔案。S11（Dockerfile 加 `USER`）會動到檔案權限，不是零風險。
- **CI 在 Gate 1，所以 Gate 0 的每一項都只能手動驗證**（§3.4-2 的排序張力）。

修正估計：**2–3 週**。個別修改仍是一行級——但「改完」不等於「驗完」。

**每個 Gate 結束時**：跑一次完整驗證（不只看 diff），分清 regression vs pre-existing，跨切面改動從 `main` 起、再 port downstream。

---

## 9. 本路線圖未驗證的部分（誠實揭露）

以下是**沒有**親自驗證的，引用時請標註來源而非當成已證實：

- **真 PostgreSQL 下的 RLS 行為**——本次無 PG 環境。這正是 F2 存在的理由。
- **Studio／pptx-renderer 的測試現況**——未跑。（但 Studio 的 `document_ids` 呼叫鏈已於 v1.1 靜態驗證，見 M6。）
- **ingestion 的 ruff 基線**——未跑。
- **GPT-5.6 §13.1 的備份／DR／restore drill 宣稱**——只驗了與 C1 相關的部分。**這正是 Gate 6 P1 存在的理由。**
- **FLUX.2-dev 的 BFL Non-Commercial 授權**——正式啟用前需法務確認。**這不是工程可以自行假設的事**（Gate 6 P7；若要提早啟用圖像功能則需提早確認）。
- **`cht/` 測材是否已進入 git history 的遠端**——S3 需要先做 history 掃描才能定範圍。
- **H6 的實際暴露面**——已驗證 nginx 設定與寫入鏈，但**未實際對執行中的部署發出未認證請求**。H5 只能證明新 air-gap bundle 缺 image，不能證明既有內網沒有 image；因此內外網的 live 暴露狀態都標記為**未知，需實測**。
- **M7 的圖像 usage 是否有其他記帳路徑**——已驗證 `flux_client.py` 直連且 CSP 不在資料路徑上，但未窮舉是否有旁路的計量。

---

## 10. 外部審查裁決紀錄（v1.0 → v1.1 → v1.1.1）

> GPT-5.6 對 v1.0 提出一組 top-level 與子項修訂意見。每一條的**新事實宣稱**都派了獨立查證代理回程式碼求證。v1.1.1 再做一次跨元件與 Gate 一致性覆核。這裡不再用容易誤算的「幾點採納／駁回」摘要，直接保留逐項裁決與依據。

| # | GPT-5.6 的意見 | 裁決 | 依據 |
|---|---|---|---|
| 1 | Gate 0 退出條件無法達成（H2 排在 Gate 2；S1b 漏了 `prod-intranet-card` 的 n8n/GitLab；漏了 `/uploads/flux`；S2 只改建立端）；「數天」過於樂觀 | **✅ 採納，但處方修正** | 全部查證屬實。**處方修正**：H2 不必整條移進 Gate 0——加 `ENABLE_MEMORY` kill switch（S2b）即可止血，完整修復留 Gate 2。**S2 的處方也修正**：讀取端是即時查詢，改 level 判斷後分類升級會自動失效，**不需要**額外的「撤銷舊 token」機制 |
| 2 | Gate 0 完成後仍不能放機敏資料 | **✅ 全盤採納。這是最重要的一條** | v1.0 §6 那句「任何 pilot 放機敏資料 → Gate 0 全部完成」是全文最危險的錯誤。已改寫為 §6.1 三級門檻，並新增 Gate 6 |
| 3 | contracts 與共同交易排太晚 | **✅ 採納** | Gate 4 要用 StepEvent 契約，v1.0 卻把 `anila-contracts` 排在 Gate 5。已改為三階段抽取（F5 → Gate 2 → R1）。「只有 Ingestion 排了 outbox，Task／Policy／Audit／Artifact 沒有交易閉合」屬實 → 新增 G6b |
| 4 | Gate 3 應拆成展示層與正式 HITL；safe summary 需二次驗證 | **✅ 採納** | v1.0 的 Gate 3 同時寫「approval E2E 通過」和「不做 EventStore」——**內部矛盾**。已拆為 Gate 4（唯讀）與 Gate 5 R4/R5（durable）。「agent 自我遮罩不是信任邊界」與不變式 2 一致 → 已改寫不變式 5，新增 T3 |
| 5a | SourceSnapshot 是「空殼」 | **⚠ 採納方向，修正事實** | **不是空殼。** `models/source_snapshot.py` 的 table 與欄位齊全，`Citation` 已刻意設計為指向不可變快照（不掛 live FK）。缺的是**寫入面**（`create_task` 只寫 collection_ids）。→ G11 的措辭是「接線」不是「建立」。**未來的人請不要重新設計這個 model** |
| 5b | browser-side RAG「根本沒有受到 Task-derived ceiling 保護」 | **⚠ 部分成立，整體誇大** | 「是瀏覽器端 RAG」✅、「不建 Task」✅。但 `enforce_model_ceiling` **仍在此路徑執行**（`proxy.py:789-795`），只是等級來源退化為 conversation latch，且模型未設 ceiling 時 no-op。精確表述已寫入 H7 |
| 5c | `document_ids` recall bug 應是 MEDIUM，不是假警報 | **✅ 完全正確——這是 v1.0 的錯誤** | Studio 四條路徑全部帶 `document_ids`。已從 §7 移出、升為 M6，並把教訓寫進「更新規則」 |
| 5d | §7 把 document classification 降為 LOW 不合理 | **⚠ 部分採納** | 原裁決的**技術內容仍成立**（ceiling 從 collection 導出，無 fail-open）。但它的**適用範圍**被高估——只在 task-linked 路徑成立。已在 §7 加 scope caveat，並把真正的缺口獨立為 H7 |
| 6 | Gate 4 範圍不足，且應排在 Agentic UI 前 | **✅ 採納** | redis `--appendonly no` 屬實（§3.4-7）。已換序（資料可靠 → Gate 3；Agentic UI → Gate 4 Demo Lane）並補 I2b／I2c／I8。backfill 從排序表補進工作項（G1b） |
| 7 | Artifact／Studio 整條工作流被漏排 | **✅ 完全採納** | v1.0 把 M2／M3 列在 MEDIUM 卻**沒有任何 Gate 負責修**。已新增 Gate 3B（A1–A6） |
| 8a | `caller_clearance` 無資料模型 | **✅ 完全採納** | 實測：`User` 只有 `role`／`department_id`，全 repo 含 migration 零個 clearance 模型。→ 新增 G2a 作為 G2 的前置 |
| 8b | 三路登入產生相同 claims；需 `sid/jti/amr/acr`、one-time challenge、離線撤銷名單 | **✅ 採納，一處修正；v1.1.1 再擴充** | claims 相同 ✅、無 `amr/acr/jti` ✅、challenge 無 consume ✅。**修正**：撤銷機制存在（per-user `token_version`＋事件表＋Redis），缺 per-token 粒度。v1.1.1 另驗出 refresh reuse 與憑證 CRL/X.509 profile 缺口。→ M8／M9／M11／M12／G14–G18 |
| 8c | **「正式 Agent 仍可直連模型，繞過 CSP Model Gateway」** | **⚠ 按元件拆分後部分成立（v1.1.1 修正 v1.1 的過度駁回）** | **Router 部分不成立**：`anila-core-router` 打 `{csp_base_url}/v1/chat/completions`（`router_server.py:2005`），帶呼叫者 Bearer（`:1992`）；容器不在 `anila-models-net`。**官方 Agent 部分成立為合規缺口**：`packages/anila-agent/configs/model.yaml:6` 預設裸模型 URL，`config.py:77` 接受任意 `ANILA_BASE_URL`，`runtime/model.py:49-53` 直接建立 `AsyncOpenAI`；Developer Guide 要求人工作業改 CSP URL，但 approval／trace-test／egress policy 不強制。未證明現行 MLSteam Agent 正在繞過，故列 **M10**，不是 live incident。**圖像部分是已確認 live path**：`flux2-dev-agent`／`anila-studio` 直連 T2I、不寫 `token_usage`，列 **M7** |
| 8d | 簽章 deployment profile 不能留到條件式 Gate | **✅ 採納** | v1.0 §2.4 自稱「這是真正的風險」卻排在 Gate 6 條件式能力。已拆為 Gate 1 F6（startup posture assertion ＋ image digest）與 Gate 6 P4（完整簽章 release ＋ SBOM） |
| 9 | Gate 順序應加入 production acceptance | **✅ 完全採納** | v1.0 宣告 No-Go 卻從未定義 Go。新增 **Gate 6** |
| 10 | roadmap 與三份報告尚未 git 追蹤，不是真正的 SSOT | **✅ 採納，user 已於 2026-07-10 指示依序提交** | 依兩個 commit 切分：① roadmap v1.1.1 ＋三份 gpt-report；② `AGENTS.md`／`CLAUDE.md` 的分支模型與交付缺口更正 |

**淨結果**：大多數結構意見採納，部分事實或處方經程式碼修正；查證新增 H6（`/uploads/` 未認證）、M7（圖像直連）、M8（nonce 可重放）、M10（官方 Agent 模型端點未強制）、M11（卡片憑證撤銷來源缺口）、M12（refresh reuse）、M13（背景推論旁路）與 §3.4-7（redis volatile queue），並更正 v1.0 的 M6 誤判與 v1.1 對 8c 的過度駁回。以逐項表為權威，不再用互相重疊的數量摘要。

### 10.1 v1.1.1 一致性修訂

1. **Gate 4 不再反向依賴 Gate 5**：T3 改為薄型 `StreamValidator/BridgeCore`，完整 `StreamBridge` 留 R4；新增 T5 負責真實 cancel propagation。
2. **Gate 退出條件改成可執行證據**：Gate 2 用全量 reconciliation 證明 100%，補 compartment／challenge replay／`jti` 撤銷；Gate 3 補 owner／membership／compartment；Gate 6 新增 signed acceptance profile 與 P8/P9。
3. **Production Go 不再由工程師主觀宣告**：P0 先凍結數值門檻與 workflow matrix，P1–P9 產生 evidence package，由 system owner、data owner、PKI owner、資安、維運五方簽核。
4. **營業秘密不再以「可承受洩漏」作門檻**：真實營業秘密 pilot 必須有資料與資安權責人書面核准；能接受洩漏假設的環境只能使用 synthetic／去識別資料。
5. **卡片登入補上憑證層撤銷策略**：現況只驗鏈／效期／nonce，CRL／OCSP 是明示不做；G17 與 P8 將離線撤銷來源、freshness、lost-card／離職流程與殘餘風險簽核納入正式門檻。
6. **營業秘密 pilot 不得先繞過模型治理**：G19 要求 Gate 2 的 signed pilot profile 盤點全部 enabled inference callsite；未經 CSP／ceiling／usage 驗證者必須關閉，完整 production governance 再由 R7 接手。

---

## 11. 給未來 session 的一段話

這個專案的程式碼品質不差。憑證卡 CMS 驗證是真的（不是假 SSO）、五級分類的契約設計是嚴謹的（`total_ordering` enum、「排序不可變」寫在 docstring 裡）、SSRF guard 是 fail-closed 的、CSP 的治理資料模型很完整、Ingestion 的功能廣度是全專案最強的一塊。**問題從來不是「做得爛」，而是「控制面的概念比執行路徑成熟，而這些控制還不是不可繞過的系統不變式」。**

具體來說：分類欄位加了 migration，但 worker 不寫、search 不讀；trace SDK 寫好了，但 compose 不注入 endpoint；agentic UI 元件寫好了還有單測，但沒人 import；`delete_document()` 寫好了，但零呼叫點；`similarity_search_per_document()` 寫好了，但只被 relation 展開用到；`SourceSnapshot` 的 table 與 `Citation` 的不可變設計都做好了，但 `create_task` 只寫 collection ID；RLS 政策寫好了，但測試跑 SQLite。**一次又一次，能力被建好、卻沒有接線。**

所以這份路線圖的核心不是「加東西」，而是**接線、立約、驗證**。當你想加一個新 agent、新 artifact 類型、新 memory 功能時，先問一句：**上一個建好的東西接線了嗎？**

還有兩句，是 v1.1／v1.1.1 才學到的：**當你把一條缺陷判為「不可達」時，先數一數你列舉了幾個呼叫者；當你把一個 Gate 判為「通過」時，確認每個工作項都有可執行的退出證據。** 操作指南、人工約定與「預期應該如此」都不是 enforcement。

---

## 附：來源交叉索引

| 本文件 | 來源報告 | 驗證 |
|---|---|---|
| §3.1 C1 | GPT-5.6 §13.1-3（**低估**：只提 JWT 私鑰） | 逐行親驗 ＋ 對抗驗證推翻失敗 ＋ 原始發現者 → **三重確認** |
| §3.2 H1 | GPT-5.6 §7.1 | 獨立發現並確認；v1.1 追加讀取端與 `expires_at` 證據 |
| §3.2 H2 | GPT-5.6 §7.1 | 確認；CRITICAL → HIGH（per-user，非跨使用者外洩）；v1.1 追加「無 kill switch」 |
| §3.2 H3/H4 | GPT-5.6 §十一 | 確認 |
| §3.2 H5 | 無（兩份報告皆未提） | 本團隊獨立發現 ＋ 二次確認 |
| §3.2 H6 | 無（GPT-5.6 對 v1.0 的審查提及方向） | **v1.1 查證確認**：nginx `/uploads/` 全域無 auth，僅 `ingestion/` 被擋 |
| §3.2 H7 | GPT-5.6 對 v1.0 的審查（**誇大**：稱「完全沒保護」） | **v1.1 查證修正**：ceiling 仍執行，但輸入不含瀏覽器注入內容 |
| §3.3 M6 | GPT-5.6 對 v1.0 的審查 | **v1.1 更正 v1.0 的誤判**。Studio 四條路徑實證 |
| §3.3 M7 | 無（雙方皆未抓到；GPT 的 8c 指控方向相反） | **v1.1 獨立發現**：LLM 已收斂、圖像未收斂 |
| §3.3 M8/M9 | GPT-5.6 對 v1.0 的審查 | 查證確認；撤銷機制部分修正 |
| §3.3 M10 | GPT-5.6 對 v1.0 的審查＋v1.1.1 元件拆分覆核 | Router 路徑已收斂；官方 Agent starter 的 model endpoint 仍只靠指南、未強制 |
| §3.3 M11 | v1.1.1 card-primary acceptance 覆核 | `card_auth.py:30-31` 明示不做 CRL／OCSP；補 G17 與 Gate 6 P8 |
| §3.3 M12 | v1.1.1 session assurance 覆核 | `/refresh` 不消費舊 token；補 G18 token family／rotation／reuse detection |
| §3.3 M13 | v1.1.1 inference inventory 覆核 | Memory／prompt generator／Ingestion relation／Judge 旁路；補 G19 pilot closure、R7 與 P9 production inventory |
| §3.4-1（RLS 不可測） | 無 | 本團隊獨立發現 |
| §3.4-2（無 CI） | GPT-5.6 §13.1-7、§14.1 | 確認；並實跑 CSP 全套（兩份報告都沒跑） |
| §3.4-3（Full Trace no-op） | 變更計畫書 §2.7 ＋ GPT-5.6 §7.3 | 確認（compose 全文零 TRACE） |
| §3.4-5（clearance 無來源） | GPT-5.6 對 v1.0 的審查 | 查證確認 |
| §3.4-6（SourceSnapshot 沒接線） | GPT-5.6 對 v1.0 的審查（**用詞「空殼」不精確**） | **v1.1 查證修正**：schema 齊全、Citation 設計正確、缺寫入面 |
| §3.4-7（redis volatile） | GPT-5.6 對 v1.0 的審查 | 查證確認（`platform.yml:201`） |
| §3.4-8（無 Go 條件） | GPT-5.6 對 v1.0 的審查 | **v1.0 的結構性缺口** → Gate 6 |
| Gate 4（Agentic UI） | 變更計畫書 WS6 ＋ Router 分析 P0-3 | 確認「零件存在、沒接線」；**修正計畫書漏掉的 `sendCompare` 路徑** |
| Gate 5（架構裁決） | 變更計畫書 WS1-5 **vs** GPT-5.6 §4.1（方向相反） | 以 import graph 實測裁決：**先抽後建** |
| Gate 6（Production Acceptance） | GPT-5.6 對 v1.0 的審查＋v1.1.1 一致性覆核 | v1.1 新增；**v1.1.1 以 signed profile、P0–P9、具名 owner 與 evidence package 客觀化** |
| §7（假警報） | — | 對抗式驗證產出；v1.1 翻回一條並記錄教訓 |
| §10（外部審查裁決） | GPT-5.6 對 v1.0 的審查 | **v1.1 新增**：四路查證代理逐條求證 |
