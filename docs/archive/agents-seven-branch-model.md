# 七分支模型（自 AGENTS.md §3 移出）

> 歷史紀錄。2026-07-28 重啟後專案是單一開發線，這個模型已經不存在。
> 同步清單見同目錄 [`branch-sync-backlog.md`](./branch-sync-backlog.md)。
> 現行必讀：[`../CURRENT-STATUS.md`](../CURRENT-STATUS.md)、[`../../PLAN.md`](../../PLAN.md)、[`../../SYSTEM-MAP.md`](../../SYSTEM-MAP.md)。

`main` 是 SSOT。通用 feature、bugfix、docs、測試先進 `main`，再同步 downstream。downstream 之間不要互相 merge；需要跨分支修補時，先進 `main`，再分別 port。另注意：分支 `anila-redesign` 已改用 §17.1 目錄配置（`services/` / `apps/` / `packages/` / `infra/`），該分支的權威文件在 `docs/anila-redesign-docs/`。

| Branch | 定位 | 維護重點 |
|---|---|---|
| `main` | 開發 SSOT | 所有通用變更來源。 |
| `dev-public` | 對外網 dev | 目前接近 `main`，保留 dev/public 說明。 |
| `prod-public-passwd` | 對外網 prod，純帳密 | 目前所在分支。必須維持 code-server 移除；`n8n` / `gitlab` 仍存在，外網部署前若不需要要移除 compose service、nginx location 與導覽 link。 |
| `dev-military` | 國軍 dev，純帳密 | 目前移除 code-server / n8n / GitLab；同步時避免 dev tooling 回流。 |
| `prod-military-passwd` | 國軍 prod，純帳密 | 目前移除 n8n / GitLab；code-server 是否保留須由交付規格明確決定。 |
| `prod-intranet-card` | 中科院內網 prod，SSO + 自然人憑證卡 | 唯一 card/SSO fork；不得用 `main` wholesale merge 覆蓋 auth/card 檔案。 |
| `trial-military` | 國軍 trial / 展示精簡版 | 刪減型分支；只挑選式 port 核心修補，不做整批 merge。 |
| `feature/document-relations` | 已被 `main` 包含的舊 feature | 可視為已合併封存，不當新工作來源。 |

分支操作規則：

- 分析分支差異時不要 checkout 擾動工作樹；用 `git show <ref>:path`, `git diff`, `git log`, `git cherry` 直接比較 refs。
- ahead/behind 與 commit count 會誤導。多條 downstream 有語意相同但 SHA 不同的 commit；同步前同時看檔案差異與 cherry 狀態。
- 同步前至少跑：
  - `git status --short --branch`
  - `git diff --name-status origin/main...<branch>`
  - `git diff --stat origin/main...<branch>`
  - `git log --oneline --no-merges origin/main..<branch>`
  - `git cherry -v origin/main <branch>`
- commit 標籤維持既有規則：`[card-only]`, `[public-only]`, `[military-only]`, `[dev-only]`, `[security-all]`。
- `prod-intranet-card` 的 SSO/card 永久 fork 熱區包含 `services/csp/app/api/auth.py`, `users.py`, `auth_providers.py`, `models/user.py`, `services/card_auth*.py`, `services/external_auth_service.py`, `schemas/card.py`, `apps/csp-governance-ui/src/views/LoginView.vue`, `AuthProvidersView.vue`, nginx card/Host allowlist 設定等。

建議同步順序：

1. `main` 先完成通用修補與驗證。
2. `dev-public`。
3. `prod-public-passwd`，確認 code-server 沒回來，並檢查 n8n/GitLab 暴露策略。
4. `dev-military`。
5. `prod-military-passwd`。
6. `prod-intranet-card` 手動 port，保留 card/SSO fork。
7. `trial-military` 挑選式 port，只帶安全與核心 bugfix。
