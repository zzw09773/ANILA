# CLAUDE.md — ANILA 專案記憶(給 Claude Code)

> **程式架構 / 分支模型細節 / compose / 資料流契約 / 測試矩陣 / 寫碼規則的權威細節在 `AGENTS.md`**(很完整、讀過碼)。本檔**不重複**那些,只補 `AGENTS.md` 沒涵蓋的:① 內網部署運維知識 ② 工作流分工 ③ 重點提醒。
> 回覆一律**繁體中文 + 台灣用語**(禁簡體、禁大陸用語)。動手前先讀 `AGENTS.md` 與相關子專案程式碼,不只 README。

---

## -1. 跨專案制度（playbooks，2026-07-03 立）

派工／驗收／完成判準／制度維護規則在 `~/.claude/playbooks/`（symlink，真身＝可攜 kit `/home/aia/claude-harness-kit/`；00 診斷、10 調度、20 判斷、30 派工模板、40 維護、90 給未來 session 的信）。觸發路由表在全域 `~/.claude/CLAUDE.md`：**派 subagent 前讀 10、宣稱完成前讀 20**。playbooks 是跨專案通則；與本檔或 `AGENTS.md` 衝突時，**專案檔優先**。

---

## 0. 一句話定位

ANILA = 中科院/NCSIST 軍方**內網(air-gapped)** 的 NotebookLM 式平台,**PKI 自然人憑證卡登入**。多服務 monorepo,**4 分支模型(2026-07-24 精簡)**:`main` 是 SSOT 且 **dev 直接在此**;3 條部署分支 = `prod-military-passwd`/`prod-intranet-card`(與 main **只差 `.env.example`**)+`trial-military`(另含開發者視圖刪減 8 檔)。dev-* 與 prod-public-passwd 已裁撤(零語意/無存活部署;外網帳密部署概念上可用 main+`.env` 重啟,⚠ 但 `deploy-prod.sh` 的 `check_branch` 目前仍只接受 prod-*/trial 分支——真要重啟前需先調整該檢查,屬部署腳本決策)。⚠ 歷史教訓:07-22 曾查獲雙向漂移,「領先 11 commit」經 `git patch-id` 證實是 main 上同卵雙胞的假警報——**量測分支差異看 `git diff` 與 patch-id,別信 ahead/behind**。**詳細服務地圖與分支模型見 `AGENTS.md` §2–3。** Repo 是 **PUBLIC** → 祕密零外洩。

> **後續開發路線圖:`docs/planning/anila-development-roadmap.md`**(Gate 制、不可倒置的排序規則、已推翻的假警報清單)。動手前先看你在哪個 Gate。

---

## 1. 工作流分工(2026-06 起)

- **Codex 是開發主力、Claude(我)審查 Codex 的產出**。反向仍成立:我自己寫的碼交 Codex/user 審。看誰寫的決定誰審。
- **審 Codex 產出時**:① **一定跑測試,別只看 diff**(實做過:抓到既有 stale 測試 FAIL,非 Codex regression);② 分清 **regression vs pre-existing**(看 Codex 有沒有動到該處 + base 是否已壞);③ **跨切面核心改動從 `main` 起、再散 3 條部署分支**,別只落在當下那支;④ **混 concern 要拆 commit**(如 SSE 邏輯 vs CSS token)。
- 我自己若要在部署分支落地:`main` 先 commit → merge/cherry-pick 到 prod-military-passwd、prod-intranet-card → **`trial-military` 是刪減分支**(移除 developer 視圖如 `DeveloperAgentsView.vue`),前端改動會撞 modify/delete,要挑選式 port。

---

## 2. 內網機器拓撲(`AGENTS.md` 沒有)

| 主機 | IP / 名稱 | 角色 |
|---|---|---|
| 平台主機 | `.15` = 10.53.100.15 / `anila.ai.ncsist.org.tw` | docker compose 全棧(project `anila-platform`) |
| 模型 gateway | `.12` = 10.53.100.12 / `aiagent2.ai.ncsist.org.tw` | My-OpenAI-Frontend;`/v1` 出 gpt-oss-20b / gemma4 / nv-embed,需 Bearer `MODEL_GATEWAY_API_KEY` |
| MLSteam | `aiops.ai.ncsist.org.tw` | anila-agent 跑在這的 Lab(純 http NodePort 對外) |
| 本開發機 | `$HOME/ANILA` | 寫碼處;跑本機 dev stack **`anila-platform-dev`**(`compose.dev.yaml`,從 `main` 建)+ 本機 anila-models。(07-24:舊敘述的外網 `anila-platform` prod-public stack 已不存在) |

- 本機 `anila-platform-*` 容器 = user dev 環境,**未授權不要 restart/動它**。
- **csp 容器沒裝 `curl`** → 測內部端點用 `docker exec <csp> python3 -c "import httpx; ..."`(curl 回空 = 假陰性)。

---

## 3. 內網 TLS / CSPKI(踩過大坑,`AGENTS.md` 沒有)

