# 錯誤呈現修復報告

結論：已完成 Router 503 與 shell SSE 非成功 response 的錯誤呈現修復；可行動句保留，原始下游細節改留在既有 log，JSON 不再直接成為聊天錯誤文字。

## 實作結果

- [已驗證：`git diff --check` exit 0、無輸出] `services/anila-core-router/main.py:340` 先以既有 `anila-router` logger 記錄 `_ensure_primary()` 的內部錯誤；`services/anila-core-router/main.py:347`–`:350` 只回傳完整可行動句，不再把 downstream body 拼進 `detail`。
- [已驗證：Router guard ASGI smoke exit 0，輸出 `router guard ASGI smoke: passed (503 actionable detail, downstream body logged)`] response status 為 503、response detail 完整保留「前往 CSP Models 指定主路由」句子、原始下游 JSON 不在 response，且出現在 logger handler；行為覆蓋 `services/anila-core-router/tests/test_primary_gate.py:72`–`:86`。
- [已驗證：`npm test` exit 0，54 test files / 672 tests passed；`npm run build` exit 0，2642 modules transformed] `apps/anila-shell/src/runtime/sse.js:32`–`:47` 依 JSON 結構讀取 `detail`；JSON 缺少 `detail` 時使用 fallback，plain text 原樣保留，empty body 維持既有 fallback；兩個非成功路徑位於 `apps/anila-shell/src/runtime/sse.js:152`–`:156` 與 `:437`–`:441`。
- [已驗證：`npm test` 的 `src/__tests__/sse.test.js` 顯示 34 tests passed] 四種 body shape 與 chat/resume 兩個 transport 都由 `apps/anila-shell/src/__tests__/sse.test.js:393`–`:441` 以實際 thrown `Error.message` 驗證，沒有依賴後端文案比對。

## Mutation

- [已驗證：`cd apps/anila-shell && node scripts/mutation-check.mjs backend-error-text-swallowed` exit 0] 既有 mutation `backend-error-text-swallowed`：新測試抓到 1/1，既有測試抓到 1/1。這次沒有新增 mutation entry。

## Router backend verification

- [未驗證：`cd services/anila-core-router && PYTHONPATH=../../packages/anila-core/src pytest`] pytest 收集到 9 tests，但第一個 `TestClient` request 沒有完成；以 Ctrl-C 中止，未宣稱通過。
- [未驗證：`PYTHONPATH=../../packages/anila-core/src timeout 20s pytest -vv -s tests/test_primary_gate.py::test_clean_request_is_gated`] exit 124；輸出停在 collected 1 item 後的 `test_clean_request_is_gated`。診斷用最小 FastAPI `TestClient` request 亦在 `timeout 5s` 後 exit 124，停在 `before` / `client` 之後，顯示是本機 TestClient/portal 等待問題，不是本次 assertion 的失敗證據。
- [已驗證：`httpx.AsyncClient(ASGITransport(app=main.app))` inline component smoke exit 0] 本次 503 response/log 行為已在不經 TestClient 的同一 Router app 上實際驗證；這不能取代上列 pytest，因此仍保留 pytest verification gap。

## Survey（只報告，不修補）

- [已驗證：`rg -n -C 2 'resp\\.text|JSONResponse|_primary_state\\["error"\\]|primary_status' services/anila-core-router --glob '*.py'`] `services/anila-core-router/main.py:298` 把 `resp.text[:200]` 放入 `_primary_state["error"]`，並由 `services/anila-core-router/main.py:362` 的 `/router/primary-status` JSON 回傳；這對該 debug endpoint consumer 是同型 raw-body exposure，但不是本次聊天 503 路徑，依 survey 要求未修改。
- [已驗證：同一 survey grep] `services/anila-core-router/main.py:303` 只把 `resp.text[:200]` 送入既有 logger，不是 user-visible JSON content，因此不是同一缺陷；未修改。
- [已驗證：同一 survey grep 與 `git diff`] `services/anila-core-router/main.py:344` 的 503 `JSONResponse` 已不再使用 downstream body；這是本次修復位置，不列為其他未修補命中。

## Scope / non-goals

- [已驗證：最終提交前 `git status --short --branch` 只列出本次 Router/SSE 程式與測試及 `REPORT.md`，且 `BRIEF.md` 已不存在] 未修改成功串流、billing/usage、auth/SSRF/JWT、日期格式或 survey 命中點。
- [已遵守：本回合未執行 `git checkout`、`git switch`、`git push`、Docker Compose 或 full-stack E2E] 未觸碰 live deployment。
- [未完成：Router 原始 pytest suite 尚未取得完整通過結果] 原因與替代 component smoke 證據如上；這是目前唯一未閉合的 scope verification gap。
