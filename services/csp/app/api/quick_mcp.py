"""快製平台的 MCP 治理與授權工具目錄。"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.schemas.quick_mcp import MCPServiceApprove, MCPServiceCreate
from app.schemas.quick_builder import QuickBuilderRoute
from app.services import quick_mcp
from app.services.auth_service import get_current_user, require_admin

router = APIRouter(
    prefix="/api/quick-builder/mcp",
    tags=["quick-builder-mcp"],
    route_class=QuickBuilderRoute,
)


@router.get("/tools")
def tools(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return quick_mcp.list_tools(db, user)


@router.get("")
def services(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return quick_mcp.list_services(db, user)


@router.post("", status_code=201)
async def register(
    body: MCPServiceCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    return await quick_mcp.register_service(db, user, body)


@router.post("/{service_id}/approve")
async def approve(
    service_id: int,
    body: MCPServiceApprove,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    return await quick_mcp.approve_service(db, user, service_id, body.approved_tool_ids)


@router.post("/{service_id}/disable")
def disable(
    service_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin)
):
    return quick_mcp.disable_service(db, user, service_id)


@router.post("/{service_id}/refresh")
async def refresh(
    service_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin)
):
    return await quick_mcp.refresh_service(db, user, service_id)
