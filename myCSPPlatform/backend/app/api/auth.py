from datetime import datetime, timedelta, timezone
from html import escape
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse
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
from app.services.api_key_service import create_api_key
from app.services.audit_service import log_audit_event
from app.services.auth_service import (
    authenticate_user,
    create_tokens,
    get_current_user,
    _load_user_from_payload,
    PENDING_APPROVAL_SENTINEL,
    LOCAL_PASSWORD_DISABLED_SENTINEL,
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

router = APIRouter(prefix="/api/auth", tags=["認證"])


def _require_card_login_enabled() -> None:
    """Endpoint guard：``ENABLE_CARD_LOGIN`` 未啟用時假裝 endpoint 不存在。

    Pattern 對齊 OIDC：未啟用時回 404 而非 403，避免暴露功能 existence。
    """
    if not settings.ENABLE_CARD_LOGIN:
        raise HTTPException(status_code=404)


def _reject_when_card_only() -> None:
    """Branch SSO lockdown：``REQUIRE_CARD_LOGIN_ONLY`` 時封閉非卡片登入路徑。

    回 404 而非 403/410，讓非卡片 endpoint 在內網部署「看起來不存在」 —
    跟 ``_require_card_login_enabled`` 對稱，外部探測無法區分「該功能本來
    就沒做」還是「被政策關掉」。
    """
    if settings.REQUIRE_CARD_LOGIN_ONLY:
        raise HTTPException(status_code=404)


def _mint_sso_api_key(db: Session, user: User) -> str | None:
    """Mint a short-lived (24h) API Key bound to all currently-active models so
    the SPA can immediately call /v1/* after SSO without forcing the user to
    paste a key. Returns the raw key, or None if no active models exist (in
    which case the SPA falls back to its API-Key popover).

    Any prior ``sso-*`` keys for the same user are revoked first so the DB
    does not accumulate orphan keys across repeated OIDC round-trips. The
    raw key is only delivered once (hand-off through the HTML callback) so
    old rows can never be recovered by the SPA anyway.
    """
    model_ids = [
        row.id for row in db.query(ModelRegistry).filter(ModelRegistry.is_active == True).all()
    ]
    if not model_ids:
        return None

    # Revoke previously-minted SSO keys for this user. `sso-*` is a naming
    # convention owned entirely by this function — user-named keys (CLI
    # tokens, etc.) remain active.
    (
        db.query(ApiKey)
        .filter(
            ApiKey.user_id == user.id,
            ApiKey.name.like("sso-%"),
            ApiKey.is_active == True,  # noqa: E712
        )
        .update({"is_active": False}, synchronize_session=False)
    )

    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
    _, raw_key = create_api_key(
        db,
        user_id=user.id,
        name=f"sso-{user.username}-{int(expires_at.timestamp())}",
        model_ids=model_ids,
        expires_at=expires_at,
    )
    return raw_key


def _build_oidc_callback_html(
    tokens: dict, next_path: str, api_key: str | None = None
) -> HTMLResponse:
    """Render the tiny HTML page that finalizes an OIDC round-trip.

    Wave 2: tokens are delivered via ``Set-Cookie`` on this response, not
    injected into ``localStorage`` / ``sessionStorage``. The HTML's only
    job is to redirect the browser back to the SPA's next_path. We keep
    ``tokens`` / ``api_key`` parameters in the signature for transitional
    compatibility with any callers still constructing the response
    manually; neither is embedded in the HTML anymore.
    """
    del tokens, api_key  # no longer embedded — cookies do the handoff
    # B3 縱深：state 端與 query 端都 sanitize 過一次，這裡再 sanitize 一次，
    # 然後 HTML escape — 三層任何一層通過都安全。
    safe_next = escape(sanitize_next_path(next_path))
    body = f"""
<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="0; url={safe_next}">
  <title>登入完成</title>
</head>
<body>
<script>window.location.replace('{safe_next}');</script>
正在完成登入...
</body>
</html>
"""
    return HTMLResponse(body)


@router.get("/providers", response_model=list[PublicAuthProviderResponse])
def public_providers(db: Session = Depends(get_db)):
    providers = list_public_auth_providers(db)
    # Branch SSO：強制卡片登入時，不在 /providers 列出 OIDC providers，
    # 讓 SPA 自然不顯示對應的 tab。已建立的 OIDC provider row 不刪除（admin
    # 切回非鎖死模式時應該還能用）；純粹在邊界 hide 掉。
    if settings.REQUIRE_CARD_LOGIN_ONLY:
        providers = [p for p in providers if p.provider_type != "oidc"]
    return [
        {
            "id": provider.id,
            "name": provider.name,
            "provider_type": provider.provider_type,
            "button_text": provider.button_text,
        }
        for provider in providers
    ]


@router.get("/oidc/{provider_id}/start")
async def start_oidc_login(
    provider_id: int,
    next_path: str = "/",
    db: Session = Depends(get_db),
):
    _reject_when_card_only()
    provider = (
        db.query(AuthProvider)
        .filter(
            AuthProvider.id == provider_id,
            AuthProvider.provider_type == "oidc",
            AuthProvider.is_active == True,
        )
        .first()
    )
    if not provider:
        raise HTTPException(status_code=404, detail="OIDC Provider 不存在")
    authorization_url = await build_oidc_authorization_url(
        provider, next_path=sanitize_next_path(next_path),
    )
    return {"authorization_url": authorization_url}


@router.post("/register", status_code=201)
def register(request: RegisterRequest, http_request: Request, db: Session = Depends(get_db)):
    _reject_when_card_only()
    existing = db.query(User).filter(User.username == request.username).first()
    if existing:
        raise HTTPException(status_code=400, detail="帳號已被使用")
    user = User(
        username=request.username,
        email=request.email,
        hashed_password=hash_password(request.password),
        role="user",
        is_active=True,
        is_approved=False,
    )
    db.add(user)
    db.commit()
    log_audit_event(
        db,
        action="register",
        resource_type="auth",
        actor=user,
        resource_id=user.id,
        detail="使用者送出註冊申請",
        ip_address=http_request.client.host if http_request.client else None,
        commit=True,
    )
    return {"message": "註冊成功，請等待管理員核准後再登入"}


def _finalize_login(response: Response, tokens: dict) -> dict:
    """Attach session cookies to the response and surface the CSRF token
    in the JSON body so the SPA can read it even on its first request."""
    csrf = set_session_cookies(
        response,
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
    )
    return {**tokens, "csrf_token": csrf}


def _stamp_last_login(db: Session, user: User) -> None:
    """Record the current timestamp on the user's profile. Called on every
    successful login path (local, LDAP, OIDC) so the admin user panel can
    show ``last_login_at`` without scanning the audit log."""
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()


@router.post("/login", response_model=TokenResponse)
def login(
    request: LoginRequest,
    http_request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    _reject_when_card_only()
    ip_address = http_request.client.host if http_request.client else None

    if request.auth_source not in (None, "", "local"):
        # LDAP 已自系統移除（將以 SSO 取代），僅保留本地登入 + OIDC callback。
        raise HTTPException(
            status_code=400,
            detail="僅支援本地登入；OIDC 請走 /api/auth/oidc 流程",
        )

    result = authenticate_user(db, request.username, request.password)
    if result is None:
        log_audit_event(
            db,
            action="login",
            resource_type="auth",
            status="failure",
            detail=f"本機登入失敗: {request.username}",
            ip_address=ip_address,
            commit=True,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="帳號或密碼錯誤",
        )
    if result is PENDING_APPROVAL_SENTINEL:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="等待核准中，請通知 admin",
        )
    if result is LOCAL_PASSWORD_DISABLED_SENTINEL:
        # Sprint 6 X / B2：使用者已切換到 SSO-only，引導改走 OIDC。
        log_audit_event(
            db,
            action="login",
            resource_type="auth",
            status="failure",
            detail=f"本機登入被阻擋（local_password_disabled=true）: {request.username}",
            ip_address=ip_address,
            commit=True,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="此帳號已切換為 SSO 登入；請改用單一登入按鈕。",
        )
    tokens = create_tokens(result)
    _stamp_last_login(db, result)
    log_audit_event(
        db,
        actor=result,
        action="login",
        resource_type="auth",
        resource_id=result.id,
        detail="本機登入成功",
        ip_address=ip_address,
        commit=True,
    )
    return _finalize_login(response, tokens)


@router.get("/oidc/{provider_id}/callback", include_in_schema=False)
async def oidc_callback(
    provider_id: int,
    code: str,
    state: str,
    db: Session = Depends(get_db),
):
    _reject_when_card_only()
    provider = (
        db.query(AuthProvider)
        .filter(
            AuthProvider.id == provider_id,
            AuthProvider.provider_type == "oidc",
            AuthProvider.is_active == True,
        )
        .first()
    )
    if not provider:
        return HTMLResponse("OIDC Provider 不存在", status_code=404)

    try:
        state_payload = decode_external_state(state)
        if int(state_payload["provider_id"]) != provider_id:
            raise ValueError("state provider 不一致")
        # state 內有 PKCE verifier 與 nonce，必須完整傳給 authenticate_oidc_code
        # 才能驗 id_token；任何缺漏由該函式 raise ValueError。
        user = await authenticate_oidc_code(db, provider, code, state_payload)
        tokens = create_tokens(user)
        _stamp_last_login(db, user)
        log_audit_event(
            db,
            actor=user,
            action="login",
            resource_type="auth",
            resource_id=user.id,
            detail=f"OIDC 登入成功: {provider.name}",
            commit=True,
        )
        html = _build_oidc_callback_html(
            tokens,
            state_payload.get("next_path", "/"),
        )
        # Cookies carry the session — SPA reads `anila_csrf` (non-httpOnly)
        # on first render and echoes it on mutating requests.
        set_session_cookies(
            html,
            access_token=tokens["access_token"],
            refresh_token=tokens["refresh_token"],
        )
        return html
    except Exception as exc:
        log_audit_event(
            db,
            action="login",
            resource_type="auth",
            status="failure",
            detail=f"OIDC 登入失敗: {provider.name} ({exc})",
            commit=True,
        )
        return HTMLResponse(
            f"<html><body><h3>OIDC 登入失敗</h3><p>{escape(str(exc))}</p><a href='/login'>返回登入頁</a></body></html>",
            status_code=400,
        )


@router.get("/card/challenge", response_model=CardChallengeResponse)
def card_challenge() -> CardChallengeResponse:
    """簽發一條 2 分鐘有效的卡片簽章 challenge。

    Client 流程：
      1. ``GET /api/auth/card/challenge`` → 拿到 ``{challenge_token, nonce, expires_in}``
      2. 開 popup 跟本機 CHT 元件 (``localhost:16888``) 通訊，用 ``nonce`` 當
         ``tbsPackage.tbs`` 簽章
      3. ``POST /api/auth/card/verify`` 帶 ``{challenge_token, signature, card_serial}``

    Endpoint 在 ``ENABLE_CARD_LOGIN=false`` 時回 404。
    """
    _require_card_login_enabled()
    token, nonce, expires_in = issue_card_challenge()
    return CardChallengeResponse(
        challenge_token=token,
        nonce=nonce,
        expires_in=expires_in,
    )


@router.post("/card/verify")
def card_verify(
    request: CardVerifyRequest,
    http_request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """驗證憑證卡簽章並建立 session。

    回應有三種形態（response_model 因此宣告 None；caller 用 ``status`` 欄位判別）：

    1. **登入成功** — ``TokenResponse`` shape，HTTP 200，set-cookie 完成
    2. **Pending registration** — ``CardPendingResponse`` shape，HTTP 202，
       含 ``registration_token``；UI 應該渲染「完成註冊」表單。發生於：
       (a) 第一次刷卡且不在 ``CARD_INITIAL_OWNERS`` 內，
       (b) 已建帳號但 ``department_id IS NULL`` 且仍 ``is_approved=False``。
    3. **Pending approval** — 同 shape 但無 ``registration_token``，HTTP 202；
       UI 顯示「等待管理員核准」訊息。發生於：已填單位但 admin 未核准。

    失敗對應：
      - ``CardAuthError`` (簽章解析失敗 / cert 不合法) → ``401``
      - ``CardLoginRejected`` (challenge 過期 / email 衝突 / 設定錯誤) → ``400``
    """
    _require_card_login_enabled()
    ip_address = http_request.client.host if http_request.client else None

    try:
        user, claims = verify_card_and_resolve_user(
            db,
            signature_b64=request.signature,
            challenge_token=request.challenge_token,
            card_serial=request.card_serial,
        )
    except CardAuthError as exc:
        log_audit_event(
            db,
            action="card_login",
            resource_type="auth",
            status="failure",
            detail=f"憑證卡簽章驗證失敗: {exc}",
            ip_address=ip_address,
            commit=True,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"憑證卡驗證失敗: {exc}",
        ) from exc
    except CardLoginRejected as exc:
        log_audit_event(
            db,
            action="card_login",
            resource_type="auth",
            status="failure",
            detail=f"憑證卡登入拒絕: {exc}",
            ip_address=ip_address,
            commit=True,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    # ── Pending branch ───────────────────────────────────────────────────
    if not user.is_approved:
        if user.department_id is None:
            # Pending registration: 還沒填單位 → 發 short-lived JWT 讓他完成
            reg_token, expires_in = issue_registration_token(user.id)
            payload = {
                "status": "pending_registration",
                "employee_id": claims.employee_id,
                "display_name": claims.display_name,
                "email": claims.email,
                "registration_token": reg_token,
                "expires_in": expires_in,
                "message": "請完成註冊：選擇您所屬的單位後送出。",
            }
            audit_detail = (
                f"卡片驗章通過但 pending_registration: "
                f"employee_id={claims.employee_id} name={claims.display_name}"
            )
        else:
            # Pending approval: 已填單位、等 admin 點頭
            payload = {
                "status": "pending_approval",
                "employee_id": claims.employee_id,
                "display_name": claims.display_name,
                "email": claims.email,
                "registration_token": None,
                "expires_in": None,
                "message": "註冊資料已記錄，請等待管理員核准。",
            }
            audit_detail = (
                f"卡片驗章通過但 pending_approval: "
                f"employee_id={claims.employee_id}"
            )
        log_audit_event(
            db,
            action="card_login",
            resource_type="auth",
            resource_id=user.id,
            status="pending",
            detail=audit_detail,
            ip_address=ip_address,
            commit=True,
        )
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=payload)

    # ── Approved: 正常登入流程 ───────────────────────────────────────────
    tokens = create_tokens(user)
    _stamp_last_login(db, user)
    log_audit_event(
        db,
        actor=user,
        action="card_login",
        resource_type="auth",
        resource_id=user.id,
        detail=(
            f"憑證卡登入成功: employee_id={claims.employee_id} "
            f"name={claims.display_name} card_sn={claims.card_serial or '-'}"
        ),
        ip_address=ip_address,
        commit=True,
    )
    return _finalize_login(response, tokens)


@router.get(
    "/card/registration/departments",
    response_model=list[CardDepartmentOption],
)
def card_registration_departments(db: Session = Depends(get_db)):
    """列出可選的 active departments，給 pending 使用者「完成註冊」表單下拉用。

    Public endpoint（不需要 cookie session）— 因為 pending 使用者本來就還沒
    登入。只回 ``id`` + ``name``，避免暴露管理性 metadata。
    """
    _require_card_login_enabled()
    rows = (
        db.query(Department)
        .filter(Department.is_active == True)  # noqa: E712
        .order_by(Department.name)
        .all()
    )
    return [CardDepartmentOption(id=d.id, name=d.name) for d in rows]


@router.post(
    "/card/complete-registration",
    response_model=CardCompleteRegistrationResponse,
)
def card_complete_registration(
    request: CardCompleteRegistrationRequest,
    http_request: Request,
    db: Session = Depends(get_db),
):
    """Pending 使用者完成註冊：填上 ``department_id``，狀態進到 pending_approval。

    Auth：``registration_token`` JWT (audience=card-registration)，**不**靠
    cookie session（pending 使用者根本沒 cookie）。

    完成後不種 cookie、不發 access token — 使用者仍須等 admin 核准。下次
    刷卡時 ``/card/verify`` 會直接回 ``pending_approval`` 訊息（不再要表單）。
    """
    _require_card_login_enabled()
    ip_address = http_request.client.host if http_request.client else None

    try:
        user_id = decode_registration_token(request.registration_token)
    except CardRegistrationTokenInvalid as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc

    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        # token 對應的 user 被 admin 刪掉之類
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="使用者不存在")
    if user.is_approved:
        # 已核准的使用者不該走這條 endpoint
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="使用者已核准，無需重新完成註冊",
        )

    department = (
        db.query(Department)
        .filter(Department.id == request.department_id, Department.is_active == True)  # noqa: E712
        .first()
    )
    if department is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"指定的 department_id={request.department_id} 不存在或已停用",
        )

    user.department_id = department.id
    db.commit()
    log_audit_event(
        db,
        actor=user,
        action="card_registration",
        resource_type="user",
        resource_id=user.id,
        detail=(
            f"完成註冊：department_id={department.id} ({department.name})"
        ),
        ip_address=ip_address,
        commit=True,
    )
    return CardCompleteRegistrationResponse(
        status="pending_approval",
        message="已記錄您的單位資訊，請等待管理員核准。",
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    http_request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """Rotate access/refresh tokens.

    Refresh token may arrive in either:
    - The ``anila_refresh_token`` cookie (SPA, Wave 2 default; cookie is
      scoped to this path only, never leaks elsewhere), or
    - The JSON body ``{"refresh_token": "..."}`` (SDK / legacy SPA).

    On success we set fresh cookies AND return the tokens in the JSON
    body — the body keeps the SDK path working, the cookies keep the
    browser happy without JS token juggling.
    """
    token = http_request.cookies.get(REFRESH_COOKIE_NAME)
    if not token:
        try:
            payload_body = await http_request.json()
        except Exception:
            payload_body = {}
        token = (payload_body or {}).get("refresh_token")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少 refresh token",
        )
    payload = decode_token(token)
    user = _load_user_from_payload(payload, db, "refresh")
    tokens = create_tokens(user)
    set_session_cookies(
        response,
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
    )
    return tokens


