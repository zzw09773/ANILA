# SSO Migration Plan — 從本機帳密到 OIDC SSO

**狀態**：Sprint 6 X 起草。`local_password_disabled` flag、PKCE / nonce
驗證、id_token 驗簽、next_path sanitize 已落地（B2 / A6 / B3）；
本文件描述後續的 SSO cutover roadmap，以及帳號合併的長期政策。

**目標**：把所有人類使用者改用 OIDC SSO 進站，本機密碼僅保留給 break-glass
admin 與系統帳號（ingestion-worker 之類）；agent / SDK 仍走 API key。

**非目標**：完全砍掉本機登入；自動合併不同來源的身分（風險見 §3）。

---

## 1. 既有狀態（Sprint 6 X 結束時）

| 能力 | 狀態 | 備註 |
|---|---|---|
| 本機帳密登入 | ✅ 仍可用 | `/api/auth/login` `auth_source=local` |
| OIDC 登入 | ✅ 可用，PKCE + nonce + id_token 驗簽 | `/api/auth/oidc/{id}/start` |
| LDAP 登入 | ❌ 已移除 | Sprint 5 X，`/login` 對 `auth_source=ldap` 回 400 |
| `users.local_password_disabled` flag | ✅ 預設 False | admin UI 可在 UsersView 切換 |
| OIDC client_secret 加密儲存 | ✅ AES-256-GCM | `auth_provider_secret.py` envelope |
| Email-based 帳號自動合併 | ❌ 已停用 | `_provision_external_user` raises ValueError |

## 2. 階段性 cutover 路線

### Phase 1：admin 試點（建議 1 週）

**目標**：admin 與少數先行使用者轉成 SSO-only，驗證 IdP 整合完整無漏網。

行動：
1. ops 在 IdP 建立 Confidential Client，取得 client_id / client_secret，登入
   ANILA 控制台「SSO / OIDC」頁建立 provider。
2. admin 用既有本機密碼登入，從 IdP 完成首次 OIDC 綁定（會以 OIDC `sub`
   建立 ExternalIdentity row）。
3. 驗證：admin 登出後可從 OIDC 登入按鈕重進站；`/api/auth/me` role 仍是
   admin。
4. admin 自行對自己按「切 SSO-only」讓 `local_password_disabled=True`。
5. 為 break-glass 機制保留：另一個 admin 帳號 (`admin-break-glass`) **不**
   切 SSO-only，密碼長期保管於密碼管理工具，僅在 IdP 出事時用。

驗收：
- 從瀏覽器 https://platform/login 點 SSO 按鈕一路到首頁，無錯誤。
- `users.local_password_disabled` 對 break-glass 帳號是 False，其他 admin
  是 True。
- 對 SSO-only admin 用密碼登入回 403「請改用 SSO」。

### Phase 2：developer 與一般使用者批次轉移（建議 2 週）

**目標**：所有有對應 IdP 帳號的人切換完成。

行動：
1. ops 用 SQL 撈出 `users` 表沒有對應 `external_identities` row 的清單，
   分批通知使用者「請於下週前用 SSO 登入綁定」。
2. 使用者收到通知後從 SSO 按鈕登入；後端會：
   - 找不到 `(provider_id, subject)` → 試 email match → 若有同 email 的
     local user 則 raise ValueError「請聯絡 admin 手動處理」（B6 對應的
     UX 阻擋）。
   - admin 收到工單後使用 §3 的合併工具核可合併。
3. 一週後 ops 對「已有 ExternalIdentity 但仍有本機密碼」的 user 執行
   bulk SQL UPDATE 把 `local_password_disabled` 設為 True。
4. 廣播「下週起一律 SSO」。

驗收：
- 99% 以上 active user 都有 `external_identities` row。
- bulk flip 後沒有人來反映「我登不進來」（除非帳號該下線）。

### Phase 3：本機登入下架（建議 1 週）

**目標**：把 `/api/auth/login` 對 `auth_source=local` 改成預設拒絕，僅當
`ANILA_BREAK_GLASS=1` 時才允許 break-glass 帳號登入。

