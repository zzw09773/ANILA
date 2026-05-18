# ANILA Branch Sync Backlog

兩條長期維護的 branch 之間的同步狀態與待辦清單。每次有 commit 落地,**立刻評估是否要同步另一邊**,並在這份檔記錄。目的是避免兩條線長期漂移後變成完全不同的產品。

> Initial backlog created 2026-05-18,在 prod (中科院) 與 feature/no-sso (國軍交付) 雙線結構正式確立後。

---

## 雙線結構

| Branch | 部署對象 | 識別特徵 |
|---|---|---|
| `prod` | 中科院內部署 | SSO + 自然人憑證 (card auth) + 內網 hardening + 部署 runbook |
| `feature/no-sso` | 國軍交付 | 僅 username+password + FLUX.2-dev image generation |

共同基底:`973b959` (main HEAD, 2026-05-13)

兩線從未互相合過,各自演進。

---

## Commit 標記規則

寫新 commit 時,**訊息開頭加標籤**,作為日後 audit 與 cherry-pick 依據:

| 標籤 | 意思 |
|---|---|
| `[both]` | 兩邊都要進。寫完一邊立刻 cherry-pick 另一邊。 |
| `[prod-only]` | 只進 prod(例如 card auth 細節)。 |
| `[no-sso-only]` | 只進 no-sso(例如 SSO 移除相關)。 |
| (無標籤) | 進落地當下的 branch,事後審視是否該補另一邊。 |

```bash
# 隨時 audit
git log --oneline | grep '\[both\]'
git log --oneline --all | grep '\[prod-only\]'
```

---

## 待同步 Backlog

### `prod` → `feature/no-sso` (還沒處理)

`prod` 上有 9 個 commit 比 `feature/no-sso` 多,其中部分可以 cherry-pick 帶過來。

| Commit | 類型 | 描述 | Status |
|---|---|---|---|
| `93bd3e8` | hardening | 內網部署前 hardening + 部署 runbook | ❌ TODO |
| `d41993d` | CVE | bump 5 packages to clear all known CVEs | ❌ TODO |
| `f490aba` | hardening | audit fail-soft + nginx Host allowlist | ❌ TODO |
| `7ccc618` | CVE | clear Tier-1 residual CVE + recon surface | ❌ TODO |
| `02076f2` | docs | AI risk management + third-party AI governance | ❌ TODO(文件,純加值,無風險) |
| `567bcc4` | mixed | SSO migration plan + intranet deployment runbook | 🟡 partial — no-sso 沒 SSO,只取 intranet runbook 部分 |
| `f5ada48` | card-auth | CARD_INITIAL_OWNERS startup check | 🟦 skip — card-auth 專屬,no-sso 不適用 |
| `f01288e` | card-auth | Refactor card login functionality | 🟦 skip — 同上 |
| `a762a1c` | card-auth | Card login integration tests | 🟦 skip — 同上 |

### `feature/no-sso` → `prod` (規劃中,Q1 進行中)

`feature/no-sso` 上有 19 個 commit 比 `prod` 多。多數是 FLUX 整合(可帶過去),其中一個是 SSO 移除(prod 不要)。

| Commit | 類型 | 描述 | Status |
|---|---|---|---|
| `bf9983e` | flux | schemas | 🔄 進行中 |
| `5ffe584` | flux | image_store | 🔄 進行中 |
| `63d6f68` | flux | flux_client | 🔄 進行中 |
| `943e24f` | flux | prompt_translator | 🔄 進行中 |
| `56ff038` | flux fix | broaden translator fallback | 🔄 進行中 |
| `870e5f8` | flux | chat_handler | 🔄 進行中 |
| `357266f` | flux | FastAPI main | 🔄 進行中 |
| `e5860f0` | flux fix | tighten main entrypoint hardening | 🔄 進行中 |
| `396c7d5` | flux chore | silence pylance warnings | 🔄 進行中 |
| `def7e0a` | flux chore | add pyright ignore | 🔄 進行中 |
| `cae6ff5` | flux | flux2-dev-agent Dockerfile | 🔄 進行中 |
| `ef686c0` | flux | flux2-dev server (FastAPI + injectable pipeline) | 🔄 進行中 |
| `338b3b2` | flux | flux2-dev CUDA Dockerfile | 🔄 進行中 |
| `0b97772` | flux | 加 service 到 `models/docker-compose.yml` | 🔄 進行中(此檔兩邊一樣) |
| `fa3f3df` | flux | CSP 註冊 image-generator agent | ⚠️ **需要 adapt** — 原本改 `docker-compose-dev.yml`,prod 對應到 `docker-compose.yml` |
| `ab69419` | flux fix | torch bump + healthcheck + e2e report | 🔄 進行中 |
| `28ada1d` | flux fix | declarative CSP wiring(env JSON) | ⚠️ **需要 adapt** — 同 `fa3f3df` |
| `6d79b71` | flux fix | emit SSE chunks when stream=true | 🔄 進行中 |
| `cbdee49` | no-sso-only | Refactor auth flow + remove SSO | 🟦 skip — 這個 commit 就是 no-sso 之所以為 no-sso |

