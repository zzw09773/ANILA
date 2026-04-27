from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from app.database import Base


class AuthProvider(Base):
    """External authentication provider definition.

    LDAP support has been retired (replaced by SSO via OIDC).  The legacy
    ``ldap_*`` columns are kept on the table to preserve existing rows
    until a follow-up migration drops them; the application no longer
    reads or writes them.  ``oidc_client_secret`` is now stored as an
    AES-GCM envelope (see ``services/auth_provider_secret.py``); existing
    plaintext rows are decoded transparently and re-encrypted on next
    save.
    """

    __tablename__ = "auth_providers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False, index=True)
    provider_type = Column(String(20), nullable=False, index=True)  # 僅 'oidc'（'ldap' 已棄用）
    button_text = Column(String(100), nullable=True)
    is_active = Column(Boolean, default=True)
    auto_create_users = Column(Boolean, default=True)
    default_role = Column(String(20), nullable=False, default="user")
    default_department_id = Column(
        Integer,
        ForeignKey("departments.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ── Deprecated (LDAP retired) ─ kept only so legacy rows still load.
    ldap_server_uri = Column(String(255), nullable=True)
    ldap_bind_dn = Column(String(255), nullable=True)
    ldap_bind_password = Column(String(255), nullable=True)
    ldap_base_dn = Column(String(255), nullable=True)
    ldap_user_filter = Column(String(255), nullable=True)
    ldap_start_tls = Column(Boolean, default=False)
    ldap_email_attribute = Column(String(100), nullable=True)
    ldap_display_name_attribute = Column(String(100), nullable=True)

    # ── OIDC ──
    oidc_issuer_url = Column(String(255), nullable=True)
    oidc_client_id = Column(String(255), nullable=True)
    # 加密 envelope（services/auth_provider_secret.encode_oidc_client_secret）
    # 寬度比照其他 envelope 留 String(2000) 以容納 base64 結果。
    oidc_client_secret = Column(String(2000), nullable=True)
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
