# Sprint 7 X — 規劃文件（doc-only）

**狀態**：Draft（2026-04-27）。本 sprint **只寫文件**，不動 production
code。Sprint 6 X 已把資安修補尾巴與 SSO 地基鋪完；7 X 的角色是「在還
沒上線、尚無大量帳號」的時間點，把後續 SSO 切換、帳號合併、break-glass
等需要 design 決策的議題定型，等 Sprint 8 X 再依此實作。

**Companion docs**：
- [`sso-migration.md`](./sso-migration.md) — SSO cutover 三階段 roadmap
- [`runbooks/rotate-tls-cert.md`](../runbooks/rotate-tls-cert.md) — 私鑰
  / 憑證輪換（destructive）

---

## 0. 為什麼此 sprint 不寫 code

Sprint 6 X 結尾的狀態：

| 能力 | 狀態 |
|---|---|
| 本機帳密登入 | ✅ 仍可用，預設行為 |
| OIDC 登入（PKCE / nonce / id_token 驗簽 / next_path 白名單） | ✅ |
| `users.local_password_disabled` 個別切換 | ✅ admin UI 可用 |
| OIDC client_secret 加密儲存 | ✅ AES-256-GCM envelope |
| 既有本機帳號 + OIDC email 衝突 | ✅ 拒絕自動合併，raise 給 admin |
| LDAP path | ❌ 已下線 |

剩下的需求都是「正式上線後才會用到」的：

1. **帳號合併工具**（B6）：目前 raise 出來的衝突需要 admin 手動處理，
   尚無 UI；正式上線前才會有「使用者報修 → admin 點按合併」的真實流量。
2. **break-glass admin 機制**：IdP 故障的應急通道，目前 default admin
   `local_password_disabled=False` 已經是天然 break-glass，更嚴格的雙人
   核可流程在「沒人用」之前不必先實作。
3. **`LOCAL_LOGIN_DISABLED` 全域 flag**：等所有人都遷移完才會打開；現
   在打開 = 自己鎖門外。
4. **Cutover dashboard / audit alert**：目前活躍帳號 < 10 人，靠肉眼
   看 audit_logs 即可；建 dashboard 是 premature optimisation。
5. **註冊頁前端下架**：等本機登入下架的同一個 sprint 再做更節省。

所以這個 sprint 把 1 / 2 / 3 設計完整、把實作 ticket 寫到 actionable，
等真的要上線時直接執行；4 / 5 設計暫緩，到接近上線日再回頭。

---

## 1. 帳號合併工具（B6 詳設計）

### 1.1 觸發條件

`_provision_external_user` 在以下情境 raise 並把錯誤訊息回給 callback
HTML：

> OIDC 回應的 email「{email}」已綁定到本地帳號；為避免帳號接管風險，
> 自動合併已停用，請聯絡 admin 手動處理。

callback 把這段 message 顯示給使用者，並印一個 short trace ID（從 audit
log 反查）。使用者把 trace ID 提給 admin。

### 1.2 admin 操作流程（建議 UX）

新頁面 `/auth-providers/account-merge`（admin only）：

1. **Step 1 — 輸入 trace ID 或 email**
   - 用 trace ID → 後端 audit_logs 找到當時 raise 的 attempt（含
     `provider_id`、`external_subject`、`email`）。
   - 用 email → 列出所有 `_provision_external_user` 失敗於該 email 的
     audit row（按時間排序）。
2. **Step 2 — 顯示對照表**

   | 欄位 | 本地帳號 | OIDC 來源 |
   |---|---|---|
   | username | `alice` | `alice@company.com`（external_username） |
   | email | `alice@company.com` | `alice@company.com` |
   | role | `developer` | provider.default_role |
   | last_login_at | 2026-04-25 | — |
   | provider | local | `Company SSO (id=3)` |
   | external_subject | — | `idp-sub-abc123` |

3. **Step 3 — admin 動作三選一**
   - **A. 綁定到現有本地帳號**：建立 `external_identities` row
     `(user_id=本地id, provider_id, subject)`；可選地一併把該 user 切成
     SSO-only（勾選 checkbox）。
   - **B. 拒絕合併**：寫 audit log 標註 admin 拒絕；使用者下次仍會
     遭遇相同錯誤，需另闢蹊徑（換 email / 刪除舊帳號）。
   - **C. 建立新帳號**：強制以 `external_username-2` 之類為 username
     建立新本地 user，與舊帳號平行存在（極少用，僅在「同 email 不同
     人」時）。

