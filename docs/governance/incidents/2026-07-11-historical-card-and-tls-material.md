# Incident: Git 歷史含卡片身分材料與 TLS 私鑰

- **Tier**: T1 Critical
- **Detected**: 2026-07-11T01:37:21Z
- **Resolved**: 未結案
- **Affected**: 30 個可達 refs；565 個 redacted candidates；142 個 blobs；其中 2 個 serialized TLS private-key blobs
- **Owner**: AI Security Lead + AIMS Owner
- **Related risk**: R-016
- **Evidence**: `docs/security/2026-07-10-card-material-audit.{md,json}`

## Timeline

- 2026-07-11：Gate 0 audit 確認 current tree 為 0 findings，但 reachable history 仍有 565 candidates 與 2 個 critical private-key blobs。
- 2026-07-11：PR #22 保留 scanner 與 current-tree fail gate；獨立 review 要求在 merge 前留下正式裁決、deployment 前完成輪換證據。
- 2026-07-11：更正 TLS runbook 的實際歷史路徑，建立本事件與 R-016。

## Root cause

早期版本把測試／開發 TLS 私鑰及真人卡片衍生資料提交進 Git。後續只清理
working tree，沒有同步撤銷金鑰與重寫所有 branches、tags、mirrors、caches，
因此舊 objects 仍能由 30 個 refs 取回。

## What worked / what broke

### What worked

- Current tree 已無卡片 fixture 或 serialized private key。
- 正式 Compose 強制從 repo 外 `ANILA_TLS_CERTS_DIR` 載入 TLS material。
- `scan_card_material.py` 產出 redacted 報告，Gate 0 CI 阻擋 current-tree 回歸。
- `reissue-tls-cert.sh` 可在外部 state directory 產生新 keypair。

### What broke

- Git history、舊 clone、mirror、cache 與 backup 仍可能保留 objects。
- 尚無部署中 certificate/key fingerprint 與歷史 key 的正式比對／輪換證據。
- 舊 runbook 的 filter paths 與 audit 命中的 `myCSPPlatform/docker/certs/` 不一致。
- 人員身分材料需要 data owner 判定清理範圍，不能只靠刪除兩個 key paths 結案。

## Merge / deployment decision

- **PR #22 merge**：修完測試 blocker 且 CI 全綠後可合併；它降低 current-tree 與部署風險，合併不代表事件結案。
- **`prod-intranet-card` deployment**：**No-Go**，直到 Security Lead 留存部署中 TLS fingerprint，並證明歷史 key 已停用；無法證明時必須先執行 `reissue-tls-cert.sh`。
- **History rewrite**：不在 PR #22 自動執行。必須由 AIMS Owner 另行明確核准 coordinated freeze、全 refs force-push、clone/mirror/cache 汰換與重掃。

## Corrective actions

- [x] Current tree 移除卡片 fixture 與私鑰。
- [x] Gate 0 加入 current-tree material scanner。
- [x] 更正 TLS runbook，先輪換再 rewrite，且涵蓋 audit 實際命中的路徑。
- [ ] Security Lead 比對並留存部署 TLS certificate/key fingerprint；不確定即輪換。
- [ ] Data owner 驗證 25 個 unique redacted values，決定個資 rewrite manifest。
- [ ] AIMS Owner 核准停推窗口與 `git filter-repo` 全 refs rewrite。
- [ ] 清除／汰換舊 clones、forks、mirrors、CI caches、release archives、backups。
- [ ] 重跑 `scan_card_material.py --scope all`，確認 `history_critical_blobs == 0` 並保存 after report。
- [ ] Security Lead 與 AIMS Owner 簽核結案。

## Lessons learned

- 從 working tree 刪除 secret 不等於撤銷，也不等於從 Git objects 清除。
- Secret 事件處理順序固定為：先 revoke/rotate，再 coordinated history rewrite，最後重掃與汰換副本。
- 稽核與 runbook 必須共享實際 object path；不能沿用已搬遷前的路徑假設。
