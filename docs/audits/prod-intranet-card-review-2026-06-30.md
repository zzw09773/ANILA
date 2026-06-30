# ANILA `prod-intranet-card` 重新審查摘要

日期：2026-06-30  
審查對象分支：`prod-intranet-card`  
審查時 HEAD：`a06c0cb fix(nginx): tighten /anila to trailing-slash prefix (drop broad match)`  
性質：內網部署 / 中科院憑證卡登入 / card-SSO fork  

重建註記：本檔是依前一次針對 `prod-intranet-card` 的讀碼結果重建。重建時工作樹可能已切回其他分支，因此本檔內容描述的是 `prod-intranet-card` 審查結果，不代表重建當下所在分支的狀態。

## 結論

`prod-intranet-card` 才是內網部署的重要主線。先前在 `prod-public-passwd` 產出的外網純帳密審查報告不能直接套用在這裡；`prod-intranet-card` 的核心風險不是「移除 n8n/GitLab」，而是：

1. 保留並保護 card/SSO fork，不可被 `main` 或其他 downstream wholesale merge 覆蓋。
2. 內網維運入口（code-server / n8n / GitLab）必須有明確策略與 gate。
3. air-gap / 內網 deployment 必須能重複驗證：TLS/CA、`.12` model gateway、JWT keypair、secret guard、card nonce binding。
4. 分支同步只能挑選式 port，尤其 auth/card/nginx/compose 熱區要手動比對。

本次審查只讀取與分析，未修改 runtime code。

## 審查時工作樹狀態

當時切到 `prod-intranet-card` 後，工作樹不是乾淨狀態：

- tracked deletion：
  - `models/inference/src/tensorrtllm-1.2.0rc5-openai-gpt-oss-20b/encodings/cl100k_base.tiktoken`
  - `models/inference/src/tensorrtllm-1.2.0rc5-openai-gpt-oss-20b/encodings/o200k_base.tiktoken`
- 未追蹤：
  - `docs/audits/anila-file-ledger-2026-06-29.csv`
  - `docs/audits/anila-file-ledger-audit-2026-06-29.md`
  - `docs/planning/roadmap-2026h2-intranet.md`

這兩個 tokenizer 檔案刪除不是該次審查造成。正式 build / package 前要先確認是否刻意刪除；若不是，應先還原，避免 inference bundle 缺檔。

## 已讀重點檔案

- `AGENTS.md`
- `README.md` 與分支差異資訊
- `docker-compose.yml`
- `myCSPPlatform/docker/nginx.conf`
- `scripts/intranet-deploy.sh`
- `scripts/deploy-prod.sh`
- `.env.example`
- `docs/runbooks/intranet-deployment-runbook.md`
- `myCSPPlatform/backend/app/config.py`
- `myCSPPlatform/backend/app/main.py`
- `myCSPPlatform/backend/app/services/startup_security.py`
- `myCSPPlatform/backend/app/services/card_auth.py`
- `myCSPPlatform/backend/app/services/card_auth_service.py`
- `myCSPPlatform/backend/app/api/auth.py`
- 現有未追蹤草稿：`docs/planning/roadmap-2026h2-intranet.md`

## 分支差異觀察

- `prod-intranet-card` 相對 `origin/main` 有大規模 fork，`git diff --stat origin/main...prod-intranet-card` 顯示 554 個檔案差異。
- 審查時 `prod-intranet-card` tracked file 數為 1205；舊的 `prod-public-passwd` ledger 報告是 1102 檔，不能視為本分支完整檔案審查。
- card branch 內含：
  - 真實 PKCS#7/CMS card verification。
  - CSPKI CA bundle。
  - intranet deployment scripts / runbook。
  - `models/inference` 重組。
  - code-server / n8n / GitLab 內網維運入口。
- `git cherry` 顯示部分 `main` commit 已有 patch-equivalent 進 card，部分只是語意相近但 patch 不同。不能用 ahead/behind 或 commit count 判斷同步狀態。

