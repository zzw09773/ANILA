# anila-shell 本機驗證

`e2e/functions.spec.js` 已不存在，也不要把它當現行 Playwright 套件。
本目錄目前沒有可跑的 Playwright spec，也沒有接到 GitHub CI。

主流程（登入態 → 送出 → 串流 → 存檔 → 重整回讀）用 Vitest orchestrator 重跑：

```bash
cd apps/anila-shell
npm test -- src/__tests__/orchestratorSend.test.jsx src/__tests__/orchestratorConversations.test.jsx src/__tests__/orchestratorFailures.test.jsx
```

這些測試掛的是真的 `App` + `AuthProvider`，假後端回答 `/api/auth/me` 與 `/v1/chat/completions`。
composer 用 accessible name「傳訊息給 ANILA」定位，不要再用舊 placeholder「問 ANILA 任何事情」。
