# Handoff — Router auto-route demo + proxy 相容修正 (2026-07-20)

> 承接 `2026-07-19-gate6-continuation.md`。本次聚焦:① 把 UI 優化 (PR #23) 與兩個 proxy bug 修 (PR #36) 落進 main;② 起一套完整 dev demo 讓平台擁有者操作(卡登 / admin / 你的外部模型);③ 把「ANILA Router auto 聊天」端到端打到設計天花板(DIRECT_ANSWER 待 R7)。

---

## 0. TL;DR 狀態

| 項目 | 狀態 |
|---|---|
| PR #23 — 你的 UI 優化 + `/anila` 白畫面 (`BASE_PATH=/anila/`) | ✅ **merged 進 main** (`b647250`,Gate 1/5/6 綠) |
| PR #36 — 兩個 CSP proxy bug 修 | ✅ **merged 進 main** (`1c8568d`,24/24 CI 綠) |
| 本地 `main` | 已與 `origin/main` 同步 (`1c8568d`),working tree 乾淨 |
| 全棧 dev demo | ✅ **跑著**(12 容器,`https://localhost:8443`) |
| UI auto-route 聊天 | ⚠️ route decision 成功,但 **DIRECT_ANSWER 撞待-R7 硬閘**(設計,非 bug) |
| PR #34 / #35 (downstream sync) | ⏳ 開著,**待你授權 merge** |

---

## 1. 進 main 的程式碼(永久)

### PR #23 `feat(design-system)` → `b647250`
你之前的 UI 優化分支 `claude/design-system-foundation-c4fa21`。原本 base 是死巷 `fix/gate0-security-hardening`(已被 main 吸收),retarget 到 `main` 後乾淨 merge(57 檔,僅 3 檔與 main 重疊、無衝突)。**同時修好 `/anila` 白畫面**:dev.yml 沒傳 `BASE_PATH=/anila/`,anila-ui 建成 base `/` → 資產撞 CSP `/assets` → 404。

### PR #36 `fix(csp/proxy)` → `1c8568d`
兩個獨立的 proxy 相容 bug,是在把 Router formal path 對「嚴格 `extra_forbidden` 外部模型」打通時發現的:
1. **內部 router seam 跳過記憶注入** — `/internal/v1/router/chat/completions` 是路由決策、非對話輪次,fail-closed 記憶策略會 503 每次決策。
2. **轉發真模型前剝除 `anila_session_id`/`session_id`** — doc 04 §3 本就規定 regular gateway 不該看到這些欄位;嚴格 vLLM/SGLang 會 400。內部 `anila-router` 保留(其 body-bound 情境權杖需要)。
CSP proxy suite 63 passed;PR CI 24/24。

---

## 2. Demo 環境(**容器留著跑**)

### 2.1 入口 / 憑證
- 入口:`https://localhost:8443`(用 **Chrome**,本地 CA 已裝進 `~/.pki/nssdb`,不跳警告)。
- **admin 密碼登入**:`admin` / `AI12345678`(role owner)。「其他登入方式 → 帳號密碼登入」。
- **卡登(合成卡)**:點「偵測憑證卡」,PIN `654321`;合成卡 emulator = `cht-api-1`(`127.0.0.1:16888`)。新卡會落「待管理員核准」(治理閘,正常)。
- **你的外部模型**:`gpt-oss-20b` @ `http://172.16.120.35:7000`,直打聊天(target 選 gpt-oss-20b)可回。

### 2.2 容器(專案 `anila-platform-dev`,獨立 `-dev` volumes,**不碰你原本的 anila-platform**)
`csp / csp-db / redis / ingestion-worker / router / anila-agent / pptx-renderer / anila-studio / anilalm / anila-ui / nginx` + `cht-api-1`(合成卡)。全 healthy。

### 2.3 起法(重開機或重建時)
所有 demo 材料在 repo **外**:`~/.cache/anila-demo/`(TLS、CA、governance stub、secrets、`router.env`、`demo-override-full.yml`、`nginx-demo.conf`)。
```bash
cd <repo>
set -a; . ~/.cache/anila-demo/router.env; set +a          # ANILA_ROUTER_SVC_TOKEN
export ANILA_DEV_TLS_CERTS_DIR=~/.cache/anila-demo/tls
export EMBEDDING_MODEL_FINGERPRINT_DEV=sha256:$(printf '0%.0s' {1..64})
export LOCAL_LLM_BASE_URL=http://172.16.120.35:7000        # ← 見 §4 footgun
OV=~/.cache/anila-demo/demo-override-full.yml
docker compose -f compose.dev.yaml -f "$OV" up -d --build
```
> 卡登也要 cht:`docker compose -f cht/docker-compose.yaml up -d --build`。

### 2.4 收掉(要時)
```bash
docker compose -f compose.dev.yaml -f ~/.cache/anila-demo/demo-override-full.yml down
docker compose -f cht/docker-compose.yaml down
# 需要連 volume:加 -v
```

---

## 3. auto-route 聊天:打通到設計天花板

「ANILA Router auto」送的 model 是虛擬 sentinel `anila-router`(非真模型)。要它端到端跑通,做了 10+ 層(每層都是真卡點):路由改走 CSP `/v1/` → 註冊 `anila-router` 模型(endpoint `router:9000`)→ `router` 加信任主機 → 過 R6 provenance → 配 per-model Bearer key → 注入 Router named service-token(`router.env`)→ 內部 seam 跳記憶(PR #36)→ 修回模型 endpoint → 剝 body 擴充欄位(PR #36)。**→ route decision 成功、gpt-oss-20b 回 200。**

**天花板(改不動、非 bug):** `packages/anila-core/.../router/policy_gate.py` 對 `RouteType.DIRECT_ANSWER` 無條件 `allowed=False`(reason `DIRECT_MODEL_POLICY_NOT_EVALUATED`),commit `fa517f9`(2026-07-15 R3 RouterRuntime)。即「Router 自主直答」的治理(R7 model-governance)還沒接線,fail-closed 擋掉。**本分支 Router 只能派工給健康 agent,不能直接 LLM 文字回答。** 要文字回答就直打指定模型(走 `enforce_model_ceiling`,可用)。

> 詳細操作步驟見 auto-memory `router-autoroute-chain-and-r7-fence`。

---

## 4. Footgun / 注意

- **`AUTO_REGISTER_MODELS` 的 `${LOCAL_LLM_BASE_URL:-http://gpt-oss-20b:8000}` 是 compose 解析時內插**(shell env),不是 csp 容器 environment。每次重建 csp,auto_seed 會把手動改過的 gpt-oss-20b endpoint **洗回 `gpt-oss-20b:8000`**(打不到)。→ 起 stack 時 shell `export LOCAL_LLM_BASE_URL=http://172.16.120.35:7000`,或重建後 API `PUT /api/models/1 {endpoint_url}` 補回。
- demo override 的 csp `LOCAL_LLM_BASE_URL` 放在 environment **無效**(同上原因),留著只是文件用途。
- Router `MODEL=gpt-oss-20b` 由 override 設(dev 預設 gemma4 常沒起)。
- demo 用 governance stub(`GATE5_MODEL_GOVERNANCE_ENABLED=False` 預設關)+ 自簽本地 CA,**非正式治理材料**。

---

## 5. 待辦(交回你決定)

1. **PR #34 / #35**(dev-public、prod-intranet-card downstream sync)開著,待你審後授權 merge(production gate)。注意 `prod-intranet-card` 落後 `origin/main` 已達百餘 commit(見 §sol 於 07-19 handoff)。
2. **R7 model-governance for direct answer** — 讓 Router 直答能安全開(或復用現有的 `enforce_model_ceiling`)。這是正式功能任務,非 demo 修。
3. demo 若要給別人看:`admin`/`AI12345678` 是弱密碼、僅 demo。

---

## 6. 關鍵檔案指標

- 修改進 main:`services/csp/app/api/proxy.py`、`services/csp/app/services/proxy/service.py`(PR #36)。
- 白畫面 / UI:`apps/anila-shell/*`、`infra/compose/dev.yml`(PR #23)。
- 天花板:`packages/anila-core/src/anila_core/router/policy_gate.py:101`。
- demo(repo 外):`~/.cache/anila-demo/`。
