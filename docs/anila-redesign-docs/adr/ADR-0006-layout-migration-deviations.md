# ADR-0006: §17.1 目錄搬遷之現場決策（D1–D6 + ALM 過渡）

> ⚠ 2026-08-17 盤點：本檔為【歷史紀錄】——redesign 收斂期的決策紀錄(ADR),保留決策當時的理由與依據,不代表現況。專案權威＝`PLAN.md`(現況與執行順序),規格＝`SYSTEM-MAP.md`。

> Status: accepted
> Date: 2026-07-02
> Deciders: ANILA system owner（佈局策略）＋ Claude（現場 disposition，依偵察證據）
> Related: doc 10 §17.1、`docs/specs/plans/2026-07-02-anila-redesign-master.md`、commit b5c5e32

## 背景

doc 10 §17.1 的搬遷表為「新 repo」而寫，且部分路徑與實際樹狀不符。使用者拍板「在本 repo 新分支 `anila-redesign` 立即照 §17.1 搬遷」。搬遷前偵察（recon-move-impact）發現 6 個表上未載明的落差，需現場裁決。

## 決策

| # | 決策 |
|---|---|
| D1 | §17.1 所寫 `models/docker-compose.yml` 為過時路徑；以實際 `models/inference/*` 為準。 |
| D2 | `models/inference/src`（embedding proxy 與模型設定掛載來源）隨 compose 同遷至 `infra/models/src`，compose 內 `${ANILA_MODELSRC_DIR:-./src}` 預設維持不變。 |
| D3 | 模型權重 `models/model/`（數百 GB、gitignored）不搬；compose 預設 `${ANILA_HF_DIR:-../model}` 改 `../../models/model`。 |
| D4 | `myCSPPlatform/scripts/init_db.py` 併入 `services/csp/scripts/`；刪除 csp.Dockerfile 的重複 COPY，避免蓋掉 `generate-jwt-keypair.py`。 |
| D5 | certs 目錄遷 `infra/nginx/certs/`（追蹤的 `.gitignore` 隨遷、live 憑證為 untracked 由部署主機自理）；`myCSPPlatform/README*.md`、`.env.example`、巢狀 `.gitignore` 併入 `services/csp/`；scripts 孤兒（phase1-e2e、calibration 工具、drop-card-auth-schema.sql）遷 `infra/deployment/scripts/`。 |
| D6 | `myCSPPlatform/start.sh` 與 `myCSPPlatform/docker/docker-compose.yml`（獨立 CSP 舊部署，已被 root compose 全棧取代）直接移除，不再攜帶。 |
| ALM | `ANILALM` 過渡遷至 `apps/anilalm`，不直接照 §17.1 併入 `apps/anila-shell/features/*`——兩個 SPA 的整併是 Slice 9 的產品決策（IA/導流），不是檔案搬移；先保 build 綠。 |
| Shim | 新增 root `compose.yaml` / `compose.dev.yaml`（`include:` 指向 `infra/compose/*.yml`），保住「repo 根 `docker compose up -d`」與 `.env` 解析錨點；compose 檔內相對路徑一律以檔案自身位置重錨（`../../`）。需 Compose ≥ 2.20；內網 air-gap toolkit 的 Docker 版本上線前須確認，否則 fallback 為 deploy script 內 `-f … --env-file .env` 包裝。 |
| 本機 | 開發機 running `anila-platform-*` 容器 bind-mount 三個舊路徑（nginx.conf、certs、anila-agent 模板），已留 untracked symlink 相容墊片並記入 `.git/info/exclude`；重建 stack 改用新 compose 後可移除。 |

## 影響

- 本分支與 main／7 分支模型的 cherry-pick 互通中斷（使用者已知悉並拍板）。
- 內網離線 bundle（`00-anila-src.tar.gz`）須以本分支重打包後才含新佈局。
- 部署主機上的 live TLS 憑證需手動遷至 `infra/nginx/certs/`（或重跑 reissue 腳本）。

## 憲法檢核

- [x] 不違反 `00-product-constitution.md` §5 功能准入合約（純佈局，無產品行為變更）
- [x] 不落入 §6 凍結清單
- [x] 不弱化安全不變量（card SSO / JWT / CSRF / RLS / SSRF guard / 單向閂鎖）
