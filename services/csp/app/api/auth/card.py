"""Card-login endpoints: /card/challenge and /card/verify.

SECURITY-CRITICAL — hardened invariant. Split from the original
``app/api/auth.py`` god-module with the two endpoint bodies moved
byte-for-byte verbatim; only this import header is new. Do not weaken:
signature verification, challenge nonce, audit posture (see
``card_auth.py`` / ``card_auth_service.py``, which are also not to be
modified).
"""
from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.card import (
    CardChallengeResponse,
    CardVerifyRequest,
)
from app.services.audit_service import log_audit_event
from app.services.auth_service import create_tokens
from app.services.card_auth import CardAuthError
from app.services.card_auth_service import (
    CardLoginRejected,
    issue_card_challenge,
    issue_registration_token,
    verify_card_and_resolve_user,
)

from ._common import (
    _finalize_login,
    _require_card_login_enabled,
    _stamp_last_login,
    router,
)


@router.get("/card/challenge", response_model=CardChallengeResponse)
def card_challenge(db: Session = Depends(get_db)) -> CardChallengeResponse:
    """簽發一條 2 分鐘有效的卡片簽章 challenge。

    Client 流程：
      1. ``GET /api/auth/card/challenge`` → 拿到 ``{challenge_token, nonce, expires_in}``
      2. 開 popup 跟本機 CHT 元件 (``localhost:16888``) 通訊，用 ``nonce`` 當
         ``tbsPackage.tbs`` 簽章
      3. ``POST /api/auth/card/verify`` 帶 ``{challenge_token, signature, card_serial}``

    Endpoint 在 ``ENABLE_CARD_LOGIN=false`` 時回 404。
    """
    _require_card_login_enabled()
    token, nonce, expires_in = issue_card_challenge(db)
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
        rejection_metadata = {"reason": exc.reason}
        if exc.challenge_jti_hash:
            rejection_metadata["challenge_jti_hash"] = exc.challenge_jti_hash
        log_audit_event(
            db,
            action="card_login",
            resource_type="auth",
            status="failure",
            detail=f"憑證卡登入拒絕: {exc}",
            ip_address=ip_address,
            metadata=rejection_metadata,
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
    tokens = create_tokens(user, db=db, amr=("sc",))
    _stamp_last_login(db, user)
    log_audit_event(
        db,
        actor=user,
        action="card_login",
        resource_type="auth",
        resource_id=user.id,
        detail=(
            f"憑證卡登入成功: employee_id={claims.employee_id} "
            f"name={claims.display_name} cert_serial={claims.card_serial} "
            f"cert_fp_sha256={claims.certificate_fingerprint_sha256}"
        ),
        ip_address=ip_address,
        commit=True,
    )
    return _finalize_login(response, tokens)
