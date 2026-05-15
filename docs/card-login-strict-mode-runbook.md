# 中科院憑證卡登入 — STRICT mode 升級 Runbook

**Branch:** `SSO`
**Owner:** myCSPPlatform auth team
**Status:** ⏳ pending 中科院 IT 提供 CA bundle / OCSP endpoint

---

## 政策背景

院內 production 政策是「**卡片登入是唯一活路**」。目前 myCSPPlatform 有兩段驗證模式：

| Mode | 用途 | 驗證範圍 | 部署環境 |
|---|---|---|---|
| `loose` (current default) | dev / staging | 只 parse PKCS#7 內含 signer cert，抽 claims | host 端 `cht/` mock 容器 |
| `strict` (this runbook) | **院內 production** | 上面所有 + PKCS#7 簽章數學一致性 + X.509 chain 到中科院 CA Root + OCSP/CRL 撤銷檢查 | 真實使用者卡片 |

`strict` mode 還沒實作（`card_auth._verify_strict()` 目前 `raise NotImplementedError`），原因是 `cryptography==41.0.7` 沒有完整的 `pkcs7_verify_*` API。本 runbook 列出補完所需的所有 input、升級步驟、驗證方式。

---

## 從中科院 IT 需要拿到的 input

| # | 項目 | 用途 | Owner | 預設交付格式 |
|---|---|---|---|---|
| 1 | **CSPKI Root CA cert (PEM)** | chain 驗證的 trust anchor | 中科院 PKI 管理單位 | `.pem` 或 `.cer` |
| 2 | **CSPKI Intermediate CA cert (PEM)** | 中科院憑證管理中心 - G1（中間 CA）公鑰，掛在 root 下 | 同上 | `.pem` |
| 3 | **OCSP responder URL** | 即時撤銷檢查；mock cert 提示是 `http://ocsp.ncsist.org.tw/OCSP`，內網 OCSP URL 是否相同需確認 | 中科院 PKI 管理單位 | URL |
| 4 | **CRL 下載 URL（fallback）** | 內網 OCSP 不可達時的離線檢查；mock cert 提示是 `http://repository.ncsist.org.tw/crl/...` | 同上 | URL |
| 5 | **OCSP 連線是否需要 client cert** | 決定 httpx config | 同上 | yes / no |
| 6 | **個人卡 sample cert** | 驗證對非「鄒惠翔測試卡」的卡能 round-trip | 內部測試者（你自己的卡） | base64 PKCS#7 |

⚠️ 內網部署常見問題：CRL/OCSP URL 在 cert extensions 內是 internet 域名（`http://repository.ncsist.org.tw/...`），但實際內網可能 proxy 到別處或完全不可達。先 dry-run 確認可達性。

---

## 升級步驟

### Step 1：升級 cryptography 依賴

```diff
# myCSPPlatform/backend/requirements.txt
- python-jose[cryptography]==3.3.0
+ python-jose[cryptography]==3.3.0
+ cryptography>=43,<45
```

需要 `cryptography ≥ 43` 取得：

- `cryptography.hazmat.primitives.serialization.pkcs7.pkcs7_verify_*`（驗 signed data 對 tbs 數學）
- `cryptography.x509.verification.PolicyBuilder`（policy-driven chain 驗）— 也接受 `42.0+`
- 既有 `41.x` API 全部 backward compatible，dev/LOOSE mode 不受影響

驗證升級：

```bash
docker compose build csp-backend
docker compose run --rm csp-backend pytest tests/test_card_auth.py tests/test_card_endpoints.py -v
# 既有 LOOSE 測試應該全綠
```

### Step 2：把 CA cert 放進 image

選一個策略：

**選項 A：環境變數注入 PEM 字串**（最易部署，適合短 cert）

```yaml
# docker-compose.yml
csp-backend:
  environment:
    NCSIST_CA_BUNDLE_PEM: |
      -----BEGIN CERTIFICATE-----
      MIIDpzCCAwmgAwIBAgIQ...  (Root)
      -----END CERTIFICATE-----
      -----BEGIN CERTIFICATE-----
      MIIE5jCCBGyg...  (Intermediate)
      -----END CERTIFICATE-----
```

**選項 B：volume mount PEM 檔**（適合 cert 較多 / 經常輪替）

```yaml
csp-backend:
  volumes:
    - ./secrets/ncsist-ca-bundle.pem:/etc/anila/ncsist-ca-bundle.pem:ro
  environment:
    NCSIST_CA_BUNDLE_PATH: /etc/anila/ncsist-ca-bundle.pem
```

對應 `config.py` 加：

```python
NCSIST_CA_BUNDLE_PATH: str = ""
NCSIST_CA_BUNDLE_PEM: str = ""
CARD_CHECK_REVOCATION: bool = True
```

### Step 3：實作 `card_auth._verify_strict()`

