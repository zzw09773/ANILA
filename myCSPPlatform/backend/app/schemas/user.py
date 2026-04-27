from datetime import datetime
from typing import Literal
from pydantic import BaseModel, field_validator


# L3: role 改 Literal 而非任意字串，避免 admin 不慎把 role 設為「typo」字串。
# system 是 ingestion-worker 之類的內部帳號（auto_seed 會用到），不對外開放
# 由 admin 介面手動指派。
UserRole = Literal["admin", "developer", "user", "system"]


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