@router.post("/logout")
def logout(
    http_request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """Clear session cookies and bump the user's token_version.

    Bumping ``token_version`` invalidates any outstanding JWTs the user
    already issued — so logout is effective even if an attacker copied
    the access token before logout. Cookie removal handles the active
    browser tab; token_version handles everything else.
    """
    try:
        current_user = get_current_user(http_request, None, db)
    except HTTPException:
        current_user = None

    if current_user is not None:
        current_user.token_version = (current_user.token_version or 0) + 1
        db.commit()
        log_audit_event(
            db,
            actor=current_user,
            action="logout",
            resource_type="auth",
            resource_id=current_user.id,
            detail="使用者登出（cookie 清除 + token_version++）",
            ip_address=http_request.client.host if http_request.client else None,
            commit=True,
        )

    clear_session_cookies(response)
    return {"message": "已登出"}


@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    return current_user


@router.put("/password")
def change_password(
    request: PasswordChangeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Card-only deployments：本機帳密整套都不接受，change_password 自然也封閉。
    # 卡片帳號的 hashed_password 是 unguessable random，使用者本來就提供
    # 不出 current_password；這個 guard 是雙重保險 + 一致性。
    _reject_when_card_only()
    if not verify_password(request.current_password, current_user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="目前密碼不正確",
        )
    current_user.hashed_password = hash_password(request.new_password)
    current_user.token_version = (current_user.token_version or 0) + 1
    db.commit()
    db.refresh(current_user)
    log_audit_event(
        db,
        actor=current_user,
        action="change_password",
        resource_type="auth",
        resource_id=current_user.id,
        detail="使用者更新自身密碼",
        commit=True,
    )
    return {"message": "密碼已更新，請重新登入", **create_tokens(current_user)}
