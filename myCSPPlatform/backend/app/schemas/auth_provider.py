from datetime import datetime
from pydantic import BaseModel, field_validator


class AuthProviderBase(BaseModel):
    name: str
    provider_type: str
    button_text: str | None = None
    is_active: bool = True
    auto_create_users: bool = True
    default_role: str = "user"
    default_department_id: int | None = None

    ldap_server_uri: str | None = None
    ldap_bind_dn: str | None = None
    ldap_bind_password: str | None = None
    ldap_base_dn: str | None = None
    ldap_user_filter: str | None = None
    ldap_start_tls: bool = False
    ldap_email_attribute: str | None = None
    ldap_display_name_attribute: str | None = None

    oidc_issuer_url: str | None = None
    oidc_client_id: str | None = None
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
        if value not in {"ldap", "oidc"}:
            raise ValueError("provider_type 僅支援 ldap 或 oidc")
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

    ldap_server_uri: str | None = None
    ldap_bind_dn: str | None = None
    ldap_bind_password: str | None = None
    ldap_base_dn: str | None = None
    ldap_user_filter: str | None = None
    ldap_start_tls: bool | None = None
    ldap_email_attribute: str | None = None
    ldap_display_name_attribute: str | None = None

    oidc_issuer_url: str | None = None
    oidc_client_id: str | None = None
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
