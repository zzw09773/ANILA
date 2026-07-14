# 憑證卡離線 CRL 維運

本 runbook 適用 `prod-intranet-card` 與其具名 break-glass profile。正式
card-only CSP 在 CRL 缺失、簽章/issuer 不符、`thisUpdate` 位於未來、
`nextUpdate` 已過或超過 `CARD_CRL_MAX_AGE_HOURS` 時拒絕啟動；服務運行中每次
卡片驗證也會重新檢查 freshness，因此 stale CRL 不會因容器已啟動而放行。

## 固定契約

- Host 檔案：`share/pki/card-crl-bundle.pem`。
- Container 檔案：`/etc/anila/pki/card-crl-bundle.pem`（read-only mount）。
- `CARD_CRL_REQUIRED=true`。
- `CARD_CRL_SOURCE` 必須具名同步來源/責任單位，不得填 `manual` 或空值。
- `CARD_REQUIRED_CERT_POLICY_OIDS` 必須是 PKI owner 核准的卡片 policy OID
  allow-list；`CARD_REQUIRED_EKU_OID` 預設為 `id-kp-clientAuth`。
- 撤銷比對使用已驗證 CMS signer certificate 的 X.509 serial；前端
  `card_serial` 永遠不是撤銷依據。成功 audit 另記 signer SHA-256 fingerprint。

## 更新程序

1. 由 signed pilot profile 具名來源取得 PEM CRL bundle，確認傳輸媒體、交付人、
   取得時間及來源雜湊；不得從瀏覽器臨時下載未核對的檔案。
2. 在隔離主機先以 `openssl crl -in <file> -noout -issuer -lastupdate -nextupdate
   -fingerprint -sha256` 逐份記錄 issuer、`thisUpdate`、`nextUpdate` 與 fingerprint。
3. 將檔案以同目錄暫存檔寫入，設定為非可執行、僅維運者可改，再用 atomic rename
   取代 `share/pki/card-crl-bundle.pem`。不要原地逐 byte 覆寫 live 檔。
4. 執行 `docker compose up -d --no-build --pull never --force-recreate csp`。
   CSP startup 會驗釘選 CA、CRL issuer/signature/freshness、完整 issuer coverage、
   EKU、KeyUsage、BasicConstraints、pathLen 與 certificate policy。
5. 從 CSP log 留存每份 `card CRL validated` 記錄；內容含
   `CARD_CRL_SOURCE`、issuer、`thisUpdate`、`nextUpdate` 與 max age。
6. 用一張未撤銷測試卡完成一次登入，並用 synthetic/專用撤銷測材確認 revoked
   signer 被拒。不得拿真實已撤銷人員憑證作例行 smoke。

任何驗證失敗都維持服務不可用並通知 PKI owner；不得把
`CARD_CRL_REQUIRED` 改為 false、放寬 max age、移除 policy OID 或回退 stale CRL
來恢復服務。

## 遺失卡與離職事件

收到事件後先停用 CSP 使用者，立即撤銷其現有 user/session token，再依 signed
pilot profile 的 SLA 要求 PKI owner 發布並同步新 CRL。實體卡回收只是額外控制，
不能取代憑證撤銷。營業秘密 pilot 的人工停用例外必須具名風險接受者、監控與到期
日；該例外不得沿用為機密以上 production Go。

## 證據包

每次更新至少保存：來源/責任人、取得與套用 UTC 時間、檔案 SHA-256、每份 CRL
issuer/`thisUpdate`/`nextUpdate`/fingerprint、CSP startup 驗證 log、未撤銷正向 smoke、
撤銷負向 smoke，以及事件單或例行變更單編號。不得保存卡片私鑰、PIN、原始 session
token 或原始 JWT `jti`；audit 只保存識別子的 SHA-256 digest。