⚠️ **adapt 的兩個 commit** 不能直接 cherry-pick,因為它們改的是 dev 專屬的 compose 檔。要在 prod 端手動寫等效改動到 `docker-compose.yml`,並注意:
- 容器名:`anila-platform-dev-csp-1` → `anila-platform-csp-1`
- bind-mount 路徑:`./share-dev/uploads/flux` → `./share/uploads/flux`(若 prod 用 `./share/`)
- 環境變數名與 API key 可能對應到不同的 production secret

---

## 永久 fork 區(設計理念差異,**不互相同步**)

這些檔在兩條 branch 上已演進成不同的最終樣貌,**接受它們從此是不同的檔**,不要嘗試合併:

| 檔案 | prod 樣貌 | no-sso 樣貌 |
|---|---|---|
| `myCSPPlatform/backend/app/api/auth.py` | 含 SSO endpoint + card auth | SSO 全砍,僅 username/password |
| `myCSPPlatform/backend/app/api/users.py` | 含 card-related user 操作 | 簡化版 |
| `myCSPPlatform/backend/app/schemas/auth_provider.py` | 存在 | 已刪除 |
| `myCSPPlatform/frontend/src/views/LoginView.vue` | 自然人憑證 + SSO 入口 | 純帳密表單 |
| `myCSPPlatform/frontend/src/views/AuthProvidersView.vue` | 存在 | 已刪除 |
| `myCSPPlatform/frontend/src/views/UsersView.vue` | 含 SSO 帳號管理 | 簡化版 |
| `myCSPPlatform/frontend/src/api/auth.js` | 含 SSO methods | 簡化版 |
| `myCSPPlatform/frontend/src/api/users.js` | 含 SSO 操作 | 簡化版 |
| `myCSPPlatform/frontend/src/stores/auth.js` | 含 SSO state | 簡化版 |
| `ANILA_UI/anila-ui/src/app.jsx` | 含 SSO 路由 | 已簡化 |
| `ANILA_UI/anila-ui/src/login.jsx` | 已刪除(改走 LoginView.vue) | 仍存在 |
| `ANILA_UI/anila-ui/src/runtime/auth.jsx` | SSO 流程 | 帳密流程 |

**例外 — 雙邊都要修的緊急情境**:
- CVE / SQL injection / XSS / 鑑權繞過 → 兩邊各自手寫一次修補,不靠 cherry-pick
- 在兩邊 commit message 都加 `[security-both]` 標籤,便於日後 audit

---

## 同步操作 SOP

### A. 純加值 commit(無 branch-specific 檔)

```bash
git checkout <target-branch>
git cherry-pick <sha>
# 若有衝突:逐個解 → git add → git cherry-pick --continue
# 完成後更新本檔 Status 為 ✅ <date> by <name>
```

### B. 需要 adapt 的 commit(動到 branch-specific 檔)

不要 cherry-pick,改用「手動 port」流程:

1. 看原 commit 的 diff:`git show <sha>`
2. 在 target branch 上手寫等效改動
3. Commit:
   ```
   [adapted from <source-sha>] <description>

   原 commit 改 <source 檔>,本 branch 對應到 <target 檔>。
   ```
4. 更新本檔 Status 為 ✅ adapted <date> by <name>,標明 adapter commit sha

### C. 雙邊都要寫的 commit(security / 核心 bug)

1. 先在主 branch 寫 + commit + 跑測試
2. **立刻** 切到另一 branch,**手動重寫** (不 cherry-pick,因為共動檔內容已 fork)
3. 兩邊 commit message 都用 `[security-both]` 前綴
4. 兩邊都更新本檔的「永久 fork 區」備註,標明雙邊都 patched 過

---

## Audit 指令

```bash
# 看哪些 commit 標 [both] 但只在一邊
( git log --oneline prod --grep='\[both\]' | sort
  git log --oneline feature/no-sso --grep='\[both\]' | sort ) | uniq -u

# 比較兩邊
git log --oneline main..prod
git log --oneline main..feature/no-sso

# 兩邊都動過的檔(潛在合併痛點)
comm -12 \
  <(git diff --name-only $(git merge-base feature/no-sso prod) feature/no-sso | sort) \
  <(git diff --name-only $(git merge-base feature/no-sso prod) prod | sort)
```

---

## 變更紀錄

- **2026-05-18** — Initial backlog 建立。盤點 prod ahead 9 commits、no-sso ahead 19 commits、兩邊衝突檔清單、永久 fork 清單。