- 內網一切 https 走中科院 **CSPKI**:`CSPKI Root CA G1`(自簽 root)→ `中科院憑證管理中心 G1`(中繼)→ `*.ai.ncsist.org.tw`(leaf)。`.12` 只送 leaf。
- **`model-ca.pem` = `services/csp/app/services/cspki_ca_bundle.pem`**:卡登驗章那份 CSPKI bundle(Root+中繼)正好就是 csp 信任 `.12` 所需的完整鏈(中科院 PKI 一條根,卡與伺服器憑證同源)。`intranet-deploy.sh [2/7]` 已預設 cp 它(`6eadd83`)。
- ⚠ **`SSL_CERT_FILE`(=`ANILA_MODEL_CA_FILE`)是「取代」整個系統信任庫,非疊加**。指到空/壞檔 → csp **所有**出向 https 全 `CERTIFICATE_VERIFY_FAILED`(admin 模型健康欄全 X509、連 agent 都連不上)。
- TLS 鏈要爬到自簽 root 才算數;先在本機 `openssl s_client -connect host:443 -CAfile X` 驗到 `verify return code: 0` 再接進服務。
- **`.12` 一律用 FQDN `aiagent2.ai.ncsist.org.tw`,不用 raw IP**(SSRF 私網 guard 擋 + 憑證主機名不符);容器靠 compose `extra_hosts` 解析,不需 DNS;`ANILA_TRUSTED_HOSTS=aiagent2.ai.ncsist.org.tw` 放行;model 註冊填 FQDN。
- **MLSteam agent(aiops)是純 http NodePort** → CSP agent endpoint 填 `http://aiops.ai.ncsist.org.tw:<port>` + `ANILA_ALLOW_HTTP_ENDPOINT=1`。填 https → `WRONG_VERSION_NUMBER`。
- **平台門面**:從 `server.pfx`(空密碼)抽 fullchain `server.crt` + `server.key`。用 IP 連會跳憑證警告(正常,主機名不符);正解 DNS 或 client hosts 檔。

---

## 4. 部署運維 footgun

- **別用 `START-HERE.sh` / `intranet-deploy.sh` 做小修改** —— 每跑一次就無條件重設 `ANILA_ALLOW_HTTP_ENDPOINT=0` / `ANILA_ALLOW_PRIVATE_ENDPOINT=0` / `ANILA_MODEL_CA_FILE`,蓋掉手動修正。**改設定 = 編 `.env` + `up -d csp`**(內網 `.15` 用 `-p anila-platform` 正式 stack;本機開發一律用 `compose.dev.yaml` 的 `-p anila-platform-dev`,別碰正式專案名)。
- **`docker restart` ≠ recreate**(不重載 `.env`/compose);套設定一律 `up -d`。(`AGENTS.md` §4 同調。)
- prod 模式缺 JWT keypair → JWKS 500、登入炸、studio crash-loop;deploy 腳本會在 up 前產 RSA-2048 到 `./secrets`(compose 掛 `:ro`)。
- bundle `intranet-prod-v1.0.0` 的 `00-anila-src.tar.gz` 是源碼快照;改了 git 腳本要重打包才帶進去。

---

## 5. 當前部署狀態(會變,動前先核對)

### 5.1 ⚠ 機敏資料 production:**No-Go**(2026-07-10)

目前**任何部署都不得放機密以上資料**。資料門檻以 roadmap §6.1 為準:Gate 0 只允許無機密受控 pilot;營業秘密的 RAG/chat-only pilot 至少需要 Gate 0+1+2 且由資料/資安權責人書面核准,若啟用 Studio/Artifact/FLUX/export 再加 Gate 3;機密以上 production 必須完成 Gate 0+1+2+3+5 並通過 Gate 6 的 signed acceptance。高危項的數量與完整證據一律以 `docs/planning/anila-development-roadmap.md` §3 為準,不要在本檔複製會漂移的計數。

> **2026-07-26 改寫(W0-8)**:本節先前列為「最嚴重一條」的 codeserver 阻斷項**已經修好了**,但本檔沒跟上,導致它在排序上壓在 No-Go 首位長達兩週,並成為另一條發現的威脅模型前提。過時的安全主張同時造成**重工**與**錯誤排序**,所以改寫並附驗證指令與快照日期。完整稽核見 `docs/planning/platform-audit-synthesis-2026-07-26.md`;執行計畫見 `platform-remediation-plan-2026-07-26.md`。

**已修,不要再排工**(逐條附複驗指令,2026-07-26 快照):

