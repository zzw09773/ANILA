"""Card registration-token endpoints: departments listing + complete-registration.

Split from the original ``app/api/auth.py`` god-module — bodies moved
verbatim; only this import header is new.
"""
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.department import Department
from app.models.user import User
from app.schemas.card import (
    CardCompleteRegistrationRequest,
    CardCompleteRegistrationResponse,
    CardDepartmentOption,
)
from app.services.audit_service import log_audit_event
from app.services.card_auth_service import (
    CardRegistrationTokenInvalid,
    decode_registration_token,
)

from ._common import _require_card_login_enabled, router


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
