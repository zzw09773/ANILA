# 內網部署 Runbook (branch SSO)

> **Owner**:你 (1147259)
> **目標環境**:中科院內網,IP `10.53.100.12`
> **更新**:2026-05-16
> **配套檔**:[`.env.example`](../../.env.example) / [`docker-compose.yml`](../../docker-compose.yml) / [`scripts/build-and-export-for-intranet.sh`](../../scripts/build-and-export-for-intranet.sh)

這份是「**今天從外網 dev 機 → 帶進內網一鍵跑起來**」的逐步操作手冊。任何一步看不懂或卡住,直接看「Troubleshooting」段。

---

## 0. 架構摘要 (一頁懂)

```
員工 PC                                   內網 Server (10.53.100.12)
─────────────────                         ─────────────────────────
[實體卡 + 讀卡機]                         
   ↑↓ ISO-7816 APDU                      
[HiPKI 本機元件]                          
 (PKCS#11 over HTTP @localhost:16888)    
   ↑↓ HTTP                                
[瀏覽器 popup → main page]               
   ↑↓ HTTPS (TLS, IT-issued cert)        
                            ─────→        nginx :443 / :4443
                                              ↓
                                          csp (FastAPI)
                                              ├─ /api/auth/card/* (PKCS#7 verify)
                                              ├─ /api/* (control plane)
                                              ├─ /v1/* (data plane → router → models)
                                              └─ Vue SPA serve /login + admin UI
                                              ↓
                                          postgres + redis + ingestion-worker
```

**Trust 邊界**:使用者 PC 上的卡片硬體 + PIN + HiPKI driver。Backend 收到 PKCS#7 簽章,parse 抽 employee_id 就完成驗證,**不再驗鏈也不打 OCSP** (內網 ocsp.ncsist.org.tw 不可達且非必要)。

---

## 1. Phase 0:外網準備 (在這台 dev 機跑)

### 1.1 確認跟 IT 拿到的東西

| 項目 | 狀態 | 備註 |
|---|---|---|
| TLS cert + key (CN/SAN 含 `10.53.100.12`) | ⏳ 等 IT | `server.crt` + `server.key`,放進 `myCSPPlatform/docker/certs/` |
| 內網 DNS 是否解析 anila / 平台 hostname | ⏳ 待確認 | 沒有也沒差,直接用 IP |
| HiPKI 元件已預載到所有員工 PC | ⏳ 待確認 | 你裝的版本相同即可 (`localhost:16888` 要能回應) |
| LLM service (gpt-oss-20b / gemma4) 在內網由誰提供 | ⏳ 待確認 | 我們的 model stack 走別的管道進內網 |

### 1.2 Build + 打包 image

```bash
cd /home/aia/c1147259/ANILA

# 跑打包腳本 (預設不打包 model image,model 走別管道)
bash scripts/build-and-export-for-intranet.sh
```

預期產出 (在 `/tmp/anila-images-export/`):

```
01-anila-built.tar.gz   ~1.5GB  (csp / ingestion-worker / router / anilalm / anila-ui / pptx-renderer)
02-base.tar.gz          ~0.4GB  (pgvector / redis / nginx)
03-cold.tar.gz          ~5GB    (codeserver / n8n / gitlab — 暫時 nginx 鎖死但保留)
INTRANET-LOAD.sh                (內網一鍵 import script)
MANIFEST.txt                    (sha256 checksum + git commit,給 IT 對檔)
```

> Model image 暫時不打包,需要時跑 `WITH_MODELS=1 bash scripts/build-and-export-for-intranet.sh` 即可加上 `04-models.tar.gz`。

### 1.3 (內網生成更乾淨) 4 個 secret 怎麼生

到內網 server 上跑:

```bash
echo "CSP_SECRET_KEY=$(openssl rand -hex 32)"
echo "CSP_SERVICE_TOKEN=$(openssl rand -hex 32)"
echo "INTERNAL_PLATFORM_API_KEY=sk-internal-$(openssl rand -hex 24)"
echo "CODESERVER_PASSWORD=$(openssl rand -base64 24)"
```

把這 4 條輸出**直接複製到密碼管理器**,等下填進內網的 `.env`。

