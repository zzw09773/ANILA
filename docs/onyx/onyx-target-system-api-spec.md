# Onyx Target System API Spec

**對方業務系統 → ANILA Onyx Agent 接入 API 規格**

| 欄位 | 內容 |
|---|---|
| Version | 1.0 |
| Date | 2026-04-27 |
| Audience | 對方系統開發人員 |

---

## 0. 整合拓樸

```
ANILA Onyx Agent  ──HTTPS──▶  Target System API
        │
        ├ Authorization: Bearer <ServiceCredential>
        ├ X-Impersonate-User-Id: <真正使用者 ID>
        ├ X-ANILA-Trace-Id: <ULID>
        ├ Idempotency-Key: <ULID>   (POST/PATCH/DELETE)
        └ Content-Type: application/json
```

- **ServiceCredential**：對方發給 ANILA 的 long-lived token（也可選 OAuth Client Credentials）
- **User Impersonation**：透過 header 帶真正使用者；對方 RBAC 仍以此 user 為準（Service token 不取代使用者授權）
- **Trace ID**：跨系統 correlation，對方需寫進 audit log

---

## 1. 通用約束

### 1.1 Response Envelope（所有回應一致）

```json
{
  "data":  <T> | <T[]> | null,
  "meta":  {} | null,
  "error": null | {
    "code":     "E_XXX",
    "message":  "user-facing 訊息",
    "details":  {},
    "trace_id": "01HXY..."
  }
}
```

成功 → `error: null`；失敗 → `data: null`。

### 1.2 Headers

| Header | 必要 | 說明 |
|---|---|---|
| `Authorization: Bearer <token>` | ✅ | ServiceCredential |
| `X-Impersonate-User-Id` | ✅ | 真正使用者 ID（對方 RBAC 用） |
| `X-ANILA-Trace-Id` | ✅ | ULID，audit correlation |
| `X-ANILA-Conversation-Id` | ✅ | 同對話多筆呼叫共用 |
| `Idempotency-Key` | POST/PATCH/DELETE 必要 | ULID，去重 |
| `traceparent` | 可選 | W3C trace context |

### 1.3 Idempotency 規則

- 對方需保留 `Idempotency-Key` ≥ **24 小時**
- 同 key + 同 body → 回原結果，加 `meta.idempotent_replay: true`
- 同 key + 不同 body → `422 E_IDEMPOTENT_MISMATCH`

### 1.4 時間 / 數值

- Timestamp：ISO 8601 UTC（`2026-04-27T08:30:15Z`），禁 local time
- Date：`YYYY-MM-DD`
- Money：string + currency（`{"amount": "1234.56", "currency": "TWD"}`），禁 float

### 1.5 Pagination

cursor-based，不用 offset：

```
GET /api/v1/leave?cursor=<opaque>&page_size=20
```

```json
"meta": {
  "page_size": 20,
  "next_cursor": "eyJpZCI6IjE4MCJ9",
  "prev_cursor": null
}
```

---

## 2. Endpoints

> Base path: `/api/v1/<resource>`，`<resource>` 為業務名詞（`leave` / `expense` / `purchase` ...）。
>
> 以下範例以 `leave`（請假）為例。

### 2.1 `GET /_health`

健康檢查，公開（不驗 token）。

```json
{
  "status":         "ok",
  "version":        "2026.04.27-1",
  "schema_version": "1.0.0",
  "server_time":    "2026-04-27T08:30:15Z"
}
```

`schema_version` 變動 → ANILA 端要 regression。

---

### 2.2 `GET /<resource>/schema`

回傳表單欄位定義 + 業務規則。Onyx 用這份做 deterministic validation 與多輪補欄。

