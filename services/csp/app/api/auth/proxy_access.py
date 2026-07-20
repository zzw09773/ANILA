"""Authorization probes consumed by nginx ``auth_request``.

These endpoints intentionally return no user data.  They only establish the
minimum policy decision needed at the reverse-proxy boundary before a request
can reach a private asset or retained intranet developer tool.
"""

from fastapi import Depends, HTTPException, Request, Response, status

from app.models.user import User
from app.services.auth_service import get_current_user, is_admin_tier

from ._common import router


def _require_smart_card_claim(request: Request) -> None:
    claims = getattr(request.state, "auth_claims", None)
    amr = claims.get("amr") if isinstance(claims, dict) else None
    if not isinstance(amr, list) or "sc" not in amr:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要憑證卡登入",
        )


@router.get("/proxy-access/card-session", status_code=status.HTTP_204_NO_CONTENT)
def allow_card_session_proxy_access(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> Response:
    """Allow an active, cryptographically verified smart-card session."""
    _require_smart_card_claim(request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/proxy-access/card-developer", status_code=status.HTTP_204_NO_CONTENT)
def allow_card_developer_proxy_access(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> Response:
    """Allow smart-card developer/admin/owner sessions to reach tooling."""
    _require_smart_card_claim(request)
    if current_user.role != "developer" and not is_admin_tier(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要開發者權限",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/proxy-access/card-admin", status_code=status.HTTP_204_NO_CONTENT)
def allow_card_admin_proxy_access(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> Response:
    """Allow smart-card admin/owner sessions to reach opt-in code-server."""
    _require_smart_card_claim(request)
    if not is_admin_tier(current_user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="需要管理員權限",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
