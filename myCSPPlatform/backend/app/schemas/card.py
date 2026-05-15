"""中科院憑證卡登入 endpoint 的 Pydantic DTO。

對應 ``app/api/auth.py`` 的 ``/api/auth/card/challenge`` 與
``/api/auth/card/verify`` 兩支 endpoint。

設計參照 OIDC state JWT pattern（``app/services/external_auth_service.py``）：
challenge endpoint 產一條簽過名的 ``challenge_token`` JWT 與明文 ``nonce``，
client 簽 ``nonce``（透過本機 CHT 元件 / dev mock）後把
``challenge_token + signature`` 一起送回 verify endpoint。後端用 SECRET_KEY
驗 JWT 還原 ``nonce``，再用 ``nonce`` 當 ``expected_tbs`` 驗 PKCS#7 簽章。
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class CardChallengeResponse(BaseModel):
    """``GET /api/auth/card/challenge`` 的回應。

    Attributes:
        challenge_token: SECRET_KEY 簽過的 JWT，``aud="card-challenge"``、
            ``exp=2min``，內含 ``nonce``。client 應原樣回傳給 verify endpoint。
        nonce: 明文 nonce，client 簽章時用作 ``tbsPackage.tbs`` 的值。
        expires_in: ``challenge_token`` 剩餘有效秒數，僅供 client 顯示倒數用。
    """

    challenge_token: str
    nonce: str
    expires_in: int


class CardVerifyRequest(BaseModel):
    """``POST /api/auth/card/verify`` 的請求 body。

    Attributes:
        challenge_token: 從 ``/card/challenge`` 拿到的同一份 JWT。
        signature: base64 編碼的 PKCS#7 SignedData（由本機 CHT 元件回傳，
            等同於 ``cht/app.py:18`` 的 ``signature`` 欄位）。
        card_serial: 元件回應的 ``cardSN`` 欄位（例：``CS00000000025247``）。
            純 audit log 用途；不參與密碼學驗證。
    """

    challenge_token: str = Field(..., min_length=1)
    signature: str = Field(..., min_length=1)
    card_serial: str | None = None


# ─── Pending registration / approval (branch SSO) ─────────────────────────────


class CardPendingResponse(BaseModel):
    """``POST /api/auth/card/verify`` 在 user 尚未核准時的回應 (HTTP 202)。

    跟登入成功的 ``TokenResponse`` shape 完全不同，UI 必須先檢查 status：
    - ``pending_registration``：使用者卡片首次刷，還沒填單位 — UI 應該
      顯示「完成註冊」表單蒐集 ``department_id`` 後 POST 到
      ``/api/auth/card/complete-registration``。
    - ``pending_approval``：已填過單位但 admin 尚未核准 — UI 顯示等待頁。

    ``registration_token`` 只在 ``pending_registration`` 狀態下發出（用過
    一次 endpoint 就棄）。``pending_approval`` 不發 token，因為使用者已經
    沒有事可做，只能等待。
    """

    status: str = Field(..., description="'pending_registration' or 'pending_approval'")
    employee_id: str
    display_name: str
    email: str | None = None
    registration_token: str | None = None
    expires_in: int | None = None
    message: str


class CardCompleteRegistrationRequest(BaseModel):
    """``POST /api/auth/card/complete-registration`` 請求 body。

    Attributes:
        registration_token: 從 ``/card/verify`` pending response 拿到的 JWT。
        department_id: 使用者選擇的 ``departments.id``。後端會驗存在且 active。
    """

    registration_token: str = Field(..., min_length=1)
    department_id: int = Field(..., gt=0)


class CardCompleteRegistrationResponse(BaseModel):
    """``POST /api/auth/card/complete-registration`` 回應。

    成功時 user 仍是 ``is_approved=False``，但已有 ``department_id``，狀態
    從 ``pending_registration`` 過渡到 ``pending_approval``。
    """

    status: str
    message: str


class CardDepartmentOption(BaseModel):
    """``GET /api/auth/card/registration/departments`` 回應的單筆。

    pending 使用者拿這個 list 渲染「完成註冊」表單的單位下拉選單。只暴露
    ``id`` 跟 ``name``，避免洩漏管理性 metadata（description / 時間戳）。
    """

    id: int
    name: str
