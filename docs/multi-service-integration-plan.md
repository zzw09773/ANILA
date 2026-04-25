# ANILA Multi-Service Integration Plan v0.1

**Status**: Draft for review
**Date**: 2026-04-25
**Author**: ANILA 平台團隊
**Companion docs**: [`ingestion-platform-design.md`](./ingestion-platform-design.md) · [`anila-core-boundary.md`](./anila-core-boundary.md)
**Source of investigation**: `/home/aia/c1147259/project` 目錄實際 grep 結果

---

## 0. Decisions Log

### v0.6 (2026-04-25) — Phase 1 全部完工 + 結構性決策修訂

**Phase 1 §8.1 task list 全 11 項 + 1 follow-up 完成。** Branch `ingestion-design`，14 個 commits（從 `e611d89` Step 1 migration 到 `c322427` nginx resolver tech debt）。

| Step | 狀態 | Commit |
|---|---|---|
| 1 / 1.5. Migrations 0012 + 0013 | ✅ | `e611d89` + `cc121e3` |
| 2 / 2.5. access_control + grant CRUD + is_public | ✅ | `7c4df4a` + `91f1b10` |
| 3. AUTO_REGISTER_LINKS 5 筆 | ✅ | `c9aefc7` |
| 4. CSP UI Service Access management（Vue）| ✅ | `af450ea` |
| 5–8. codeserver + GitLab + n8n + nginx 同源 | ✅ | `11d572c` → `5c2a5ac` |
| 9–10. E2E grant flow + 同源路由驗證 | ✅ | `28fc351` |
| 11. My-OpenAI-Frontend 停用 | ✅ | (user 直接 down，README 不動 — 不是我們的 repo) |
| follow-up. n8n self-hosted | ✅ | `88df3fc` |
| follow-up. /static + /uploads share dir | ✅ | `d687080` |
| follow-up. nginx resolver auto-recover | ✅ | `c322427` |

**結構性決策修訂：**

| # | 之前 | v0.6 修訂 | 為什麼 |
|---|---|---|---|
| 30 | §6 規劃 NotebookLM Phase 4 agentification（單 agent + 9 tools 路線 B）| **取消 agentification，NotebookLM 永遠保持獨立 service** | NotebookLM 對話流（給文件 → 生 PPTX/PDF/mind map）跟 OpenAI chat 完全不同範式，包成 agent 收益小、複雜度高。保持獨立 service + platform_link 卡片 + 之後做 OIDC SSO 即可 |
| 31 | nginx 每次 service recreate 都要手動 `docker restart anila-nginx` flush stale upstream IP | **`resolver 127.0.0.11 + variable proxy_pass`** 自動 re-resolve | codeserver pivot saga 撞了 4 次 502 才察覺，commit `c322427` 一次解決 |
| 32 | 前述 Phase 1 過程中 nginx 對 `set` / `rewrite ... break` 的順序敏感不知道 | doc 化 trap：**`set` 必須在 `rewrite ... break` 之前**（同 module，break 終止後續 set 處理）| `c322427` commit message 詳細紀錄 |

**§6 整段重新定位：** Phase 1 SSO + grant 不變（已實作）；§6.1–6.5 agent 化內容**標為 cancelled**，保留作為「曾考慮的 alternative，後人不要重複論證」的紀錄。

### v0.5.5 (2026-04-25) — codeserver 真實 root cause: image fork + nginx config 一起錯