## 主要 Findings

### F1. `CARD_DEV_SKIP_NONCE_BINDING` 是 dev-only，但 production 沒有 runtime fail-fast

嚴重度：High

證據：

- `myCSPPlatform/backend/app/services/card_auth.py:71-77` 明確標註 `CARD_DEV_SKIP_NONCE_BINDING` 只供固定 mock 測試，production 不可開。
- `myCSPPlatform/backend/app/services/startup_security.py:48-72` 的 protected defaults 清單沒有檢查 `CARD_DEV_SKIP_NONCE_BINDING`。
- `docs/runbooks/intranet-deployment-runbook.md:26-28` 也提醒內網一律不可設此旗標。

風險：

- 若內網環境誤帶 `CARD_DEV_SKIP_NONCE_BINDING=1`，card verification 仍驗簽章與憑證鏈，但 nonce binding 被跳過，replay 防護被降級。
- 這是部署設定一旦錯誤就會影響 card login 安全邊界的問題，應該在 app startup fail-fast，而不是靠 runbook 人工記憶。

建議：

- 在 `assert_no_dev_defaults()` 或新增 `assert_card_prod_flags()` 中，當 `ANILA_ALLOW_DEV_SECRET != 1` 且 `CARD_DEV_SKIP_NONCE_BINDING` 為 truthy 時直接 `RuntimeError`。
- 補測試：prod mode truthy 應 fail；dev mode 可 warn 或允許；unset/false 可通過。

### F2. code-server / n8n / GitLab 已在 nginx 解凍，但 runbook 仍寫預設 404

嚴重度：High/Medium

證據：

- `myCSPPlatform/docker/nginx.conf:311-334`：`/codeserver` 實際 proxy 到 `codeserver:8080`。
- `myCSPPlatform/docker/nginx.conf:346-366`：`/n8n` 實際 proxy 到 `n8n:5678`。
- `myCSPPlatform/docker/nginx.conf:459-475`：`/gitlab/` 實際 proxy 到 `gitlab:8181`。
- `docs/runbooks/intranet-deployment-runbook.md:528` 仍寫 nginx 對 `/codeserver`、`/n8n`、`/gitlab/` 預設 `return 404`。
- `docker-compose.yml:422-455`、`457-489`、`491-532` 顯示三個 service 都已在 root compose 中啟用。

風險：

- 維運文件與實際 ingress 狀態相反，現場可能誤以為這些入口沒有暴露。
- 這些工具各自有 auth，但目前不是 CSP session / card SSO gate。
- code-server 掛 repo root，雖然遮蔽了 `.env` 與 `server.key`，仍是高權限維運入口。

建議：

- 先做產品/維運決策：正式保留、加 gate；或重新冷凍。
- 若保留：文件改成「已解凍」，加強密碼/帳號輪換、使用者範圍、log/audit、備份與 incident runbook。
- 若要收斂：nginx 對三個入口恢復 404，或加 CSP `auth_request` / network allowlist。

### F3. `deploy-prod.sh preflight` 對內網必要 env 檢查不足

嚴重度：Medium

證據：

- `scripts/deploy-prod.sh:110-134` 只檢查 `CSP_SERVICE_TOKEN`、`INTERNAL_PLATFORM_API_KEY`、`CSP_SECRET_KEY/SECRET_KEY`。
- `docker-compose.yml:56-66` 實際要求 `CSP_APP_DB_PASSWORD`、`CSP_DB_PASSWORD`、`CSP_SECRET_KEY`、`ADMIN_PASSWORD`。
- `docker-compose.yml:86-98` 實際要求 `CSP_SERVICE_TOKEN`、`CARD_INITIAL_OWNERS`。
- `docker-compose.yml:433-434` 實際要求 `CODESERVER_PASSWORD`。

風險：

- `preflight` 可能顯示成功，但 `docker compose up` 階段才因缺 env 或 placeholder 爆掉。
- 現場部署時錯誤會晚出現，且可能已開始 build / load / partial service 啟動。

