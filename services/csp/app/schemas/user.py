from datetime import datetime
from typing import Literal
from pydantic import BaseModel, field_validator
from app.schemas.base import ApiResponseModel


# L3: role 改 Literal 而非任意字串，避免 admin 不慎把 role 設為「typo」字串。
# system 是 ingestion-worker 之類的內部帳號（auto_seed 會用到），不對外開放
# 由 admin 介面手動指派。
# owner 是 0032 加的最高層 — 由 require_owner 把關 admin 帳號變更、auth
# provider 編輯、purge、audit log 敏感欄位、model endpoint URL。
UserRole = Literal["owner", "admin", "developer", "user", "system"]


class UserBase(BaseModel):
    username: str
    email: str | None = None
    role: UserRole = "user"


class UserCreate(UserBase):
    password: str
    department_id: int | None = None


class UserUpdate(BaseModel):
    email: str | None = None
    role: UserRole | None = None
    department_id: int | None = None
    is_active: bool | None = None
    # Sprint 6 X / B2：admin 可切到 SSO-only。預設不變更（None）；
    # True = 拒絕此使用者用本機密碼登入；False = 允許。
    local_password_disabled: bool | None = None


class UserResponse(ApiResponseModel, UserBase):
    id: int
    department_id: int | None = None
    department_name: str | None = None
    is_active: bool
    is_approved: bool = True
    local_password_disabled: bool = False
    last_login_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class LoginRequest(BaseModel):
    username: str
    password: str
    auth_source: str = "local"
    provider_id: int | None = None


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    # Non-httpOnly CSRF token for the SPA to echo in X-CSRF-Token headers
    # on cookie-authenticated mutating requests. Optional so legacy /refresh
    # responses that predate Wave 2 can still parse.
    csrf_token: str | None = None


class RefreshRequest(BaseModel):
    refresh_token: str


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str


class AdminResetPassword(BaseModel):
    new_password: str


class RegisterRequest(BaseModel):
    username: str
    email: str
    password: str

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("密碼至少需要 8 個字元")
        if not any(c.isupper() for c in v):
            raise ValueError("密碼需包含至少一個大寫字母")
        if not any(c.islower() for c in v):
            raise ValueError("密碼需包含至少一個小寫字母")
        if not any(c in '!@#$%^&*()_+-=[]{}|;:,.<>?/~`"\'\\' for c in v):
            raise ValueError("密碼需包含至少一個特殊符號")
        return v


class AllowedModelItem(BaseModel):
    id: int
    display_name: str
    model_type: str

    model_config = {"from_attributes": True}


class UserAllowedModelsUpdate(BaseModel):
    model_ids: list[int]


class UserAllowedAgentsUpdate(BaseModel):
    agent_ids: list[int]


class BatchApproveRequest(BaseModel):
    """P1.4 批次核准 — 恰好一個 selector（user_ids 或 department_id）。"""

    user_ids: list[int] | None = None
    department_id: int | None = None
    include_descendants: bool = True
    dry_run: bool = False


class BatchApproveRejectedItem(BaseModel):
    user_id: int
    reason: str


class BatchApproveResponse(BaseModel):
    """批次核准結果。

    ``total_requested`` 是解析後、呼叫者可見的目標帳號數（兩種 selector
    定義相同）：已核准、略過（已是核准）、拒絕三桶長度之和。不可見／不存在
    的 id 不會進入任何桶，也不計入此數。
    """

    approved: list[int]
    skipped_already_approved: list[int]
    rejected: list[BatchApproveRejectedItem]
    total_requested: int
    dry_run: bool
