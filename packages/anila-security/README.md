# anila-security

ANILA 各後端服務共用的薄安全套件。它只包含兩組不依賴平台 schema 或 web
framework 的 primitive：

- `credential_crypto`：AES-256-GCM、PBKDF2-SHA256 600k，以及 100k legacy
  decrypt fallback。
- `url_guard`：user-supplied outbound endpoint 的 scheme、hostname、DNS、
  private/loopback/metadata SSRF 防護與 trusted-host provider hook。
- `model_governance`：由已驗 inventory/profile/signatures 建立 immutable
  `VerifiedModelGovernanceAuthority`；disabled template 只可作 evidence，永遠
  不會 authorize。這是 runtime 可消費的 static authority，不負責 CSP egress、
  live health probe 或 usage reconciliation。

Runtime 唯一第三方依賴是 `cryptography`。新程式碼應直接從
`anila_security` import；`anila_core.security` 僅保留為舊使用者的相容 facade。
