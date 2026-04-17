from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.audit_log import AuditLog
from app.models.user import User
from app.schemas.audit_log import AuditLogResponse
from app.services.audit_service import parse_metadata
from app.services.auth_service import require_admin

router = APIRouter(prefix="/api/audit-logs", tags=["審計日誌"])


def _serialize(log: AuditLog) -> dict:
    return {
        "id": log.id,
        "actor_user_id": log.actor_user_id,
        "actor_username": log.actor_username,
        "action": log.action,
        "resource_type": log.resource_type,
        "resource_id": log.resource_id,
        "status": log.status,
        "detail": log.detail,
        "ip_address": log.ip_address,
        "metadata": parse_metadata(log.metadata_json),
        "created_at": log.created_at,
    }


@router.get("", response_model=list[AuditLogResponse])
def list_audit_logs(
    action: str | None = None,
    resource_type: str | None = None,
    actor_username: str | None = None,
    status: str | None = Query(None, regex="^(success|failure)$"),
    limit: int = Query(100, ge=1, le=500),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    query = db.query(AuditLog).order_by(AuditLog.created_at.desc())
    if action:
        query = query.filter(AuditLog.action == action)
    if resource_type:
        query = query.filter(AuditLog.resource_type == resource_type)
    if actor_username:
        query = query.filter(AuditLog.actor_username == actor_username)
    if status:
        query = query.filter(AuditLog.status == status)
    return [_serialize(log) for log in query.limit(limit).all()]