填補 `app/services/card_auth.py` 目前的 stub。骨架（pseudo-code）：

```python
def _verify_strict(der_bytes, signer_cert, expected_tbs):
    # 3.1 驗 PKCS#7 簽章對 tbs 數學一致性
    pkcs7.pkcs7_verify_smime(  # or pkcs7_verify_der, 視 cryptography 版本
        signature=der_bytes,
        signers=[signer_cert],
        detached_data=expected_tbs.encode("utf-8"),
        options=[pkcs7.PKCS7Options.NoVerify],  # chain 我們自己跑
    )
    # 3.2 驗 chain 到中科院 CA Root
    roots = _load_trusted_roots()  # 從 NCSIST_CA_BUNDLE_PATH 或 NCSIST_CA_BUNDLE_PEM
    verifier = (
        x509.verification.PolicyBuilder()
        .store(x509.verification.Store(roots))
        .build_client_verifier()
    )
    verifier.verify(signer_cert, intermediates=[...])
    # 3.3 OCSP（settings.CARD_CHECK_REVOCATION=True 時）
    if settings.CARD_CHECK_REVOCATION:
        _check_ocsp(signer_cert, issuer_cert)
        # OCSP 失敗 fallback CRL
```

### Step 4：staging 環境 dry-run

```bash
# staging .env
ENABLE_CARD_LOGIN=true
CARD_VERIFY_MODE=strict
REQUIRE_CARD_LOGIN_ONLY=true   # 順便 enforce policy
NCSIST_CA_BUNDLE_PATH=/etc/anila/ncsist-ca-bundle.pem
CARD_CHECK_REVOCATION=true

docker compose up csp-backend

# 用自己的真卡測（不是 cht/ mock — mock 在 strict mode 會被拒）：
# 開瀏覽器 → ANILA UI → 憑證卡 tab → 輸入 PIN → 應該成功登入
# 驗證 audit log 內：action=card_login, status=success, employee_id=<你的真實員工號>
```

### Step 5：切流量

切流量前確認：

- [ ] `assert_intranet_lockdown_consistency()` 啟動通過（不會 brick）
- [ ] `tests/test_card_auth.py` 在 STRICT mode 跑通（要更新 test fixture，加真實簽章）
- [ ] 內部 5+ 員工真卡實際登入測試通過
- [ ] OCSP responder 連線 SLA 量測（每次登入 +200~500ms 是常態，超過 1s 要 cache）
- [ ] Rollback 路線：把 `CARD_VERIFY_MODE` 改回 `loose` + 重啟容器即可降級

---

## STRICT mode 驗證 checklist（IT 來資料後跑）

```bash
# 1. 升級依賴 + 重建 image
docker compose build csp-backend

# 2. 把 CA bundle 放好（選 A 或 B）

# 3. 跑既有 LOOSE 測試，確保沒回歸
docker compose run --rm csp-backend pytest tests/test_card_auth.py -v
docker compose run --rm csp-backend pytest tests/test_card_endpoints.py -v

# 4. 跑 STRICT 測試（要先寫新 fixture，內含真實簽章）
docker compose run --rm csp-backend pytest tests/test_card_auth_strict.py -v

# 5. End-to-end with 真卡
#    啟動 staging stack, 自己刷卡 → 看 /api/auth/me 回的 employee_id 對不對

# 6. 撤銷檢查：找一張已 revoke 的卡（或 mock cert 過 expiry）→ 應該被拒
```

---

## 為什麼這份 runbook 存在

`branch SSO` 的 A/B/C 三階段都已實作完成（PKCS#7 verifier、JWT challenge、cookie session、UI tab、policy lockdown），**LOOSE mode 已能端到端 round-trip**。STRICT mode 是「production-ready」的最後一哩，但實際補完需要兩個外部依賴（cryptography 升版的 ops 決定、中科院 IT 提供 PKI inputs），不適合單方面寫死。

合併 branch SSO 時這份 runbook 應一起合進去，後續執行任由認領。

---

## 相關檔案

- [`myCSPPlatform/backend/app/services/card_auth.py`](../myCSPPlatform/backend/app/services/card_auth.py) — `_verify_strict()` 的 stub 與 inline TODO
- [`myCSPPlatform/backend/app/services/card_auth_service.py`](../myCSPPlatform/backend/app/services/card_auth_service.py) — 主流程，跟 mode 解析
- [`myCSPPlatform/backend/app/api/auth.py`](../myCSPPlatform/backend/app/api/auth.py) — endpoint 與 audit log
- [`myCSPPlatform/backend/app/services/startup_security.py`](../myCSPPlatform/backend/app/services/startup_security.py) — `assert_intranet_lockdown_consistency()`
- [`cht/app.py`](../cht/app.py) — dev mock 容器（搬到內網後 disable）
