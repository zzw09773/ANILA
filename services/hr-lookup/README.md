# hr-lookup

卡片登入時，CSP 把連線參數與員工編號送來，這個服務向人資 Oracle 查那一個人，查完就丟掉。服務自己沒有設定，也不讀環境變數。

查詢固定是 `ovc_PNO`、`ovc_NAME`、`ovc_DEPT1_NAME`、`ovc_DEPT2_NAME`、`ovc_EMAIL`、`ovc_DUTY_DS`，條件是 `WHERE ovc_PNO = :pno`。不接受呼叫端自訂的 SQL。

`GET /health` 只回答服務還在，不去連 Oracle。`POST /lookup` 才查。

映像使用 `python:3.12-slim`，Dockerfile 的 `FROM` 已釘 digest。輪檔在 `wheelhouse/`，雜湊寫在 `wheelhouse/SHA256SUMS` 與 `requirements.txt`。

測試（不需要 Oracle）：

```bash
cd services/hr-lookup && python -m pytest
```
