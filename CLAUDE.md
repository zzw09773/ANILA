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

## 4. 當前快照(2026-07-31 凌晨,會變,動前核對)

**細節看 `PLAN.md` 的〈現在在哪裡〉**——這裡只留每次 session 開頭必須知道的。

- alembic head = **`r1_0024`**。本機 `-p anila-restart` 12 容器全綠,三個入口(`/`、`/anila/`、`/anilalm/`)都通。
- **7/30–7/31 一夜合併部署 46 包**:P4 全關、P3 除 SMTP 外全關、OE-1～OE-4/OW/G9/D1 全關、
  時區、連線池、串流錯誤可見、假綠燈、配色 AA、備份含還原演練、壓力測試、ASR/FLUX/心智圖撿回。
- ⚠ **`.15` 尚未部署過任何一項**。本機是唯一驗證環境。
- **待裁決集中在 `docs/OWNER-QUESTIONS.md`**(11 題,7 題已答),**不要重問**。
  規則:遇到需要裁決的事**不停下來等**,記進去、用最保守假設繼續、註明假設。
- **`wt/core-opt` 與 `wt/leak` 刻意未合併**——擁有者要親自體驗 anila-core 後才決定。
- 其他文件:`docs/FAKE-CONTROLS.md`(26 項假控制項)、`docs/UX-IDEAS.md`、`docs/designs/`。

### 這台機器的操作陷阱(每一條都今晚踩過)

- **`docker compose up -d` 之後要 reload nginx**(已寫進 `deploy-prod.sh`)。upstream 區塊 DNS
  只在載入設定時解析一次,recreate 任何服務都會讓 nginx 打舊 IP → **全站 502 但容器全綠**。
- **治理中心在 csp 映像裡**(`infra/docker/csp.Dockerfile` 多階段 build)。改治理中心要 **rebuild csp**,
  不是 anila-ui。⚠ 查部署路徑要**從跑著的容器往回追**,不要從 repo 裡看起來對的檔案往前推。
- **`npm run build` 過 ≠ 映像建得起來**。本機借用的 node_modules 有 devDeps,映像只裝 production。
  前端要合併前用 `docker compose build <service>` 驗;平行建置的錯誤訊息不會說是哪個服務。
- **平行派多包前先分配 migration 編號**,並把檔案集真的列出來對。一晚撞三次(兩次編號、一次同檔)。
- **驗 API 要看 Content-Type**。SPA catch-all 會回 `200 text/html`,看起來像端點沒有保護。

### 這個專案最貴的四條教訓

1. **不要把系統越搞越嚴。** 上一輪 25 天工作被整包放棄,直接原因就是安全限制越加越複雜。
   擁有者原話:「過於嚴格我也不會想維護,開發者不會想掛 agent,使用者也不想用。」
   加限制前先問:擋掉的是誰／被擋的人怎麼自救／**它會不會在未來擋住我們自己**。
2. **驗證要驗行為。** 只有眼睛能判斷的就渲染出來看(浮水印深色主題的結論與審查猜測相反);
   「有呼叫 close()」不算證明,要量到連線真的回到池子;
   **把 production 改動還原回去還會過的測試,等於不存在**(今晚抓到三條)。
3. **審查者給的修法方向是假設不是解答。** 派修訂輪傳**不變式與驗收情境**;
   同一缺陷兩輪關不掉就問「這兩個目標是不是互斥」。
4. **靜默成功比報錯危險。** 26 個「按了、沒報錯、什麼也沒發生」的控制項,
   其中兩個是使用者以為鎖住了存取但沒有。**任何讓使用者以為發生了什麼的控制項,
   都要確認後端真的收得到那個意圖。**

## 5. 鐵則

- **祕密零外洩**(PUBLIC repo):`.env`/`*.pem`/`*.key`/`secrets/` 不進追蹤;容器掛載不看 gitignore,新增 RW 掛載要想清楚。
- **端到端驗證用對方法**:別只看 status code(SPA catch-all 回 200 text/html → 驗 Content-Type);取資料走正式 API＋auth,不直連 DB。
- **別腦補成 bug**:功能按 spec ≠ bug;先客觀呈現,讓 user 判斷。
- 前端驗 `npm run build`(非只 tsc);後端 pytest(本樹尚無 venv,暫借舊樹 `services/csp/.venv` 的直譯器＋`PYTHONPATH=packages/*/src`,建好自己的 venv 後改用)。
- SSRF guard、卡登驗章、JWT 信任錨不可弱化(P0.2 的 http 旗標分域是擁有者拍板的例外,紀錄在 PLAN.md);改 schema 必加 alembic migration;runtime DB 用 `csp_app` role(非 superuser,否則繞過 RLS)。
- commit/push 只在 user 要求時。