v0.5.4 把 codeserver 改 dedicated port 仍然爆炸，最後翻 [`/home/aia/c1147259/project/My-OpenAI-Frontend/docker-compose.prod.yml`](https://) + `nginx.prod.conf` 對照組裡 prod 跑了多年的 working setup，找到真正 root cause：

| # | 之前以為的問題 | 真實 root cause |
|---|---|---|
| 26 | code-server 對 subpath 不友善 | **是 `lscr.io/linuxserver/code-server` fork 的問題**。fork 額外的 s6-overlay init 讓 client bundle 收到 X-Forwarded-Prefix 後構造 `_doResolveAuthority` URL 失敗。upstream `codercom/code-server` 沒有此問題，**subpath 部署完全 work**（My-OpenAI-Frontend prod 跑了好幾年） |
| 27 | 多塞 X-Forwarded-Prefix / X-Forwarded-Proto 才會工作 | 反過來，**少塞越好**。My-OpenAI-Frontend 只送 `Host $http_host` + `Upgrade $http_upgrade` + `Connection 'Upgrade'`（literal 字串），其他都不要。code-server upstream 自己會從 Host 推所有需要的東西 |
| 28 | nginx `proxy_pass http://codeserver:8443/` 用 trailing slash strip prefix | 改用 **`rewrite ^/codeserver/(.*)$ /$1 break;` + 無 trailing slash 的 proxy_pass**。My-OpenAI-Frontend 證明這 pattern 對 code-server 比 trailing slash 友善 |
| 29 | PROXY_DOMAIN env 必須設成 user 看到的 host:port | **完全不設 PROXY_DOMAIN**。upstream image 不需要 |

最終 working setup：
- Image: `codercom/code-server:latest`（不是 lscr.io fork）
- Port: 8080（upstream 的 default）
- subpath: `/codeserver` 透過 nginx rewrite + minimal headers
- 無 PROXY_DOMAIN env

`§5.0.1` 設計表的「同源 path 採用」這列**仍然成立** — subpath 對 codercom/code-server upstream 完全 work。之前 v0.5.4 改 dedicated port 是錯誤的判斷（誤以為是 code-server 通病，實際是 image fork bug）。

**修正 commits**: `<TBD>` — docker-compose.yml + nginx.conf + AUTO_REGISTER_LINKS + design doc

### v0.5.4 (2026-04-25) — codeserver 短暫嘗試 dedicated port（v0.5.5 已 revert）

實際 browser 測試發現 codeserver subpath 部署有 hard limitation：

| # | 問題 | 修正 | 為什麼 |
|---|---|---|---|
| 25 | code-server `_doResolveAuthority` 在 subpath 部署下 throw `Failed to construct 'URL': Invalid URL` | 改成 **dedicated port `:8443`**（nginx 加新 server block，不再 strip prefix）| ~~code-server client bundle 有 hardcoded URL builders 不認 `X-Forwarded-Prefix`~~ — 此判斷在 v0.5.5 推翻：實為 lscr.io/linuxserver fork 的 bug，換 codercom upstream 後 subpath 完全 work |

對 §5.0.1 設計表的修訂：
- 原本「同源 path 採用」這列要改寫，subpath 在 code-server 上是錯的選擇 ~~v0.5.5 推翻：subpath 對 upstream image 完全 work~~
- 但 GitLab 跟其他 service 仍走 subpath（GitLab 自己對 subpath 部署支援度高，已驗證 work）
- 所以 §5.0.1 的決策表應理解為「**個別 service 看狀況決定 subpath / dedicated port**」，不是統一 subpath

**修正 commits**: `34ec13f` — 已被 v0.5.5 revert

### v0.5.3 (2026-04-25) — Phase 1 Step 5-8 落地 + GitLab Subpath 真實 trap

實際把 codeserver + GitLab 跑起來才發現 §5.0.1 / §5.0.2 設計遺漏的 4 個 trap，doc 補進來：

| # | 問題 | 修正 | 為什麼 |
|---|---|---|---|
| 21 | nginx `proxy_set_header Host $host` 對 subpath service 不夠 | 改 **`Host $http_host`** | `$host` 只有 server name 不含 port。GitLab 收到 `Host: localhost`（沒 :4443）後 redirect 也少 port，跳到 default port 443 server block 結果落到 CSP backend 拿到 CSP HTML。`$http_host` 保留完整 Host header（含 port） |
| 22 | GitLab Docker healthcheck 設 `/gitlab/-/health` 一直 unhealthy | 改 **`/gitlab/users/sign_in`** | GitLab 的 `/-/health` 有 `monitoring_whitelist`（預設 127/8 only），nginx 透過 docker bridge 172.20.x.x 來打就 404；in-container loopback OK 但 external monitor 不通。`/users/sign_in` public + 200 即代表 Puma 在 serve |
| 23 | Edit 工具改 bind-mounted file 後 `nginx -s reload` 沒效果 | 必須 **`docker restart anila-nginx`** | Edit 工具是 atomic rename（換新 inode）；Docker bind mount 釘原 inode，container 還看舊檔。reload 只重讀同一份 in-memory config。改 nginx config 必須 restart container |
| 24 | docker-compose `expose:` 先寫 `8443` 上面提到「expose 不對外」可能讓人誤會 | doc + compose 註解都加上「**expose ≠ publish**」說明 | `expose` 只是 declarative metadata 不開 host port；`ports:` 才是 publish。codeserver/gitlab 都用 expose 而非 ports，刻意只給內網 nginx 看 |

**Step 5+6+7+8 落地 commits**：
- `11d572c` — codeserver + gitlab compose + nginx 同源 path 初版
- `ec2272e` — Host header fix（trap #21）+ GitLab healthcheck URL fix（trap #22）

**驗證**：
- `https://localhost:4443/codeserver/` → 302 redirect 到 `/codeserver/login` ✓
- `https://localhost:4443/gitlab/` → 302 redirect 到 `/gitlab/users/sign_in`（port 保留）✓
- `https://localhost:4443/gitlab/users/sign_in` → 200，HTML 是真 GitLab login page，asset path 都帶 `/gitlab` prefix ✓
- GitLab 初始 root password 在 `docker exec anila-platform-gitlab-1 cat /etc/gitlab/initial_root_password`，**首次登入立即 rotate**

**已知未解但 acceptable**：
- 既存 gitlab container 仍顯示 `unhealthy`（用著舊 healthcheck URL `/gitlab/-/health`），純粹 cosmetic — 容器其實 work。下次 `docker compose up -d gitlab` 觸發 recreate 即會套新 healthcheck 變 healthy

### v0.5.2 (2026-04-25) — Phase 1 Step 3 落地 + URL/語意修正

| # | 設計（v0.5 之前） | 實作（v0.5.2 修訂） | 為什麼 |
|---|---|---|---|
| 18 | NotebookLM/Code Server URL 用 internal DN（`notebooklm.internal:3100`）| 全部改 **nginx 同源 path**（`/notebooklm`, `/codeserver`, `/n8n`, `/gitlab`）| Browser 無法解析 internal DN 且 cross-origin 觸發 Mixed Content 警告。同源 path 解決此問題並讓 Phase 3 OIDC SSO 不需處理 cookie cross-origin |
| 19 | n8n / GitLab / MLSteam 的 `required_roles=['admin', 'developer']` | 改為 `['developer']` + `is_public=true` | v0.5.1 admin bypass 提前後不需 explicit 列 `'admin'`；`is_public=true` 才能讓 developer 不需 grant 就看到（之前 `is_public=false` 會卡在 grant check 步驟 5）|
| 20 | `auto_seed.py` 的 AUTO_REGISTER_LINKS 處理只認 5 個欄位、INSERT-only | 擴充認 `is_public` + `required_roles` + 改成 idempotent upsert | 0012/0013 加的欄位舊 seed 不認；env 改了 restart 不會 sync 是隱蔽 bug |

**Step 3 落地 commit**：`<TBD>` — `docker-compose.yml` AUTO_REGISTER_LINKS 5 筆 + `auto_seed.py` upsert 邏輯。

3 role × 6 link 訪問矩陣全部驗證對齊預期（admin: 6, user: 1, developer: 4）。

### v0.5.1 (2026-04-25) — admin bypass 提前

| # | 設計 | 修正 | 為什麼 |
|---|---|---|---|
| 18-pre | algorithm step 2 = role gate, step 3 = admin bypass | 對調：step 2 = admin bypass, step 3 = role gate | `required_roles=['developer']` 會把 admin 擋掉，跟「admin 永遠通過」矛盾；提前後 §7.1 不必寫 `['admin','developer']` 那種重複 |

修正 commit：`91f1b10`。

### v0.5 (2026-04-25) — Phase 1 Step 1+2+2.5 落地，記錄與 v0.4 設計的差異

實作 access control 過程中發現幾個 design doc 與真實 schema / codebase 慣例的不對齊，已修正：

| # | 設計（v0.4 之前） | 實作（v0.5 修訂） | 為什麼 |
|---|---|---|---|
| 12 | Migration `0013` 一支搞定三表 | 拆成 **`0012`**（required_roles + service_access_grants + dev_db_credentials）+ **`0013`**（is_public column + grandfather backfill）| 寫的時候才發現現有最高 revision 是 0011（不是 0012），且 is_public 是後來才補的設計（v0.4 沒有），分兩支讓 migration history 反映真實的設計演進 |
| 13 | PK 用 `BIGSERIAL` / `BIGINT` | PK 用 **`Integer` / `SERIAL`** | 既有 schema 全部用 Integer，跟著走；grant 數量級 < 10K，BIGINT 是過度設計 |
| 14 | `required_roles TEXT[] DEFAULT NULL` | `required_roles JSONB DEFAULT '[]'::jsonb NOT NULL` | codebase 慣例（[`agent.py`](../myCSPPlatform/backend/app/models/agent.py)）已用 `JSONValue` pattern 處理 JSON-shaped data；JSONB 跟 TEXT[] 的查詢效能對 < 10 個元素的 array 沒差別 |
| 15 | `UNIQUE (user_id, link_id)` 純 unique | **partial unique `WHERE revoked_at IS NULL`** | 原版 unique 會擋「revoke 後 re-grant」（除非手動清 row）。partial unique 讓 revoked row 留作 audit trail，新 grant 不撞 unique |
| 16 | 沒有 `is_public` column，algorithm 是「default-deny + role auto-pass」 | **新增 `is_public` BOOLEAN**（migration 0013）+ 演算法改成 5 步：`is_active → role gate → admin bypass → is_public → grant` | v0.4 設計的「`required_roles` 自動通過」語意有歧義（`['admin']` 是「只開放給 admin」還是「admin 自動通過」？）。改成乾淨的 5 步：`required_roles` 是**過濾 gate**（不通過直接 deny），`is_public` 是**通過判定**（通過則不需 grant）。詳見更新後的 §7.5.2 |
| 17 | `dev_db_credentials.last_used_at` | `dev_db_credentials.reminder_sent_at` | `last_used_at` 需要 PG hooks（pg_stat_activity polling 或 login event trigger）才能填，工程量比預想大；`reminder_sent_at` 直接由 reminder cron 寫，idempotent（避免重複寄 email）。前者是「nice to know」、後者才是 lifecycle 必需。先做後者，前者等真的需要時補 |

**3 個 commit 落地（branch `ingestion-design`）：**
- `e611d89` — migration 0012 + ORM models
- `7c4df4a` — Step 2: access_control service + grant CRUD endpoints
- `cc121e3` — Step 2.5: migration 0013 + is_public column + 5-step algorithm

22 個 E2E checkpoints（5 SQL smoke + 12 endpoint + 10 is_public flow）全部通過。

### v0.4 (2026-04-25) — GitLab 納入 ANILA container + ComfyUI 推後

兩個 decision 影響 Phase 1 的範圍：

| # | 議題 | 立場 |
|---|---|---|
| 10 | **GitLab 跟 codeserver 一樣，由 ANILA monorepo 自己 deploy**（不是等 SRE 另開）— 走同源 nginx path `/gitlab` | GitLab 是 ISO 42001 必備（§7.4），由 ANILA 自己 deploy 確保上線時序。技術細節比 codeserver 複雜（GitLab Omnibus 對 subpath 部署敏感），詳見 §5.0.2 |
| 11 | **ComfyUI 模型還沒跑起來** → ComfyUI agent 推後到 Phase 1.5 | Phase 1 範圍縮小、更聚焦：access control + codeserver + GitLab + NotebookLM 卡片 + My-OpenAI-Frontend 停用。ComfyUI 等模型部署完成後再做（§8.4） |

對 sprint 的影響：
- Phase 1 工程量從 v0.3 的 **3-5 天** 微幅調整：移除 ComfyUI 任務 (-1 天) + 加 GitLab 部署 (+1.5 天) = **3-5.5 天**
- 新增 **Phase 1.5**（ComfyUI agent，~1.5 天，ComfyUI 模型上線後啟動）

### v0.3 (2026-04-25) — ISO 42001 合規 + codeserver nginx 同源 path

兩個關鍵 follow-up：

| # | 議題 | 立場 |
|---|---|---|
| 8 | **GitLab 是 ISO 42001 合規必備基礎設施，不是「nice to have」** | 內網部署嚴格遵守 ISO/IEC 42001（AI Management System 標準）：所有 agent 源碼**必須**有版本控制存放庫。GitLab 從「Platform Link 卡片之一」升格為**平台核心 service**。詳見 §7.4 |
| 9 | **codeserver 對外 = 同源 nginx path `/codeserver`** | 不獨立 cert，走 `https://<anila-host>/codeserver/*` 反向代理。詳見 §5.0.1 更新版（需要特殊 WebSocket 設定）|

對 sprint 排程的影響：
- Phase 1 task list（§8.1）多一項：**確認 GitLab 部署狀態 + 註冊到 platform_links**
- 若組裡 GitLab 還沒部署，**Phase 1 不能等 GitLab 就緒**才上線（會 block Phase 1 ~~~ 需要 GitLab 自己一個 sprint）→ 解法：Phase 1 註冊 GitLab card 但 URL 寫 `pending_deployment`，平台 UI 顯示「即將上線」，不 block 其他服務

### v0.2 (2026-04-25) — 7 個議題 review 後的修訂

第二輪討論（含 ComfyUI namespace 釐清、NotebookLM 部門級 grant、My-OpenAI-Frontend 「停用備份」而非廢除等）後的修訂：

| # | 議題 | v0.1 立場 | v0.2 修訂 |
|---|---|---|---|
| 1 | Memory file 的定位 | 不明確 | **`memory/` module 留 anila-core；`MemoryFileStore` KEEP（dev mode）；Phase 3+ 補 `PostgresMemoryStore`（prod mode）**。詳見 §1.4 與 [`anila-core-boundary.md`](./anila-core-boundary.md) §2.3 |
| 2 | ComfyUI 整合方式 | 註冊為 Model | **改為註冊為 Agent**。理由：ANILA 主介面是 chat 對話，Router 必須在 manifest 看到「會畫圖的 agent」才能自動分派；Model 介面只能透過 OpenAI SDK 顯式呼叫 model name 觸發。詳見 §4 |
| 3 | NotebookLM 權限模式 | 平台連結卡片（無 access control）| **OIDC SSO + 模式 A 嚴格白名單**。`required_roles=NULL` + `service_access_grants` table，**支援 user-level 與 department-level grant 兩種**（部門 grant 一次蓋整批）。詳見 §6 與 §7.5 |
| 4 | codeserver 部署位置 | 平台連結（外部跳轉）| **納入 ANILA monorepo `docker-compose.yml`** — 組裡沒獨立部署，要新增 service。詳見 §5 |
| 5 | n8n / gitlab / mlsteam URL | 假設值 | **實際 URL 已查證**（從 `My-OpenAI-Frontend/webui/src/pages/index.tsx` 取得）：mlsteam=`https://aiops.ai.ncsist.org.tw:4443/`、n8n=`/n8n`（nginx 內部 path）。GitLab URL 待你補。詳見 §7 |
| 6 | My-OpenAI-Frontend 處置 | 2-3 週廢除工程 + data migration | **改為「停用備份」**：現在沒人在用，不做 migration。docker compose down + 保留 image + README 加 archive notice。詳見 §3 |
| 7 | dev DB credential TTL | 24 小時 | **改為 30 天** + revoke API + 30d 自動 reminder + audit log。詳見 §10.2 |

並加入：
- **§1.4 Memory architecture 章節**（澄清 platform memory ≠ Ingestion 的關係）
- **§4 ComfyUI namespace 釐清**（ANILA 的「model」vs ComfyUI workflow 內部的「model」是兩個不相干 namespace）
- **§4.5 ComfyUI workflow preset 4 層擴展機制**（Layer 0 內建預設 / Layer 1 admin 加 preset / Layer 2 LLM 自動選 / Layer 3 dev fork agent template）
- **§7.5 Service Access Control**（`required_roles` + `service_access_grants` 雙機制 + effective access 演算法）

### v0.1 (2026-04-25) — 初版

關於組內既有服務（位於 `/home/aia/c1147259/project`）如何整合進 ANILA 平台的初版決議：

| 服務 | 決議 | 理由 |
|---|---|---|
| **My-OpenAI-Frontend** | ~~取代~~ → **停用備份** (v0.2 修訂) | 離職同仁開發、無人維護；現在沒 active 使用者 |
| **NotebookLM** | **暫保獨立** + 平台連結卡片 + 寫 future agent 化 plan | 現役 prod 服務，破壞風險高 |
| **data-quality** | **不納入** | 是 n8n workflow 的一部分 |
| **ComfyUI** | ~~註冊為 Model~~ → **註冊為 Agent** (v0.2 修訂) | Router 須能在自然對話分派 |
| **codeserver** | ~~Platform Link 卡片~~ → **納入 monorepo** (v0.2 修訂) | 組裡沒獨立部署，要新增 service |
| **n8n** | **平台連結卡片** | workflow 平台 |
| **gitlab** | **平台連結卡片** | git server |

---

## 1. 動機

### 1.1 從 LLM Gateway 升格為「多服務管理平台」

CSP 目前定位是 control + data plane，但組內現實是**還有一堆獨立服務各自跑**（NotebookLM、ComfyUI、n8n、gitlab、codeserver）。使用者每個服務都要單獨記網址、單獨登入、各自管 API key。

CSP 已有的 `AUTO_REGISTER_LINKS` 機制（範例見 `myCSPPlatform/README.md`）是「平台連結卡片」雛形。本 plan 把它擴充成完整的多服務管理平台，讓 ANILA dashboard 變成**組內所有 AI / dev 服務的統一入口**。

### 1.2 兩條主線並行

```
主線 A：取代 My-OpenAI-Frontend     ← 解決功能重疊
主線 B：把外部服務串進 dashboard   ← 解決使用者多次登入痛點
```

兩條主線**獨立進行**：A 完成不會 block B，反之亦然。

### 1.3 與其他 design doc 的關係

| 本 doc | 相關 doc | 關聯點 |
|---|---|---|
| §4 ComfyUI 註冊為 Agent | `AgenticRAG/anila-agent.yaml` template | 走 agent 註冊流程 |
| §5 codeserver DB credential | [`ingestion-platform-design.md`](./ingestion-platform-design.md) §3.3 RLS | per-dev credentials 自動 `SET LOCAL anila.agent_id` |
| §6 NotebookLM agent 化 | [`anila-core-boundary.md`](./anila-core-boundary.md) | NotebookLM 9 種 artifact 對應 agent template fork pattern |
| §1.4 Memory architecture | [`anila-core-boundary.md`](./anila-core-boundary.md) §2.3 | platform memory module 留 anila-core；MemoryFileStore 為 dev-mode impl |

### 1.4 Memory ≠ Ingestion（澄清）

設計討論中常被誤會的一個點：「ANILA 平台需要 memory 嗎？跟新做的 Ingestion Platform 有衝突嗎？」

**不衝突。兩者解的問題完全不同**：

| 面向 | Ingestion Platform | Platform Memory |
|---|---|---|
| 解的問題 | 把使用者上傳的「文件」變成可檢索的 chunks | 記住跨 session 的「對話 facts / user preferences / past decisions」 |
| 寫入時機 | Dev 主動上傳 documents（一次性 batch）| 每個 chat turn 自動 background extraction（trailing-run coalescing）|
| 資料量級 | 大 — 整本 PDF 切幾百個 chunk | 小 — 每個 user 累積幾百個 markdown memory file |
| 儲存 | pgvector 共用 cluster + agent_id 隔離 | 目前 file system (`MemoryFileStore`)；Phase 3+ 新增 PG impl |
| Schema | `document_chunks` table | `MEMORY.md` 索引 + 個別 `.md` files（4-type taxonomy: user_preference / project_convention / debugging_lesson / api_pattern）|
| 對應 anila-core | 從 anila-core **搬出**到 AgenticRAG template（見 boundary doc）| **留在 anila-core**（pure runtime 必備）|
| 程式入口 | `anila_core.ingestion.*`（搬走後）→ `agentic_rag.ingestion.*` | `anila_core.memory.*`（memdir / extract_memories / relevance_selector / consolidation）|

**生命週期對比**：

```
Ingestion 寫入：dev 一次性上傳 → IngestionService.ingest() → 寫 pgvector
Memory  寫入：每個 chat turn 結束 → background ExtractMemories worker
              → 從 conversation 萃取 facts → 寫 MemoryStore

Ingestion 讀取：agent 對話時 vector_search 工具 → top-k chunks
Memory  讀取：agent turn 開始時 RelevanceSelector → 選相關 memories 注入 context
```

**未來進階方向（v0.3 才考慮，本 plan 不做）**：類似 Ingestion Platform 的 design pattern，做一個 **central memory service**，讓 100 agent 共享 user-level preferences（例如「使用者 A 偏好簡短回覆」這個 fact 被所有 agent 共用）。屆時 `MemoryStore` Protocol 加一個 `CentralMemoryClient` impl 走 CSP `/api/memory/*`。但這要等 platform memory 真有跨 agent 共用需求時再啟動。

**本 plan 立場**：memory 不是 multi-service integration 的範疇，只在這節做澄清避免誤會。memory module 本身的演進（`PostgresMemoryStore` 補上、未來 central memory service）走獨立 design doc。

---

## 2. 服務分類

### 2.1 四種整合 Class

```
Class A: 「取代」                   — CSP 直接接手功能
  └─ My-OpenAI-Frontend

Class B: 「平台連結卡片」（純跳轉） — 使用者點卡片開新分頁
  ├─ NotebookLM
  ├─ codeserver
  ├─ n8n
  └─ gitlab

Class C: 「註冊為 Model」           — 走 model_registry，使用者透過 OpenAI SDK 呼叫
  └─ ComfyUI

Class D: 「不納入」
  └─ data-quality（屬於 n8n workflow）
```

### 2.2 每個服務的 Port / 對接資訊

| 服務 | 內部 URL | Auth 模式 | 說明 |
|---|---|---|---|
| NotebookLM | `http://localhost:3100` (FE) / `:8100` (BE) | 自帶 JWT | 9 種 artifact 生成 |
| codeserver | `http://localhost:8443`（推測，需確認）| 自帶（VS Code session）| Dev workspace |
| n8n | `http://n8n:5678` | OAuth2 capable | Workflow 平台 |
| gitlab | `http://gitlab:8080`（內網推測）| OAuth2 capable | Git server |
| ComfyUI | `http://localhost:8188` | 無 auth | 圖片生成 |

---

## 3. Class A：My-OpenAI-Frontend 處置（v0.2: 從廢除改為停用備份）

### 3.0 v0.2 修訂

> v0.1 規劃 2-3 週的「廢除工程」（含 model_registry / api_keys / token_usage / users 一次性 migration）。
>
> **v0.2 修訂為「停用備份」**：經確認**現在沒有 active 使用者打 My-OpenAI-Frontend 的 `/v1/*`**，data migration 沒必要做。改為直接停用 + 保留 image 作為災難備份。

### 3.0.1 v0.2 處置步驟（簡化版）

```
Step 1  ──  Audit
   grep 整個內網確認沒有 client 還在打 My-OpenAI-Frontend `/v1/*`
   工具：grep -rn "my-openai-frontend\|MY_OPENAI" /home /opt
   若找到，先請該 client owner 改打 CSP

Step 2  ──  Sunset
   docker compose down (My-OpenAI-Frontend 那組 service)
   不刪 image，保留作備份
   nginx 把 /v1/* 路由改回 410 Gone（防誤打）
   保留 admin UI（如果還有人想看歷史 usage 數據）

Step 3  ──  Archive notice
   在 My-OpenAI-Frontend repo README 加上：
     ⚠️ DEPRECATED — Use myCSPPlatform instead.
     This service is kept as a backup; new traffic should not be sent here.
```

**完成標準**：
- `docker ps | grep my-openai-frontend` 無 running container
- `curl <my-openai-frontend>/v1/...` 回 410 Gone
- README 有 archive notice
- ANILA dashboard 上不顯示 My-OpenAI-Frontend 卡片（或顯示但標 `[ARCHIVED]`）

**不做的事（跟 v0.1 對比）**：
- ❌ 不做 model_registry data migration
- ❌ 不做 api_keys / token_usage migration
- ❌ 不開發 audio transcription endpoint（v0.1 為了「無縫 cutover」要補的）— 改為**有人實際需要時再補進 CSP**

### 3.1 為什麼還是要記錄這段（功能重疊實證）

雖然不做廢除，但功能重疊事實要留檔，避免日後有人不知情又啟用 My-OpenAI-Frontend。

### 3.1.1 重疊範圍實證

從 `/home/aia/c1147259/project/My-OpenAI-Frontend/README.md` 與 `myCSPPlatform/README.md` 對比：

| 功能 | My-OpenAI-Frontend | myCSPPlatform | 結論 |
|---|---|---|---|
| OpenAI 相容 `/v1/*` | ✅ chat / embeddings / audio | ✅ chat / embeddings | 取代 |
| OAuth2 + JWT | ✅ | ✅（含 LDAP / OIDC）| CSP 更全面 |
| API Key management | ✅ | ✅ | 取代 |
| Usage tracking | ✅ basic | ✅ + dashboard 圖表 + CSV export | CSP 更全面 |
| Web dashboard | ✅ Next.js | ✅ Vue 3 | 取代 |
| Multi-model | ✅ load balance | ✅ model_registry + 健康檢查 + 自動註冊 | CSP 更全面 |
| Audio transcription endpoint | ✅ | ❌ | **CSP 缺，需補** |

**唯一 CSP 缺的功能**：audio transcription（whisper-style）endpoint。
- v0.1 計畫：cutover 前先補上
- **v0.2 修訂**：改為「有人實際需要時再補」（沒人用、沒急迫性）

### 3.2 ~~廢除 Migration 步驟（v0.2 已棄用）~~

> 以下保留作為**未來若改變決策**（決定真的廢除 + migrate）的執行 reference。

#### v0.1 原計畫

```
┌─ Step 1: Audit ──────────────────────────────────────────────┐
│ grep 整個內網找出所有打 My-OpenAI-Frontend `/v1/*` 的 client：│
│   - NotebookLM backend `.env` 的 LLM_API_BASE_URL            │
│   - data-quality n8n workflow 內的 LLM call URL              │
│   - 其他散落的 SDK caller / curl script                       │
│                                                              │
│ 工具：`grep -rn "my-openai-frontend\|MY_OPENAI" /home /opt`  │
└──────────────────────────────────────────────────────────────┘

┌─ Step 2: Data Migration（一次性，down-time ~ 30 min）────────┐
│ a. Models       → CSP `model_registry` table                 │
│ b. API Keys     → CSP `api_keys` table（保留 hash, 不重發）  │
│ c. Usage data   → CSP `token_usage` table（保留歷史）        │
│ d. Users        → CSP `users` table（合併重複帳號）          │
│                                                              │
│ Migration script: `scripts/migrate-from-openai-frontend.py`  │
│ Rollback: 保留 My-OpenAI-Frontend DB dump 7 天                │
└──────────────────────────────────────────────────────────────┘

┌─ Step 3: Add audio transcription to CSP ─────────────────────┐
│ 在 myCSPPlatform/backend/app/api/proxy.py 新增：             │
│   POST /v1/audio/transcriptions                              │
│ 介接到既有 vision / audio model endpoint                     │
└──────────────────────────────────────────────────────────────┘

┌─ Step 4: Client cutover ─────────────────────────────────────┐
│ 一鍵切換：                                                    │
│   - NotebookLM `.env` LLM_API_BASE_URL → CSP                 │
│   - data-quality n8n workflow 改 URL                          │
│   - 其他 client 同步                                          │
│                                                              │
│ 觀察 1 週確認無流量回打 My-OpenAI-Frontend                    │
└──────────────────────────────────────────────────────────────┘

┌─ Step 5: Sunset ─────────────────────────────────────────────┐
│ My-OpenAI-Frontend `/v1/*` 回 410 Gone（保留 admin UI 90 天  │
│ 讓使用者匯出資料）                                            │
│ 90 天後 docker compose down + 移除 image                      │
└──────────────────────────────────────────────────────────────┘
```

### 3.3 Migration script skeleton

```python
# scripts/migrate-from-openai-frontend.py
import asyncpg
import asyncio

async def migrate():
    src = await asyncpg.connect(MY_OPENAI_DB_URL)
    dst = await asyncpg.connect(CSP_DB_URL)

    # 1. Models
    src_models = await src.fetch("SELECT * FROM models WHERE active = true")
    for m in src_models:
        await dst.execute(
            "INSERT INTO model_registry (name, display_name, model_type, "
            "endpoint_url, api_version, ...) VALUES (...) "
            "ON CONFLICT (name) DO UPDATE SET ...",
            m['name'], m['display_name'], _map_type(m['type']), ...
        )

    # 2. API Keys（保留 key_hash，不重發明文）
    src_keys = await src.fetch("SELECT * FROM api_keys WHERE revoked = false")
    for k in src_keys:
        # 對應使用者：以 username 比對 CSP users 表
        user = await dst.fetchrow(
            "SELECT id FROM users WHERE username = $1",
            k['owner_username']
        )
        if not user:
            print(f"WARN: user {k['owner_username']} 不存在於 CSP，跳過 key {k['name']}")
            continue
        await dst.execute(
            "INSERT INTO api_keys (key_hash, user_id, name, allowed_models, ...) "
            "VALUES ($1, $2, $3, $4, ...)",
            k['key_hash'], user['id'], k['name'], k['allowed_models']
        )

    # 3. Usage history
    # ... bulk copy 同 schema 對應
```

### 3.4 風險與 Mitigation

| 風險 | Mitigation |
|---|---|
| Client 漏改，仍打舊 URL | Step 4 觀察 1 週流量、Step 5 改 410 Gone 暴露漏網之魚 |
| API Key hash 演算法不同 | 預先驗證兩邊都用 bcrypt（從 source 確認）；不同則統一改一邊 |
| Username 衝突 | Migration 跑前先 dry-run 列出衝突，由 admin 手動 merge |
| Audio transcription 補上的時間 | 排在 Step 2 之後、Step 4 之前；無此 endpoint 不能 cutover |

---

## 4. Class C：ComfyUI 整合 — 註冊為 Agent（v0.2 修訂）

### 4.0 v0.1 → v0.2 修訂

> v0.1 推薦「Model 優先 + Agent 升級」，理由是「90% 使用情境只需要 OpenAI Images API」。
>
> **v0.2 修訂為單一路徑「Agent」**。理由：ANILA 主介面是 chat 對話，必須有「會講 chat 介面的 agent」才會被 Router 自動分派；Model 介面只能透過 OpenAI SDK 顯式呼叫 model name 觸發，**一般使用者在 ANILA chat 永遠用不到**。

### 4.1 為什麼必須是 Agent（場景對比）

#### 場景 A：ComfyUI 是 Model（v0.1 路線，已被 v0.2 否決）

```
使用者 chat 框輸入「畫一張海報」
    ↓
Router 看 message + agent manifest：
   「manifest 裡沒有 image agent，只能回答『我不會畫圖』」
    ↓
使用者體驗 ❌ — 一般 user 在 ANILA chat 永遠摸不到 ComfyUI

只有寫 SDK script 的人能用：
   openai.images.generate(model="comfyui-x", prompt=...)
```

#### 場景 B：ComfyUI 是 Agent（v0.2 採用）

```
使用者 chat 框輸入「畫一張海報」
    ↓
Router 看 message + agent manifest：
   「manifest 裡有 comfyui-image agent，描述包含『圖片生成』
    → 分派！」
    ↓
ComfyUI Agent 收到 chat request：
   1. 從 messages 抽出 prompt
   2. 跑 ComfyUI workflow
   3. SSE stream：先「生成中...」 → markdown image embed
    ↓
使用者在 chat 介面看到圖 ✅
```

### 4.2 Namespace 釐清（重要：兩個「model」不是同一個）

**設計討論裡常被混淆的點**：

| 名詞 | 意義 | 例子 | 在哪管 |
|---|---|---|---|
| **ANILA model** | `model_registry` 註冊的 LLM / Embedding / VLM | `google/gemma4`、`nvidia/NV-embed-V2`、`meta/llama-4-maverick` | CSP 的 `model_registry` table |
| **ComfyUI 內部的 model** | Diffusion checkpoint file（圖片生成的「畫筆」）| `flux1-dev.safetensors`、`sd_xl_base_1.0.safetensors` | ComfyUI 機器的本機 `models/checkpoints/` |

**兩者完全不相干**。本節討論的 ComfyUI workflow preset **只跟 ComfyUI 自己的 model 有關**，跟 ANILA 的 LLM `model_registry` 無關。

一份 ComfyUI workflow JSON 簡化長這樣：

```json
{
  "1": {
    "class_type": "CheckpointLoaderSimple",
    "inputs": { "ckpt_name": "flux1-dev.safetensors" }
  },
  "2": {
    "class_type": "CLIPTextEncode",
    "inputs": { "text": "{{USER_PROMPT}}", "clip": ["1", 1] }
  },
  "3": {
    "class_type": "KSampler",
    "inputs": {
      "model": ["1", 0], "positive": ["2", 0],
      "steps": 20, "cfg": 7.5, "sampler_name": "euler",
      "scheduler": "normal", "seed": 42
    }
  },
  "4": { "class_type": "VAEDecode", "inputs": { "samples": ["3", 0], "vae": ["1", 2] } },
  "5": { "class_type": "SaveImage", "inputs": { "images": ["4", 0] } }
}
```

「workflow preset / 食譜」就是**這整份 JSON**。它指定的全部是 ComfyUI 自己世界的東西（哪個 .safetensors / 幾步 / 什麼 sampler / 要不要套 LoRA）。

### 4.3 工程上要做什麼：`comfyui-agent` service

寫一個 **約 250 行 Python 的 service**（fork AgenticRAG template 起步）：

```
   對外（給 ANILA Router 用）
      POST /v1/chat/completions   ← OpenAI 格式
                                      ↓
                comfyui-agent 內部邏輯
                                      ↓
            從 messages 抽 user prompt
            選 workflow preset（預設或從文字判斷風格）
                                      ↓
   對內（呼叫 ComfyUI 真的 API）
      POST http://comfyui:8188/prompt   ← ComfyUI 自己的格式
                                      ↓
                拿到 image url
                                      ↓
            SSE stream 回傳給 ANILA Router
              「生成中...」
              ![](https://anila.tw/images/abc.png)
```

**`anila-agent.yaml` 註冊**：

```yaml
name: comfyui-image
endpoint_url: http://comfyui-agent:8210
api_version: v1
description_for_router: |
  圖片生成 agent — 海報、概念圖、繪畫、視覺素材。
  支援風格：FLUX 高品質繁中字型（預設）。
  輸入：自然語言描述要的圖。輸出：markdown embedded image。
base_model: flux1-dev   # 註：這是 description 用，不是真實 ANILA model_registry 名字
capabilities:
  streaming: true
  image_generation: true
  languages: [zh-TW, en]
approval_status: approved
```

### 4.4 一個預設食譜，使用者完全不用設

Phase 1 出貨時 `comfyui-agent/workflows/` 只放**1 個 preset**：

```
comfyui-agent/
├── main.py
├── workflows/
│   └── default.json    ← 唯一的預設食譜（FLUX 高品質繁中字型）
└── Dockerfile
```

使用者在 chat 說「畫海報」就直接用 `default.json` 出圖。**完全不用知道有「workflow preset」這個概念**。

### 4.5 workflow preset 的 4 層擴展機制（未來）

當需要更多風格時，4 層擴展：

| Layer | 怎麼加 | 誰可以加 | 何時做 |
|---|---|---|---|
| **0** | 內建 1 個預設食譜 | 平台團隊 | **Phase 1 出貨**（本次）|
| **1** | 多個 preset 並列（日系 / 寫實 / 卡通 / ...）| **admin** 在 CSP UI 上傳 workflow JSON | Phase 2+（看需求）|
| **2** | Agent 從使用者文字自動選 preset（LLM 判斷風格）| 由 LLM 判斷 | Phase 2+ |
| **3** | Dev 自帶整套 workflow → 註冊成自己的 agent | **developer** fork comfyui-agent template | Phase 3+ |

**為什麼 Layer 1 上傳 workflow 限 admin**（更正 v0.1 措辭）：

不是因為「會洩漏 ANILA LLM model」（兩個 namespace 不交集），實際理由：

1. ComfyUI workflow 會 reference **本機存在的 .safetensors file** — admin 才知道 GPU 機器上有哪些 checkpoint，user 上傳 workflow 引用不存在的 model 會跑爆
2. 控制 **GPU 資源使用** — 某些 workflow 設成 1024×1024 + 100 steps + batch=4 會吃光顯存
3. **平台一致性** — 讓使用者從乾淨選單挑 preset，不接觸 raw workflow JSON

### 4.6 兩種介面是否並存？

**v0.2 立場**：Phase 1 **只做 Agent 介面**（OpenAI chat completions）。

理由：
- ANILA 主流量是 chat 對話，Agent 是必須的
- OpenAI Images API (`/v1/images/generations`) 在組內沒人在寫（grep 沒 caller）
- 加 SDK direct 介面 = 多寫一個 endpoint + 多一份維護成本，沒收益就不做

**未來若有 SDK 直接呼叫需求**：在 `comfyui-agent` 加 `POST /v1/images/generations` endpoint，內部共用同一份 workflow runner。一個服務兩 endpoint 不衝突。

---

## 5. ANILA 內網部署的 Dev / 合規服務（codeserver + GitLab）

> **v0.4**：本章從「只談 codeserver」擴展為「ANILA monorepo 自己 deploy 的 dev / 合規 infra」群組。codeserver 與 GitLab 走同樣的 pattern：同源 nginx path、不獨立 cert、Phase 3 SSO 走 nginx `auth_request`。

### 5.0 codeserver — ANILA 平台集成 Dev 入口（v0.2: 納入 monorepo deploy）

### 5.0 v0.1 → v0.2 修訂

> v0.1 把 codeserver 視為「外部已部署服務」，只在 `AUTO_REGISTER_LINKS` 加導向卡片。
>
> **v0.2 修訂**：經確認**組裡沒有獨立 codeserver 部署**，要由 ANILA 平台**自己提供**。所以 codeserver 不只是「導向卡片」，要**納入 ANILA monorepo 的 `docker-compose.yml`** 作為平台 service 之一。

### 5.0.1 部署整合（v0.3: 同源 nginx path）

#### Docker Compose service

ANILA 根 `docker-compose.yml` 加新 service（v0.3: **不再 expose port 8443**，改透過內網訪問）：

```yaml
services:
  # ... 既有 csp / router / anila-ui / csp-db ...

  codeserver:
    image: lscr.io/linuxserver/code-server:latest
    container_name: anila-codeserver
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=Asia/Taipei
      - PASSWORD_FILE=/run/secrets/codeserver_password
      - SUDO_PASSWORD_FILE=/run/secrets/codeserver_sudo_password
      - PROXY_DOMAIN=${ANILA_HOST}             # v0.3: 設成 ANILA host
      - DEFAULT_WORKSPACE=/workspace
    volumes:
      - codeserver_config:/config
      - codeserver_workspace:/workspace
      - /var/run/docker.sock:/var/run/docker.sock:ro
    # v0.3: 不對外 expose port，由 nginx 內網訪問
    expose:
      - "8443"
    networks:
      - anila_internal
    secrets:
      - codeserver_password
      - codeserver_sudo_password
    restart: unless-stopped

volumes:
  codeserver_config:
  codeserver_workspace:

secrets:
  codeserver_password:
    file: ./secrets/codeserver_password.txt
  codeserver_sudo_password:
    file: ./secrets/codeserver_sudo_password.txt
```

#### Nginx 同源 reverse proxy 設定

走 `https://<anila-host>/codeserver/*` 反向代理到 `http://codeserver:8443`。
**WebSocket 是必須的**（code-server 的 terminal、file watcher、live preview 全部依賴 WS）：

```nginx
# anila/nginx/anila.conf

server {
    listen 443 ssl;
    server_name anila.internal;
    # ... ANILA UI / CSP / Router 既有設定 ...

    # ───── codeserver 同源 path ─────
    location /codeserver/ {
        # 1. 路徑改寫：strip /codeserver prefix
        proxy_pass http://codeserver:8443/;

        # 2. WebSocket upgrade 必須（terminal / file watcher / live reload）
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        # 3. 標準 reverse proxy headers
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Prefix /codeserver;

        # 4. code-server 偶有大檔上傳（git push、artifact）
        client_max_body_size 200M;

        # 5. 長連線：codeserver 內部 WS 不要被 nginx idle timeout 砍
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;

        # 6. v0.3 Phase 1: 仍用 codeserver 自己的 password (PASSWORD_FILE)
        # v0.3 Phase 3 SSO: 改走 nginx auth_request 模組驗 CSP JWT cookie
        # auth_request /_csp_validate;          # ← Phase 3 才打開
    }

    # ───── 其他既有 location（CSP / UI / Router）保持不變 ─────
}
```

#### 為什麼選同源 path（vs 獨立 hostname）

| 方案 | 同源 path（採用）| 獨立 hostname |
|---|---|---|
| SSL cert | 與 ANILA 共用 | 要另一張 cert |
| CORS | 同源無問題 | 要設 CORS / iframe sandbox |
| Cookie | 自動帶 | 要 SameSite/Cookie domain 配置 |
| Phase 3 SSO 整合 | 直接 `auth_request` | 要做 cross-domain JWT 傳遞 |
| 部署複雜度 | 低 | 中 |
| 缺點 | URL 帶 `/codeserver` 前綴；code-server 要設 `--proxy-domain` 或 `PROXY_DOMAIN` env | — |

#### Phase 1 → Phase 3 SSO 演進

**Phase 1**：codeserver 自己的 PASSWORD_FILE 認證
- `PASSWORD_FILE=/run/secrets/codeserver_password`
- 使用者第一次點卡片要輸入 codeserver password
- admin only（required_roles=['admin']，§7.5）

**Phase 3**：nginx `auth_request` + CSP JWT
- 移除 `PASSWORD_FILE`
- nginx 設 `auth_request /_csp_validate`
- `/_csp_validate` 內部跳到 CSP 驗 JWT cookie，回 200 → forward；回 401 → 跳登入
- code-server 直接 trust upstream auth header（從 X-Forwarded-User 撈 username）

---

### 5.0.2 GitLab — ISO 42001 必備存放庫（v0.4 新增，納入 monorepo）

#### 為什麼比 codeserver 複雜

GitLab Omnibus image 對 reverse proxy subpath 部署有特殊要求：
- 必須在 `gitlab.rb` 設 `external_url` 與正確的 subpath
- GitLab 內建 nginx 要關掉，否則跟 ANILA nginx 撞 port
- 內部 service（Workhorse / Gitaly / Sidekiq / PostgreSQL / Redis）路由要正確
- 首次啟動需要 5-10 分鐘 reconfigure（不要急著測 health check）
- 大量 disk 需求（每個 repo 平均 50-500 MB）

#### Docker Compose service

```yaml
services:
  # ... csp / router / anila-ui / csp-db / codeserver 既有 ...

  gitlab:
    image: gitlab/gitlab-ce:16.10.0-ce.0     # 或更新版（注意 Omnibus 升級規則）
    container_name: anila-gitlab
    hostname: gitlab.anila.internal
    environment:
      GITLAB_OMNIBUS_CONFIG: |
        # 同源 subpath 部署 — 關鍵設定
        external_url 'https://${ANILA_HOST}/gitlab'
        nginx['enable'] = false                          # 關掉內建 nginx
        nginx['listen_port'] = 80                        # workhorse 走這個 port
        nginx['listen_https'] = false
        gitlab_workhorse['listen_network'] = "tcp"
        gitlab_workhorse['listen_addr'] = "0.0.0.0:8181"
        # PostgreSQL 走內建（GitLab 跟 ANILA CSP 用不同 PG instance — 隔離）
        # Redis 走內建
        # Sidekiq / Gitaly 走 default
        gitlab_rails['time_zone'] = 'Asia/Taipei'
        # 關閉 sign-up（內網用，由 admin 開帳號或 Phase 3 走 OIDC）
        gitlab_rails['gitlab_signup_enabled'] = false
        # 監控
        prometheus['enable'] = false                     # 太重，先關
        alertmanager['enable'] = false
    expose:
      - "8181"                                           # Workhorse 對 nginx 暴露
      - "22"                                             # SSH (git push/pull) — 之後考慮 expose
    volumes:
      - gitlab_config:/etc/gitlab
      - gitlab_logs:/var/log/gitlab
      - gitlab_data:/var/opt/gitlab                      # 大磁碟，建議掛獨立 disk
    networks:
      - anila_internal
    restart: unless-stopped
    shm_size: 256m
    # GitLab 啟動慢，給足夠 healthcheck 緩衝
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8181/-/health"]
      interval: 60s
      timeout: 10s
      retries: 5
      start_period: 600s    # 首次啟動 5-10 分鐘 reconfigure

volumes:
  gitlab_config:
  gitlab_logs:
  gitlab_data:
```

#### Nginx 同源 reverse proxy 設定

跟 codeserver 一樣的 pattern，但 GitLab 走 Workhorse port 8181：

```nginx
# anila/nginx/anila.conf — 在 codeserver location 之後加

server {
    listen 443 ssl;
    server_name ${ANILA_HOST};

    # ... 既有 / 與 /codeserver/ 不變 ...

    # ───── GitLab 同源 path ─────
    location /gitlab/ {
        # 1. 不要 strip /gitlab — GitLab external_url 已知道自己在這個 path
        proxy_pass http://gitlab:8181;

        # 2. WebSocket（CI live log、collaborative editing、Web IDE）
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        # 3. 標準 reverse proxy headers
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Ssl on;        # GitLab 看這個判斷 https

        # 4. Git LFS / 大檔 push 可能上 GB
        client_max_body_size 5G;
        proxy_request_buffering off;                # 大上傳不要 buffer

        # 5. CI build log 可能跑很久
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_buffering off;                        # CI tail log 即時 flush

        # 6. Phase 3 SSO 同 codeserver
        # auth_request /_csp_validate;              # ← Phase 3 才打開
    }
}
```

#### 與 codeserver 的差異對比

| 面向 | codeserver | GitLab |
|---|---|---|
| 部署複雜度 | 低（一個 image、無自帶 DB）| 高（Omnibus 含內建 PG / Redis / Workhorse / Gitaly / Sidekiq）|
| 啟動時間 | < 30 秒 | 5-10 分鐘（首次 reconfigure） |
| Disk 需求 | 配置檔 + workspace（小）| Git repos + LFS objects（GB 級） |
| Subpath 部署 | 加 `--proxy-domain` env 即可 | 必須在 `gitlab.rb` 設 `external_url` |
| Port forwarding | nginx → 8443（內建 web）| nginx → 8181（Workhorse），SSH 22 另議 |
| client_max_body_size | 200 MB | 5 GB（git LFS）|
| WebSocket usage | Terminal / file watcher | CI live log / Web IDE collaborative |
| Healthcheck start_period | 60s 即可 | 600s（首啟動 reconfigure 期）|

#### Phase 1 → Phase 3 SSO 演進

跟 codeserver 完全一樣的 pattern：

**Phase 1**：GitLab 自己的 root admin password 啟動，後續手動建 user
- 第一次啟動後 `docker exec` 重設 root 密碼
- admin 在 GitLab 內為每個 dev 開帳號

**Phase 3**：CSP 當 OIDC IdP，GitLab 改認 CSP token
- 在 `gitlab.rb` 加 OmniAuth provider 設定（`gitlab_rails['omniauth_providers']`）
- 移除 PASSWORD_FILE 流程
- 使用者第一次登入 GitLab 自動建帳號（綁 CSP user_id）

#### 平台 PG 資源規劃 — GitLab 不共用 ANILA PG

**重要架構決定**：GitLab 用 **Omnibus 內建 PostgreSQL**，**不**跟 ANILA `csp-db` 共用 PG instance。理由：
- GitLab 的 PG schema 跟 ANILA 完全無關，沒有共用 query 路徑
- GitLab 升級會跑 PG migration，把 ANILA PG 一起升風險高
- 機敏資料隔離：GitLab data 跟 ANILA CSP data 完全分離 db
- 操作簡單：兩個 PG 各自 backup / restore，不互相牽動

但**ingestion-platform-design §3.3 的 RLS 機制不受影響** — 那個是用在 ANILA pgvector 上，跟 GitLab PG 無關。

#### 容量規劃預估

| 資源 | 估算（內網 100 agent + 50 dev） | 備註 |
|---|---|---|
| Disk (gitlab_data) | 200 GB - 1 TB | 主要是 git repos + LFS。建議掛獨立 disk |
| RAM | 4-8 GB | Sidekiq / Workhorse / Puma 加總 |
| CPU | 2-4 vCPU | CI runner 不在這算（runner 走 mlsteam）|
| 啟動時間 | 5-10 分鐘 | 包括 PG migration + reconfigure |

#### CI Runner 跑哪？

**不在 ANILA monorepo 內部 deploy GitLab Runner**。CI 工作（agent build / test / deploy）跑在 mlsteam GPU 機器上，由 mlsteam 註冊成 GitLab Runner instance。理由：
- ANILA monorepo container 主要是 control / data plane，不該跑重 build
- mlsteam 有 GPU 適合 ML build / model training CI
- Runner 註冊在 GitLab UI 一次性設好，後續自動接 job

### 5.1 角色定位

```
            Dev 真正的開發環境在哪裡？
                        │
        ┌───────────────┴───────────────┐
        ▼                               ▼
  mlsteam（GPU host）              codeserver
  「跑 model / 跑 agent」          「browser-based VS Code」
        │                               │
        │                               │
        └───────┬───────────────────────┘
                ▼
        都需要連到 ANILA postgres
        （pgvector，做 ingestion / 檢索）
```

codeserver 在 ANILA 體系的角色 **不是 agent，也不是 model**：

> codeserver 是 **dev workspace 入口**。Dev 透過 codeserver 寫 agent code，但實際 agent 跑在 mlsteam GPU 機器。codeserver 與 mlsteam 都需要連回 ANILA 的 pgvector 做 ingestion 與檢索（這就是之前 ingestion-platform-design 在解決的問題）。

### 5.2 dev 連 ANILA postgres 的安全機制

**問題**：mlsteam 上的 dev 寫 agent，需要 `DATABASE_URL` 連 ANILA postgres 寫 chunks。但是：
- 不能給原始 superuser credentials（會洩漏）
- 不能給 read-write 整個 cluster（會踩到別 agent 的 data）
- 必須跟 [`ingestion-platform-design.md`](./ingestion-platform-design.md) §3.3 的 RLS 機制整合

**解法**：CSP 提供 **per-developer scoped DB credentials**（短效）

```
┌─────────────────────────────────────────────────────────────┐
│  CSP Dev Console                                            │
│                                                             │
│  Agent: my-legal-rag                                        │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ 🔑 Database Credentials                              │   │
│  │                                                      │   │
│  │   Host:     pgvector.anila.internal                  │   │
│  │   Port:     5432                                     │   │
│  │   Database: anila_pgvector                           │   │
│  │   User:     dev_my-legal-rag_eyJk...                 │   │
│  │   Password: (one-time copy)                          │   │
│  │                                                      │   │
│  │   Valid for: 24 hours                                │   │
│  │   Scope:     agent_id = 42 (my-legal-rag)            │   │
│  │                                                      │   │
│  │   [Copy connection string]  [Revoke]                 │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

### 5.3 實作機制

CSP 後端提供 endpoint `POST /api/dev-credentials/db`（v0.2: TTL 改 30d + revoke + reminder）：

```python
DEV_CRED_TTL = timedelta(days=30)   # v0.2: 從 24h 改為 30d

@router.post("/api/dev-credentials/db")
async def issue_db_credential(
    agent_id: int,
    caller: User = Depends(get_current_user),
):
    # Authorization
    agent = await get_agent(agent_id)
    if agent.owner_user_id != caller.id and not caller.is_admin:
        raise HTTPException(403)

    # 撤銷該 agent 之前的 active credential（同一 agent 同時只能有一把）
    await revoke_active_credentials_for_agent(agent_id)

    # 建立 30 天 PG role
    role_name = f"dev_{agent.name}_{secrets.token_hex(4)}"
    password = secrets.token_urlsafe(32)
    expiry = datetime.utcnow() + DEV_CRED_TTL
    await pg_admin.execute(f"""
        CREATE ROLE {role_name} LOGIN PASSWORD '{password}'
            VALID UNTIL '{expiry.isoformat()}';
        GRANT pg_read_all_data TO {role_name};
        GRANT pg_write_all_data TO {role_name};
        ALTER ROLE {role_name} SET anila.agent_id = '{agent_id}';   -- 關鍵：自動觸發 RLS
    """)

    # 記錄到 dev_db_credentials 表（追蹤 active credentials 用）
    cred_id = await db.insert(DevDbCredential(
        agent_id=agent_id,
        issued_to=caller.id,
        pg_role_name=role_name,
        issued_at=datetime.utcnow(),
        expires_at=expiry,
        revoked_at=None,
    ))

    # 寫進 audit log
    await audit_log("issued_dev_db_credential", actor=caller.id,
                    resource_type="agent", resource_id=agent_id,
                    detail={"cred_id": cred_id, "ttl_days": 30})

    return {
        "credential_id": cred_id,
        "host": settings.PG_HOST,
        "port": settings.PG_PORT,
        "database": settings.PG_DB,
        "user": role_name,
        "password": password,           # 只回傳這一次
        "valid_until": expiry.isoformat(),
        "scope": {"agent_id": agent_id},
    }


@router.post("/api/dev-credentials/{cred_id}/revoke")
async def revoke_db_credential(
    cred_id: int,
    caller: User = Depends(get_current_user),
):
    """Dev / admin 隨時可撤銷自己的 credential。"""
    cred = await get_credential(cred_id)
    if cred.issued_to != caller.id and not caller.is_admin:
        raise HTTPException(403)

    await pg_admin.execute(f"DROP ROLE IF EXISTS {cred.pg_role_name};")
    await db.update(DevDbCredential, cred_id, revoked_at=datetime.utcnow())
    await audit_log("revoked_dev_db_credential", actor=caller.id,
                    resource_type="dev_credential", resource_id=cred_id)
    return {"status": "revoked"}
```

**Schema（v0.5 對齊實作 — migration 0012）**：

```sql
CREATE TABLE dev_db_credentials (
    id                SERIAL PRIMARY KEY,                      -- Integer，跟既有 schema 一致
    user_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,  -- 跟其他表的命名統一（v0.4 寫的 issued_to 改名）
    agent_id          INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    pg_role_name      VARCHAR(100) NOT NULL UNIQUE,            -- PG role 全域 unique
    issued_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at        TIMESTAMP NOT NULL,
    revoked_at        TIMESTAMP,
    -- v0.5 修訂：last_used_at（追蹤 PG role 最後使用時間，需要 PG hook）
    -- 改為 reminder_sent_at（idempotent reminder 旗標）。前者是 nice-to-know
    -- 但實作成本高（pg_stat_activity polling 或 login event trigger）；後者
    -- 才是 lifecycle 必需。等真有需求再補 last_used_at。
    reminder_sent_at  TIMESTAMP
);

-- 一個 (user, agent) 對只能有一個 active credential
CREATE UNIQUE INDEX uq_dev_db_credentials_user_agent_active
    ON dev_db_credentials(user_id, agent_id) WHERE revoked_at IS NULL;
-- Cron 用：scan WHERE revoked_at IS NULL AND expires_at < now() 自動 revoke
CREATE INDEX ix_dev_db_credentials_expires_at
    ON dev_db_credentials(expires_at) WHERE revoked_at IS NULL;
CREATE INDEX ix_dev_db_credentials_user_id ON dev_db_credentials(user_id);
CREATE INDEX ix_dev_db_credentials_agent_id ON dev_db_credentials(agent_id);
```

**自動 reminder（背景任務）**：

```python
# 每天跑一次 — 檢查即將過期的 credential
async def remind_expiring_credentials():
    soon = datetime.utcnow() + timedelta(days=3)
    creds = await db.fetch(
        "SELECT * FROM dev_db_credentials "
        "WHERE revoked_at IS NULL AND expires_at <= :soon AND expires_at > now()",
        soon=soon
    )
    for cred in creds:
        await send_notification(
            user_id=cred.issued_to,
            type="dev_credential_expiring",
            message=f"Agent '{cred.agent_name}' 的 DB credential 將於 {cred.expires_at} 過期，"
                    f"請至 ANILA Dev Console 重新 issue。"
        )
```

**關鍵設計**：`ALTER ROLE ... SET anila.agent_id = N` — 這個 PG role 一登入就自動 set session var，**RLS policy 自動套用**，dev 寫的任何 query 都不可能看到別 agent 的 data。完全跟 [`ingestion-platform-design.md`](./ingestion-platform-design.md) §3.3 layer 2 對齊。

**TTL 30d 的理由（v0.2 修訂）**：
- 24h 太短：dev 每天要 issue 麻煩，倒逼大家把 credential 寫死在 config
- 30d 適中：對應一個 sprint 週期，過期前有 reminder
- 90d 太長：洩漏風險明顯放大
- **配套**：revoke API 隨時可撤、自動 reminder（過期前 3 天）、audit 每次 issue 與 revoke

### 5.4 codeserver 的使用流程

codeserver 由 ANILA 平台部署（見 §5.0.1），dev 流程：

1. ANILA dashboard 點「Code Server」卡片（admin role 才看得到，§7.5）
2. 開新分頁進入 codeserver workspace（`https://anila.internal/codeserver`）
3. 用 git clone 自己 agent 的 repo（gitlab）
4. 點 ANILA dashboard 的「Get DB Credentials」拿 connection string（30d TTL）
5. 貼進自己 agent 的 `.env`
6. 寫 code、跑 test、push 回 gitlab
7. agent build 完 deploy 到 mlsteam（不是 codeserver — codeserver 不跑 production agent）

**codeserver 在 ANILA 的位置**：
- **Phase 1**：admin-only platform service（`required_roles=['admin']`），其他人看不到
- **Phase 3 SSO**：可以擴大開放給 developer role
- **長期**：作為 ANILA 平台與 mlsteam 之間的「地面站」— dev 在 codeserver 開發、測試（連回 ANILA postgres），確認 OK 後 deploy 到 mlsteam GPU 機器跑

### 5.5 為什麼 mlsteam 不算 ANILA 子服務

mlsteam 是 organization-wide GPU 平台，**不歸 ANILA 管**。ANILA 跟 mlsteam 的關係是：
- mlsteam 提供算力給 dev / agent 跑
- agent 完成後在 ANILA 註冊 endpoint（用 mlsteam 上的 IP）
- 但 mlsteam 本身不該被 ANILA dashboard 接管

導向卡片可以放 mlsteam 連結（已有），但**不需要做 SSO**。

---

## 6. NotebookLM 整合（Phase 1 SSO + grant；agent 化 v0.6 取消）

> **v0.6 決策更新（§0 #30）**：原本 §6.1 之後規劃的 Phase 4 agentification（單 agent + 9 tools）**取消**。NotebookLM 對話範式（給文件 → 生 PPTX/PDF/mind map artifact）跟 OpenAI chat completions 完全不同，包成 agent 工程量大、收益小。**永遠保持獨立 service** + platform_link 卡片 + Phase 3 OIDC SSO 即可。
>
> 以下 §6.1–6.5 內容是 v0.6 之前的設計，保留作為 historical record（避免後人重新論證 agentification）。實作面看 §6.0.1 + §6.0.2 即可。


### 6.0 v0.2 修訂：兩個階段

> v0.1 只規劃了 Phase 4 的 agent 化計畫。v0.2 補上 **Phase 1 / Phase 3 的 access integration**：

| 階段 | 整合內容 |
|---|---|
| **Phase 1** | 平台連結卡片 + `service_access_grants` 嚴格白名單（模式 A） |
| **Phase 3** | OIDC SSO（NotebookLM auth 認 CSP token） |
| **Phase 4** | Agent 化（單 agent + 9 tools） |

### 6.0.1 Phase 1：嚴格白名單 grant（模式 A）

NotebookLM 在 `platform_links` table 註冊：

```sql
INSERT INTO platform_links (name, url, icon, description, required_roles)
VALUES (
  'NotebookLM',
  'http://notebooklm.internal:3100',
  'book-open',
  'AI 學習內容生成（podcast / slides / mind map / quiz / report）',
  NULL   -- ★ 純白名單模式：required_roles=NULL → 任何 role 預設都看不到
);
```

**Effective access**（演算法見 §7.5）：
- `required_roles=NULL` → 沒有 role-based 自動授權
- 使用者必須有 `service_access_grants` 紀錄才看得到卡片
- admin 在 CSP UI 一個個（或一個部門一次）grant

**Admin UI**：
```
Platform Link: NotebookLM
  Required Roles: (none — 純 grant 白名單模式)

  🏢 Department Grants
     ✅ 工程部           granted 2026-04-25 by admin (12 users)
     ✅ 產品部           granted 2026-04-26 by admin (8 users)
     [+ Grant to department]

  👤 Individual User Grants (例外加開)
     ✅ alice@example.com   granted 2026-04-25 by admin
     [+ Grant to user]

  📊 Effective Access: 21 users
```

### 6.0.2 Phase 3：OIDC SSO（auth 共用）

Phase 1 階段使用者點卡片進 NotebookLM **仍要單獨登入**（NotebookLM 自己的 JWT）。Phase 3 補上 SSO：

```
User 已登入 ANILA
    ↓ CSP 已發 JWT cookie
User 點 NotebookLM 卡片
    ↓ CSP 簽 OIDC ID token 帶在 redirect URL
NotebookLM 收到 callback
    ↓ 用 CSP 的 JWKS 驗 token（不是用本地 password）
NotebookLM 直接進 workspace（無需再登入）
```

**NotebookLM 改動**：
- `auth.py` 加 OIDC client middleware（FastAPI `authlib`）
- 收到 `Authorization: Bearer <csp_jwt>` → 用 CSP JWKS 驗 → 從 token claim 撈 `user_id` / `username`
- 本地 password 認證 deprecated（保留 superuser fallback for emergency）

### ~~6.1 為什麼 agent 化現在不做（v0.6 取消，整段不再 actionable）~~

NotebookLM 是 prod 服務、有自己 user base、有 9 種 artifact 生成，貿然 agent 化的破壞風險高。先放著、寫 plan，等 ANILA platform 成熟後再啟動。

### ~~6.2 兩種 agent 化路線（v0.6 取消）~~

#### 路線 A：9 種 artifact 各自當一個 agent

```
notebooklm-podcast-generator        agent
notebooklm-slides-generator         agent
notebooklm-mindmap-generator        agent
notebooklm-flashcards-generator     agent
notebooklm-quiz-generator           agent
notebooklm-infographic-generator    agent
notebooklm-datatable-generator      agent
notebooklm-report-generator         agent
notebooklm-video-script-generator   agent
```

| 優點 | 缺點 |
|---|---|
| Router 可以精準分派（使用者問「幫我做投影片」→ slides agent）| 9 個 agent 要維護 9 個 endpoint |
| 每個 artifact 可以獨立改 prompt / 換 model | 共用邏輯（檔案上傳、向量搜索）要拆出 |
| 可以細粒度開放權限（dept A 只能用 podcast）| Agent 數量爆炸（100 agent 計畫變 100+9 個）|

#### 路線 B：合一個 multi-tool agent

```
notebooklm-studio          agent
  ├─ tool: generate_podcast
  ├─ tool: generate_slides
  ├─ tool: generate_mindmap
  └─ ... (9 個 tool)
```

| 優點 | 缺點 |
|---|---|
| 單一 agent endpoint，簡單 | Router 無法針對 artifact 類型精準分派 |
| 共用邏輯不用拆 | 使用者要在對話中明確說「請幫我用 podcast 工具」|
| Agent 數量不爆炸 | tool_calling 失敗時整個 agent 不可用 |

#### 推薦：**路線 B**（單 agent + 9 tools）

理由：
- NotebookLM 9 個 artifact 共用底層邏輯（同一份文件 → 不同呈現），拆 9 個 agent 等於重複實作
- Router 精準分派的價值在 RAG 類「不同知識庫」，不在「同知識庫不同呈現方式」
- LLM tool_calling 的能力已經足夠在 multi-tool agent 內做選擇

### ~~6.3 Agent 化的前置條件（v0.6 取消）~~

NotebookLM 啟動 agent 化前，必須完成：
1. **CSP 取代 My-OpenAI-Frontend** — NotebookLM 內部要打 CSP `/v1/*` 而非 OpenAI Frontend
2. **Ingestion Platform 上線** — NotebookLM 的 chroma 改用 ANILA pgvector（或保留 chroma 但加同步）
3. **SSO** — NotebookLM auth 改認 CSP token

未滿足這 3 個條件，agent 化會打結。

### ~~6.4 Agent 化執行步驟（v0.6 取消）~~

```
Step 1: NotebookLM backend 加 OpenAI-compatible endpoint
        POST /v1/chat/completions
          ↓ 內部 dispatch 到 9 個 artifact generator
          ↓ 用 tool_calling pattern
          ↓ SSE stream

Step 2: 註冊到 CSP 為 Agent
        anila-agent.yaml:
          name: notebooklm-studio
          endpoint_url: http://notebooklm-backend:8100
          base_model: gemma4
          requires_encryption: false
          capabilities:
            tool_calling: true
            tools: [podcast, slides, mindmap, ...]

Step 3: ANILA UI 的 router 自動發現
        使用者透過 anila-router 呼叫 notebooklm-studio
        Router 把 LLM 主對話轉給它
```

### 6.5 與 AgenticRAG template 的差異

| 面向 | AgenticRAG template | NotebookLM (agent 化後) |
|---|---|---|
| 知識來源 | pgvector（共用 ANILA Ingestion Platform） | Chroma（自有，內部生成 artifact 用）|
| 對話類型 | 多輪檢索 + 來源標注 | 單次「給文件 → 生 artifact」|
| Output | 文字 + citations | PPTX / PDF / mind map JSON 等 |
| Fork 對象 | 一般部門 RAG agent | 不適合 fork（特殊用途）|

NotebookLM 不會變成 template，它是**一個特殊的 agent 實例**（One of a kind）。

---

## 7. Platform Links — 完整服務清單（v0.2 更新）

### 7.1 完整註冊內容（v0.5.1 對齊實作 — Phase 1 Step 3 已落地）

> **實作狀態**：✅ 5 筆 link seed 完成。權威 source 在 [`docker-compose.yml`](../docker-compose.yml) 的 `csp.environment.AUTO_REGISTER_LINKS`，對應 [`auto_seed.py`](../myCSPPlatform/backend/app/services/auto_seed.py) §5 邏輯。

**設計重點：4 個內網服務全走 nginx 同源 path**（v0.5.1 修正 — `notebooklm.internal:3100` / `codeserver.internal:8443` 那種內網 DN 是錯的，browser 進不去且觸發 Mixed Content 警告）。只有 MLSteam 是公網 service，保留絕對 FQDN。

**`is_public` × `required_roles` 兩個維度的組合語意**（admin 永遠通過，因為 v0.5.1 algorithm step 2 admin bypass）：

| 用途 | `is_public` | `required_roles` | 效果 |
|---|---|---|---|
| 全員可見入口（如 ANILA 主面板）| `true` | `[]` | 任何登入用戶都看得到 |
| 角色公開（如 n8n / GitLab）| `true` | `[role1, role2]` | 該 role 通過 gate 就看得到 |
| 角色限定 + 個別 grant（如 NotebookLM mode A）| `false` | `[]` | 任何 role 都過 gate，但要 admin 個別 grant 才看得到 |
| Admin 專屬 + 個別 grant（極少見）| `false` | `['admin']` | 等同 admin only |

```yaml
# docker-compose.yml csp.environment.AUTO_REGISTER_LINKS（Phase 1 Step 3 final）

- name: "NotebookLM"
  url: "/notebooklm"                           # ← nginx 同源 path（在 monorepo）
  icon: "book-open"
  description: "AI 學習內容生成（podcast / slides / mind map / quiz / report）"
  is_public: false                             # 不公開
  required_roles: []                           # role gate 開放（admin 個別 grant，§6.0.1）

- name: "Code Server"
  url: "/codeserver"                           # ← nginx 同源 path（Phase 1 Step 5 部署）
  icon: "code"
  description: "Browser VS Code — agent 開發整合入口"
  is_public: false                             # admin only（admin bypass 已涵蓋；is_public 對非 admin 無關）
  required_roles: ["admin"]

- name: "n8n 工作流程"
  url: "/n8n"                                  # nginx 同源 path（已部署）
  icon: "workflow"
  description: "自動化工作流程平台 — 排程任務、跨服務串接"
  is_public: true                              # 對 developer 公開（不需 grant）
  required_roles: ["developer"]                # admin 透過 step 2 bypass 也看得到

- name: "GitLab"
  url: "/gitlab"                               # nginx 同源 path（Phase 1 Step 6+8 部署）
  icon: "git-branch"
  description: "Git server — agent 程式碼與 issue tracking（ISO 42001 合規必備，§7.4）"
  is_public: true
  required_roles: ["developer"]

- name: "MLSteam"
  url: "https://aiops.ai.ncsist.org.tw:4443/"  # 公網 FQDN（外部 service，不是 monorepo 部署）
  icon: "cpu"
  description: "MLOps 平台 — agent 訓練與部署"
  is_public: true
  required_roles: ["developer"]

# ComfyUI Studio (raw UI) 是否要做卡片是個 design choice：
#   - 一般使用者透過 chat agent 用（§4），不需要直接進 ComfyUI UI
#   - 但 admin / developer 可能需要「進去看 ComfyUI 自己有什麼 model file」
#   - 本 v0.2 暫不加 ComfyUI Studio 卡片，等真有需求時再開
# - name: "ComfyUI Studio"
#   url: "http://comfyui.internal:8188"
#   icon: "image"
#   description: "ComfyUI 直接介面（進階；一般使用者請在 ANILA chat 直接畫圖）"
#   required_roles: ["admin", "developer"]
```

### 7.2 SSO 整合（Phase 3 才做）

n8n 與 gitlab 都支援 OAuth2：
- n8n: `N8N_AUTH_TYPE=oauth2` + IdP discovery
- GitLab: 內建 OmniAuth

CSP 啟用 OIDC Provider mode 後（見 §6.0.2），這兩個服務改認 CSP 為 IdP，使用者點導向卡片就直接登入。

**Phase 1 不做 SSO** — 純跳轉，使用者重新登入但破壞風險最低。

### 7.3 你要補的資訊（v0.4 更新）

| Item | 為什麼缺 | 怎麼補 |
|---|---|---|
| ~~GitLab 內網 URL~~ | ✅ **v0.4 確認**：由 ANILA monorepo 自己 deploy，URL=`/gitlab`（§5.0.2 已寫完整 compose service + nginx config）| done |
| ~~codeserver 暴露方式~~ | ✅ **v0.3 確認**：同源 nginx path `/codeserver`（§5.0.1） | done |
| ~~`comfyui.internal` 的實際 host~~ | ✅ **v0.4 確認**：ComfyUI 模型還沒部署，整個 ComfyUI agent 推後到 Phase 1.5（§8.4） | deferred |

### 7.4 ISO 42001 合規與 GitLab 的角色（v0.3 新增）

#### 為什麼 GitLab 從「nice to have」升格為「必備基礎設施」

ISO/IEC 42001:2023（AI Management System）對「AI 系統開發」要求：

| 條款 | 要求 | 對 ANILA 的意義 |
|---|---|---|
| 8.2 (Resources for AI systems) | AI 系統開發資源（含 source code）必須**有管理、有追蹤** | agent 源碼不能散落在 dev 個人機器，必須中央存放庫 |
| 8.4 (AI system requirements analysis) | 變更必須可追溯、有 audit trail | 每次 agent code 變更要有 commit + reviewer |
| 9.1 (Performance evaluation) | AI 系統效能評估要可驗證 | Eval result 要能 trace 回到 source commit hash |
| A.6.2.5 (Verification & validation) | 部署前必須有 V&V 流程 | PR review + CI 必過 |
| A.6.2.6 (Deployment) | 部署 artifacts 必須可追溯回 source | docker image tag → git commit → reviewer |

**換句話說**：沒有版本控制中央存放庫，整個平台**無法通過 ISO 42001 驗證**。GitLab 不是「為了方便」，是「為了能上線」。

#### 對 ANILA 平台設計的牽連（不只是 GitLab 一張卡片）

ISO 42001 影響面遠超出 GitLab 本身。**這次 design doc 範圍內先處理 GitLab**，但下面這些值得後續另開 design doc：

| 影響面向 | 現況 | 42001 要求 | 需要動的地方 |
|---|---|---|---|
| Agent registration | `agents` table 有 `endpoint_url` / `description` | 要記 `source_commit_sha` / `last_reviewer` / `v&v_status` | CSP `agents` schema 加欄位 |
| Model registry | `model_registry` 有 `endpoint_url` | 要記 `model_card_url` / `training_dataset_ref` / `weights_sha256` | CSP `model_registry` schema 加欄位 |
| Audit log | 已有 `audit_logs` table | ✅ 已符合「變更追蹤」要求 | 確認 retention 期 ≥ 12 個月 |
| Deployment artifacts | docker compose 直接拉 image | image tag 要能 trace 回 git commit | CI 流程 — image tag = `git rev-parse HEAD` |
| Eval results | （尚未實作）| 要記 `eval_run_id` ↔ `source_commit_sha` 對應 | Ingestion Platform §6 evaluator 要加這個欄位 |

#### Phase 1 GitLab 整合的具體步驟（v0.4 更新）

```
1. ✅ GitLab 部署方式：由 ANILA monorepo 自己 deploy（§5.0.2）
   - 加 service `gitlab` 到根 docker-compose.yml
   - 加 nginx `/gitlab` location block

2. 首次啟動流程（5-10 分鐘）：
   ☐ docker compose up -d gitlab
   ☐ 等 healthcheck 變 healthy（首次 reconfigure 約 5-10 分鐘）
   ☐ docker exec anila-gitlab gitlab-rails runner "..." 設 root 密碼
   ☐ 登入 GitLab admin → 開帳號 / 設定 group

3. AUTO_REGISTER_LINKS 註冊 GitLab card（URL = /gitlab）：
   - card 在 ANILA dashboard 顯示
   - admin / developer role 自動可見
   - 點擊跳到同源 https://<anila-host>/gitlab/

4. ISO 42001 audit checklist（給 admin 用）：
   ☐ 所有 agent repo 已 push 到 GitLab
   ☐ 所有 commit 都有 reviewer
   ☐ docker image tag 已關聯 git commit hash
   ☐ audit_logs retention ≥ 12 個月
```

#### Future design doc：ANILA × ISO 42001 完整合規

ISO 42001 對 ANILA 的影響不只 GitLab。建議下一份 design doc：

> **`docs/iso-42001-compliance.md`** — 完整盤點 ISO 42001 對 ANILA 各 component 的要求、現況 gap、補強計畫
>
> 範圍：agent registration metadata / model card / eval traceability /
> deployment SBOM / audit retention / V&V 流程 / dataset lineage

**本 plan 不展開**，只在這節記錄 surface area。下一輪 design 衝刺再做。


### 7.5 Service Access Control（v0.2 新增，v0.5 對齊實作）

> **實作狀態**：✅ Phase 1 Step 1+2+2.5 已落地。Migrations [`0012`](../myCSPPlatform/backend/migrations/versions/0012_add_service_access_control.py) + [`0013`](../myCSPPlatform/backend/migrations/versions/0013_add_platform_link_is_public.py)，service [`access_control.py`](../myCSPPlatform/backend/app/services/access_control.py)，router [`service_access_grants.py`](../myCSPPlatform/backend/app/api/service_access_grants.py)。本節以實際 schema / 演算法為準。

#### 7.5.1 Schema（實作版）

```sql
-- ── Migration 0012 ──────────────────────────────────────────────────────
-- platform_links 加 required_roles 欄位（role 過濾 gate，非自動通過）
ALTER TABLE platform_links
    ADD COLUMN required_roles JSONB NOT NULL DEFAULT '[]'::jsonb;
-- []                       = role gate 開放（任何 role 都通過此 gate）
-- ['admin']                = 只有 admin 通過 gate（其他人連看不到）
-- ['admin','developer']    = 這兩個 role 通過 gate

-- 新表：grant 記錄（支援 user-level 與 department-level）
CREATE TABLE service_access_grants (
    id                SERIAL PRIMARY KEY,            -- Integer 跟既有 schema 一致
    user_id           INTEGER REFERENCES users(id) ON DELETE CASCADE,
    department_id     INTEGER REFERENCES departments(id) ON DELETE CASCADE,
    platform_link_id  INTEGER NOT NULL REFERENCES platform_links(id) ON DELETE CASCADE,
    granted_by        INTEGER REFERENCES users(id) ON DELETE SET NULL,  -- 保 audit
    granted_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at        TIMESTAMP,

    -- 強制 exactly-one：每筆 grant 要嘛 user 要嘛 dept，不能兩者都填或都空
    CONSTRAINT ck_service_access_grants_user_xor_department
        CHECK ((user_id IS NOT NULL) <> (department_id IS NOT NULL))
);

-- Lookup indexes（兩個方向都會被查）
CREATE INDEX ix_service_access_grants_user_id
    ON service_access_grants(user_id) WHERE user_id IS NOT NULL;
CREATE INDEX ix_service_access_grants_department_id
    ON service_access_grants(department_id) WHERE department_id IS NOT NULL;
CREATE INDEX ix_service_access_grants_platform_link_id
    ON service_access_grants(platform_link_id);

-- Partial unique：只在「active grant（revoked_at IS NULL）」上強制 unique
-- 這讓 revoke 後 re-grant 不撞 unique（revoked row 留作 audit trail）
CREATE UNIQUE INDEX uq_service_access_grants_user_active
    ON service_access_grants(user_id, platform_link_id)
    WHERE user_id IS NOT NULL AND revoked_at IS NULL;
CREATE UNIQUE INDEX uq_service_access_grants_department_active
    ON service_access_grants(department_id, platform_link_id)
    WHERE department_id IS NOT NULL AND revoked_at IS NULL;

-- ── Migration 0013 ──────────────────────────────────────────────────────
-- is_public：portal-style link 不需要 grant 也能讓所有通過 role gate 的 user 看到
ALTER TABLE platform_links
    ADD COLUMN is_public BOOLEAN NOT NULL DEFAULT false;
-- 0013 同時 backfill 既有 row 為 is_public=true 以保留 pre-migration 可見性
UPDATE platform_links SET is_public = true;
```

**為什麼選兩個 nullable FK + CHECK 強制 exactly-one**（不變）：
- 比 polymorphic 安全：FK constraint 可以正常 cascade（部門刪掉時 grant 自動清）
- 比兩張表簡單：同一個 SQL 查就能列，UI 只需要一個 list 介面
- CHECK 強制 exactly-one 防止資料髒掉

**為什麼 partial unique 而非純 unique**（v0.5 補充）：
- 純 unique 會擋掉「revoke 後 re-grant」這個常見流程（admin 撤回 grant 後又改主意要 re-grant）
- partial unique `WHERE revoked_at IS NULL` 把 revoked row 從 unique 索引中排除，等同「軟刪除」語意
- 結合 `revoked_at` 的 audit 用途：歷史 grant 都看得到，新 grant 又能順利寫入

**為什麼 is_public 是獨立 column 而非 `required_roles=['*']` 之類的 sentinel**（v0.5 新增）：
- 語意分離乾淨：`required_roles` = **過濾 gate**，`is_public` = **通過判定**
- 兩個維度可獨立組合：`required_roles=['developer'] + is_public=true` = 「公開給所有 developer」（這在 sentinel 設計下難表達）

#### 7.5.2 Effective Access 演算法（實作版 — 5 步，v0.5.1 admin bypass 提前）

權威實作：[`app/services/access_control.py`](../myCSPPlatform/backend/app/services/access_control.py)。語意改變請看 §0 v0.5 #16。

```python
def can_access_link(db, user, link) -> bool:
    """Single source of truth — every API endpoint that gates on a link MUST
    go through this function (or accessible_links_for() for batch listing).
    """

    # 1. is_active gate — 停用的 link 對誰都 deny（包含 admin，admin 要看
    #    要走 GET /api/platform-links?include_inactive=true）
    if not link.is_active:
        return False

    # 2. admin bypass — admin 在 active link 上一律通過
    #    放在 role gate 之前：避免 required_roles=['developer'] 把 admin
    #    擋在外面（admin 永遠是 superuser，universally trusted）
    if user.role == "admin":
        return True

    # 3. role gate — required_roles 是過濾條件，不是自動通過。空 list = gate
    #    開放；非空 = user.role 必須在裡面才通過 gate
    required = link.required_roles or []
    if required and user.role not in required:
        return False

    # 4. is_public bypass — 通過 gate 的 public link 對任何人都允許（不需 grant）
    if link.is_public:
        return True

    # 5. grant check — 必須有 active grant（revoked_at IS NULL），user-level
    #    或 user.department 所屬的 dept-level 都算
    return link.id in _active_link_ids_for_user(db, user)
```

**v0.5.1 修正（admin bypass 提前）**：
- 原 v0.5 把 admin bypass 放在 role gate 之後，這代表 `required_roles=['developer']` 會把 admin 也擋掉 — 跟「admin 永遠通過」的意圖矛盾，且 §7.1 必須寫 `['admin','developer']` 才 work（重複贅字）
- v0.5.1 後 admin bypass 在 step 2，admin 看得到任何 active link，§7.1 可以乾脆只寫 `['developer']`（admin 隱式通過）

**為什麼 `required_roles` 是 filter 不是 auto-pass**（v0.5 修訂）：
- v0.4 寫法（auto-pass）有歧義：`required_roles=['admin']` 到底是「admin 自動通過」還是「只開放 admin 看」？
- v0.5 把 `required_roles` 定位為「**必須要的 role gate**」（filter）— 想要 admin 自動通過已經有 step 2 admin bypass；想要公開可見已經有 step 4 的 `is_public`。維度分離乾淨。

**Cascade 行為**（admin 撤部門 grant 時的安全提醒）：
- 部門 grant 撤銷 → 該部門 user **只靠部門 grant 的會失去 access**
- 該部門 user **有額外個別 grant 的不受影響**（兩種 grant 是 OR 關係）
- UI 撤銷部門 grant 時要警告：「將影響 12 位使用者，其中 2 位有個別 grant 不受影響」

#### 7.5.3 Admin UI

```
Platform Link: NotebookLM
  Required Roles: (none — 純 grant 白名單模式)
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  🏢 Department Grants (一次蓋整批)
     ✅ 工程部           granted 2026-04-25 by admin (12 users)
     ✅ 產品部           granted 2026-04-26 by admin (8 users)
     [+ Grant to department]

  👤 Individual User Grants (例外加開)
     ✅ alice@example.com   granted 2026-04-25 by admin
     ✅ bob@example.com     granted 2026-04-26 by admin
     [+ Grant to user]

  📊 Effective Access Summary
     Total users with access: 22 (20 via dept + 2 individual)
     [View user list]
```

#### 7.5.4 Audit log

每次 `grant_*` / `revoke_*` 寫進既有 `audit_logs` 表（CSP 已有此基礎建設）：

```python
await audit_log(
    action="grant_platform_link_access",
    actor=admin.id,
    resource_type="platform_link",
    resource_id=link.id,
    detail={
        "grantee_type": "department",
        "grantee_id": dept.id,
        "grantee_name": dept.name,
    }
)
```

---

## 8. Phase 1 立即可做（v0.4 更新）

### 8.1 工作項目（v0.5 — Step 1+2+2.5 已落地）

| # | 項目 | 工程量 | 狀態 |
|---|---|---|---|
| 1 | Alembic migration `0012`：`platform_links.required_roles` + `service_access_grants` + `dev_db_credentials` 三表（§7.5、§5.3）| 2 小時 | ✅ commit `e611d89` |
| 1.5 | Alembic migration `0013`：`platform_links.is_public` + grandfather backfill（§7.5.1、§0 v0.5 #16）| 30 分鐘 | ✅ commit `cc121e3` |
| 2 | CSP backend 實作 `can_access_link()` + grant CRUD endpoints（§7.5）| 0.5 天 | ✅ commit `7c4df4a` |
| 3 | 寫 `AUTO_REGISTER_LINKS` 5 筆（NotebookLM / codeserver / n8n / gitlab / mlsteam，URL 見 §7.1） | 30 分鐘 | ✅ 待 commit |
| 4 | CSP UI Dashboard 顯示卡片時依 `can_access_link()` 過濾 + Service Access 管理 UI（§7.5.3）| 1-1.5 天 | ⏳ |
| 5 | ANILA monorepo `docker-compose.yml` 新增 `codeserver` service（§5.0.1）| 30 分鐘 | ✅ commit `11d572c` |
| 6 | **(v0.4)** ANILA monorepo `docker-compose.yml` 新增 `gitlab` service（§5.0.2）| 1 小時 | ✅ commits `11d572c` + `ec2272e`（healthcheck fix）|
| 7 | **(v0.4)** GitLab 首次啟動 + reconfigure（等 healthy）+ root 密碼設定 + 開 admin 帳號 | 1-2 小時 | ✅ booted；初始 root password 在 `/etc/gitlab/initial_root_password`（首次登入務必輪換）|
| 8 | Nginx 設定 `/codeserver` + `/gitlab` 同源 reverse proxy（§5.0.1、§5.0.2）| 1.5 小時 | ✅ commits `11d572c` + `ec2272e`（Host header fix）|
| 9 | E2E 測試 1：admin grant 工程部 access NotebookLM → 部門使用者 dashboard 看到卡片 → 點開（Phase 1 仍重新登入）| 1.5 小時 | ✅ `scripts/phase1-e2e.sh` Step 9（default-deny → dept grant unlock → revoke 三段全綠）|
| 10 | E2E 測試 2：admin / developer 進 codeserver 與 GitLab 同源 path、WebSocket terminal / CI log live tail 都能用 | 1.5 小時 | ✅ `scripts/phase1-e2e.sh` Step 10（codeserver/gitlab/n8n title 都正確；WS upgrade probe 401 = 路由通但 auth gate 擋下，跟 standalone 部署一致）|
| 11 | My-OpenAI-Frontend 停用：`docker compose down` + nginx `/v1/*` 改 410 + README archive notice（§3.0.1）| 30 分鐘 | ⏳ 需 user 確認再動（destructive，需要先 migrate user 的 n8n workflows 等資料）|

**Phase 1 follow-up（不在原 §8.1 但 Step 3 之後 surfaced）：**
- ✅ n8n 自部署（commit `88df3fc`）— 之前 `/n8n` 是 my-openai-frontend nginx 代理的，Step 11 cutover 前必須先 self-host 否則 path 會 502

**進度**：1 / 1.5 / 2 完工（~6 小時實際工程量，含 smoke test）；剩 3 → 11 約 2.5–4 天。

**v0.4 從 Phase 1 移除的項目**（推到 Phase 1.5，§8.4）：
- ~~寫 `comfyui-agent` service~~
- ~~`docker-compose.yml` 新增 `comfyui-agent` service~~
- ~~CSP `AUTO_REGISTER_AGENTS` 註冊 `comfyui-image` agent~~
- ~~E2E 測試「畫海報」流程~~

理由：ComfyUI 模型還沒實際跑起來，agent 開發與 E2E 測試無法驗收。

### 8.2 Phase 1 Deliverable

- 使用者登入 ANILA dashboard 看到「自己有權限」的服務卡片（依 `service_access_grants` 過濾）
- ANILA chat 使用者說「畫海報」可以拿到圖（透過 comfyui-image agent）
- admin 可以一次 grant 整個部門 access 某個服務
- codeserver 跟著 ANILA stack 一起部署（admin only）
- My-OpenAI-Frontend 已停用，不再有流量

### 8.3 Phase 1 不會做

- 不做 SSO（各服務維持原 auth；使用者點 NotebookLM / codeserver / GitLab 卡片仍要重新登入）→ Phase 3
- 不做 My-OpenAI-Frontend data migration（沒 active 使用者，不需要）
- 不動 NotebookLM 內部結構 → Phase 4 才動
- **(v0.4)** 不做 ComfyUI agent → Phase 1.5（等模型部署）
- 不做 ComfyUI workflow preset Layer 1+（admin 上傳新 preset 的 UI）→ Phase 2+ 看需求
- 不做 dev DB credential endpoint（§5.3）→ Phase 3
- 不做 GitLab CI Runner（runner 跑在 mlsteam，§5.0.2 末段）

### 8.4 Phase 1.5 — ComfyUI Agent（待 ComfyUI 模型啟動後 trigger）

**啟動條件**：ComfyUI 模型 (FLUX / SD XL) 在 GPU 機器實際跑起來、能對外 expose `http://comfyui:8188`

**任務清單**（從 v0.3 的 Phase 1 移過來）：

| # | 項目 | 工程量 |
|---|---|---|
| 1 | 寫 `comfyui-agent` service（fork AgenticRAG template，250 行 Python + 1 個預設 workflow JSON）| 1 天 |
| 2 | ANILA monorepo `docker-compose.yml` 新增 `comfyui-agent` service | 30 分鐘 |
| 3 | CSP `AUTO_REGISTER_AGENTS` 註冊 `comfyui-image` agent（§4.3） | 15 分鐘 |
| 4 | E2E 測試：使用者在 ANILA chat 說「畫海報」→ Router 分派 → 收到圖 | 2 小時 |

**總工程量**：~1.5 天（連續沒 block 的話）。

**Phase 1.5 觸發時機**：誰 deploy ComfyUI 模型完成、看到 `curl http://comfyui:8188/system_stats` 回 200，就可以啟動。

---

## 9. Phase 2：My-OpenAI-Frontend 廢除（2-3 週）

詳見 §3。要點：
- 補 CSP 的 audio transcription endpoint
- 一次性 migration script 跑完
- Client cutover + 觀察期
- 90 天後完全 sunset

---

## 10. Phase 3：SSO + codeserver Dev Workspace（2-3 週）

### 10.1 SSO

- CSP 啟用 OIDC Provider mode
- NotebookLM / n8n / gitlab / codeserver 改認 CSP 為 IdP

### 10.2 codeserver Dev Workspace 整合

- 新增 CSP endpoint `POST /api/dev-credentials/db`（§5.3）
- ANILA UI Dev Console 加「Get DB Credentials」按鈕
- 短效 PG role 機制（24h TTL）+ 自動 SET LOCAL anila.agent_id

---

## ~~11. Phase 4：NotebookLM Agent 化（v0.6 取消）~~

> v0.6 決策（§0 #30）：NotebookLM 永遠保持獨立 service。整章 cancelled。下面內容是歷史紀錄。

詳見 §6。要點：
- 必須先完成 Phase 2（取代 My-OpenAI-Frontend）+ Ingestion Platform 上線 + Phase 3 SSO
- 採路線 B：單 agent + 9 tools
- 新加 OpenAI-compatible endpoint 到 NotebookLM backend
- 註冊為 CSP agent

---

## 12. 風險與 Mitigation

| 風險 | Phase | Mitigation |
|---|---|---|
| Phase 2 cutover 漏改 client | 2 | grep audit + Step 5 410 Gone 暴露漏網之魚 |
| ComfyUI bridge 預設 workflow 不滿足使用者 | 1 | 提供 3-5 種預設、之後加 `workflow_url` 欄位讓 dev 自帶 |
| codeserver dev credential 洩漏 | 3 | 24h TTL + audit log + revoke API |
| NotebookLM agent 化破壞 prod 流量 | 4 | 雙軌跑 1 個月（舊 UI + 新 agent endpoint），確認後再 sunset 舊 UI |
| n8n / gitlab SSO 失敗 | 3 | 各服務本地登入 fallback 保留 |
| ComfyUI bridge timeout（圖片生成慢） | 1 | wrapper 內部 polling 10 分鐘 ceiling，超時回 504 |

---

## 13. Open Questions

1. **codeserver 部署位置** — 是放在 ANILA monorepo 內 `docker-compose.yml`，還是組裡已經有獨立部署？確認 URL 後填 §7.1
2. **mlsteam 連線資訊** — 確認內部 hostname（目前 §7.1 寫 `mlsteam.internal` 是猜的）
3. ~~**NotebookLM Chroma 處理**~~ — v0.6 取消 NotebookLM agent 化（§11），NotebookLM 永遠用自己的 chroma，無需處理 ANILA pgvector 共用問題。
4. **My-OpenAI-Frontend 廢除時間軸** — 跟 Ingestion Platform Sprint 1 並行，還是 Sprint 4 之後再做？
5. **ComfyUI workflow preset 選誰負責** — `comfyui-bridge` 內建 3-5 種 workflow，由誰提供（內部設計師 / 沿用 ComfyUI community workflow）？
6. **dev credentials 的 PG role TTL** — 24h 太長還是太短？要不要做「續期」endpoint？

---

## 14. 整體時程整合表

```
Week 1     ─── Phase 1 (導向卡片 + ComfyUI wrapper)
Week 2-3   ─── Ingestion Platform Sprint 1 (含 anila-core 瘦身)
Week 4-5   ─── Phase 2 (My-OpenAI-Frontend 廢除) +
                Ingestion Platform Sprint 2 (UI + worker)
Week 6-7   ─── Ingestion Platform Sprint 3 (Evaluator)
Week 8-9   ─── Phase 3 (SSO + codeserver dev workspace)
Week 10+   ─── 觀察期（NotebookLM agent 化 v0.6 取消，§11）
```

**Critical path**：Phase 1 → Ingestion Platform → Phase 2 → Phase 3。

各階段獨立 deploy、獨立 rollback。

---

**Last updated**: 2026-04-25 · **Companion docs**: [`ingestion-platform-design.md`](./ingestion-platform-design.md) · [`anila-core-boundary.md`](./anila-core-boundary.md)
