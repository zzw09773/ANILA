# PR 檢查

現況以 `docs/CURRENT-STATUS.md` 與程式為準。

- [ ] 沒有提交 `.env`、`secrets/`、憑證私鑰
- [ ] 沒有繞過 card SSO、JWT、CSRF、RLS、SSRF guard
- [ ] schema 改動有 Alembic migration
- [ ] 改過的套件測試有跑
- [ ] 介面文字是繁體中文（台灣用語）
- [ ] `anila-studio` 沒有 import `anila_core`
- [ ] 產品程式沒有呼叫院外服務
