from datetime import datetime
from pydantic import BaseModel, field_validator


class UserBase(BaseModel):
    username: str
    email: str | None = None
    role: str = "user"


class UserCreate(UserBase):
    password: str
    department_id: int | None = None


class UserUpdate(BaseModel):
    email: str | None = None
    role: str | None = None
    department_id: int | None = None
    is_active: bool | None = None


class UserResponse(UserBase):
    id: int
    department_id: int | None = None
    department_name: str | None = None
    is_active: bool
    is_approved: bool = True
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
