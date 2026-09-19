# 交接：CSP 調整包（2026-09-19 收尾）

> 這份是給下一個 session 的接手 prompt。基準：`main @ 9ef47d7a`，成果全在工作樹、**未 commit**。

## 現況一句話

這一批 CSP 調整**已完成並驗證**：CSP 全套 `3085 passed / 0 failed`（同一棵樹的基準是 51 failed，淨減 51、零新增）。
唯一未結項是 `packages/anila-core` 的 118 條**既有**失敗（乾淨 HEAD 同樣 118），與本包無關。

## 已完成（不要重做、不要重驗）

- 清單與各包證據：`docs/office/2026-09-19-csp-adjustments.md`
- 50 條既有失敗的處置與分類（每一條都經指揮官複核 diff）：
  - 10 條＝host 缺套件（`bcrypt==5.0.0`、`opencc-python-reimplemented==0.1.7`）；裝回 requirements 釘的版本後全過
  - 36 條＝`services/csp/tests/test_institutional_kb_injection.py` fixture 沒跟上 Router 權限契約（`X-ANILA-Route` 會走 `user_can_use_router_model`，需 `router_enabled` + active `RouterModelGrant`）
  - 5 條＝`infra/docker/n8n.Dockerfile`、`infra/docker/nginx.Dockerfile`、`services/anila-core-router/Dockerfile` 缺同層 DCS 清理
  - 4 條＝CSV/proxy 契約漂移、`url_guard` 的 trusted-hosts 與 structural deny 語意
  - 2 條＝`tests/test_image_copy_file_modes.py` 的 checker 祖先判定收斂
- 另有本輪功能/誠實化改動：模型 create 契約補欄位、audit fail-closed、知識庫升密 UI 講明、大小寫族收尾（含 migration `r1_0043`）、健康與告警誠實化、單位管理員範圍、cookie 收斂、前端吞錯三態。
- 驗證狀態：CSP 3085 passed；shell 951 passed；治理中心 37 passed／0 fail；兩個 npm build 過；worker 37 passed；core 36 passed。

## 未結項（下一手做這個）

1. `packages/anila-core` 的 118 條既有失敗。**乾淨 HEAD 同樣 118 條**，不是本包造成。
   集中檔：test_router_forced_retry(45)、test_router_direct_header(39)、test_router_session(10)、test_router_resume_proxy(5)、test_router_trace_spans(4)、test_router_streaming_multi_turn(4)…
   已知症狀：`create_router_app()` 的 `app.routes` 裡看得到 `/v1/chat/completions`，但 TestClient POST 回 starlette 預設 404 `{"detail":"Not Found"}`；已確認路由與 method 都對，還沒定位到 404 來源。
   建議照本輪同一原則：先判定「產品錯 vs 測試/環境假設」，再改對應那一側。
2. 決定要不要把本包切 commit（建議按包邊界切，不要一個 commit 塞 84 檔）。

## 環境陷阱（會咬人）

- 沙箱 `network: restricted`：**所有用 `client` fixture（TestClient）的 pytest 會假死**，不是程式壞。要用 `sandbox_permissions: require_escalated` 跑；同一組測試沙箱內約 20 分鐘、沙箱外 7.9 秒。看到卡在 `tests/conftest.py:155 TestClient` 就是這個。
- 這台 `anila_core` 的 editable 安裝已被改指向本樹（原本指向 `/home/c1147259/桌面/ANILA/AgenticRAG/src`）。若你要並用另一個 checkout，先確認 `python -c "import anila_core; print(anila_core.__file__)"`。
- 後端測試一律加 `-p no:cacheprovider -o addopts=''`。
- `/tmp` 的證據 log（`/tmp/work_full2.log`、`/tmp/core4.log` 等）重開機可能消失，別當長期依據。

## 擁有者已裁決（不要再問一次）

1. audit：高風險 mutation（核准／停用／發撤金鑰／service grant）audit 失敗要 fail-closed；低風險維持 fail-soft。已實作。
2. fleet service token：先寫成具名已接受風險，等重寫部署腳本時再拆。已註記在 `_service_principal.py`。
3. 知識庫升密：不清既有長期記憶，只在 UI 講明。已實作。
4. SMTP：先標示「目前只寫 log、未寄信」，真連測試另行。已註記。
5. 單位管理員：維持「人事與用量」小範圍；未來部門改走 Oracle 員編取得，屆時 grant 繼承語意要重評。

另：擁有者說「http 放行」與「部署腳本等開發完重寫」——現階段**不要動** `infra/deployment/scripts/deploy-prod.sh` 與其分支白名單。

## 工作樹邊界

- 有 4 個更早的 agents 文案線未提交檔，**不可 revert**：`apps/csp-governance-ui/src/views/DeveloperAgentsView.vue`、`docs/guides/developer-guide.md`、`services/csp/app/api/agents/registration.py`、`services/csp/app/schemas/contracts/agents.py`。
- 全部未 commit、未 push、未跑 docker。

## 給下一手的起手式

```bash
git status --short | wc -l          # 應為 84
git log --oneline -1                # 應為 9ef47d7a
cat docs/office/2026-09-19-csp-adjustments.md
```

要動 anila-core 那批時，先在乾淨 worktree 跑一次 `packages/anila-core` 全套確認基準仍是 118，再逐檔定位 404。