4. **Step 4 — 雙人核可**
   - 動作 A / C 對 admin role 的影響太大，必須兩位 admin 分別簽核。
   - 第一位 admin 點完「動作 A 建議」；request 寫入 `pending_merges`
     表，狀態 `pending`，產生第二支 trace ID。
   - 第二位 admin 開同一頁、貼第二個 trace ID，看到「待第二位簽核」
     的對照表，按「核准」才執行。
   - 若是 `developer` / `user` role 的 user，可由設定決定是否需要雙
     人；admin 只允許雙人。

### 1.3 後端 API 草案

新 router `app/api/account_merge.py`：

```
GET    /api/account-merge/lookup?trace_id=...
GET    /api/account-merge/lookup?email=...
POST   /api/account-merge/propose
       body: {
         "user_id": int,                  # 既有本地 user
         "provider_id": int,
         "external_subject": str,
         "external_username": str,
         "external_email": str | None,
         "action": "bind" | "reject" | "new_account",
         "force_sso_only": bool,          # 動作 A 時可選
       }
       回傳：{ "merge_id": int, "trace_id": str, "status": "pending" }
POST   /api/account-merge/{merge_id}/approve
POST   /api/account-merge/{merge_id}/cancel
GET    /api/account-merge?status=pending
```

### 1.4 新 schema 草案

`pending_merges`：

| 欄位 | 型別 | 備註 |
|---|---|---|
| id | int PK | |
| trace_id | uuid | URL-safe，給 admin 使用 |
| proposed_by | FK users.id | 第一位 admin |
| approved_by | FK users.id NULL | 第二位 admin（同 proposed_by 拒絕）|
| user_id | FK users.id | 要綁定 / 新建的目標 |
| provider_id | FK auth_providers.id | |
| external_subject | varchar(255) | |
| external_username | varchar(255) | |
| external_email | varchar(255) NULL | |
| action | varchar(20) | bind / reject / new_account |
| status | varchar(20) | pending / approved / cancelled / executed |
| force_sso_only | bool | |
| proposed_at | timestamptz | |
| executed_at | timestamptz NULL | |

### 1.5 安全要點

- **不允許 self-approval**：`approved_by != proposed_by`，後端強制檢
  查並寫 audit。
- **連結至 audit**：每個 propose / approve / execute 動作都寫
  `audit_logs(action='account_merge_*', resource_type='user',
  resource_id=user_id)`。
- **TTL**：pending_merges 24 小時未 approve 就自動 cancel（cron job）；
  避免「半年前的請求被 admin 不小心 approve」。
- **Idempotent execute**：approve handler 對同一個 (provider_id,
  external_subject) 已經有 ExternalIdentity 時直接回成功，不重複建。

### 1.6 前端 UX 草案

- 入口：UsersView 頁面新增按鈕「帳號合併請求」，顯示 pending count
  badge。
- 列表：表格顯示 trace_id / user / provider / external_email / status /
  proposed_by / proposed_at；點 row 進詳情。
- 詳情：顯示對照表 + 三選一動作；右上角「核准」按鈕只在 `status=pending
  && current_user.id != proposed_by` 時顯示。

### 1.7 估時

- 後端 schema + API + service 邏輯：1 d
- 前端 admin UI：1 d
- pytest（含 self-approve 阻擋、TTL）：0.5 d
- E2E（兩個 admin tab 模擬）：0.5 d

合計 3 d，建議併入 Sprint 8 X。

---

## 2. Break-glass admin 機制

### 2.1 為什麼需要

當 IdP 故障 / OIDC provider 被誤設定 / cert 過期到無法 trust 時，所有
SSO-only admin 都會被鎖在外。需要保留一條「不依賴 IdP」的恢復通道。

### 2.2 目前的天然機制

Sprint 6 X 後，預設 admin 帳號 `local_password_disabled=False`，所以
admin 仍可用本機密碼登入。等同「天然 break-glass」。問題：

1. 沒有規範要求至少一個 admin 維持 `=False`；ops 可能不小心把所有 admin
   都切 True 鎖門。
2. 本機密碼若用太久未換、又不慎外流，break-glass 路徑會被當成 backdoor
   濫用。

### 2.3 設計選項

#### Option A：純 invariant（最簡單）

- 系統永遠保證至少一個 admin 滿足 `local_password_disabled=False`。
- `update_user` / `bulk-flip` 嘗試把最後一個 SSO-allowed admin 切 True
  時 raise `400 Refusing to lock out all admins`。
