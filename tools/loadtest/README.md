# 平台負載（不進映像）

量的是平台自己：nginx → CSP → 假模型。模型主機不是我們的。假模型每秒 30 個 token、每則 400 token、第一個 token 前等 0.3 秒；預設每 10 個 token 一個 SSE 框，避免 3000 條串流把單一 Python process 用喚醒次數打滿。時間到第一個 token（TTFT）含排隊。

只打隔離的 dev stack（`compose.dev.yaml`，nginx `:8443`）。不會啟動或重建線上那套。資料庫用獨立的 `loadtest-pgdata2`，不碰工作站上既有的 dev volume。簽章金鑰寫在 `tools/loadtest/.jwt/`（不進版控），掛進 CSP 讓空的金鑰圈能做第一次匯入。

```bash
tools/loadtest/run.sh
```

可加 `--levels 300` 只跑一檔。帳密不印出來：先看環境變數 `ANILA_LOADTEST_PASSWORD`，否則看 dev CSP 的 `ADMIN_PASSWORD`，最後才用 `changeme`。

一檔是同時送出那麼多條串流，然後等它們結束。400 token、每秒 30 個，一則大約 13 秒；要看到 3000 條同時在線，必須在第一則結束前把 3000 條都送出去，所以 `/v1/` 的 burst 是 4000 且 `nodelay`。
