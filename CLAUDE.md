# CLAUDE.md — ANILA 重啟樹(給 Claude Code)

> **這是重啟樹**:2026-07-28 平台擁有者決定回到 redesign 收斂點 `a4118a3`(2026-07-03)重新出發。
> 權威文件就在本樹根目錄:**規格＝`SYSTEM-MAP.md`**(28 題 QA)、**順序＝`PLAN.md`**(到 8 月底上線)、
> **歷史＝`RESTART-FROM-REDESIGN.md`**(373 commit 履歷＋attic 取回方式)。
> 📌 **接手先讀 `HANDOFF-2026-07-29.md`** —— P0/P1 做了什麼、哪些是刻意不做、待擁有者決定的四件事、派工與審查的操作要點。
> ⚠ **`AGENTS.md` 是 2026-06-22 版,大幅過時**——讀碼以 SYSTEM-MAP 與現行程式碼為準,別照它辦事。
> 舊資料夾 `~/桌面/ANILA/anila-migration-20260706/ANILA`(main＋attic/2026-07-28/*)只當**參照**,不在上面開發。
> 回覆一律**繁體中文＋台灣用語**(禁簡體、禁大陸用語)。

---

## -1. 跨專案制度(playbooks)

派工／驗收／完成判準在 `~/.claude/playbooks/`(symlink,真身＝`~/claude-harness-kit/`;10 調度、20 判斷、30 派工模板、40 維護、90 信)。路由表在全域 `~/.claude/CLAUDE.md`:**派 subagent 前讀 10、宣稱完成前讀 20**。與本檔衝突時專案檔優先。工作流編組(grok 主力、Claude 審查、sol 二票、kimi-k3 三票)照《10 §3b–3c》,不在此重複。

---

## 0. 一句話定位

ANILA = 中科院/NCSIST 軍方**內網(air-gapped)** 的 NotebookLM 式平台,PKI 自然人憑證卡登入。目前**單一開發線 `restart/from-redesign`**(工作 worktree 分支除外);舊 4 分支模型已進 attic,**不要**在 PLAN 排到之前重建部署分支。Repo 是 **PUBLIC** → 祕密零外洩。目標:**8 月底全院上線**(PLAN.md),一人維運。

---

## 1. 內網機器拓撲(環境事實,與程式基底無關)

| 主機 | IP / 名稱 | 角色 |
|---|---|---|
| 平台主機 | `.15` = 10.53.100.15 / `anila.ai.ncsist.org.tw` | docker compose 全棧;**現為 redesign 前舊版,資料可刪、砍掉重來**(RESTART 文件 §四) |
| 模型 gateway | `.12` = 10.53.100.12 / `aiagent2.ai.ncsist.org.tw` | My-OpenAI-Frontend;`/v1` 出 gpt-oss-20b / gemma4 / nv-embed,需 Bearer key |
| MLSteam | `aiops.ai.ncsist.org.tw` | anila-agent 跑在這的 Lab(**純 http NodePort**) |
| 本開發機 | `~/桌面/ANILA/anila-restart-20260729/ANILA` | 重啟樹寫碼處;舊 dev stack 已於 07-29 授權下線,埠 80/443/4443/5433 已釋出 |

- **csp 容器沒裝 `curl`** → 測內部端點用 `docker exec <csp> python3 -c "import httpx; ..."`(curl 回空＝假陰性)。

## 2. 內網 TLS / CSPKI(踩過大坑)

- 內網 https 走 **CSPKI**:`CSPKI Root CA G1`(自簽 root)→ 中繼 → `*.ai.ncsist.org.tw` leaf;`.12` 只送 leaf。
- **`services/csp/app/services/cspki_ca_bundle.pem`**(卡登驗章那份)= csp 信任 `.12` 所需完整鏈,可直接當 `ANILA_MODEL_CA_FILE`。
- ⚠ **`SSL_CERT_FILE`(=`ANILA_MODEL_CA_FILE`)是「取代」整個信任庫,非疊加**——指到空/壞檔,csp 所有出向 https 全掛。
- 先 `openssl s_client -connect host:443 -CAfile X` 驗到 `verify return code: 0` 再接進服務。
- **`.12` 一律用 FQDN**(SSRF 私網 guard＋憑證主機名);compose `extra_hosts` 解析;`ANILA_TRUSTED_HOSTS` 放行。**`.12` 本身走 https(CSPKI),不需 http 旗標**;P0.2 的 `ANILA_ALLOW_HTTP_ENDPOINT` 是為其他純 http 模型端點(PLAN 0.2)。
- **aiops agent 是純 http** → endpoint 填 `http://...` 並設 `ANILA_ALLOW_HTTP_AGENT_ENDPOINT=1`;填 https → `WRONG_VERSION_NUMBER`。
- 平台門面:`server.pfx`(空密碼)抽 `server.crt`＋`server.key`;用 IP 連跳憑證警告是正常。

## 3. 運維 footgun

- **`docker restart` ≠ recreate**(不重載 `.env`/compose);套設定一律 `up -d`。
- csp 啟動要求 secrets 非 dev 預設值(`startup_security.py`),缺 JWT keypair → JWKS 500、登入炸;keypair 產到 `./secrets`(compose 掛 `:ro`)。
- 本機起正式 compose 用**獨立 project 名**(如 `-p anila-restart`),避免撞舊 stack 的網路/volume 名。

## 4. 當前快照(2026-07-29 晚,會變,動前核對)

- **P0 四項全關**:P0.1 消解(四閘不存在於本基底);P0.2 已合併(`657ba43`,sol＋kimi 雙跨家審查通過,e2e PASS:production＋旗標下註冊 `http://` 端點 200、旗標未設仍拒);P0.3 本機 `-p anila-restart` 全量起 12/13 healthy;P0.4 alembic 從零→`r1_0008` 已驗(權威文件已入庫 `faa2ac3`)。
- **codeserver 已修復上線(13/13 healthy)**:擁有者裁定必要(掛 repo＋docker extension 內網維運)。自建 image `anila-codeserver:local`(docker CLI 28.5.2＋containers/docker 兩個 extension 離線烘焙,內網零下載)＋`codeserver-init` chown 機制(`infra/codeserver/Dockerfile`＋platform.yml);runtime UID/GID 跟 `.env`(本機 1026:516,`DOCKER_GID` 對 socket 群組)。⚠ docker.sock=主機 root 等價,密碼即失守——取捨已記錄在 platform.yml 註解;recreate codeserver 會被權限 classifier 擋,由擁有者親跑。
- **n8n/gitlab 裁定保留**。gitlab 的 `.env` 必帶 `GITLAB_ROOT_PASSWORD`(omnibus 直讀 env,空字串在 Ruby 是 truthy,長度檢查會炸)。
- **本分支獨立演進,不進 main**;commit/push 時機授權 Claude 判斷(push 前必掃祕密＋全 RFC1918)。
- 本機 `.env` 已是正式姿態(`ANILA_ENV=production`＋`ANILA_ALLOW_DEV_SECRET=0`,csp healthy)。⚠ 這兩者**無交叉檢查**——內網部署照抄 dev 值不會被擋,上線前自查。
- **P1.1 已關板**(`93ba157`):departments 三層樹(`parent_id`+`r1_0009`+守衛+advisory lock);grok 作者、opus+sol 三輪跨家審查收斂、本機活體 migration+e2e 全過。深度上限 3、停用 fail-closed、name 全域唯一是指揮官保守預設,擁有者可翻案。遺留 LOW(不擋路):/tree 對環成員靜默省略(診斷性)、advisory lock 無 timeout(單管理員可接受)。
- **P1.2 已關板**(`a2d1f42`):usage 查詢的 department 過濾統一經 `_department_scope_ids` 展開為子樹(讀取時聚合,寫入歸屬/特權閘門/top-departments 直接歸屬不變);雙審收斂,本機 e2e 巢狀 400⊂600⊂700+chart+直接歸屬全過。遺留給 P1.3:summary 每請求 4 次重複 scope 載入要做 per-request 記憶化。
- **P1.3 已關板**(`526783c`):unit_admin 綁定表(`r1_0010`,每節點上限 3、可跨節點、soft-revoke)＋`/api/unit-admins`(僅 admin 可指派)＋usage/users 三層閘門＋`get_top_agents` 部門維度＋P1.2 記憶化。**`users.role` 與 `auth_service.py` 一字未動**(審查以 sha256 驗);unit_admin 永不通過 `is_admin_tier`,對話/memory/audit 面零新增存取;範圍外請求 403、不可見帳號回 404(與 list 不可見性一致)。額度分配依擁有者拍板遞延至計價 epic(docstring 已註記)。雙審 APPROVE＋本機 e2e 全過。
- **P1.4 已關板**(`5062c96`):`POST /api/users/batch-approve`,選擇器二擇一(user_ids／department_id 預設含子樹)＋`dry_run` 預覽;授權沿用 P1.3 分層並把部門集合與 `unit_scope` 取交集(擋兩次樹讀取之間的 re-parent);不可見目標不進任何分桶(與單筆端點的 404 同語意);整批單次 commit,稽核寫入失敗即 500 中止(不留半套);兩道界線=原始輸入 1000／實際核准 500(算待核准數,大節點才批得動)。雙審 APPROVE＋活體 50 帳號 e2e 全過(dry-run 零寫入、冪等、稽核 50+2 筆)。
- ⚠ 觀察(非 P1.4 缺陷,待日後處理):`UserResponse.updated_at` 非選擇性,若有資料列該欄為 NULL(外部工具/migration 灌入),`GET /api/users` 會 500 而非降級。
- **OE-2 已關板**(`647c3fc`,2026-07-29 晚):六域對照 SYSTEM-MAP 稽核 338 構造(CAT-A 101/B 80/C 157),sol 跨家覆核 10C/9P/0R;收斂包 D1–D6、反向缺口 G1–G8、缺陷 B1–B4 已排入 PLAN(擁有者裁決 R1 降級雙人流程退場、R2 服務表叢收斂為入口連結目錄、R3 D4 併 OE-1)。產出=`docs/audits/oe2-2026-07-29/`。
- **OE-3 已關板**(merge `c4bee70`):分類四級 無機密<營業秘密<密<機密;r1_0003 **原地改寫**(擁有者裁決,全庫可拋、本機已砍庫從零驗到 r1_0011);舊識別字 CONFIDENTIAL/TOP_SECRET/ABSOLUTE_SECRET 已刪無 alias;手動分類寫「密」(rank-2 行為保持);前端(governance-ui+anila-shell 含浮水印)四級化,anila-ui 容器已重建。⚠ 門檻仍是統一判準,兩條線=OE-4。⚠ sol 通道額度罄至 2026-08-05,二票=kimi-k3、驗收=fresh opus(已揭露)。
- 下一步:**OE-4 門檻兩條線**(併 C5 boolean 鏡射退場+G4+artifact export 判定軸)→ OW-1 訊息樹完整分支 → OW-3;P1.6(`.12` 前綴快取查證)需進內網現場做。

## 5. 鐵則

- **祕密零外洩**(PUBLIC repo):`.env`/`*.pem`/`*.key`/`secrets/` 不進追蹤;容器掛載不看 gitignore,新增 RW 掛載要想清楚。
- **端到端驗證用對方法**:別只看 status code(SPA catch-all 回 200 text/html → 驗 Content-Type);取資料走正式 API＋auth,不直連 DB。
- **別腦補成 bug**:功能按 spec ≠ bug;先客觀呈現,讓 user 判斷。
- 前端驗 `npm run build`(非只 tsc);後端 pytest(本樹尚無 venv,暫借舊樹 `services/csp/.venv` 的直譯器＋`PYTHONPATH=packages/*/src`,建好自己的 venv 後改用)。
- SSRF guard、卡登驗章、JWT 信任錨不可弱化(P0.2 的 http 旗標分域是擁有者拍板的例外,紀錄在 PLAN.md);改 schema 必加 alembic migration;runtime DB 用 `csp_app` role(非 superuser,否則繞過 RLS)。
- commit/push 只在 user 要求時。
