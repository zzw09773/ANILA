# FLUX 主圖像模型：前端設定 + 消費端自動取用（image-primary）

日期：2026-07-06 ｜ 狀態：已批准（方案 1）｜ 作者：Claude（user 拍板）

## 目標

管理者在治理前端（ModelsView）註冊雲端 FLUX 模型並標記為「主圖像模型」後，flux2-dev-agent 與 anila-studio **不重啟**即自動改用新的端點與模型名（60 秒內生效）。API key 不進前端、不經 csp 下發，維持各服務 env 管理。

## 非目標

- 圖像請求資料面**不經 csp proxy**（維持直連雲端 FLUX；集中計量/稽核留待第二階段）。
- key 管理不變（`FLUX_API_KEY` env）。
- 不動 flux2-dev-agent 對 csp 曝露的 `/v1/chat/completions` 假串流契約。

## 架構（完全複製既有 router-primary 模式）

```
ModelsView（admin 註冊 image 模型、按「設為主圖像模型」）
      │ admin JWT
      ▼
csp ModelRegistry（is_image_primary 旗標，partial unique index）
      │ GET /api/models/image-primary（X-CSP-Service-Token；回 name/endpoint_url/api_version，不回 key）
      ▼
flux2-dev-agent ─┐  60s TTL 快取 fetcher（比照 anila-core-router _refresh_primary）
anila-studio    ─┘  取 (endpoint, model)；404/連線失敗 → fallback env
```

## 元件細節

### 1. csp（services/csp）

- **Migration**（接續 0023）：`model_registry` 加 `is_image_primary BOOLEAN NOT NULL DEFAULT FALSE` + partial unique index（`WHERE is_image_primary`），完全比照 `is_router_primary` 的既有 migration 寫法。
- **端點**（`app/api/models.py`，照抄 L202-295 router-primary 三件組）：
  - `POST /api/models/{id}/set-image-primary`：`require_admin`；限 `model_type == "image"`；限 `is_active`；先清舊 primary 再設；audit log。
  - `POST /api/models/{id}/unset-image-primary`：`require_admin`；audit log。
  - `GET /api/models/image-primary`：`verify_service_token`；未設回 404、已停用回 409；回 `{id, name, display_name, model_type, endpoint_url, api_version, health_status}`——**不含 key**。
- **model_type**：欄位是自由字串（String(20)），後端無 enum 約束；`image` 為新慣例值，同步更新 `app/models/model_registry.py:13` 與 `app/schemas/model_registry.py` 的註解。
- 註冊時既有 `validate_outbound_url`（models.py:63）SSRF 驗證自動生效，無需新碼。

### 2. 前端（apps/csp-governance-ui/src/views/ModelsView.vue）

- `model_type` 選單加 `image`。
- image 類型列加「設為主圖像模型」／「取消主圖像」動作與 primary pill——UI 樣式、確認流程、API 呼叫完全比照既有 router-primary 按鈕。
- 驗收：`npm run build` 過。

### 3. flux2-dev-agent（services/flux2-dev-agent）

- 新增 image-primary fetcher：60s TTL 快取 + asyncio lock，逐請求呼叫（生圖前取值），比照 `services/anila-core-router/main.py` `_refresh_primary` 的結構（含 401/403 時 token 熱重載——若該服務有 rotating token 機制才做，否則單純讀 env token）。
- 認證：`X-CSP-Service-Token`。**實作時查現況**：flux2-dev-agent 若已有 csp service token 機制則重用；沒有則新增 env `CSP_SERVICE_TOKEN`。
- `FluxClient`：endpoint 或 model 變更時重建（或改為 per-request 傳入）；`aspect_ratio→size` 對映與 b64 驗證邏輯不變。
- Fallback：csp 回 404（未設定）或連線失敗 → 用 env `FLUX_BACKEND_URL`/`FLUX_MODEL`；兩者皆無 → 回明確錯誤。
- key：一律 env `FLUX_API_KEY`。

### 4. anila-studio（services/anila-studio）

- 同款 fetcher（60s TTL、404/失敗 fallback env、endpoint 變更重建 provider 的 httpx client）。
- 認證同上：查現況重用 service token 機制，否則加 env。
- 既有語意保留：完全沒有端點（csp 沒設定且 env 空）＝ FLUX 停用。

## 錯誤處理

| 情境 | 行為 |
|---|---|
| csp 未設 image-primary（404） | fallback env；log 一次（別每 60 秒刷屏） |
| csp 連不上/timeout | 沿用上次快取值；快取也沒有 → fallback env |
| primary 模型被停用（409） | 視同 404 fallback |
| service token 無效（401/403） | 有 rotating 機制則熱重載重試一次，否則 fallback env + warning |
| 端點熱切換 | TTL 到期後下一次生圖用新端點，httpx client 重建 |

## 測試

- csp：set/unset/get 三端點；非 image 類型 set → 400；無 service token → 401；未設 → 404；停用 → 409；旗標唯一性（設新的自動清舊的）。
- flux2-dev-agent / studio：TTL 快取（60 秒內不重打 csp）；404→fallback env；csp 值優先於 env；端點切換後 client 重建打新 URL。全部 fail-then-pass。
- 前端：`npm run build`。

## 部署備註

- 消費端各需 service token env（名稱依實作時現況定），部署腳本/compose 對應補上。
- 雲端 FLUX 的 key 仍填 `FLUX_API_KEY`（agent 與 studio 各自的 env）。
