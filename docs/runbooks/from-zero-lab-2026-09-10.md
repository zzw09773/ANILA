# 從零部署實錄 — Lab 2026-09-10（內網可照抄）

> 這份是 **2026-09-10 在 Lab（172.16.120.153）對 `main` `9434e7a6` 實際跑過的從零重部**。
> 權威程序仍是 [`intranet-deployment-runbook.md`](./intranet-deployment-runbook.md) 與 [`first-install-rehearsal.md`](./first-install-rehearsal.md)。
> 這裡只記：**這次按了哪些鍵、每個服務怎麼驗、內網時哪幾行必須換掉**。
>
> 祕密不進 git。Lab 新密碼在 host 本機 `~/anila-deliverables/from-zero-lab-20260910-200355/generated-secrets.txt`（mode 600）。

## 0. 這次要證明什麼

不是「容器還在跑」，而是「清掉 volume／JWT／舊 secret 之後，照文件能再裝起來，而且每個入口都有真實 HTTP 證據」。

出貨 SHA：**`9434e7a6`**（已在 `origin/main`）。
Compose 專案名：**`anila`**（不要再用 `anila-restart`）。

## 1. Lab 與 `.15` 不可混用的值

| 項 | 本次 Lab | 內網 `.15` |
|---|---|---|
| `COMPOSE_PROJECT_NAME` | `anila` | `anila` |
| `ANILA_HOST` | `anila.ai.ncsist.org.tw`（憑證 SAN 可能對不到 Lab IP） | `anila.ai.ncsist.org.tw` |
| `ANILA_AUTH_MODE` | `password`（本機無真卡） | `card-only` 或 runbook 規定值 |
| `CARD_*` / `secrets/dev-card-ca/` | Lab mock，可留 | **禁止帶入**；卡登用映像內 CSPKI |
| `ANILA_ALLOW_PRIVATE_ENDPOINT` / `GRPC` / `HTTP` | Lab 要連 `172.16.120.35` Triton、`172.16.120.38` GLM/Qwen，開著 | 內網依 gateway／SSRF 正式姿態 |
| `LOCAL_EMBEDDING_BASE_URL` | `grpc://172.16.120.35:9001`（**不是 9000**） | 內網 embedding 實際位址 |
| `MODEL_GATEWAY_API_KEY` | 空（改註冊 LAN 模型） | `.12` 新簽 |
| 七把 secret | **本次重生** | **到場再重生，不要帶 Lab 這批** |
| JWT keypair | **本次重生** | **到場再重生** |

## 2. 從零清除（本次已執行）

備份（先做）：

```bash
# 實際備份目錄（本次）
# ~/anila-deliverables/from-zero-lab-20260910-200355/
# 內含 .env.pre-wipe、secrets/jwt-*.pem、pki/model-ca.pem、generated-secrets.txt、HEAD
```

```bash
cd /path/to/ANILA   # Lab: 本 repo 根；內網: 解開的出貨樹根
set -a && source .env && set +a
# 專案名以 .env 為準
: "${COMPOSE_PROJECT_NAME:=anila}"

# 1. 停棧並刪 named volume（DB／Redis／router session 歸零）
docker compose -p "$COMPOSE_PROJECT_NAME" down -v

# 2. bind mount 交回主機帳號（down -v 不動 share/）
docker run --rm -v "$PWD/share:/s" alpine:3.20 sh -c "chown -R $(id -u):$(id -g) /s"

# 3. A 類產物（存在就會讓腳本印綠字跳過）
rm -f secrets/jwt-private.pem secrets/jwt-public.pem
rm -f share/pki/model-ca.pem

# 4. 使用者資料（本次 Lab 一併清；內網若是全新機本來就是空的）
rm -rf share/uploads/ingestion/* share/attachments/* share/uploads/flux/* || true
```

**不刪**：源碼、`.git/`、`docs/`、`infra/`、`secrets/dev-card-ca/`（僅 Lab）、`node_modules`。

## 3. 重建 secret／JWT／model-CA（本次已執行）

Lab 這次是保留 `.env` 的**非秘密** LAN 設定，只重生密碼／token。內網請用 `intranet-deploy.sh` 的 `[3/7]`（從零答 **n** 保留現有 secret？）。

JWT（必須在 `up` 之前；`--user 0:0` 必要）：

```bash
mkdir -p secrets share/pki
cp services/csp/app/services/cspki_ca_bundle.pem share/pki/model-ca.pem
docker run --rm --user 0:0 -v "$PWD/secrets:/out" --entrypoint python \
  "${COMPOSE_PROJECT_NAME:-anila}-csp:latest" \
  /app/scripts/generate-jwt-keypair.py --output-dir /out --force
bash infra/deployment/scripts/fix-runtime-ownership.sh "${COMPOSE_PROJECT_NAME:-anila}-csp:latest"
```

驗收（不要 `env` 整包）：

```bash
test -s secrets/jwt-private.pem && test -s secrets/jwt-public.pem
test -s share/pki/model-ca.pem
# 容器起來後再查這一顆：
# docker exec anila-csp-1 printenv ANILA_MODEL_CA_FILE
# 期望：/etc/anila/pki/model-ca.pem（空字串＝無聲降級，內網會炸）
```

## 4. 起棧

Lab（有原始碼、要建這棵 SHA）：

```bash
docker network inspect anila-models-net >/dev/null 2>&1 || docker network create anila-models-net
docker compose -p anila up -d --build
```

內網（已 load 出貨映像、不要現場 build）：

