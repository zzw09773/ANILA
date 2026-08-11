# `cht/` — 中華電信 HiPKI 本機元件的 dev mock

真元件跑在**使用者自己的 PC** 上（`localhost:16888`），接讀卡機、要 PIN、
在卡片晶片內做簽章。開發機沒有讀卡機，所以用這支 Flask app 假裝它，
協定與埠號完全一致。

## 正式流程長怎樣（本 mock 要忠實還原的對象）

```
瀏覽器 ──GET /api/auth/card/challenge──────────► csp        取得一次性 nonce
瀏覽器 ──window.open localhost:16888/popupForm─► 本機元件
瀏覽器 ──postMessage {tbs: nonce, pin, …}──────► 本機元件   把 nonce 當 tbs 交給卡片
本機元件 ─卡片私鑰簽章─► PKCS#7（eContent = tbs 原文）
瀏覽器 ──POST /api/auth/card/verify────────────► csp        驗簽章 + 鏈 + nonce → session
```

關鍵事實：**元件把 tbs 原文放進 CMS 的 eContent 才簽**。
證據在 `services/csp/tests/test_card_auth.py` 的 `MOCK_SIGNATURE_B64` ——
那是真元件的實際輸出，而當初送進去的 tbs 是 `login.html` 寫死的字串 `"TBS"`，
那份簽章的 eContent 正好就是 `b"TBS"`，且能通過釘死的 `cspki_ca_bundle.pem`
全套驗證。所以 2026-06-12 加的 nonce 綁定對真卡是**可滿足**的，正式流程一致。

## 這支 mock 做了什麼

- `POST /cht_api/sign` — 用自己的測試卡私鑰，對**收到的那個 tbs** 真的簽一份
  CMS SignedData，結構逐欄對齊真元件輸出（sha256 / rsassa_pkcs1v15 /
  signedAttrs 含 contentType+signingTime+messageDigest / sid 用
  issuerAndSerialNumber / 只夾 leaf）。PIN 仍是 `123456`，錯了回 `ret_code=1`。
- `POST /cht_api/pkcs11info` — 卡片與讀卡機資訊，結構照真元件。
- `GET /popupForm` — postMessage 橋接頁，前端 `caAuth.js` 的對口。
- `GET /dev/ca-bundle.pem` — 把測試信任 bundle 露出來（只有公開憑證）。

2026-07-31 之前它是假的：不管給什麼 tbs 都回一份**寫死的** PKCS#7
（eContent 永遠 `b"TBS"`），所以 nonce 綁定在架構上不可能通過，本機只好把
`ANILA_AUTH_MODE=password`，整條登入路徑走不完。

## 測試身分（合成，非真人）

| 欄位 | 值 |
|---|---|
| 工號（`subject.serialNumber`） | `9999999` |
| 姓名（`subject.CN`） | `測試人員（MOCK 假卡）` |
| Email（`SAN.rfc822Name`） | `mock-card-9999999@example.invalid` |
| cardSN | `MOCKCARD00000001` |

2026-07-31 擁有者裁決：mock 內原本嵌的是一位同仁的真憑證，已全部換掉。
DN 形狀刻意跟真卡一致（`C / O / CN / serialNumber` + email SAN），因為
`card_auth._extract_claims()` 只讀這三個位置，形狀不同會讓 parsing bug 溜過去。

## 金鑰在哪、為什麼不會外洩

測試 PKI（root → 中繼 → 卡片）**在容器啟動時才生成**，落在 `/dev-ca`
（host 上是 `secrets/dev-card-ca/`）。`secrets/`、`*.pem`、`*.key` 都在
`.gitignore` 內，私鑰不進版控、不進任何映像。效期只有 30 天，過期自動重生。

## 怎麼起

```bash
cd cht
CHT_DEV_CA_HOST_DIR=../secrets/dev-card-ca docker compose -p cht-mock up -d --build
```

只綁 `127.0.0.1:16888`。

## csp 端怎麼信任它

```
CARD_CA_BUNDLE_PATH=/app/secrets/dev-card-ca/dev_ca_bundle.pem
CARD_DEV_TRUST_TEST_CA=1
ANILA_AUTH_MODE=mixed
```

前兩條缺一不可，而且 `ANILA_AUTH_MODE` 必須不是 `card-only`，否則
`card_auth` 會**整包拒收**這份 bundle（fail-closed，不是靜默降級）。
內網 production 什麼都不用設 —— 不設 `CARD_CA_BUNDLE_PATH` 就是用釘死的
`cspki_ca_bundle.pem`。

⚠ 換掉的只有信任錨。簽章驗證、憑證鏈驗證、nonce 綁定、效期檢查**全部照跑**。
`CARD_DEV_SKIP_NONCE_BINDING` 現在不需要了，也不該再用。