> 為什麼要在內網生成而不是外網?外網 entropy + bash history + 你的 dev 環境都可能殘留;內網 server 是乾淨環境。但這只是建議,用我外網生的也不會破。

---

## 2. Phase 1:內網首次部署

### 2.1 帶進內網的東西

1. 整個 ANILA repo (USB / 內部閘道)
2. `/tmp/anila-images-export/` 整個資料夾
3. IT 給的 TLS cert (`server.crt` + `server.key`)
4. 4 個 secret (你密碼管理器內的)

### 2.2 內網 server 上的初始化

```bash
# 1. 把 repo 解壓到 /opt/anila (或你想要的路徑)
cd /opt/anila

# 2. TLS cert 放好,key 改 mode 600
cp /path/to/server.crt myCSPPlatform/docker/certs/server.crt
cp /path/to/server.key myCSPPlatform/docker/certs/server.key
chmod 600 myCSPPlatform/docker/certs/server.key

# 3. 編 .env (見下方範本)
cp .env.example .env
nano .env   # 把 4 個 secret + ANILA_HOST + CARD_INITIAL_OWNERS 填進去

# 4. 建 cross-project external network (一次性)
docker network create anila-models-net

# 5. import 全部 image
cd /tmp/anila-images-export
bash INTRANET-LOAD.sh
```

### 2.3 `.env` 完整範本 (內網版)

```bash
# ── 啟動安全檢查:全部關 (prod) ────────────────────────────────────────
ANILA_ALLOW_DEV_SECRET=0
ANILA_ALLOW_HTTP_ENDPOINT=0
ANILA_ALLOW_PRIVATE_ENDPOINT=0

# ── 4 個必填 secret (在內網生成,用 openssl rand) ──────────────────────
CSP_SECRET_KEY=<paste from password manager>
CSP_SERVICE_TOKEN=<paste from password manager>
INTERNAL_PLATFORM_API_KEY=sk-internal-<paste from password manager>
CODESERVER_PASSWORD=<paste from password manager>

# ── 內網 hostname / IP ─────────────────────────────────────────────────
ANILA_HOST=10.53.100.12

# ── code-server 工作目錄 (一定要設,compose required) ──────────────────
CODESERVER_WORKSPACE=./share/codeserver-sandbox

# ── branch SSO: 中科院憑證卡登入 ───────────────────────────────────────
ENABLE_CARD_LOGIN=true
REQUIRE_CARD_LOGIN_ONLY=true
CARD_INITIAL_OWNERS=1147259    # 你的真實員工編號;要加同事改 "1147259,xxx,yyy"

# ── (可選) model endpoint 覆寫 ────────────────────────────────────────
# 如果內網用 IT 的 LLM service,改這條指過去:
# LOCAL_LLM_BASE_URL=http://<intranet-llm-host>:<port>
# LOCAL_LLM_MODEL=<model-name>
# LOCAL_EMBEDDING_BASE_URL=http://<intranet-embed-host>:<port>
# LOCAL_EMBEDDING_MODEL=<embed-model-name>

# 沿用我們的 model stack 就不必設 — compose 預設指 docker DNS
# (gpt-oss-20b:8000 / nv-embed-proxy:8000 透過 anila-models-net)
```

---

## 3. Phase 2:首次 boot + 驗證

### 3.1 啟動

```bash
cd /opt/anila
docker compose up -d --no-build
# --no-build 是關鍵:image 都已 import,跳過 build 階段直接用 cache
```

### 3.2 預期看到的事

```bash
# 看每個 service 是否健康
docker compose ps

# 預期 status 都是 "Up X seconds (healthy)" 或 "Up X seconds"
# 例外:gitlab 啟動 ~10 分鐘 (initial reconfigure)
```

### 3.3 startup_security 一定要過

```bash
docker compose logs csp 2>&1 | grep -E "startup_security|RuntimeError|Refusing"
```

| 看到的 log | 意義 | 處理 |
|---|---|---|
| 沒輸出 | 一切正常 | 繼續下一步 |
| `Refusing to start: 下列環境變數仍為 dev 預設值: SECRET_KEY` | secret 沒換成真值 | 把 .env 那條改成 `openssl rand` 出來的值 |
| `Refusing to start: REQUIRE_CARD_LOGIN_ONLY=True 但 ENABLE_CARD_LOGIN=False` | env flag 不一致 | 把 ENABLE_CARD_LOGIN 改 true |
| `required variable XXX is missing` | .env 漏了 secret | docker compose 階段就 fail,根本進不去 startup_security |

