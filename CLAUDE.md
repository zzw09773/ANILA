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

## 4. 當前快照(2026-07-30 深夜,會變,動前核對)

**細節一律看 `PLAN.md`**——它已重整成「計畫在前、已關板紀錄在後」,這裡只留每次 session 開頭必須知道的。

- **P0 全關;P1 除 1.6(需進內網)全關;OE-1～OE-4、OW-1、OW-3、G9 全關。**
- **2026-07-30 一天內合併部署十五項**,其中六項是實地查出的既有缺陷,不是排程功能:
  URL 拼接(串流聊天 100% 404)、連線池耗盡、串流失敗對使用者沉默、agent 密等可被競態降低、
  API key 有到期日就 500、nginx 釘住舊 IP 導致全站 502 但容器 healthy。
- alembic head = **`r1_0018`**。本機 `-p anila-restart` 全綠。
- ⚠ **`docker compose up -d` 之後要 reload nginx**(已寫進 `deploy-prod.sh`)。
  upstream 區塊的 DNS 只在載入設定時解析一次,recreate 任何服務都會讓 nginx 打舊 IP →
  **全站 502 但所有容器 healthy**,健康檢查完全看不出來。
- ⚠ **`.15` 尚未部署過今天的成果**;本機是唯一驗證環境。
- **待擁有者裁決的事集中在 `docs/OWNER-QUESTIONS.md`**,不要重問。
  規則:遇到需要裁決的事**不停下來等**,記進去、用最保守假設繼續、註明假設。
- **UX 觀察累積在 `docs/UX-IDEAS.md`**(只記「不必要的難」,不是許願單)。
- **`wt/core-opt` 刻意不 merge**——擁有者要親自體驗過才決定。

### 這個專案最貴的三條教訓(2026-07-30 用真實輪次換來的)

1. **不要把系統越搞越嚴。** 上一輪 25 天工作被整包放棄,直接原因就是安全限制越加越複雜。
   擁有者原話:「過於嚴格我也不會想維護,開發者不會想掛 agent,使用者也不想用。」
   加限制前先問:擋掉的是誰／被擋的人怎麼自救／**它會不會在未來擋住我們自己**。
   同一屬性冒出第三個外洩出口時,該問的是「這保護值不值得」而不是「怎麼封第三個」。
2. **驗證要驗行為,不要驗程式碼讀起來對不對。** 今天三個實例:只有眼睛能判斷的就渲染出來看
   (浮水印深色主題的問題與審查猜測相反);「有呼叫 close()」不算證明,要量到連線真的回到池子;
   **把 production 改動還原回去還會過的測試,等於不存在**(今天抓到三條)。
3. **審查者給的修法方向是假設不是解答。** 派修訂輪傳**不變式與驗收情境**,
   不要傳「照審查者說的改」;同一缺陷兩輪關不掉就問「這兩個目標是不是互斥」。

## 5. 鐵則

- **祕密零外洩**(PUBLIC repo):`.env`/`*.pem`/`*.key`/`secrets/` 不進追蹤;容器掛載不看 gitignore,新增 RW 掛載要想清楚。
- **端到端驗證用對方法**:別只看 status code(SPA catch-all 回 200 text/html → 驗 Content-Type);取資料走正式 API＋auth,不直連 DB。
- **別腦補成 bug**:功能按 spec ≠ bug;先客觀呈現,讓 user 判斷。
- 前端驗 `npm run build`(非只 tsc);後端 pytest(本樹尚無 venv,暫借舊樹 `services/csp/.venv` 的直譯器＋`PYTHONPATH=packages/*/src`,建好自己的 venv 後改用)。
- SSRF guard、卡登驗章、JWT 信任錨不可弱化(P0.2 的 http 旗標分域是擁有者拍板的例外,紀錄在 PLAN.md);改 schema 必加 alembic migration;runtime DB 用 `csp_app` role(非 superuser,否則繞過 RLS)。
- commit/push 只在 user 要求時。