- 前端 button 在這種情況變灰。

優點：零新基礎設施。
缺點：本機密碼仍是「永久 valid」的弱點，沒解決濫用風險。

#### Option B：one-time CLI break-glass token

- 加 `manage.py break-glass --admin-username admin` CLI；只能 docker exec
  進入 csp container 執行（不開 HTTP endpoint）。
- 該 CLI 產生一個 1-hour 有效的 one-time JWT，列印在 stdout；admin 用
  該 JWT 直接打 `/api/auth/me` 取得 cookie 流程登入。
- token 寫入 `break_glass_tokens` 表，使用後標記為 consumed；同 admin
  在 24h 內只能產生一次。

優點：本機密碼可全面禁用；break-glass 變成審計可追蹤的事件。
缺點：要新 CLI + 一張表 + 兩個 endpoint；對 ops 多一道學習曲線。

#### Option C：CLI + 雙人 OOB 解鎖（最嚴格）

- CLI 產生 token 時要兩位 admin 在 docker exec session 內各執行一次
  `manage.py break-glass-approve`（看到對方的 challenge 並輸入正確的
  signed challenge response）。
- 通過後 stdout 印 one-time JWT。

優點：任何 break-glass 事件都需要兩個人在場，大幅降低 social engineering
風險。
缺點：實作複雜（challenge / response state 機）；故障場景下可能多一個人
無法湊齊就回不去。

### 2.4 建議

**先做 Option A**（invariant 保護），合計 0.5 d；正式上線、且使用者數
增長到 30+ 時再升級到 Option B。Option C 留到合規要求出現再做。

### 2.5 實作 ticket（Option A）

```
title: SSO-only flip：保證至少一個 admin 仍可用本機密碼
acceptance:
  - PUT /api/users/{id} 將最後一個 admin 從 local_password_disabled=False
    切到 True 時 raise HTTPException(400, "Refusing to lock out the last
    local-password admin")
  - bulk-flip endpoint（若有）同此規則
  - UsersView 對該 admin 顯示 button 為 disabled + tooltip
  - pytest 涵蓋：5 個 admin 4 個 True + 1 個 False，flip 第 5 個被擋；
    flip 已 True 的回 False 不受限
files:
  - app/api/users.py — update_user 加 invariant 檢查
  - app/services/auth_service.py — _admin_lockout_guard helper
  - tests/test_admin_lockout_guard.py — 新檔
estimate: 0.5 d
```

---

## 3. `LOCAL_LOGIN_DISABLED` 全域 flag

### 3.1 為什麼不現在做

「全域關閉本機登入」是切換流程的最後一步。現在做沒意義，會立刻把
admin 自己鎖在外。

### 3.2 何時開始實作

Sprint 8 X 進入 cutover Phase 3 時。觸發條件：

- 所有 active user 都有 `external_identities` row。
- audit_logs 過去 7 天 `auth_source=local` 成功登入只剩 break-glass admin。
- ops 已演練過 IdP 故障的 break-glass 流程（Option A invariant 已落地）。

### 3.3 設計

新環境變數 `LOCAL_LOGIN_DISABLED`（預設 False）：

```python
# app/config.py
LOCAL_LOGIN_DISABLED: bool = False
```

`/api/auth/login` 的 `local` 路徑：

```python
if settings.LOCAL_LOGIN_DISABLED:
    # 仍允許 last admin invariant 保留的 break-glass admin
    if not _is_break_glass_admin(request.username):
        raise HTTPException(403, "Local login disabled. Use SSO.")
```

`_is_break_glass_admin` 規則：role=admin AND
local_password_disabled=False AND IS the only such admin。判定邏輯由
auth_service 集中實作，避免散落各 endpoint。

### 3.4 前端配合

- LoginView 偵測 `/api/auth/providers` + `/api/auth/login` 行為決定是否
  顯示本機登入表單。
- 預設回傳的 OIDC providers list 已經足夠當作信號 — 若本機登入禁用，
  CSP 多回一個 flag：
  ```
  GET /api/auth/providers
  → {
      "providers": [...],
      "local_login_enabled": bool
    }
  ```
  schema 變更需要前後端同 commit；包進 Sprint 8 X 一起做。

### 3.5 實作 ticket

