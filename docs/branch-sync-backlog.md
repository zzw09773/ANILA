# ANILA Branch Sync Backlog

5 條長期維護的 branch 之間的同步狀態與 fork 區策略。**main 為 single source of truth (SSOT)**,其他 4 條 downstream 定期 sync from main。每次 main 有 commit 落地,**立刻評估是否要 sync 進 downstream**,並在這份檔記錄。

> **2026-05-26 重構**:從原本「main + prod」雙線重整為 5 條 branch(3 種 prod × 2 種 dev)。原 `prod` branch 已 rename 為 `prod-intranet-card`(GitHub API rename,PR refs / branch protection 自動跟著走)。Backup tag:[`pre-branch-restructure-2026-05-26`](https://github.com/zzw09773/ANILA/releases/tag/pre-branch-restructure-2026-05-26) 紀錄 rename 前的 prod HEAD。

---

## 五線結構

| Branch | 部署對象 | 認證 | 環境 | 識別特徵 |
|---|---|---|---|---|
| `main` | 開發 SSOT(default branch) | 純帳密 | 不指定 | 所有 feature 先進這條,downstream 從這 sync |
| `prod-intranet-card` | 中科院內網部署 | **SSO + 中科院 PKI 卡** | 內網 | 唯一含 SSO/card auth fork 的 branch;`REQUIRE_CARD_LOGIN_ONLY=true`;nginx Host allowlist + 內網 hardening |
| `prod-public-passwd` | 對外網 prod | 純帳密 | 外網 | main + 外網 hardening(rate limit / CSP 嚴一級 / WAF-ready) |
| `prod-military-passwd` | 國軍交付 prod | 純帳密 | 國軍 | main + military spec(待定;可能含 air-gap config / FLUX 啟用 / 客製分支) |
| `dev-public` | 對外網 dev | 純帳密 | 外網 | main + dev tooling(codeserver / n8n / gitlab unmuted)+ 寬鬆 hardening |
| `dev-military` | 國軍純帳密 dev | 純帳密 | 國軍 | dev-public + military 客製;國軍環境功能 dev / 測試前置 |

**共同 base**:`main` HEAD;4 條 downstream 用 `git merge origin/main` 定期 catch-up。

---

## Commit 標記規則

寫新 commit 時,**訊息開頭加標籤**,作為日後 audit 與 sync 依據:

| 標籤 | 意思 |
|---|---|
| (無標籤,在 main) | main 上的 commit,預設四條 downstream 都該 sync |
| `[card-only]` | 只進 `prod-intranet-card`(SSO / 自然人憑證卡相關) |
| `[public-only]` | 只進 `prod-public-passwd` / `dev-public` 兩條(對外網 hardening) |
| `[military-only]` | 只進 `prod-military-passwd` / `dev-military` 兩條(國軍客製) |
| `[dev-only]` | 只進 `dev-public` / `dev-military` 兩條 dev branch(dev tooling 等) |
| `[security-all]` | CVE / 鑑權繞過 等緊急情境,**5 條 branch 都要修**,不靠 cherry-pick 自動 |

```bash
# 隨時 audit
git log main --oneline | head -20                  # main 上的 commit
git log --all --oneline | grep '\[card-only\]'     # 進過 prod-intranet-card 的 fork commit
git log --all --oneline | grep '\[security-all\]'  # 緊急安全修補
```

---

## 同步策略

### main → downstream(預設方向,90% 的 commit)

```bash
git checkout <downstream-branch>
git pull origin <downstream-branch> --ff-only
git checkout -b sync/main-to-<downstream>-$(date +%Y-%m-%d) <downstream-branch>
git merge origin/main -X theirs   # content conflict 偏向 main
# 手動處理 fork 區(下方清單)+ docs rename 等
# 開 PR review 後 merge 進 downstream
```

**`-X theirs` 偏向 main 是預設**:downstream 是 "main + 少量 fork";沒理由偏向 downstream 的舊版本。

### 4 條 downstream 之間互不 sync

只透過 main 同步。如果某個 fix 跨多個 downstream 需要,先把 fix 進 main,再各 downstream sync。

唯一例外:`[card-only]` / `[public-only]` 等 fork commit 不會進 main,但這些是 fork 區的 by design,不算「downstream 互相 sync」。

---

## 永久 fork 區(不會進 main,downstream 各自維護)

### `prod-intranet-card` only(SSO / 自然人憑證卡)

| 檔案 | prod-intranet-card 樣貌 | main 樣貌 |
|---|---|---|
| `services/csp/app/api/auth.py` | 含 SSO/OIDC + card auth + revocations | 純帳密 + revocations |
| `services/csp/app/api/users.py` | 含 card-related user 操作 | 簡化版 |
| `services/csp/app/api/auth_providers.py` | 存在(SSO admin) | 已刪除 |
| `services/csp/app/models/user.py` | 多 `local_password_disabled` Column | 無此欄位 |
| `services/csp/app/models/auth_provider.py` | 存在 | 已刪除 |
| `services/csp/app/models/external_identity.py` | 存在 | 已刪除 |
| `services/csp/app/schemas/user.py` | 多 `RefreshRequest` + `local_password_disabled` field | 多 `_validate_password_strength` validator |
| `services/csp/app/schemas/auth_provider.py` | 存在 | 已刪除 |
| `services/csp/app/schemas/card.py` | 存在 | 已刪除 |
| `services/csp/app/services/auth_service.py` | 多 `LOCAL_PASSWORD_DISABLED_SENTINEL` + SSO-only reject 邏輯 | 純帳密 |
| `services/csp/app/services/external_auth_service.py` | 存在(OIDC flow) | 已刪除 |
| `services/csp/app/services/auth_provider_secret.py` | 存在(envelope encryption) | 已刪除 |
| `services/csp/app/services/card_auth.py` | 存在 | 已刪除 |
| `services/csp/app/services/card_auth_service.py` | 存在 | 已刪除 |
| `services/csp/app/api/router.py` | 多 mount `auth_providers_router` | 無此 line |
| `apps/csp-governance-ui/src/views/LoginView.vue` | 自然人憑證 + SSO 入口 | 純帳密表單 |
| `apps/csp-governance-ui/src/views/AuthProvidersView.vue` | 存在 | 已刪除 |
| `apps/csp-governance-ui/src/views/UsersView.vue` | 含 SSO 帳號管理 | 簡化版 |
| `apps/csp-governance-ui/src/api/auth.js` | 含 SSO methods | 簡化版 |
| `apps/csp-governance-ui/src/api/users.js` | 含 SSO 操作 | 簡化版 |
| `apps/csp-governance-ui/src/stores/auth.js` | 含 SSO state | 簡化版 |
| `apps/anila-shell/src/app.jsx` | 含 SSO 路由 | 已簡化 |
| `apps/anila-shell/src/login.jsx` | 已刪除(改走 LoginView.vue) | 仍存在 |
| `apps/anila-shell/src/runtime/auth.jsx` | SSO 流程 | 帳密流程 |
| `infra/nginx/anila.conf` | Host allowlist + card-verify exact-match location | 寬鬆 server_name |
| `infra/compose/platform.yml` 內 `ENABLE_CARD_LOGIN=true` / `REQUIRE_CARD_LOGIN_ONLY=true` env | 設值 | 不設或預設 false |

### `prod-public-passwd` / `dev-public` only(外網 hardening,待落實)

| 預期差異 | 說明 |
|---|---|
| nginx rate limit | 對外網需要比內網嚴的 limit(防 DDoS / scraping) |
| CSP / Permissions-Policy | 比 main 嚴(`object-src 'none'` 等) |
| WAF-ready | nginx 預留 ModSecurity / Cloudflare 整合點 |
| TLS only | 強制 HSTS preload + 不接受 http://(redirect 都不做) |
| audit log retention | 對外網需要更長 retention(法規) |

> ⚠️ 這份清單是 placeholder,等實際開始落實時(各客戶要求 + IT 需求收齊)更新。

### `prod-military-passwd` / `dev-military` only(國軍客製,待落實)

| 預期差異 | 說明 |
|---|---|
| FLUX 啟用 | 國軍環境要 FLUX 圖像生成(對應 `flux2-dev` + `flux2-dev-agent`) |
| air-gap config | 完全無外網,所有 model registry 都指向 docker DNS |
| 客製字眼 / branding | UI / docs 換掉「ANILA / 中科院」字樣為國軍版客製 |
| 限制 features | 可能砍掉某些 anila-studio artifact pipeline(如 podcast / video) |

> ⚠️ 同上,placeholder。等國軍 spec 確認後填具體。

### `dev-*` only(dev tooling,待落實)

| 預期差異 | 說明 |
|---|---|
| codeserver / n8n / gitlab | `infra/compose/platform.yml` 內 unmute(prod 系列全部 commented out) |
| dev secret 模式 | 允許 `ANILA_ALLOW_DEV_SECRET=1`、`dev-changeme` fallback |
| 寬鬆 nginx | dev 不強制 HSTS,allow http:// |
| 開放 host port | csp `:8000` / router `:9000` 之類 host port 開出來給 dev 工具直連 |

> ⚠️ 同上,placeholder。

**例外 — 緊急情境(`[security-all]`)**:
- CVE / SQL injection / XSS / 鑑權繞過 → **5 條都各自手寫修補**,不靠 cherry-pick(因為 fork 區可能讓 patch 互不適用)
- 5 條 commit message 都用 `[security-all]` 標籤
- 在本檔對應的 fork 區清單加註「YYYY-MM-DD 已 patch」備註

---

## 同步操作 SOP

### A. main 純加值 commit(無 fork 區檔)

```bash
# 對每條 downstream 重複:
git checkout <downstream>
git pull origin <downstream> --ff-only
git checkout -b sync/main-to-<downstream>-$(date +%Y-%m-%d)
git merge origin/main -X theirs
# 解 conflict → git add → 開 PR
```

### B. 動到 fork 區檔的 commit

不要走 merge,改用「手動 port」:

1. 看原 commit 的 diff:`git show <sha>`
2. 在 target branch 上手寫等效改動(略過 fork 區 path,或保留 fork 區版本)
3. Commit:
   ```
   [adapted from <source-sha>] <description>

   原 commit 改 <source 檔>,本 branch 對應到 <target 檔>(fork 區保留)。
   ```

### C. 緊急 security(5 條同時)

1. 先在 main 寫 + commit + 跑測試
2. **立刻** 逐條 downstream 重寫修補(不 cherry-pick,因為 fork 區可能讓 patch 失效)
3. 5 條 commit message 都用 `[security-all]` 前綴
4. 本檔的「永久 fork 區」相關行加 patch 紀錄

---

## 歷史 sync 紀錄

### 2026-05-26 — main → prod 一次性 sync(204 commits)+ 後續修補

舊「main + prod」結構下的最後一次大 sync。詳見 PR #16 commit history 與根 README §「最近更新」2026-05-26 條目:
- PR #16 (`88bce2f`) merge sync/main-to-prod → prod
- `25a68ab` fix:fork 區補課(5 modules + router mount)
- `46930f4` fix:compose dev fallback 改 fail-loud
- `3de7c1f` feat:`scripts/deploy-prod.sh`
- `25a68ab` 以後的 commits 列 `prod-intranet-card` 自己的 fork 區維護

### 2026-05-26 — branch restructure

`prod` → `prod-intranet-card` rename。從 main 開 `prod-military-passwd` / `prod-public-passwd` / `dev-public` / `dev-military` 4 條新 branch(初始 HEAD == main 的 `92faba3`,各 branch specific 差異待後續 commit)。Backup tag:`pre-branch-restructure-2026-05-26`。

---

**Last updated**: 2026-05-26 · **Owner**: ANILA 平台團隊 · **Branch model**: SSOT main + 4 downstream