建議：

- `preflight` 改用 `docker compose config >/dev/null` 作為最小 fail-fast gate。
- 另外顯式檢查內網部署必填值：`CSP_DB_PASSWORD`、`CSP_APP_DB_PASSWORD`、`ADMIN_PASSWORD`、`CARD_INITIAL_OWNERS`、`CODESERVER_PASSWORD`、`ANILA_HOST`、`MODEL_GATEWAY_API_KEY`（若 remote model mode）。
- 將 placeholder 字面檢查和 `startup_security.py` 的 placeholder 清單對齊。

### F4. `.12` model gateway DNS / extra_hosts 仍是 go-live 阻斷點

嚴重度：Medium

證據：

- `.env.example:118-124` 內網模型 gateway 使用 `https://aiagent2.ai.ncsist.org.tw`。
- `.env.example:132-146` 提醒要處理 gateway CA trust。
- `docker-compose.yml:171-173` 目前 `csp.extra_hosts` 只釘 `host.docker.internal:host-gateway`，沒有釘 `aiagent2.ai.ncsist.org.tw:10.53.100.12`。

風險：

- 若內網 DNS 尚未配置，CSP 容器內解析 `.12` gateway 會失敗。
- 若用 raw IP 取代 FQDN，TLS hostname verification 會失敗。

建議：

- 上線前二選一：
  - IT 完成內網 DNS：`aiagent2.ai.ncsist.org.tw -> 10.53.100.12`。
  - 或 compose 對 `csp` 加 `extra_hosts` 明確釘 FQDN 到 `.12`。
- preflight 加容器內解析與 TLS 驗證：
  - `openssl s_client -connect aiagent2.ai.ncsist.org.tw:443 -servername aiagent2.ai.ncsist.org.tw -CAfile share/pki/model-ca.pem`
  - 容器內 `GET /v1/models` 帶 `MODEL_GATEWAY_API_KEY`。

### F5. `ANILA_TRUSTED_HOSTS` compose fallback 仍偏 dev / local service

嚴重度：Medium/Low

證據：

- `docker-compose.yml:74-76` fallback 是 `gpt-oss-20b,gemma4,nv-embed-proxy,flux2-dev-agent,host.docker.internal`。
- `.env.example:33-36` 內網 production 建議是 `aiagent2.ai.ncsist.org.tw`。

風險：

- 若 production `.env` 漏設 `ANILA_TRUSTED_HOSTS`，compose fallback 不含 `.12` FQDN，model gateway 註冊或 outgoing SSRF guard 會被擋。
- 同時 fallback 含 `host.docker.internal`，在內網 production 未必應該預設信任。

建議：

- 對 `prod-intranet-card`，考慮讓 `ANILA_TRUSTED_HOSTS` 也 fail-fast，或至少在 preflight 比對是否包含 `aiagent2.ai.ncsist.org.tw`。
- `host.docker.internal` 是否保留應由部署規格決定，不應成為 production 隱性 fallback。

### F6. Worktree 當時刪了兩個 TensorRT-LLM tokenizer encoding 檔

嚴重度：Medium

證據：

- 審查時 `git status --short --branch` 顯示：
  - `D models/inference/src/tensorrtllm-1.2.0rc5-openai-gpt-oss-20b/encodings/cl100k_base.tiktoken`
  - `D models/inference/src/tensorrtllm-1.2.0rc5-openai-gpt-oss-20b/encodings/o200k_base.tiktoken`

風險：

- 若這不是刻意刪除，inference build / package 可能缺 tokenizer resource。
- 若是刻意刪除，應有 commit 或文件說明替代來源，避免下一位維運者誤還原。

建議：

- build/package 前先決定：
  - 若非刻意：`git restore` 還原。
  - 若刻意：補文件與測試，說明 tokenizer 來源與 build 驗證。

### F7. `prod-intranet-card` 的 roadmap 應以「內網可驗證交付」為主，不是外網 hardening

嚴重度：Planning

證據：

