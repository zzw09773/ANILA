# ANILA 從 0 到上線部署指南(內網 prod-intranet-card)

> **定位**:一台「什麼都沒有」的內網主機 → 全站可用 → 進入日常維運,走的**乾淨主路徑**。
> 每一步的深入細節、歷史決策與踩坑對照表在
> [`intranet-deployment-runbook.md`](intranet-deployment-runbook.md)(以下簡稱 runbook),
> 本文只給「照著做就能上線」的順序與指令,卡住才跳 runbook。
> **更新**:2026-07-03。適用分支:`prod-intranet-card`(V1.0.0+)。

---

## 0. 工具分工(先認識三支腳本)

| 檔案 | 角色 | 什麼時候用 |
|---|---|---|
| [`infra/deployment/intranet/intranet-deploy.sh`](../../infra/deployment/intranet/intranet-deploy.sh) | **一鍵部署**(一條龍) | 首次部署、版本升級後重新落地。互動式跑完 TLS → CA → .env → load image → JWT 金鑰 → up → 驗證 |
| [`infra/deployment/scripts/deploy-prod.sh`](../../infra/deployment/scripts/deploy-prod.sh) | **起停/重建** | `preflight` / `up` / `down` / `restart` / `rebuild <svc>` / `status` / `logs` / `verify` |
| [`infra/deployment/scripts/anila-ops.sh`](../../infra/deployment/scripts/anila-ops.sh) | **日常維運** | 深度健檢、備份/還原、TLS 換發、gateway key 輪替、break-glass、清磁碟 |

**三條鐵則(全程適用)**:

1. **改設定 = 編 `.env` + `docker compose up -d <svc>`**。不要重跑一鍵腳本做小修改
   (它每次都會把 strict 旗標重設回預設);也不要用 `docker restart`(不會重載 `.env`)。
2. **祕密零外洩**:`.env`、`secrets/`、`*.pem`、`*.key`、`*.pfx`、`backups/` 都已被
   `.gitignore` 擋住,不要加回追蹤。
3. **驗證看真實路徑**:SPA catch-all 對不存在的路由也回 200,別只看 status code;
   用 `anila-ops.sh health` 或 runbook §3 的正式驗收清單。

---

## 1. 架構與需求一頁懂

```
員工 PC                        平台主機 (.15)                     模型主機 (.12)
[實體卡+讀卡機]                anila.ai.ncsist.org.tw             aiagent2.ai.ncsist.org.tw
[HiPKI localhost:16888]        ─────────────────────              ─────────────────────
[瀏覽器] ──HTTPS──────────→    nginx :443 / :4443                 My-OpenAI-Frontend
                                 ├─ csp (control/data plane) ──https+Bearer──→ /v1
                                 ├─ router / anila-studio /        (gpt-oss-20b,
                                 │  anilalm / anila-ui /            nv-embed-v2)
                                 │  pptx-renderer
                                 └─ postgres(pgvector) + redis
                                    + ingestion-worker
                               (選配) MLSteam agent → http://aiops.ai.ncsist.org.tw:<port>
```

**平台主機 (.15) 需求**:

| 項目 | 需求 |
|---|---|
| OS | Linux x86_64(Ubuntu 22.04+ 或同級),可跑 Docker |
| 軟體 | Docker Engine 24+、docker compose v2、openssl、git(bash/curl 為基本工具) |
| 硬體 | 純 gateway 模式(第一版):8 core / 32GB RAM / 500GB 磁碟起跳;本機跑模型才需要 GPU(見 runbook §4.5) |
| 網路 | 對員工網段開 443/4443;`.15 → .12` 的 443 互通;不需要對外網際網路 |
| Port | 對外只有 nginx 的 80 / 443 / 4443;DB 只綁 `127.0.0.1:5433` |

---

## 2. 部署總流程

```
Phase A 外網打包 ──→ Phase B 帶進內網 ──→ Phase C 一鍵部署 ──→ Phase D 驗收 ──→ Phase E 首次營運 ──→ 日常維運
(dev 機 build+export)  (repo+image包+pfx+secret)  (intranet-deploy.sh)   (health+e2e)   (departments 等)   (anila-ops.sh)
```

---

## 3. Phase A:外網準備(在有網路的 dev 機跑)

### 3.1 前置確認(給 IT / gateway 管理側的清單,提早發包)

| # | 項目 | 誰處理 |
|---|---|---|
| 1 | DNS A record:`anila.ai.ncsist.org.tw → 10.53.100.15` | IT |
| 2 | 防火牆:`.15` 的 443 對員工網段開;`.15 → .12` 443 互通 | IT |
| 3 | 模型 gateway API key:在 `.12`(My-OpenAI-Frontend)簽發 ANILA 專用一把 | gateway 管理者(簽發指令見 runbook §2.2b) |
| 4 | HiPKI 元件預載員工 PC(`localhost:16888` 可回應) | IT |
| 5 | `*.ai.ncsist.org.tw` wildcard 憑證 `server.pfx` 在手上 | 你(已持有,2029 到期) |

### 3.2 Build + 打包 image