```json
{
  "data": {
    "resource":       "leave",
    "schema_version": "1.2.0",
    "fields": [
      {
        "name":     "leave_type",
        "label":    "假別",
        "type":     "enum",
        "required": true,
        "options": [
          {"value": "ANNUAL",   "label": "特休"},
          {"value": "SICK",     "label": "病假"},
          {"value": "PERSONAL", "label": "事假"}
        ]
      },
      {"name": "start_date", "type": "date", "required": true,
       "constraints": {"min": "today", "max": "today+365d"}},
      {"name": "end_date",   "type": "date", "required": true,
       "constraints": {"min": "start_date"}},
      {"name": "reason", "type": "text",
       "required_when": "leave_type == 'PERSONAL'",
       "constraints": {"max_length": 500}},
      {"name": "attachment", "type": "file",
       "required_when": "leave_type == 'SICK' AND duration_days >= 3",
       "constraints": {
         "mime_types": ["image/jpeg","image/png","application/pdf"],
         "max_size_mb": 10
       }}
    ],
    "computed_fields": [
      {"name": "duration_days",
       "formula": "business_days_between(start_date, end_date)"}
    ],
    "validation_rules": [
      {"id": "ANNUAL_BALANCE_CHECK",
       "expr": "leave_type != 'ANNUAL' OR duration_days <= user.annual_balance",
       "message": "特休餘額不足"}
    ]
  },
  "meta": null,
  "error": null
}
```

**型別 enum**：`string` / `text` / `int` / `decimal` / `bool` / `date` / `datetime` / `enum` / `file` / `array` / `object`

**`required_when` / `expr` 運算子限定**：`==` `!=` `>` `>=` `<` `<=` `AND` `OR` `IN`

---

### 2.3 `GET /users/me`

取得 impersonated user 資料 + 餘額 + 權限。

```json
{
  "data": {
    "user_id":           "EMP00123",
    "display_name":      "王小明",
    "email":             "ming@example.org",
    "department_code":   "IT",
    "manager_user_id":   "EMP00045",
    "balances": {
      "annual_leave_days": 14.5,
      "sick_leave_days":   30.0
    },
    "permissions": ["leave.apply", "expense.submit"]
  }
}
```

`permissions` 是 ANILA 第一道 deterministic check（無權限直接拒絕，省 round-trip）。

---

### 2.4 `GET /<resource>/options/<field>`

動態下拉選項（簽核樹、出差地、報銷類別…）。支援 query string 過濾（依 schema 的 `depends_on`）。

```
GET /api/v1/leave/options/approver_chain?leave_type=ANNUAL&duration_days=3
```

```json
{
  "data": {
    "field_name": "approver_chain",
    "options": [
      {"sequence": 1, "user_id": "EMP00045", "display_name": "張主管", "role": "DIRECT_MANAGER"},
      {"sequence": 2, "user_id": "EMP00010", "display_name": "李處長", "role": "DEPT_HEAD"}
    ]
  }
}
```

---

### 2.5 `POST /<resource>/validate`

預驗證，**無副作用**。永遠回 `200`，把錯誤放在 `violations` 而非 HTTP error。

**Request**:
```json
{"leave_type": "ANNUAL", "start_date": "2026-05-01", "end_date": "2026-05-03"}
```

**Response（OK）**:
```json
{
  "data": {
    "valid": true,
    "computed_fields": {"duration_days": 3},
    "warnings": [
      {"code": "W_TEAM_CONFLICT", "message": "同部門 5/2 已有 2 人請假"}
    ]
  }
}
```

**Response（NG）**:
```json
{
  "data": {
    "valid": false,
    "violations": [
      {
        "field":            "leave_type",
        "rule_id":          "ANNUAL_BALANCE_CHECK",
        "code":             "E_BUSINESS_BALANCE",
        "message":          "特休餘額剩 1.5 天，不足 3 天",
        "suggested_action": "改假別或縮短天數"
      }
    ]
  }
}
```

`warnings` = 能送但提醒；`violations` = 不能送。

---

### 2.6 `POST /<resource>` — Submit

真正送單。**必帶 `Idempotency-Key`**。