- `AGENTS.md` 已明確標示 `prod-intranet-card` 是唯一 card/SSO fork，不得 wholesale merge 覆蓋 auth/card 檔案。
- 目前分支有 `scripts/intranet-deploy.sh`、CSPKI bundle、card verification、內網 runbook、`models/inference` reorg。
- 現有未追蹤 `docs/planning/roadmap-2026h2-intranet.md` 已把 `.12` gateway key / DNS 視為 go-live 阻斷。

建議：

- 0-2 週 roadmap 改成：
  1. card dev flag fail-fast。
  2. dev-tool ingress 策略定案與文件修正。
  3. deploy preflight 完整化。
  4. `.12` gateway DNS/CA/API key smoke。
  5. worktree tokenizer deletion 歸零或文件化。

## 0-2 週建議工作項

### 1. 加 `CARD_DEV_SKIP_NONCE_BINDING` production guard

範圍：`myCSPPlatform/backend/app/services/startup_security.py`、測試。  
驗收：

- `ANILA_ALLOW_DEV_SECRET=0` 且 `CARD_DEV_SKIP_NONCE_BINDING=1/true/yes` 時 app startup fail。
- unset / false 時通過。
- 測試覆蓋。

### 2. 釐清 code-server / n8n / GitLab 策略

範圍：`myCSPPlatform/docker/nginx.conf`、`docker-compose.yml`、`docs/runbooks/intranet-deployment-runbook.md`。  
驗收：

- 文件與實際 nginx 行為一致。
- 若保留入口：列出 owner、帳密輪換、可用角色、incident 流程。
- 若冷凍入口：nginx smoke 要驗 404。

### 3. 強化 `deploy-prod.sh preflight`

範圍：`scripts/deploy-prod.sh`。  
驗收：

- `docker compose config` 缺 env 時 preflight 直接失敗。
- placeholder / dev defaults 檢查涵蓋內網必填值。
- remote model mode 會檢查 `MODEL_GATEWAY_API_KEY` 與 `.12` endpoint。

### 4. `.12` gateway smoke 納入部署 gate

範圍：`scripts/deploy-prod.sh`、`scripts/intranet-deploy.sh`、runbook。  
驗收：

- 容器內可解析 `aiagent2.ai.ncsist.org.tw`。
- TLS chain verify return code 0。
- 帶 `MODEL_GATEWAY_API_KEY` 可打 `/v1/models`。
- CSP `/v1/chat/completions` 能打到 `openai/gpt-oss-20b`。

### 5. 整理 tokenizer deletion

範圍：`models/inference/src/tensorrtllm-1.2.0rc5-openai-gpt-oss-20b/encodings/`。  
驗收：

- 工作樹不帶未解釋的 tracked deletion。
- 若檔案保留刪除，build/package 測試證明不需要它們。

## 後續同步原則

- `main` 仍是通用 SSOT，但 `prod-intranet-card` 的 card/SSO 熱區必須手動 port。
- 不要用 `main` 整檔覆蓋：
  - `myCSPPlatform/backend/app/api/auth.py`
  - `myCSPPlatform/backend/app/api/users.py`
  - `myCSPPlatform/backend/app/api/auth_providers.py`
  - `myCSPPlatform/backend/app/models/user.py`
  - `myCSPPlatform/backend/app/services/card_auth*.py`
  - `myCSPPlatform/backend/app/services/external_auth_service.py`
  - `myCSPPlatform/backend/app/schemas/card.py`
  - `myCSPPlatform/frontend/src/views/LoginView.vue`
  - nginx card / Host allowlist / card-only 設定
- 每次 port main 修補前，先看 `git diff origin/main...prod-intranet-card` 與 `git cherry`，再決定檔案級移植。

## 本次未做

- 未修改 runtime code。
- 未啟動 Docker stack。
- 未跑 unit / integration / e2e。
- 未重新產生完整 1205 檔 ledger。
- 未處理或還原審查時看到的兩個 tokenizer tracked deletion。

