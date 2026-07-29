# CLAUDE.md — ANILA 重啟樹(給 Claude Code)

> **這是重啟樹**:2026-07-28 平台擁有者決定回到 redesign 收斂點 `a4118a3`(2026-07-03)重新出發。
> 權威文件就在本樹根目錄:**規格＝`SYSTEM-MAP.md`**(28 題 QA)、**順序＝`PLAN.md`**(到 8 月底上線)、
> **歷史＝`RESTART-FROM-REDESIGN.md`**(373 commit 履歷＋attic 取回方式)。
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

## 4. 當前快照(2026-07-29,會變,動前核對)

- P0.1 **已消解**:PLAN 列的四個啟動閘是 07-13 後才加的,本基底不存在。
- P0.2 **進行中**:worktree `../wt-p02`(分支 `wt/p02-http-model-guard`)已改 `url_guard.py`(production model 端點尊重 `ANILA_ALLOW_HTTP_ENDPOINT`);**測試檔更新被權限 classifier 擋下待套**(提案在 `../p02-test-update-proposal.md`),之後交 sol＋kimi 跨家審。
- 根目錄三份權威文件是從舊 main staged 過來,**尚未 commit**。SYSTEM-MAP §13 說的「現行程式碼」指舊樹;本基底以實際 grep 為準(P0.1 即因此消解)。
- P0.3/0.4 待做:`.env`＋secrets → 正式 compose 全量起(**本基底 13 服務**;PLAN 0.3 寫的「15 個」照新樹數的,以 13 為準)→ alembic 從零(head `r1_0008`)。⚠ codeserver/n8n/gitlab 在本基底**無 profiles** 會預設啟動;SYSTEM-MAP 沒提這三個,去留要提請擁有者裁決。

## 5. 鐵則

- **祕密零外洩**(PUBLIC repo):`.env`/`*.pem`/`*.key`/`secrets/` 不進追蹤;容器掛載不看 gitignore,新增 RW 掛載要想清楚。
- **端到端驗證用對方法**:別只看 status code(SPA catch-all 回 200 text/html → 驗 Content-Type);取資料走正式 API＋auth,不直連 DB。
- **別腦補成 bug**:功能按 spec ≠ bug;先客觀呈現,讓 user 判斷。
- 前端驗 `npm run build`(非只 tsc);後端 pytest(本樹尚無 venv,暫借舊樹 `services/csp/.venv` 的直譯器＋`PYTHONPATH=packages/*/src`,建好自己的 venv 後改用)。
- SSRF guard、卡登驗章、JWT 信任錨不可弱化(P0.2 的 http 旗標分域是擁有者拍板的例外,紀錄在 PLAN.md);改 schema 必加 alembic migration;runtime DB 用 `csp_app` role(非 superuser,否則繞過 RLS)。
- commit/push 只在 user 要求時。