行動：
1. 加 `settings.LOCAL_LOGIN_DISABLED` env var，預設 True。
2. login handler 對非 break-glass 路徑直接 403（所有人都應該 SSO）。
3. break-glass 路徑：用一個獨立 endpoint `/api/auth/break-glass-login`，
   要求 caller 帶 SSH-key challenge / 雙人核可（後續 sprint 詳設計）；
   或乾脆只允許 console（docker exec → manage.py break-glass-login）。
4. 前端 `/login` 頁拿掉本機帳密表單，只剩 SSO 按鈕；註冊頁同步下架。

驗收：
- production 中所有 `users.local_password_disabled=True` 或 user 是
  system role；break-glass admin 例外。
- API audit log 在一週內沒有任何 `auth_source=local` 成功登入。

## 3. 帳號合併政策（B6 對應的長期解法）

當 OIDC 回傳的 `email` 與既有本機帳號相同時，目前後端 raise ValueError，
不自動合併。永遠不自動合併的理由：

- 任何被信任的 OIDC provider 若被攻陷或設定錯誤，宣告 admin email 即可
  接管 admin 帳號（Sprint 5 X 審查 §H2 已記錄此風險）。
- 即便 IdP 沒被攻陷，「同 email 不同人」的情況存在（例如員工離職交接
  email、別名）。

合併流程（admin tool 待開發，預計 Sprint 7 X）：

1. 使用者 OIDC 登入 → backend raise → callback HTML 顯示「email 衝突，
   請聯絡 admin 並提供以下 trace ID」。
2. admin 在控制台「SSO 帳號合併」頁輸入 trace ID + 確認動作：
   - 預設動作 A：把 OIDC subject 綁到既有本機帳號（如同把該帳號從本機
     升級為 SSO-only）。
   - 動作 B：拒絕，請使用者改用其他 email。
3. 合併動作必須兩位 admin 線下協同（雙人核可），動作寫入 audit_logs。
4. 合併執行後 user 需要重新走 OIDC 流程，這次 `(provider_id, subject)`
   命中會直接登入。

## 4. 維運注意事項

### 4.1 Break-glass admin 帳號

- 保持至少一個 admin 帳號 `local_password_disabled=False`，密碼存放於
  ops 密碼管理工具（HashiCorp Vault / 1Password 企業版）。
- 該帳號不綁 IdP；不參與正常工作；只用於：
  - IdP 故障無法登入時（替代恢復）
  - 後台手動合併帳號
  - 緊急停用其他 admin

### 4.2 IdP 變更

- 變更 OIDC provider 的 `issuer_url` / `client_id` 視為新 provider 流程；
  既有 ExternalIdentity row 仍綁舊 provider，需要 admin 手動 re-bind 或
  下線舊 provider。
- 變更 `client_secret` 直接從控制台「編輯」表單重設；後端 `enc::v1::`
  envelope 重新加密。

### 4.3 metrics 監控

- 每日報表：`users` 中 `local_password_disabled=False` 的 active user
  數（cutover 進度）。
- 每日報表：`audit_logs` 中 `auth_source=local` 成功登入次數（正常應該
  趨近 0，break-glass 才會 +1）。
- 警報：`auth_source=local` 連續失敗超過閾值（暴力破解徵兆）。

## 5. 對應 sprint 工作項目

| Sprint | 工作 | 對應本文件章節 |
|---|---|---|
| 6 X | id_token 驗簽 + PKCE + nonce + next_path | §1 |
| 6 X | `local_password_disabled` flag + admin UI | §1, §2 Phase 1/2 |
| 7 X | 帳號合併工具（admin UI） | §3 |
| 7 X | break-glass endpoint 設計 | §2 Phase 3, §4.1 |
| 7 X | `LOCAL_LOGIN_DISABLED` 全域 flag | §2 Phase 3 |
| 8 X | 觀測：cutover dashboard / audit alert | §4.3 |
| 8 X | 註冊頁前端下架 | §2 Phase 3 |