```bash
git checkout prod-intranet-card && git pull
# 基本款(第一版拓撲:模型全走 .12 gateway,平台主機不跑模型):
bash infra/deployment/intranet/build-and-export-for-intranet.sh
# (要本機跑模型才加 WITH_MODELS=1 WITH_WEIGHTS=1,見 runbook §1.2)
```

產出在 `/tmp/anila-images-export/`:`01-anila-built.tar.gz`(7 個自建 image)、
`02-base.tar.gz`(pgvector/redis/nginx)、`03-cold.tar.gz`(codeserver/n8n/gitlab)、
`INTRANET-LOAD.sh`(內網端一鍵 import,含 sha256 驗檔)、`MANIFEST.txt`。

> 內網**沒有任何下載通道**:image、權重、工具鏈全部只能從這裡帶。
> 超大檔案走 Google Drive + `pack-chunks.sh` 切塊流程,見 runbook §1.2b。

---

## 4. Phase B:帶進內網的四樣東西

1. **整個 ANILA repo**(`prod-intranet-card` checkout;建議解壓到 `/opt/anila`)
2. **image 包**(`/tmp/anila-images-export/` 整個資料夾,或版本化的 `intranet-prod-vX.Y.Z/`)
3. **`server.pfx`**(wildcard 憑證+私鑰;空密碼,進場後妥善保管)
4. **7 組 secret**(密碼管理器;沒有預生也沒關係——一鍵腳本會自動 `openssl` 隨機生成並顯示一次)

---

## 5. Phase C:內網一鍵部署(在 .15 上跑)

```bash
cd /opt/anila   # repo 根目錄
bash infra/deployment/intranet/intranet-deploy.sh /path/to/image包資料夾
```

互動式跑完 7 步,每步做的事與你要準備的輸入:

| 步驟 | 做什麼 | 你要輸入什麼 |
|---|---|---|
| [0] 前置檢查 | docker/openssl/分支確認 | — |
| [1] TLS 憑證 | 從 `server.pfx` 抽 fullchain `server.crt` + `server.key` | pfx 路徑、pfx 密碼(空就 Enter) |
| [2] 模型 CA | 把 repo 內建 CSPKI bundle 複製為 `share/pki/model-ca.pem`(卡登與內網 https 同一套 CA,離線即有) | — |
| [3] 產 `.env` | 自動生成 7 組 secret、設 strict 旗標(`ANILA_ENV=production`、card-only、agent http 專用旗標) | **owner 員工編號**(CSV,含你自己)、`MODEL_GATEWAY_API_KEY`(還沒簽發可先 Enter 跳過) |
| [4] load image | 跑 image 包的 `INTRANET-LOAD.sh`(sha256 驗檔 + re-tag) | — |
| [4b] JWT 金鑰 | 用 csp image 產 `secrets/jwt-{private,public}.pem`(缺這把:登入發不了 token、studio crash-loop) | — |
| [5] network | 建 `anila-models-net` | — |
| [6] up | `docker compose up -d --no-build` | — |
| [7] 驗證 | 等 csp healthy + nginx 探測 | — |

**重跑安全**:偵測到既有 `.env` 時預設「保留現有 secret」,不會重生 DB 密碼炸掉既有資料庫。

**部署完成後若 gateway key 還沒到**:拿到後不要重跑一鍵腳本,改用:

```bash
bash infra/deployment/scripts/anila-ops.sh gateway-key   # 互動輸入 → recreate csp → 自動探測
```

---

## 6. Phase D:驗收(全部要綠才算部署完成)

```bash
# 1. startup_security 必須無輸出(有輸出 = secret/旗標有問題,對照 runbook §3.2)
docker compose logs csp 2>&1 | grep -E "startup_security|RuntimeError|Refusing"

# 2. 一鍵健檢:容器、端點、JWKS、模型鏈路、憑證效期、磁碟
bash infra/deployment/scripts/anila-ops.sh health

# 3. 模型 e2e(host 上直接打 gateway,確認 key + CA + DNS 三件事)
curl --cacert share/pki/model-ca.pem \
  -H "Authorization: Bearer $(grep ^MODEL_GATEWAY_API_KEY .env | cut -d= -f2-)" \
  https://aiagent2.ai.ncsist.org.tw/v1/models
```

**人工驗收(缺一不可)**:

1. **卡片登入**:瀏覽器開 `https://anila.ai.ncsist.org.tw/login`,無憑證警告,
   插卡 → 顯示姓名/員編 → PIN → 登入;owner 員編直接以 `role=owner` 進入。
2. **對話**:新會話用 `openai/gpt-oss-20b` 問一題,串流正常、token 用量有記錄。
3. **RAG**:上傳一份 PDF → ingestion 完成 → 引用查詢命中。
4. **同事流程**:另一人首刷 → pending → `/users` 核准 → 二刷進入。

失敗對照:模型不通 → runbook §6.2;卡登失敗 → §6.3;啟動失敗 → §6.1。

---

## 7. Phase E:首次營運設定(一次性,go-live 前做完)