| 舊主張 | 現況 | 怎麼複驗 |
|---|---|---|
| `codeserver` 以 RW 掛 repo root、遮蔽清單只兩條 | **已修**:只掛隔離 workspace `../../share/codeserver-sandbox`,`working_dir` 在其內,networks 僅 `codeserver-tools` | `sed -n '807,822p' infra/compose/platform.yml` |
| `codeserver` 無 `profiles:`,預設隨 stack 啟動 | **已修**:`profiles: ["developer-tools"]` | 同上 |
| nginx `/codeserver` 無認證 | **已修**:兩個平台 server block 皆 `return 404` | `grep -n 'codeserver|n8n|gitlab' infra/nginx/anila.conf` |
| `anila-ops.sh` 的 `BACKUP_DIR` 落在 repo 內 | **已修**:`${ANILA_BACKUP_DIR:-$ANILA_STATE_DIR/backups}`,且經 `assert_outside_repo` 強制在 repo 外 | `grep -n 'BACKUP_DIR|assert_outside_repo' infra/deployment/scripts/anila-ops.sh` |
| 營業秘密對話可建立未登入分享連結 | **分類面已修**:`is_publicly_shareable` fail-closed 到只允許 `無機密` | `sed -n '376,389p' services/csp/app/services/conversation_service.py` |

**仍成立的**:

- **`n8n` 與 `gitlab` 無 `profiles:`** → 仍預設隨 stack 啟動(`platform.yml:842`、`:903`),`AGENTS.md` §3.3「交付規格要求移除」的關切未解。→ 補救計畫 **W1-8**。
- ⚠ **分享連結的旗標面沒修**:`create_share` **從未讀 `settings.ENABLE_PUBLIC_SHARE`**(`conversation_service.py:427-454`);讀取端有擋(`public_share.py:50-53`)。card 部署姿態是 `ENABLE_PUBLIC_SHARE=false` → 使用者**建得出分享連結、同事一定看到 404**。這與上表「分類面已修」是**兩個不同缺陷**,別混為一談。→ 補救計畫 **W3-7c**。

**No-Go 判定本身不變**(資料門檻仍依 roadmap §6.1)。目前排序最高的阻斷級發現已改為:分類分級的**外流面**(複製/匯出/分享/列印/稽核五個 gate 全掛在 legacy `classified` boolean 上 → 營業秘密等同無機密)與**輸入面**(文件密等純繼承 collection、零 reviewer、任何人都能建無機密知識庫)。前者 = W1-1,後者 = W2-11。

### 5.2 部署進度

- **2026-07-10 快照,動前重驗**:外網 `anila-platform`(prod-public-passwd)已 rebuild 含 system-prompt 產生器 + guide + JWT 修;之後又上 SSE robust 串流。
- **2026-07-10 快照,動前重驗**:內網 prod-intranet-card 部署收尾中;`.12` model-ca 用 CSPKI bundle 驗 `return code 0` → 填 API key → `up -d csp`;agent endpoint `http://aiops:<port>` 使用 `ANILA_ALLOW_HTTP_AGENT_ENDPOINT=1`,同時必須保持 `ANILA_ALLOW_HTTP_ENDPOINT=0`;DNS 暫用 IP 過渡。
- **2026-07-10 待辦快照**:`.12` gateway API key 簽發(user 端)。

---

## 6. 鐵則(快速;詳細安全/測試見 `AGENTS.md` §6–7、§9–10)

- 繁中台灣用語、無簡體。
- **祕密零外洩**(PUBLIC repo):`.env`/`*.pem`/`*.key`/`secrets/`/憑證私鑰 已 gitignore,別加回追蹤。⚠ **容器掛載不看 gitignore** —— `codeserver` 以 RW 掛 repo root,`backups/` 落在裡面(見 §5 安全阻斷)。
- **不動 running `anila-platform-*` 容器**(user dev 環境),除非授權。
- **端到端驗證、用對方法**:別只看 status code(SPA catch-all 對未匹配路由回 200 text/html → 驗 Content-Type);取資料走正式 HTTP API + auth,不直連 DB。
- **別腦補成 bug**:功能按 spec ≠ bug;先客觀呈現,讓 user 判斷。
- 前端驗證用 `npm run build`(非只 `tsc`);後端 `services/csp/.venv/bin/python -m pytest`。
- SSRF guard、卡登驗章、JWT 信任錨不可弱化;改 schema 必加 alembic migration;runtime DB 用 `csp_app` role(非 superuser,否則繞過 RLS)。
- commit/push 只在 user 要求時;跨分支同步遵 `AGENTS.md` §3(main 起、downstream 不互 merge、**各分支的 `.env.example` 旗標姿態不可被 main 覆蓋**)。姿態更新必**語意重推導**——以 main 現行範本為基底、只覆寫分支蓄意值,禁直接 apply 舊 diff(07-22 廢棄 sync 分支曾因此丟失 card 的 `ENABLE_PUBLIC_SHARE=false`/`ENABLE_MEMORY=false`)。
  - ⚠ **舊敘述「card fork 區不可被 main 覆蓋」已作廢**:實測 `prod-intranet-card` 與 `main` 程式碼相同(07-10 首測、07-22 重收斂再確認),card/SSO 碼已收進 `main`,不存在 fork 熱區。詳見 `AGENTS.md` §3.1。
