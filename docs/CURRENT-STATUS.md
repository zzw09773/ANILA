# 目前狀態（給交接與代理）

> 這一頁才是「現在這棵樹怎麼跑」。歷史細節在 `PLAN.md`、`docs/office/`、`docs/anila-redesign-docs/`。
> 更新：2026-09-18。HEAD 以 `git log -1` 為準。

## 開發線

- 分支：`main`（單一開發線；舊七分支模型已進 attic，不要再照 `AGENTS.md` §3 切 `prod-intranet-card` 那種線）
- 專案權威：`PLAN.md`（現況與順序）、`SYSTEM-MAP.md`（規格）
- `CLAUDE.md` **不存在**。環境事實看本頁與 `PLAN.md`，不要去找那份檔。

## 啟動與測試

```bash
# 平台（repo 根目錄）
docker compose up -d --build

# Shell 主流程（Vitest orchestrator，不是已刪的 Playwright functions.spec.js）
cd apps/anila-shell && npm test

# 治理前端
cd apps/csp-governance-ui && npm test && npm run build
```

入口：治理中心 `:443`、對話 `/anila/`。Composer 的 accessible name 是「傳訊息給 ANILA」。

## 尚未當成上線完成的項目

- P2.6 打 tag／重打包
- Q53 人資 Oracle（`csiih.vihbuy`）等資安放行 `oracledb` wheel
- G9 對話密等標記介面
- `app.jsx`／`router_server.py` 大檔拆分（等主流程測試穩定後再抽）
- 真模型負載測試（現有 loadtest 是 stub embedding）

## 本輪工程債（2026-09-18 抽查）

1. Shell／治理測試基線（composer accessible name、群組 `extractError`）
2. 共用工作站資料夾 localStorage 依帳號隔離
3. 檢索 `calibrated` 必須對應當下 embedding 模型
4. DB 備份不可在多個 `csp-db` 時猜第一個；復原清單含附件
