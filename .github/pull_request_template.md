# PR 收斂檢查（doc 10 §13）

> 憲法：`docs/anila-redesign-docs/00-product-constitution.md`。任一項違反須附 ADR。

- [ ] 這個變更屬於：任務中心 / 我的知識庫 / 產出中心 / 專案入口 / 治理中心
- [ ] 沒有新增未核准一級入口或產品名稱
- [ ] 若涉及模型，已走 Model Registry + CSP Proxy
- [ ] 若涉及 Agent，已走 Agent Registry + Full Trace
- [ ] 若涉及 GUI Service，已走 Service Registry + Launch Contract
- [ ] 若涉及知識來源，已建立 SourceSnapshot / Citation
- [ ] 若涉及 Artifact，已綁定 Task / SourceSnapshot
- [ ] 若涉及分類，已處理 classification propagation
- [ ] 若涉及降級，已走 Admin request + supervisor approval（雙人原則，申請人≠核准人）
- [ ] 有 PolicyDecision / AuditEvent / TraceSpan
- [ ] 不繞過 card SSO / JWT / CSRF / RLS / SSRF guard
- [ ] 有 contract test 或 migration test
- [ ] 前端新增字串為繁體中文（台灣用語），無簡體字 / 大陸用語（doc 11）
- [ ] 使用者可見變更已進 changelog？（`apps/anila-shell/src/changelog.jsx`，並 bump `CHANGELOG_VERSION`；W1-9：這份清單曾凍結一個半月、三批功能無人被通知）
- [ ] 使用者可見的字面與實作一致？（W1-3：不得再出現「UI 說有、實作沒有」——例如把單向密等鎖定講成加密、或在功能旗標關閉時承諾功能）
- [ ] 若違反任一項，已附 ADR