**Request**:
```http
POST /api/v1/leave HTTP/1.1
Authorization: Bearer eyJ...
X-Impersonate-User-Id: EMP00123
X-ANILA-Trace-Id: 01HXY7Q5K8X4N9P2RZJM7VW3BD
X-ANILA-Conversation-Id: conv_abc123
Idempotency-Key: 01HXY7Q5K8X4N9P2RZJM7VW3BD
Content-Type: application/json

{
  "leave_type":    "ANNUAL",
  "start_date":    "2026-05-04",
  "end_date":      "2026-05-05",
  "approver_chain": [
    {"sequence": 1, "user_id": "EMP00045"},
    {"sequence": 2, "user_id": "EMP00010"}
  ],
  "user_confirmed":    true,
  "user_confirmed_at": "2026-04-27T08:30:00Z"
}
```

**Response 201**:
```json
{
  "data": {
    "ticket_id":        "LV-2026-00187",
    "status":           "PENDING_APPROVAL",
    "current_approver": {"user_id": "EMP00045", "display_name": "張主管"},
    "submitted_at":     "2026-04-27T08:30:15Z",
    "tracking_url":     "https://leave.example.org/tickets/LV-2026-00187"
  }
}
```

**Response 200（idempotent replay）**:
```json
{
  "data":  { /* 同上 */ },
  "meta":  {"idempotent_replay": true, "original_submitted_at": "..."},
  "error": null
}
```

**規範**：
- `user_confirmed` 必為 `true` 才送（防 LLM 誤送）
- `user_confirmed: false` → `400 E_USER_NOT_CONFIRMED`

---

### 2.7 `GET /<resource>/<ticket_id>` — Status

```json
{
  "data": {
    "ticket_id": "LV-2026-00187",
    "status":    "APPROVED",
    "history": [
      {"at": "2026-04-27T08:30:15Z", "actor": "EMP00123", "event": "SUBMITTED"},
      {"at": "2026-04-27T09:45:00Z", "actor": "EMP00045", "event": "APPROVED_L1", "comment": "OK"},
      {"at": "2026-04-27T11:20:00Z", "actor": "EMP00010", "event": "APPROVED_FINAL"}
    ],
    "current_approver": null,
    "approved_at":      "2026-04-27T11:20:00Z"
  }
}
```

`status` enum：`PENDING_APPROVAL` / `APPROVED` / `REJECTED` / `CANCELLED` / `WITHDRAWN` / `EXPIRED`

---

### 2.8 `GET /<resource>?status=...&from=...&to=...&cursor=...` — List

cursor pagination。

```json
{
  "data": [
    {"ticket_id": "LV-2026-00187", "status": "APPROVED", "leave_type": "ANNUAL"},
    {"ticket_id": "LV-2026-00102", "status": "REJECTED"}
  ],
  "meta": {"page_size": 20, "next_cursor": "eyJpZCI6IjE4MCJ9"}
}
```

---

### 2.9 `DELETE /<resource>/<ticket_id>` — Cancel / Withdraw

```json
{
  "data": {
    "ticket_id":    "LV-2026-00187",
    "status":       "WITHDRAWN",
    "withdrawn_at": "2026-04-27T08:35:00Z"
  }
}
```

- 已 `APPROVED` → `409 E_FLOW_NOT_CANCELABLE`
- 別人的單 → `403 E_USER_FORBIDDEN`
- 已撤回的重打 → 200（冪等）

---

### 2.10 Webhook（可選但推薦）

對方主動 POST 到 ANILA：

```http
POST https://csp.anila.internal/api/webhooks/leave
Content-Type: application/json
X-Anila-Webhook-Signature: sha256=<HMAC>
X-Anila-Webhook-Event: leave.approved

{
  "ticket_id": "LV-2026-00187",
  "event":     "APPROVED",
  "at":        "2026-04-27T11:20:00Z",
  "trace_id":  "01HXY..."
}
```

