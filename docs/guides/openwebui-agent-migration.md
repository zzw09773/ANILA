# OpenWebUI Agent 遷移指南（Phase 0：人工盤點）

> 對應設計文件：`docs/anila-redesign-docs/06-openwebui-agent-migration-and-registration.md`
> 與 `docs/anila-redesign-docs/05-agent-registry-and-runtime-protocol.md`。
> 本指南只涵蓋 **Phase 0（人工盤點）**；Phase 1 之後的 shadow 註冊與 Full Trace
> 準入由 CSP Agent Registry（`/api/agents`）承接。

## 為什麼是人工盤點

ANILA 是 air-gapped 內網平台，repo 內**沒有** ML Team OpenWebUI 的 agent
清單、endpoint 匯出或 DB schema 範例。已拍板（2026-07-02）：

- **不做**自動匯出 / import 工具、不做 `import-openwebui` CLI、不做 Pipe 自動
  bridge / sidecar（doc 06 §5/§7、doc 10 Slice 5「不做」清單）。
- 既有 OpenWebUI Pipe agent **一律人工重新註冊**，走既有 L1 路徑：
  `/developer/agents` 精靈或 `anila-core` CLI `register` → 接上派工 JWT 驗簽
  （三級制，見 `docs/guides/developer-guide.md`）→ 健康／trace 準入 → admin 核准。
  **不核發 `csk-`／任何長效 agent 祕密。**

因此 Phase 0 是「拿一張表把現況盤點清楚」，是整個遷移的唯一入口。

## 盤點表（10 欄，逐字對齊 doc 06 §Phase 0）

範本檔：[`openwebui-agent-inventory-template.csv`](./openwebui-agent-inventory-template.csv)

| 欄位 | 說明 |
|---|---|
| `openwebui_name` | agent 在 OpenWebUI 內的名稱 |
| `owner` | 負責人（姓名或員編） |
| `department` | 所屬部門 |
| `current_endpoint` | 目前對外服務的 endpoint（若有 HTTP endpoint） |
| `runtime` | runtime 種類：`Pipe` / `Function` / `LangChain` / `custom` |
| `model_usage` | 使用的底層模型（如 `gpt-oss-20b`） |
| `tools` | 使用的工具（多個以 `;` 分隔） |
| `data_sources` | 資料來源 / 綁定的 collection |
| `sensitivity` | 機敏程度（四級分類：`無機密` / `營業秘密` / `密` / `機密`） |
| `migration_level` | 目標註冊等級：`L1`（Proxy-compatible）/ `L2`（Run Protocol）/ `L3`（Full Trace） |

> v1 正式 policy：只有 **L3 Full Trace** 可進正式任務（低機敏 L2 例外）。
> `migration_level` 先如實填現況目標，實際準入由 CSP trace-test 把守。

## 盤點完成後（銜接 Phase 1）

1. 依盤點表逐一在 CSP 註冊（`POST /api/agents/register`；`base_model` 填模型「名稱」即可，
   CSP 會解析成 id）。⚠ `shadow=true` 已無作用：OE-1 之後沒有 `draft` 狀態，
   欄位仍被接受但一律落地 `registered`，不要當成「先暫存不上線」的開關。
2. 在 agent 側接好派工 JWT 驗簽（樣板／`anila_verify.py`；`.env` 只放
   `CSP_BASE_URL`＋`ANILA_CA_FILE` 等非祕密設定，**不貼長效服務憑證**）。
3. `POST /api/agents/{id}/test-connection` 驗連線——現以派工 JWT 探測（與 live dispatch 同一簽發路徑，用來驗證 JWKS 接線）。
4. `POST /api/agents/{id}/trace-test` 跑 Full Trace 準入測試 —— 全部 required 項
   通過才會把狀態推進到 `pending_security_review`（任務內 trace 回呼複用派工 JWT）。
5. Admin 於安全審查關卡核准（`POST /api/agents/{id}/approve`）。**未通過
   trace-test 的 Agent 一律無法核准為正式使用**（hard block，無 grandfather）。
