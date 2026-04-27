from datetime import datetime
from pydantic import BaseModel, field_validator


# OIDC client_secret 不再以明文回應；list/get 回傳 mask 而非密文。
SECRET_MASK = "***"


class AuthProviderBase(BaseModel):
    name: str
    provider_type: str
    button_text: str | None = None
    is_active: bool = True
    auto_create_users: bool = True
    default_role: str = "user"
    default_department_id: int | None = None

    oidc_issuer_url: str | None = None
    oidc_client_id: str | None = None
    # 寫入 (create / update) 用 plaintext；讀取回應一律是 SECRET_MASK 或 None。
    oidc_client_secret: str | None = None
    oidc_authorization_endpoint: str | None = None
    oidc_token_endpoint: str | None = None
    oidc_userinfo_endpoint: str | None = None
    oidc_scopes: str | None = None
    oidc_username_claim: str | None = None
    oidc_email_claim: str | None = None
    oidc_subject_claim: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("名稱不可為空")
        return value

    @field_validator("provider_type")
    @classmethod
    def validate_provider_type(cls, value: str) -> str:
        # LDAP 已自系統下線，僅保留 OIDC（後續會以完整 SSO 取代本地登入）。
        if value != "oidc":
            raise ValueError("provider_type 僅支援 oidc（LDAP 已停用）")
        return value


class AuthProviderCreate(AuthProviderBase):
    pass


class AuthProviderUpdate(BaseModel):
    name: str | None = None
    button_text: str | None = None
    is_active: bool | None = None
    auto_create_users: bool | None = None
    default_role: str | None = None
    default_department_id: int | None = None

    oidc_issuer_url: str | None = None
    oidc_client_id: str | None = None
    # 傳入 SECRET_MASK 表示「不變更」；傳入空字串表示「清空」；其他字串表示「替換」。
    oidc_client_secret: str | None = None
    oidc_authorization_endpoint: str | None = None
    oidc_token_endpoint: str | None = None
    oidc_userinfo_endpoint: str | None = None
    oidc_scopes: str | None = None
    oidc_username_claim: str | None = None
    oidc_email_claim: str | None = None
    oidc_subject_claim: str | None = None


class AuthProviderResponse(AuthProviderBase):
    id: int
    created_at: datetime
    updated_at: datetime
    default_department_name: str | None = None


class PublicAuthProviderResponse(BaseModel):
    id: int
    name: str
    provider_type: str
    button_text: str | None = None