### 3.4 健康檢查

```bash
# 從 server 自己打 (應該 200)
curl -k https://localhost/health

# 從外網打 (應該 connection refused — 內網 only)
# 從 LAN 裡的另一台機器打 https://10.53.100.12/login (應該回 HTML 登入頁)
```

---

## 4. Phase 3:首次 admin ops (一次性)

### 4.1 你的卡片登入

1. 用瀏覽器打 `https://10.53.100.12/login`
2. 應該只看到「auth · pki card」這一個區塊 (本機帳密 / OIDC 都被 lockdown)
3. 點 **「detect card」** → popup 短暫開啟讀 PKCS#11 → 顯示「**鄒惠翔 員工編號 1147259**」之類資訊
4. 輸 PIN → 點 **「sign & submit」** → popup 短暫開啟做簽章 → 進入平台
5. 因為 `CARD_INITIAL_OWNERS=1147259`,你會直接以 `role=owner` + `is_approved=True` 登入

### 4.2 預先建立 departments

進 `https://10.53.100.12/departments`,建你會用到的單位:
- 例:「資通所人工智慧組」、「資通所軟體工程組」、「○○組」

> 為什麼要先建?同事第一次刷卡會跳「完成註冊」表單,要從 dropdown 選單位。沒先建 dropdown 會空。

### 4.3 確認 model 註冊成功

進 `https://10.53.100.12/models`,應該看到:
- `gpt-oss-20b` (LLM)
- `nvidia/NV-embed-V2` (embedding)

兩條 health check 應該綠 (前提:model stack 已啟動且 IT 已部署 LLM service)。

如果紅,看下面 Troubleshooting。

### 4.4 通知同事可以開始用

同事的操作:
1. 把 HiPKI 元件裝好 (確認 `localhost:16888` 可達)
2. 插卡進讀卡機
3. 開瀏覽器到 `https://10.53.100.12/login`
4. detect card → 看到自己的姓名 → 輸 PIN → sign & submit
5. 第一次會跳「完成註冊」表單,選自己的單位 → 送出 → 看到「等待管理員核准」
6. 你進 `/users` 看到 pending user → 點 approve
7. 同事下次刷卡就能真的進來

---

## 5. Phase 4:後續維運

### 5.1 解凍 codeserver / n8n / gitlab (時機到了再做)

預設 nginx 對 `/codeserver`、`/n8n`、`/gitlab/` 三個 location 都 `return 404`。要解凍:

```bash
# 編 nginx.conf,找到這 6 個 location block (port 443 + 4443 各 3 個)
nano myCSPPlatform/docker/nginx.conf

# 每個 location 內第一行就是:
#   return 404;
#
# 把那行**單獨刪掉一行**即可,下方原 proxy_pass 設定都已備齊。

# 重啟 nginx
docker compose restart nginx
```

### 5.2 TLS cert rotation (cert 過期或換新)

```bash
cp /path/to/new-server.crt myCSPPlatform/docker/certs/server.crt
cp /path/to/new-server.key myCSPPlatform/docker/certs/server.key
chmod 600 myCSPPlatform/docker/certs/server.key
docker compose restart nginx
```

### 5.3 Postgres backup (建議排程)

```bash
# 週期備份
docker exec anila-platform-csp-db-1 pg_dump -U csp csp | gzip > /backup/anila-$(date +%Y%m%d).sql.gz

# 還原 (緊急時)
gunzip -c /backup/anila-YYYYMMDD.sql.gz | docker exec -i anila-platform-csp-db-1 psql -U csp csp
```

### 5.4 加新員工到 CARD_INITIAL_OWNERS (升 owner)

```bash
# 編 .env
nano .env
# 改成:
CARD_INITIAL_OWNERS=1147259,1234567,7654321

# 重啟 csp
docker compose restart csp
```

> 這個機制只對「**新刷卡的人**」生效;已經有帳號的同事改 owner 要在 `/users` UI 改。

---

## 6. Troubleshooting

