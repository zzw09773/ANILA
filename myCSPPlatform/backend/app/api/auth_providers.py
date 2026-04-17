from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.auth_provider import AuthProvider
from app.models.department import Department
from app.models.user import User
from app.schemas.auth_provider import (
    AuthProviderCreate,
    AuthProviderResponse,
    AuthProviderUpdate,
)
from app.services.audit_service import log_audit_event
from app.services.auth_service import require_admin

router = APIRouter(prefix="/api/auth-providers", tags=["SSO / LDAP / OIDC"])


def _validate_default_department(db: Session, department_id: int | None) -> int | None:
    if department_id is None:
        return None
    department = db.query(Department).filter(Department.id == department_id).first()
    if not department or not department.is_active:
        raise HTTPException(status_code=400, detail="預設部門不存在或已停用")
    return department.id


def _serialize(provider: AuthProvider) -> dict:
    data = {
        "id": provider.id,
        "name": provider.name,
        "provider_type": provider.provider_type,
        "button_text": provider.button_text,
        "is_active": provider.is_active,
        "auto_create_users": provider.auto_create_users,
        "default_role": provider.default_role,
        "default_department_id": provider.default_department_id,
        "default_department_name": provider.default_department.name if provider.default_department else None,
        "ldap_server_uri": provider.ldap_server_uri,
        "ldap_bind_dn": provider.ldap_bind_dn,
        "ldap_bind_password": provider.ldap_bind_password,
        "ldap_base_dn": provider.ldap_base_dn,
        "ldap_user_filter": provider.ldap_user_filter,
        "ldap_start_tls": provider.ldap_start_tls,
        "ldap_email_attribute": provider.ldap_email_attribute,
        "ldap_display_name_attribute": provider.ldap_display_name_attribute,
        "oidc_issuer_url": provider.oidc_issuer_url,
        "oidc_client_id": provider.oidc_client_id,
        "oidc_client_secret": provider.oidc_client_secret,
        "oidc_authorization_endpoint": provider.oidc_authorization_endpoint,
        "oidc_token_endpoint": provider.oidc_token_endpoint,
        "oidc_userinfo_endpoint": provider.oidc_userinfo_endpoint,
        "oidc_scopes": provider.oidc_scopes,
        "oidc_username_claim": provider.oidc_username_claim,
        "oidc_email_claim": provider.oidc_email_claim,
        "oidc_subject_claim": provider.oidc_subject_claim,
        "created_at": provider.created_at,
        "updated_at": provider.updated_at,
    }
    return data


@router.get("", response_model=list[AuthProviderResponse])
def list_auth_providers(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    providers = db.query(AuthProvider).order_by(AuthProvider.provider_type, AuthProvider.name).all()
    return [_serialize(provider) for provider in providers]


@router.post("", response_model=AuthProviderResponse)
def create_auth_provider(
    request: AuthProviderCreate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    existing = db.query(AuthProvider).filter(AuthProvider.name == request.name).first()
    if existing:
        raise HTTPException(status_code=400, detail="Provider 名稱已存在")
    payload = request.model_dump()
    payload["default_department_id"] = _validate_default_department(
        db,
        payload.get("default_department_id"),
    )
    provider = AuthProvider(**payload)
    db.add(provider)
    log_audit_event(
        db,
        actor=admin,
        action="create",
        resource_type="auth_provider",
        detail=f"建立 {provider.provider_type} Provider「{provider.name}」",
    )
    db.commit()
    db.refresh(provider)
    return _serialize(provider)


@router.put("/{provider_id}", response_model=AuthProviderResponse)
def update_auth_provider(
    provider_id: int,
    request: AuthProviderUpdate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    provider = db.query(AuthProvider).filter(AuthProvider.id == provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider 不存在")

    update_data = request.model_dump(exclude_unset=True)
    if "name" in update_data and update_data["name"]:
        exists = (
            db.query(AuthProvider)
            .filter(AuthProvider.name == update_data["name"], AuthProvider.id != provider_id)
            .first()
        )
        if exists:
            raise HTTPException(status_code=400, detail="Provider 名稱已存在")
    if "default_department_id" in update_data:
        update_data["default_department_id"] = _validate_default_department(
            db,
            update_data["default_department_id"],
        )

    for field, value in update_data.items():
        setattr(provider, field, value)

    log_audit_event(
        db,
        actor=admin,
        action="update",
        resource_type="auth_provider",
        resource_id=provider.id,
        detail=f"更新 Provider「{provider.name}」",
    )
    db.commit()
    db.refresh(provider)
    return _serialize(provider)


@router.delete("/{provider_id}")
def deactivate_auth_provider(
    provider_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    provider = db.query(AuthProvider).filter(AuthProvider.id == provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider 不存在")
    provider.is_active = False
    log_audit_event(
        db,
        actor=admin,
        action="deactivate",
        resource_type="auth_provider",
        resource_id=provider.id,
        detail=f"停用 Provider「{provider.name}」",
    )
    db.commit()
    return {"message": "Provider 已停用"}
