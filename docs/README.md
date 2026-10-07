# 文件

接手的人只需要這些。舊的計畫、規格、交接、稽核草稿已經刪除，留在 git 歷史裡。

## 現在怎麼跑

[`CURRENT-STATUS.md`](CURRENT-STATUS.md)

操作時覺得不該這樣，記在 [`FRICTION-LOG.md`](FRICTION-LOG.md)。

舊 `PLAN.md`／`SYSTEM-MAP.md` 等已刪文檔去哪看：見 [`CURRENT-STATUS.md`](CURRENT-STATUS.md) 的〈舊文檔去哪了〉。

## 給人用的手冊

- [使用者手冊](user-manual/index.html)
- [管理員手冊](user-manual/admin.html)
- [開發者手冊](user-manual/developer.html)
- [部署手冊](user-manual/deploy.html)

## 安裝與維運

| 要做的事 | 文件 |
|---|---|
| 第一次安裝 | [`deploy/INSTALL.md`](deploy/INSTALL.md) |
| 出貨、更新、回復 | [`deploy/UPDATE.md`](deploy/UPDATE.md) |
| 備份與還原 | [`runbooks/csp-db-backup-restore.md`](runbooks/csp-db-backup-restore.md) |
| 改了設定要重建容器 | [`runbooks/restart-vs-recreate.md`](runbooks/restart-vs-recreate.md) |
| 換正式憑證 | [`deploy/INSTALL.md`](deploy/INSTALL.md) 的 HTTPS 憑證那一步 |
| 治理中心改平台設定 | [`runbooks/settings-page.md`](runbooks/settings-page.md) |
| 開或關 ANILA LM | [`runbooks/anilalm-release-gate.md`](runbooks/anilalm-release-gate.md) |
| 三顆預設訊息按鈕 | [`runbooks/ow3-default-actions.md`](runbooks/ow3-default-actions.md) |
| 沒有 CI 時手動檢查 | [`runbooks/manual-checks.md`](runbooks/manual-checks.md) |
| 掛助手 | [`guides/developer-guide.md`](guides/developer-guide.md) |
