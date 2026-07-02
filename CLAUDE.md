# CLAUDE.md — ANILA 專案記憶(給 Claude Code)

> **程式架構 / 分支模型細節 / compose / 資料流契約 / 測試矩陣 / 寫碼規則的權威細節在 `AGENTS.md`**(很完整、讀過碼)。本檔**不重複**那些,只補 `AGENTS.md` 沒涵蓋的:① 內網部署運維知識 ② 工作流分工 ③ 重點提醒。
> 回覆一律**繁體中文 + 台灣用語**(禁簡體、禁大陸用語)。動手前先讀 `AGENTS.md` 與相關子專案程式碼,不只 README。

---

## 0. 一句話定位

ANILA = 中科院/NCSIST 軍方**內網(air-gapped)** 的 NotebookLM 式平台,**PKI 自然人憑證卡登入**。多服務 monorepo,`main` 是 SSOT,7 分支 = main + 登入/部署設定 delta。**詳細服務地圖與分支模型見 `AGENTS.md` §2–3。** Repo 是 **PUBLIC** → 祕密零外洩。

---

## 1. 工作流分工(2026-06 起)

- **Codex 是開發主力、Claude(我)審查 Codex 的產出**。反向仍成立:我自己寫的碼交 Codex/user 審。看誰寫的決定誰審。
- **審 Codex 產出時**:① **一定跑測試,別只看 diff**(實做過:抓到既有 stale 測試 FAIL,非 Codex regression);② 分清 **regression vs pre-existing**(看 Codex 有沒有動到該處 + base 是否已壞);③ **跨切面核心改動從 `main` 起、cherry-pick 散 7 分支**,別只落在當下那支;④ **混 concern 要拆 commit**(如 SSE 邏輯 vs CSS token)。
- 我自己若要在 7 分支落地:`main` 先 commit → cherry-pick downstream → **`trial-military` 是刪減分支**(移除 developer/dev-tooling view,如 `DeveloperAgentsView.vue`),前端改動會撞 modify/delete,要挑選式 port。

---

## 2. 內網機器拓撲(`AGENTS.md` 沒有)

| 主機 | IP / 名稱 | 角色 |
|---|---|---|
| 平台主機 | `.15` = 10.53.100.15 / `anila.ai.ncsist.org.tw` | docker compose 全棧(project `anila-platform`) |
| 模型 gateway | `.12` = 10.53.100.12 / `aiagent2.ai.ncsist.org.tw` | My-OpenAI-Frontend;`/v1` 出 gpt-oss-20b / gemma4 / nv-embed,需 Bearer `MODEL_GATEWAY_API_KEY` |
| MLSteam | `aiops.ai.ncsist.org.tw` | anila-agent 跑在這的 Lab(純 http NodePort 對外) |
| 本開發機 | `/home/aia/c1147259/ANILA` | 寫碼處;另跑本機 `anila-platform` stack(prod-public-passwd,放寬旗標)+ 本機 anila-models |

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

- **別用 `START-HERE.sh` / `intranet-deploy.sh` 做小修改** —— 每跑一次就無條件重設 `ANILA_ALLOW_HTTP_ENDPOINT=0` / `ANILA_ALLOW_PRIVATE_ENDPOINT=0` / `ANILA_MODEL_CA_FILE`,蓋掉手動修正。**改設定 = 編 `.env` + `docker compose -p anila-platform up -d csp`**。
- **`docker restart` ≠ recreate**(不重載 `.env`/compose);套設定一律 `up -d`。(`AGENTS.md` §4 同調。)
- prod 模式缺 JWT keypair → JWKS 500、登入炸、studio crash-loop;deploy 腳本會在 up 前產 RSA-2048 到 `./secrets`(compose 掛 `:ro`)。
- bundle `intranet-prod-v1.0.0` 的 `00-anila-src.tar.gz` 是源碼快照;改了 git 腳本要重打包才帶進去。

---

## 5. 當前部署狀態(會變,動前先核對)

- **外網 `anila-platform`(prod-public-passwd)** 已 rebuild 含 system-prompt 產生器 + guide + JWT 修;之後又上 SSE robust 串流。
- **內網 prod-intranet-card 部署收尾中**:`.12` model-ca 用 CSPKI bundle 驗 `return code 0` → 填 API key → `up -d csp`;agent endpoint `http://aiops:<port>` + `ALLOW_HTTP=1`;DNS 暫用 IP 過渡。
- 待辦:`.12` gateway API key 簽發(user 端)。

---

## 6. 鐵則(快速;詳細安全/測試見 `AGENTS.md` §6–7、§9–10)

- 繁中台灣用語、無簡體。
- **祕密零外洩**(PUBLIC repo):`.env`/`*.pem`/`*.key`/`secrets/`/憑證私鑰 已 gitignore,別加回追蹤。
- **不動 running `anila-platform-*` 容器**(user dev 環境),除非授權。
- **端到端驗證、用對方法**:別只看 status code(SPA catch-all 對未匹配路由回 200 text/html → 驗 Content-Type);取資料走正式 HTTP API + auth,不直連 DB。
- **別腦補成 bug**:功能按 spec ≠ bug;先客觀呈現,讓 user 判斷。
- 前端驗證用 `npm run build`(非只 `tsc`);後端 `services/csp/.venv/bin/python -m pytest`。
- SSRF guard、卡登驗章、JWT 信任錨不可弱化;改 schema 必加 alembic migration;runtime DB 用 `csp_app` role(非 superuser,否則繞過 RLS)。
- commit/push 只在 user 要求時;跨分支同步遵 `AGENTS.md` §3(main 起、downstream 不互 merge、card fork 區不可被 main 覆蓋)。
