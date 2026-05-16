from datetime import datetime
from typing import Literal
from pydantic import BaseModel, field_validator


# L3: role 改 Literal 而非任意字串，避免 admin 不慎把 role 設為「typo」字串。
# system 是 ingestion-worker 之類的內部帳號（auto_seed 會用到），不對外開放
# 由 admin 介面手動指派。
# owner 是 0032 加的最高層 — 由 require_owner 把關 admin 帳號變更、purge、
# audit log 敏感欄位、model endpoint URL。
UserRole = Literal["owner", "admin", "developer", "user", "system"]


_PASSWORD_SPECIAL_CHARS = "!@#$%^&*()_+-=[]{}|;:,.<>?/~`\"'\\"


def _validate_password_strength(value: str) -> str:
    """Closed-deployment password policy: 8+ chars, mixed case, symbol.

    Shared between every endpoint that accepts a new/changed password
    (admin user creation, self-service password change, admin reset).
    No-SSO build has no self-service signup, so this is the *only* place
    weak passwords could enter the system — keep the bar high.
    """
    if len(value) < 8:
        raise ValueError("密碼至少需要 8 個字元")
    if not any(c.isupper() for c in value):
        raise ValueError("密碼需包含至少一個大寫字母")
    if not any(c.islower() for c in value):
        raise ValueError("密碼需包含至少一個小寫字母")
    if not any(c in _PASSWORD_SPECIAL_CHARS for c in value):
        raise ValueError("密碼需包含至少一個特殊符號")
    return value


class UserBase(BaseModel):
    username: str
    email: str | None = None
    role: UserRole = "user"


class UserCreate(UserBase):
    password: str
    department_id: int | None = None

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        return _validate_password_strength(v)


class UserUpdate(BaseModel):
    email: str | None = None
    role: UserRole | None = None
    department_id: int | None = None
    is_active: bool | None = None


class UserResponse(UserBase):
    id: int
    department_id: int | None = None
    department_name: str | None = None
    is_active: bool
    is_approved: bool = True
    last_login_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    # Non-httpOnly CSRF token for the SPA to echo in X-CSRF-Token headers
    # on cookie-authenticated mutating requests. Optional so legacy /refresh
    # responses that predate Wave 2 can still parse.
    csrf_token: str | None = None


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        return _validate_password_strength(v)


class AdminResetPassword(BaseModel):
    new_password: str

    @field_validator("new_password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        return _validate_password_strength(v)


class RegisterRequest(BaseModel):
    """Self-service signup payload.

    Closed-deployment policy: registered users land in ``is_approved=False``
    so they can NOT log in until an admin flips the flag on the user-
    management page. ``authenticate_user`` returns
    ``PENDING_APPROVAL_SENTINEL`` for those rows and the login endpoint
    surfaces "等待核准中" without leaking the account state.
    """
    username: str
    email: str | None = None
    password: str

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        return _validate_password_strength(v)


class AllowedModelItem(BaseModel):
    id: int
    display_name: str
    model_type: str

    model_config = {"from_attributes": True}


class UserAllowedModelsUpdate(BaseModel):
    model_ids: list[int]


class UserAllowedAgentsUpdate(BaseModel):
    agent_ids: list[int]
