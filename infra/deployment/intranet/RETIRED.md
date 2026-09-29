# 已退役

2026-09-29 起，平台出貨與內網更新改走 [`docs/deploy/UPDATE.md`](../../../docs/deploy/UPDATE.md)。

`intranet-deploy.sh`、`build-and-export-for-intranet.sh`、`anila-serve.sh` 移到 [`../archive/intranet-legacy/`](../archive/intranet-legacy/)。直接執行或 source 都會停。函式文字還在，離線測試會讀檔，不會 source。

模型生命週期不在這包裡。`model-serve.sh`、權重下載、分塊、量化仍在用，路徑是 [`../archive/model-side/`](../archive/model-side/)。