- HMAC-SHA256 簽 body（secret 由 ANILA 提供）
- ANILA 5 秒內回 2xx；超時對方 retry（30s / 5min / 30min）

---

### 2.11 File Upload（如表單有附檔）

兩階段：

1. `POST /files/upload-url` → 拿 pre-signed URL
   ```json
   {
     "data": {
       "attachment_id": "att_abc123",
       "upload_url":    "https://s3.../...?signature=...",
       "expires_at":    "2026-04-27T08:45:00Z"
     }
   }
   ```
2. ANILA `PUT` 檔案到 `upload_url`
3. Submit 時帶 `attachment_id`

限制：
- pre-signed TTL ≥ 15 分鐘
- max size 10 MB（超過走 multipart）
- mime 白名單（schema `constraints.mime_types`）

---

## 3. Error Codes

**強制原則**：每個 4xx/5xx 必須有 `error.code`。**禁** `E_UNKNOWN` / `E_GENERIC`。

| Code | HTTP | 說明 | 對方何時用 |
|---|---|---|---|
| `E_AUTH_MISSING` | 401 | 缺 Authorization | header 沒帶 |
| `E_AUTH_INVALID` | 401 | Token 無效/撤銷 | |
| `E_AUTH_EXPIRED` | 401 | Token 過期 | |
| `E_AUTHZ_DENIED` | 403 | Service token 對該 endpoint 無權限 | |
| `E_USER_HEADER_MISSING` | 400 | 缺 `X-Impersonate-User-Id` | |
| `E_USER_NOT_FOUND` | 403 | impersonated user 不存在 | |
| `E_USER_FORBIDDEN` | 403 | impersonated user 對該操作無權限 | RBAC 拒絕 |
| `E_USER_NOT_CONFIRMED` | 400 | `user_confirmed != true` | |
| `E_USER_VALIDATION` | 422 | 欄位格式 / 必填 / 範圍錯 | request body 爛 |
| `E_BUSINESS_*` | 422 | 業務規則違反 | 餘額不足、衝突 |
| `E_FLOW_DUPLICATE` | 409 | 同單據已存在 | |
| `E_FLOW_NOT_CANCELABLE` | 409 | 已通過、不可撤回 | |
| `E_FLOW_STATE_INVALID` | 409 | 當前狀態不允許此操作 | |
| `E_IDEMPOTENT_MISMATCH` | 422 | 同 Key 不同 Body | |
| `E_RATE_LIMIT` | 429 | 限流（帶 `Retry-After`） | |
| `E_SYSTEM_DEPENDENCY` | 503 | 下游系統壞 | DB/SSO 連不上 |
| `E_SYSTEM_TIMEOUT` | 504 | 內部超時 | |
| `E_SYSTEM_INTERNAL` | 500 | 預期外的錯 | bug，會被監控告警 |

**錯誤回應範例**：
```json
{
  "data":  null,
  "meta":  null,
  "error": {
    "code":     "E_BUSINESS_BALANCE",
    "message":  "特休餘額剩 1.5 天，不足 3 天",
    "details":  {"field": "leave_type", "available": "1.5", "requested": "3.0", "unit": "days"},
    "trace_id": "01HXY..."
  }
}
```

---

## 4. End-to-End 範例：請假流程

對話腳本：
```
User:  「我下週一請特休 3 天」
Onyx:  「請問是 5/4-5/6 嗎？」
User:  「對」
Onyx:  「您餘額 1.5 天不夠請 3 天，要改別種還是縮短？」
User:  「改 5/4-5/5 兩天就好」
Onyx:  「整理：特休 5/4-5/5，簽核 張主管 → 李處長，送出？」
User:  「送」
Onyx:  「已送出，單號 LV-2026-00187」
```

對應 API 序列：

