"""``app.api.auth`` — auth router package.

Split from the original single-file ``app/api/auth.py`` (837L) into
submodules with zero behavior change:

- ``_common``             shared ``router`` + cross-submodule helpers
- ``revocations``         GET /revocations feed
- ``oidc``                providers list + OIDC start/callback
- ``password``            register/login/refresh/logout/me/password
- ``card``                card challenge/verify (SECURITY-CRITICAL, verbatim)
- ``registration_tokens`` card registration departments/complete-registration

Every module-level symbol of the old module is re-exported here so
existing imports and test monkeypatch targets (``app.api.auth.<name>``)
keep resolving unchanged.
"""
from datetime import datetime, timedelta, timezone
from html import escape
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from app.database import get_db
from app.middleware.cookies import (
    REFRESH_COOKIE_NAME,
    clear_session_cookies,
    set_session_cookies,
)
from app.models.api_key import ApiKey
from app.models.auth_provider import AuthProvider
from app.models.department import Department
from app.models.model_registry import ModelRegistry
from app.models.token_revocation import TokenRevocation
from app.models.user import User
from app.config import settings
from app.schemas.card import (
    CardChallengeResponse,
    CardCompleteRegistrationRequest,
    CardCompleteRegistrationResponse,
    CardDepartmentOption,
    CardVerifyRequest,
)
from app.schemas.user import (
    LoginRequest,
    TokenResponse,
    RefreshRequest,
    PasswordChangeRequest,
    UserResponse,
    RegisterRequest,
)
from app.schemas.auth_provider import PublicAuthProviderResponse
from app.services import agent_credential_service
from app.services.api_key_service import create_api_key
from app.services.audit_service import log_audit_event
from app.services.auth_service import (
    authenticate_user,
    create_tokens,
    get_current_user,
    verify_service_token,
    _load_user_from_payload,
    PENDING_APPROVAL_SENTINEL,
    LOCAL_PASSWORD_DISABLED_SENTINEL,
)
from app.services.token_revocation_publisher import (  # noqa: F401
    publish_revocation,
    publish_revocation_sync,
)
from app.services.card_auth import CardAuthError
from app.services.card_auth_service import (
    CardLoginRejected,
    CardRegistrationTokenInvalid,
    decode_registration_token,
    issue_card_challenge,
    issue_registration_token,
    verify_card_and_resolve_user,
)
from app.services.external_auth_service import (
    authenticate_oidc_code,
    build_oidc_authorization_url,
    decode_external_state,
    list_public_auth_providers,
    sanitize_next_path,
)
from app.utils.security import decode_token, hash_password, verify_password

from ._common import (  # noqa: F401
    _finalize_login,
    _reject_when_card_only,
    _require_card_login_enabled,
    _stamp_last_login,
    router,
)
from .revocations import (  # noqa: F401
    TOKEN_REVOCATION_RETENTION_DAYS,
    RevocationEntry,
    RevocationListResponse,
    RevokeUserTokensRequest,
    RevokeUserTokensResponse,
    _serialise_ts,
    list_revocations,
    revoke_user_tokens,
)
from .oidc import (  # noqa: F401
    _build_oidc_callback_html,
    _mint_sso_api_key,
    oidc_callback,
    public_providers,
    start_oidc_login,
)
from .password import (  # noqa: F401
    change_password,
    get_me,
    login,
    logout,
    refresh,
    register,
)
from .card import (  # noqa: F401
    card_challenge,
    card_verify,
)
from .registration_tokens import (  # noqa: F401
    card_complete_registration,
    card_registration_departments,
)