```
title: LOCAL_LOGIN_DISABLED 全域 flag
acceptance:
  - settings.LOCAL_LOGIN_DISABLED env 預設 False，可由 .env 覆寫
  - True 時 /api/auth/login auth_source=local 回 403，但 break-glass
    admin（依 §2.5 invariant）仍可登入
  - GET /api/auth/providers 多回 local_login_enabled
  - LoginView 隱藏本機表單（local_login_enabled=False）
  - phase1-e2e.sh 的 admin login fallback 跟著切到 admin OIDC 流程
files:
  - app/config.py
  - app/api/auth.py — login + providers endpoint
  - app/schemas/auth_provider.py — providers response
  - frontend/src/views/LoginView.vue
  - frontend/src/api/auth.js
  - tests/test_local_login_disabled.py
estimate: 0.5 d
```

---

## 4. 觀測（cutover dashboard / audit alert）— 暫緩

### 4.1 為什麼暫緩

預設只有 admin 一兩個帳號 + 開發者數人，dashboard 與 alert 維護成本
大於即時得到的訊號量。

### 4.2 觸發實作的條件

任一條件滿足就回頭做：

1. active user 數 > 30。
2. 排程過 production launch date（例如「下個月公司 GA」）。
3. 收到合規要求 / 稽核 (e.g. ISO 42001 audit) 需要看 cutover 進度。

### 4.3 屆時要做的事（簡述，不展開）

- 每日 cron：query users / external_identities / audit_logs，輸出
  `cutover_progress` JSON 到 ops Slack 或 email。
- audit_logs `auth_source=local` 連續失敗 > 5 within 1 minute 觸發告警。
- audit_logs `account_merge_*` 寫入時觸發告警（admin 互相監督）。

---

## 5. 註冊頁前端下架 — 暫緩

### 5.1 為什麼暫緩

註冊頁本來就只能寫入 `is_approved=False` 的 row，admin 不批就無效；對
未上線的系統也無攻擊面。下架動作併進「local_login_disabled flag 上線」
的同 sprint 比較省。

### 5.2 屆時要做的事（簡述）

- LoginView 對 `local_login_enabled=false` 隱藏「註冊新帳號」button。
- 註冊 modal + `/api/auth/register` endpoint 移除（OIDC auto-create 接手
  user provisioning）。
- `RegisterRequest` schema + audit `register` action 留著（給後台手動建
  立帳號的場景用）。

---

## 6. 7 X 此 sprint 唯一交付物

只有本 plan + 兩個 follow-up 連結即可：

- `docs/sprint-7x-plan.md`（本檔）
- `docs/sso-migration.md` 已在 6 X 寫完，§5 表新增 7 X 工作項；
  本 sprint 不動該檔。
- `docs/runbooks/rotate-tls-cert.md` 已在 6 X 寫完；本 sprint 不動。

下個 sprint（8 X）開工前，再開一個「Sprint 8 X 預算」 PR 把以下 ticket
轉成 issue：

| Ticket | 來源章節 | 估時 |
|---|---|---|
| Account merge tool（後端 + UI + 雙人核可） | §1 | 3 d |
| Admin lockout guard（Option A invariant） | §2.5 | 0.5 d |
| LOCAL_LOGIN_DISABLED 全域 flag | §3.5 | 0.5 d |
| 註冊頁前端下架 | §5 | 0.25 d |
| **小計** | | **4.25 d** |

觀測 / dashboard / alert 暫不排入；接近上線時再加開 Sprint 9 X。

---

## 7. 風險與假設

- **假設 1**：在 Sprint 8 X 開工前，使用者數仍 < 30。若超過，§4 的
  cutover dashboard 必須提前。
- **假設 2**：admin 全程僅 2–3 人，雙人核可可即時取得。若部分 admin 異
  地 / 跨時區，§1.2 step 4 的雙人核可可能拖慢「半小時內登入修好」的合
  併請求 — 接受。
- **風險 1**：SSO IdP 在試營運期間出包，導致目前還能用本機密碼登入的
  admin 一邊測一邊把自己切 SSO-only。對策：admin 切自己 SSO-only 前須
  先成功用 OIDC 登入過一次（前端可加 confirmation modal 顯示 last_login
  by `provider_id`）。納入 §1 完成項清單。
- **風險 2**：帳號合併工具上線前，使用者實際撞到 email collision 沒人
  處理。對策：6 X 已在 callback HTML 顯示 contact admin 訊息；8 X 之前
  的 collision 全部走 admin 直接 SQL 處理，並寫進 docs/runbooks/account-
  merge-manual.md（暫定 8 X 一併寫）。
