import json
from sqlalchemy.orm import Session
from app.models.audit_log import AuditLog
from app.models.user import User


def log_audit_event(
    db: Session,
    *,
    action: str,
    resource_type: str,
    actor: User | None = None,
    resource_id: str | int | None = None,
    status: str = "success",
    detail: str | None = None,
    ip_address: str | None = None,
    metadata: dict | None = None,
    commit: bool = False,
) -> AuditLog:
    event = AuditLog(
        actor_user_id=actor.id if actor else None,
        actor_username=actor.username if actor else None,
        action=action,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        status=status,
        detail=detail,
        ip_address=ip_address,
        metadata_json=json.dumps(metadata, ensure_ascii=False) if metadata else None,
    )
    db.add(event)
    if commit:
        db.commit()
        db.refresh(event)
    return event


def parse_metadata(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
