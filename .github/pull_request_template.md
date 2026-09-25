# PR 收斂檢查（doc 10 §13）

> 准入依據：規格＝`SYSTEM-MAP.md`（系統應該長什麼樣）、現況與執行順序＝`PLAN.md`（專案權威）。任一項違反須附 ADR。
>
> ⚠ 2026-08-17 起本清單改對照 `SYSTEM-MAP.md` / `PLAN.md`。原憲法
> `docs/anila-redesign-docs/00-product-constitution.md` 已列為 redesign 收斂期的**歷史紀錄**，
> 仍可查閱各項條款的由來，但不再是准入依據。

- [ ] 這個變更屬於：任務中心 / 我的知識庫 / 產出中心 / 專案入口 / 治理中心
- [ ] 沒有新增未核准一級入口或產品名稱
- [ ] 若涉及模型，已走 Model Registry + CSP Proxy
- [ ] 若涉及 Agent，已走 Agent Registry（三態：registered / approved / disabled；無七態、無 trace-test 閘門）
- [ ] 若涉及 GUI Service，已走 Service Registry + Launch Contract
- [ ] 若涉及知識來源，已建立 SourceSnapshot / Citation
- [ ] 若涉及 Artifact，已綁定 Task / SourceSnapshot
- [ ] 若涉及分類，已處理 classification propagation
- [ ] 若涉及降級，已走 Admin request + supervisor approval（雙人原則，申請人≠核准人）
- [ ] 有 PolicyDecision / AuditEvent（無 Full Trace span 收攏關卡）
- [ ] 不繞過 card SSO / JWT / CSRF / RLS / SSRF guard
- [ ] 有 contract test 或 migration test
- [ ] 前端新增字串為繁體中文（台灣用語），無簡體字 / 大陸用語（doc 11）
- [ ] 若違反任一項，已附 ADR
