"""service-wrapper 認證：驗 inbound ``X-CSP-Service-Token``（fail-closed）。

認證方向（被修正過的版本）：CSP Router 派工時送
  X-CSP-Service-Token: csk-...        ← 這個 agent 自己的憑證；我們驗它
  X-ANILA-User-Id / -Email / -Groups  ← 終端使用者身分；驗過 service token 後才信任
Router **不轉發** 使用者 JWT。驗 ``Authorization: Bearer <jwt>`` 是錯的。

純函式、無副作用，可不依賴 FastAPI 單元測試。
"""

from __future__ import annotations

import hmac


def verify_service_token(provided: str | None, expected: str, *, allow_unset: bool = False) -> bool:
    """常數時間比對 inbound service token。

    回 True（放行）的情況：
      * ``expected`` 為空  → 僅當 ``allow_unset`` 為 True（明確的本地開發 opt-out）；
                            否則拒絕（fail-closed——忘記設 csk- 不等於門戶大開）；
      * 其餘            → ``provided`` 經 ``hmac.compare_digest`` 與 ``expected`` 相符才放行。
    """
    if not expected:
        return allow_unset
    if not provided:
        return False
    return hmac.compare_digest(provided, expected)


def trusted_user_identity(
    allowed: bool,
    *,
    user_id: str | None,
    email: str | None,
    groups: str | None,
) -> dict[str, str | None]:
    """只在 service token 驗過（``allowed`` 為 True）後，才回傳可信的 X-ANILA-User-* 身分。"""
    if not allowed:
        return {}
    return {"user_id": user_id, "email": email, "groups": groups}