| # | 階段 | API |
|---|---|---|
| 1 | 對話開始 | `GET /_health` |
| 2 | 取使用者 | `GET /users/me` |
| 3 | 取 schema | `GET /leave/schema` |
| 4 | 取簽核樹 | `GET /leave/options/approver_chain?leave_type=ANNUAL&duration_days=3` |
| 5 | 預驗證 (3 天) | `POST /leave/validate` → `valid: false`, balance 不足 |
| 6 | 預驗證 (2 天) | `POST /leave/validate` → `valid: true` |
| 7 | 使用者確認 | (Onyx 內部，設 `user_confirmed: true`) |
| 8 | 送單 | `POST /leave` + `Idempotency-Key` → `201` |
| 9 | (背景) 簽核 | Webhook `POST /webhooks/leave` 推 `APPROVED` |
| 10 | 查詢 | `GET /leave/LV-2026-00187` → `status: APPROVED` |

---

## 5. 安全與限流（簡）

| 項 | 要求 |
|---|---|
| TLS | 1.2+，公司 PKI |
| Credential rotation | 兩把並存 ≥ 7 天，可滾動更新 |
| Rate limit | per-credential 100 RPS（帶 `X-RateLimit-*` headers + `Retry-After`） |
| IP allowlist | 對方 SHOULD 限定 ANILA 出口 IP |
| Log redact | `Authorization` header 必須 redact |
| Audit | mutating call 寫 audit + 含 `trace_id` |

---

## 6. 不得不問清楚的 5 件事（會議用）

1. **Idempotency-Key 能不能支援？** 不能 → 後面都白談。
2. **能不能 user impersonation？** 對方 RBAC 怎麼接受 ANILA 帶來的 user？
3. **Schema endpoint 要新做還是已有？** 若無，誰來寫業務規則？(對方寫 / ANILA 寫死)
4. **驗證規則寫在 API 裡（`/validate` deterministic）還是要 ANILA 自己解？** 強烈建議前者。
5. **Webhook 還是只能 polling？** Polling 可，但對話 UX 會差。

---

## Appendix — OpenAPI Skeleton

對方可從這份起點擴充：

```yaml
openapi: 3.0.3
info:
  title: Target System API for ANILA Onyx
  version: 1.0.0
servers:
  - url: https://target-staging.example.org/api/v1
  - url: https://target.example.org/api/v1

security:
  - serviceCredential: []

components:
  securitySchemes:
    serviceCredential:
      type: http
      scheme: bearer
  parameters:
    ImpersonateUser:
      name: X-Impersonate-User-Id
      in: header
      required: true
      schema: {type: string}
    TraceId:
      name: X-ANILA-Trace-Id
      in: header
      required: true
      schema: {type: string}
    IdempotencyKey:
      name: Idempotency-Key
      in: header
      required: true
      schema: {type: string}
  schemas:
    Envelope:
      type: object
      required: [data, meta, error]
      properties:
        data:  {nullable: true}
        meta:  {type: object, nullable: true}
        error:
          type: object
          nullable: true
          properties:
            code:     {type: string}
            message:  {type: string}
            details:  {type: object}
            trace_id: {type: string}

paths:
  /_health:
    get:
      security: []
      responses: {'200': {description: OK}}

  /leave/schema:
    get:
      parameters: [{$ref: '#/components/parameters/ImpersonateUser'}]
      responses: {'200': {description: OK}}

  /leave/validate:
    post:
      parameters: [{$ref: '#/components/parameters/ImpersonateUser'}]
      responses: {'200': {description: OK}}

  /leave:
    post:
      parameters:
        - {$ref: '#/components/parameters/ImpersonateUser'}
        - {$ref: '#/components/parameters/TraceId'}
        - {$ref: '#/components/parameters/IdempotencyKey'}
      responses:
        '201': {description: Created}
        '422': {description: Validation/Business error}

  /leave/{ticket_id}:
    get:    {summary: Status query}
    delete: {summary: Withdraw}
```

---

**Last updated**: 2026-04-27
