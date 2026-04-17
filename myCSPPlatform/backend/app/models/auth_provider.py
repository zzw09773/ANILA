from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from app.database import Base


class AuthProvider(Base):
    __tablename__ = "auth_providers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False, index=True)
    provider_type = Column(String(20), nullable=False, index=True)  # ldap / oidc
    button_text = Column(String(100), nullable=True)
    is_active = Column(Boolean, default=True)
    auto_create_users = Column(Boolean, default=True)
    default_role = Column(String(20), nullable=False, default="user")
    default_department_id = Column(
        Integer,
        ForeignKey("departments.id", ondelete="SET NULL"),
        nullable=True,
    )

    # LDAP
    ldap_server_uri = Column(String(255), nullable=True)
    ldap_bind_dn = Column(String(255), nullable=True)
    ldap_bind_password = Column(String(255), nullable=True)
    ldap_base_dn = Column(String(255), nullable=True)
    ldap_user_filter = Column(String(255), nullable=True)
    ldap_start_tls = Column(Boolean, default=False)
    ldap_email_attribute = Column(String(100), nullable=True)
    ldap_display_name_attribute = Column(String(100), nullable=True)

    # OIDC
    oidc_issuer_url = Column(String(255), nullable=True)
    oidc_client_id = Column(String(255), nullable=True)
    oidc_client_secret = Column(String(255), nullable=True)
    oidc_authorization_endpoint = Column(String(255), nullable=True)
    oidc_token_endpoint = Column(String(255), nullable=True)
    oidc_userinfo_endpoint = Column(String(255), nullable=True)
    oidc_scopes = Column(String(255), nullable=True)
    oidc_username_claim = Column(String(100), nullable=True)
    oidc_email_claim = Column(String(100), nullable=True)
    oidc_subject_claim = Column(String(100), nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    default_department = relationship("Department", lazy="joined")
