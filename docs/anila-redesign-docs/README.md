# ANILA Redesign Docs Package

這個資料夾包含 12 份可放入新專案 `/docs` 的系統設計檔案。

> **版本狀態：`architecture-baseline-v0.2`（freeze candidate，2026-07-02）**
> v0.1 複核（對碼查核＋三裁決）後，已完成 v0.2 修訂（6 個 blocking issue＋4 項編輯性修正）：
> ① doc10 Slice 5 移除 OpenWebUI 自動匯入交付項（與 doc06 拍板對齊）；② doc01 role 加 `system`（internal-only）；
> ③ doc04 production model endpoint HTTPS＋API Key 升為硬規則（`ALLOW_HTTP` 不適用於正式模型；旗標按端點類型分域列為目標工程項）；
> ④ doc05/09/10 統一標記 Full Trace P0 gap（approval blocker，非 enhancement；approval_status 拆七值）；
> ⑤ doc07 `config_source` 解 env seed vs DB ownership；⑥ doc08 backfill=floor＋Classification Inventory Before Cutover；
> 另：doc09 route 相容表、doc03 csk 對外命名（Agent Integration Key／Service Client Token）、
> doc00 治理中心標 admin-facing、doc02/10 MVP module boundary 決策（Task/Policy/Launch 先在 CSP 內）。
> 另新增 doc11：前端全繁體中文（台灣用語）語言政策（✅ 已拍板：唯一介面語言；含術語表、豁免清單、CI lint、P1–P5 遷移序；現況實測：ANILA_UI 76%／ANILALM 70% 繁中、CSP 治理中心 77% 英文為最大改造面）。

> 取證基準：現況（Repo evidence）以 `origin/prod-intranet-card`（v1.2.0 系）為準；本機工作樹與其分歧時以 origin 為準。目標設計與現況相左時，一律以目標為準，現況段落僅作遷移起點對照。

## 複核狀態（2026-07-02）

- 已完成全 11 份文件對碼複核（344 條宣稱，85% 確認）並套用修訂：現況錯誤已按 origin 修正、目標項一律標「目標新增」、補齊重大遺漏章節（TLS/CSPKI、auth 契約、SSE 分層、分類 enforcement 現況、內網部署工具鏈等）。
- 複核報告：`docs/audits/anila-redesign-docs-review-2026-07-02.md`。
- 三項裁決已拍板（2026-07-02，文件內以「✅ 已拍板」標記）：
  1. OpenWebUI：遷移目標保留，一律以既有精靈/CLI **手動重新註冊**；自動化匯出匯入橋接不列入範圍（`00` ADR-0003、`06` 文首）。
  2. Service Admin 邊界：**保留** admin/owner 全域 bypass，per-service admin 是授權下放、非排他邊界；跨界操作入 audit（`03` §6、`07` §12）。
  3. 降級審批：雙人原則（申請人≠核准人），**系統內無自我核准碼路徑（變體 A）**——最高權責者（如院長）例外以紙本核定＋持權責者代錄實作（附公文文號）；**核准權與平台角色脫鉤**——平台 owner/admin 為技術角色，未持有「機密審批權責」指派者不得核准，指派本身有信任錨（核定文號＋雙人控制＋異動公告＋定期覆核）；無權責者可核准時 fail-closed 維持 pending（`08` §7/§8/§12）。

## Files

1. `00-product-constitution.md`
2. `01-domain-model.md`
3. `02-system-architecture.md`
4. `03-csp-governance-control-plane.md`
5. `04-model-gateway-design.md`
6. `05-agent-registry-and-runtime-protocol.md`
7. `06-openwebui-agent-migration.md`
8. `07-registered-gui-service-platform.md`
9. `08-classified-latch-and-policy-engine.md`
10. `09-api-event-contracts.md`
11. `10-migration-and-development-guardrails.md`
12. `11-frontend-zh-tw-language-policy.md`

## Notes

- 本版保留已確認決策：
  - 分類等級：無機密 / 營業秘密 / 密 / 機密。
  - 上鎖後僅 Admin 可申請降級，且需主管批核。
  - 模型僅考慮院內不同主機，HTTPS + API Key。
  - OpenWebUI 不是正式入口、不是 legacy host，只是目前 Agent 暫存註冊場。
  - Agent 正式接入要求 Full Trace。
  - 其他小組 GUI Service 可用院內憑證卡 SSO、自管資料、iframe、作為專案入口。
  - 有 Admin 權限者可自行上架 GUI Service。
- 本版已補入 repo evidence：
  - `00`-`02`：補上產品入口、domain model、compose / nginx / Router / `anila-core` / Studio / ingestion worker 現況。
  - `03`-`06`：補上 CSP audit / service token、model gateway、Agent Registry、OpenWebUI migration 現況。
  - `07`-`09`：補上 GUI Service、classified latch、API / SSE / Studio / ingestion response contract 現況。
  - `10`：補上可搬移檔案 mapping、可重用測試、Alembic baseline 風險、prod deploy script 可移植性。