```bash
bash infra/deployment/intranet/intranet-deploy.sh /path/to/image-bundle
# 或 load 完後：
docker compose -p anila -f compose.yaml -f /path/to/bundle/intranet-image-overrides.yml up -d --no-build
```

預設**不要** `COMPOSE_PROFILES=ops`（GitLab／n8n 不起）。code-server 預設開。

## 5. 服務驗證清單

每個入口都要看 **HTTP 狀態與 Content-Type**，不要只看 `docker compose ps` 綠燈。

（實測結果寫在文末〈本次實測〉。）

## 6. 從零之後一定要人工做的兩件事

1. owner 登入 → `/departments` 建單位（零單位＝同事卡在註冊）。
2. `/models` 指定一顆 LLM 為**主路由**（沒做，第一句對話就失敗）。

Lab 另外要重新註冊 LAN GLM／Qwen（`http://172.16.120.38:4000`）與 Triton embedding `grpc://172.16.120.35:9001`。

## 7. 本次實測（Lab 2026-09-10 20:06–20:19）

出貨樹：`9434e7a6`。專案：`anila`。`docker compose -p anila down -v` 後重建 volume，重生 JWT／secret，`up -d --build`。

| 檢查 | 結果 |
|---|---|
| `docker compose ps` | 11 個服務：csp/db/redis/nginx/router/studio/ui/anilalm/codeserver/pptx **healthy**；ingestion-worker Up（無 healthcheck） |
| 內部 `csp /health` | 200 JSON `status=healthy` |
| 內部 `anila-studio /health` | 200 JSON `ready=true` |
| 內部 `router /health` | 200；主路由來源 `csp_registry` / `glm-5.3-flash` |
| `https://127.0.0.1/health` | 200 `application/json` |
| `https://127.0.0.1/.well-known/jwks.json` | 200 RSA JWKS（新 keypair） |
| `https://127.0.0.1/login`、`/` | 200 HTML 治理中心 |
| `https://127.0.0.1:4443/` | 200 HTML 對話 UI |
| `https://127.0.0.1/codeserver/` | 200 |
| `https://127.0.0.1/n8n/`、`/gitlab/` | **502**（預設不起 ops profile，正確） |
| `https://127.0.0.1/anilalm/` | **503 尚未開放**（發行閘，正確） |
| `SSL_CERT_FILE`（csp 容器） | `/etc/anila/pki/model-ca.pem`（compose 把 `.env` 的 `ANILA_MODEL_CA_FILE` 映射成這顆；容器內**沒有**同名 `ANILA_MODEL_CA_FILE` 變數） |
| `POST /api/auth/login` admin | 200，role=`owner`，三顆 cookie |
| 建單位 資訊處／研發處／示範單位 | 200 |
| 指定 `nv-embed-v2` `protocol=triton_grpc` + 平台 embedding | 200；搜尋命中 canary，score ≈ 0.54 |
| 註冊 `glm-5.3-flash`、`qwen38-flash-next`（`http://172.16.120.38:4000/v1`） | 200 |
| 指定 GLM 為主路由 | 200 |
| `POST /v1/chat/completions` model=`qwen38-flash-next` | 200，內容含「通」 |
| `POST /v1/chat/completions` model=`anila-router` | 見下節 |

### 從零必做、文件原本沒寫清楚的一刀

預設對話目標是 **`anila-router`**。CSP 把請求轉給 router 時，會用「該模型列的 api_key，否則 `MODEL_GATEWAY_API_KEY`」當 `Authorization`。

Lab／內網若把 `MODEL_GATEWAY_API_KEY` 設成 **LiteLLM／.12 的 sk-**（打模型 gateway 用），這把 key **不是 CSP 的 API key**。router 拿它回頭打 `http://csp:8000/v1/agents` 與上游 LLM，CSP 回 **401「無效或已過期的 API Key」**，畫面上是「LLM 暫時無法回應」。

**處置（本次已做）：** owner 在治理中心建一把 CSP API key（授權含主路由 LLM），填進 **`anila-router` 那一列的模型 api_key**。不要讓平台入口列去繼承模型 gateway 的 key。之後 `anila-router` 回「通」，trace：`呼叫 glm-5.3-flash` status ok。

內網若只走 `.12` gateway、而且那把 key 也註冊成 CSP 的 sk-，才可以只靠全域 `MODEL_GATEWAY_API_KEY`。Lab 這種「CSP sk- 與 LiteLLM sk- 不是同一把」的拓撲，一定要分開。

### 密碼在哪

不進 git。Lab：`~/anila-deliverables/from-zero-lab-20260910-200355/generated-secrets.txt`。內網：`intranet-deploy.sh` 現場生成後立刻進密碼管理器。

### 內網時把這份當檢查表，只換這幾行

1. 專案名維持 `anila`（不要 `anila-restart`）。
2. 不要帶 `secrets/dev-card-ca/`、`CARD_DEV_TRUST_TEST_CA`、Lab 的 `CARD_CA_BUNDLE_PATH`。
3. `ANILA_AUTH_MODE=card-only`（或 runbook 規定值）。
4. embedding／LLM 改內網位址；`MODEL_GATEWAY_API_KEY` 用 `.12` 新簽的。
5. 仍要：JWT → 所有權腳本 → 建單位 → 指定主路由 →（若 gateway key ≠ CSP sk-）給 `anila-router` 填 CSP sk-。
6. 驗 `SSL_CERT_FILE=/etc/anila/pki/model-ca.pem`，不要只看腳本綠字。