1. **先建 departments**(`/departments`):同事首刷註冊的單位下拉靠它,沒建會全部卡在註冊。
2. **確認 `/models`**:只該有 `openai/gpt-oss-20b`(LLM)+ `nvidia/nv-embed-v2`(embedding),健康綠。
   出現 gemma4 / image-generator = `.env` 對應 BASE_URL 沒設空。
3. **記住 break-glass 程序**(讀卡機/HiPKI 故障時全員進不去的後路):
   ```bash
   bash infra/deployment/scripts/anila-ops.sh break-glass on    # 暫開帳密,用 admin + .env 的 ADMIN_PASSWORD 進去
   bash infra/deployment/scripts/anila-ops.sh break-glass off   # 修好後恢復 card-only
   ```
4. **排每日備份 cron**(root 或部署帳號):
   ```cron
   30 2 * * * cd /opt/anila && bash infra/deployment/scripts/anila-ops.sh backup >> /var/log/anila-backup.log 2>&1
   0  3 * * 0 cd /opt/anila && bash infra/deployment/scripts/anila-ops.sh backup --full >> /var/log/anila-backup.log 2>&1
   ```
   備份落在 `<repo>/backups/`(可用 `ANILA_BACKUP_DIR` 改到獨立磁碟,強烈建議),
   預設保留 14 份(`ANILA_BACKUP_KEEP`)。**備份含 `.env` 與 JWT 私鑰,目錄權限 700,別放共用碟。**

---

## 8. 日常維運速查

| 情境 | 指令 |
|---|---|
| 看整體狀態 | `anila-ops.sh status` |
| 深度健檢(排班/巡檢用) | `anila-ops.sh health`(有 FAIL 會 exit 1,可接告警) |
| 追某服務 log | `anila-ops.sh logs csp 200` |
| 手動備份 / 含上傳檔 | `anila-ops.sh backup` / `anila-ops.sh backup --full` |
| 還原資料庫 | `anila-ops.sh restore backups/<stamp>`(需輸入 `RESTORE`;`.env`/secrets 依提示手動還原) |
| wildcard 憑證換發(2029 前) | `anila-ops.sh cert-renew /path/new-server.pfx` |
| CSPKI CA 換代 | `anila-ops.sh model-ca /path/new-ca-chain.pem`(先驗鏈再上,壞檔不會被套用) |
| gateway key 輪替 | `anila-ops.sh gateway-key` |
| 讀卡環境故障應急 | `anila-ops.sh break-glass on` → 處理 → `... off` |
| 磁碟吃緊 | `anila-ops.sh prune`(只清 dangling image/builder cache,不碰 volume) |
| 停/起整個 stack | `deploy-prod.sh down` / `deploy-prod.sh up` |
| 加 owner | `/users` UI 改角色;或改 `.env` `CARD_INITIAL_OWNERS`(只對新刷卡者生效)+ `docker compose up -d csp` |

### 8.1 版本升級(新版程式落地)

內網無法 `git pull` + build,升級 = 重走打包流程:

1. 外網 dev 機:checkout 新版 `prod-intranet-card` → `build-and-export-for-intranet.sh` 重新打包
2. 帶進內網(新 image 包 + 對應版本的 repo 快照)
3. **先備份**:`anila-ops.sh backup`
4. `bash infra/deployment/intranet/intranet-deploy.sh <新image包>`
   (重跑安全:保留既有 `.env` secret;新 migration 由 csp 啟動時自動跑 alembic)
5. `anila-ops.sh health` + §6 人工驗收

### 8.2 什麼動作用什麼方式套用(再強調一次)

| 改了什麼 | 正確做法 |
|---|---|
| `.env` 任何值 | `docker compose up -d <受影響 svc>`(compose 偵測 env diff 自動 recreate) |
| `share/pki/model-ca.pem` 內容 | `docker compose up -d --force-recreate csp`(或用 `anila-ops.sh model-ca`) |
| `infra/nginx/certs/*` | `nginx -t` + `nginx -s reload`(或用 `anila-ops.sh cert-renew`) |
| compose / nginx conf | `docker compose up -d`(絕不用 `docker restart` 當作套用設定) |
| 程式碼 | 走 §8.1 版本升級,內網不現場 build |

---

## 9. 快速故障排除索引

| 症狀 | 去哪裡 |
|---|---|
| csp 起不來 / crash-loop / `Refusing to start` | runbook §3.2、§6.1(alembic 斷鏈那條特別讀) |
| 模型全 offline / `CERTIFICATE_VERIFY_FAILED` / 401 / 404 | runbook §6.2;先跑 `anila-ops.sh health` 看模型鏈路段 |
| 卡片登入失敗 / HiPKI 沒反應 | runbook §6.3;全面故障走 break-glass |
| 同事卡在 pending / 單位下拉是空的 | runbook §6.4;先建 departments |
| GPU 容器 error 803(本機模型模式) | runbook §6.1b |
| JWKS 500 / studio crash-loop | 缺 JWT keypair → `deploy-prod.sh up` 會自動補產;`anila-ops.sh health` 會點名 |