### 6.1 啟動失敗

| 症狀 | 原因 | 處理 |
|---|---|---|
| `docker compose up` 直接報 `required variable XXX is missing` | .env 缺 secret | 看哪個 var 缺,填進 .env |
| csp container 啟動後立刻 exit | startup_security raise | `docker compose logs csp` 看 RuntimeError 訊息 |
| nginx 啟動失敗 `cannot load certificate` | TLS cert 沒放好或路徑錯 | 確認 `myCSPPlatform/docker/certs/server.crt` + `server.key` 都在 |

### 6.2 卡片登入失敗

| 症狀 | 可能原因 | 處理 |
|---|---|---|
| 點 detect card 跳「尚未安裝中華電信本機元件」 | HiPKI 沒裝 / 沒跑 / port 不是 16888 | 員工 PC 確認 HiPKI 元件在跑 (`curl localhost:16888/popupForm` 應回 HTML) |
| detect 成功但 sign & submit 跳「PIN 錯誤或卡片驗證失敗」 | PIN 真的錯 / 卡片硬體故障 | 重新插卡、再試 PIN |
| 登入後立刻 redirect 回 /login | cookie 沒種好 / TLS cert hostname mismatch | 看 browser devtools cookie tab + console |
| 「卡片未插入或本機元件無法讀取卡片」 | 沒插卡或讀卡機有問題 | 確認讀卡機亮綠燈、卡片正確插入 |

### 6.3 同事登入後 stuck 在 pending

| 症狀 | 原因 | 處理 |
|---|---|---|
| 「請選擇單位」但 dropdown 是空的 | 沒有 active department | admin 進 `/departments` 建單位 |
| 提交「完成註冊」後一直 loading | registration_token 過期 (15 min TTL) | 同事重新刷卡 |
| 你在 `/users` 看不到 pending 同事 | filter 把 inactive 篩掉了 | 確認 filter 包含 "Pending" 狀態 |

### 6.4 LLM 不通

| 症狀 | 原因 | 處理 |
|---|---|---|
| `/models` 健康檢查紅 | model service 沒起 / endpoint URL 錯 | 確認 model stack 跑了:`docker ps grep -E "gpt-oss|gemma4"` |
| 對話 streaming 卡住 | router 連不到 csp / 反之 | 看 `docker compose logs router \| tail -50` |
| `503 Service Unavailable` | csp 沒 healthy | 看 `docker compose ps` 看 csp 狀態 |

---

## 7. 給 IT 的問題清單

如果有什麼搞不定要 escalate,這幾條先確認:

1. **TLS cert SAN 包含 `10.53.100.12`** (跟未來可能的 hostname)
2. **HiPKI 元件版本** 是否所有員工 PC 都是同一版,且 listen `localhost:16888`
3. **內網 firewall 規則** 允許 `:443` `:4443` (如果 anila-ui port 4443 也要對外的話)
4. **LLM service** 是用我們的 model stack 還是 IT 既有的?如果 IT 既有,給我們 endpoint URL + auth method
5. **Backup / log shipping** 公司有統一 log aggregation 嗎?需要的話我加 logging driver
6. **新員工的卡片發放流程** — 要不要 hook 到我們的 pending-approval 流程

---

## 8. 重要文件 cross-reference

- 整體架構:[`README.md`](../../README.md) §安全設計要點 + §最近更新 (2026-05-15 entry)
- 認證細節:[`myCSPPlatform/README.md`](../../myCSPPlatform/README.md) §認證整合 + §API 端點一覽
- caAuth.js 前端 helper:[`myCSPPlatform/frontend/src/api/caAuth.js`](../../myCSPPlatform/frontend/src/api/caAuth.js)
- backend 卡片驗證:[`myCSPPlatform/backend/app/services/card_auth.py`](../../myCSPPlatform/backend/app/services/card_auth.py)
- 啟動安全檢查:[`myCSPPlatform/backend/app/services/startup_security.py`](../../myCSPPlatform/backend/app/services/startup_security.py)
- mock 卡片元件:[`cht/`](../../cht/) (僅 dev,內網用真 HiPKI)

---

**有事直接看上面 troubleshooting 表;表內沒有的找架構摘要 (§0) 推一下哪一層出問題。**
