from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from anila_core.security.url_guard import UnsafeEndpointError

from app.database import get_db
from app.models.alert import Alert
from app.models.user import User
from app.schemas.alert import AlertResponse, AlertStatusUpdate, AlertSummary
from app.services.alert_mail import (
    AlertMailConfigError,
    ensure_settings,
    public_view,
    save_mail_settings,
    send_test_mail,
)
from app.services.alert_service import (
    acknowledge_alert,
    parse_alert_metadata,
    resolve_alert,
    summarize_alerts,
)
from app.services.audit_service import log_audit_event
from app.services.auth_service import require_admin
from app.services.endpoint_author_service import can_see_endpoint_address

router = APIRouter(prefix="/api/alerts", tags=["告警中心"])


class AlertMailUpdate(BaseModel):
    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    security: str = "starttls"
    username: str = ""
    password: str | None = None
    clear_password: bool = False
    from_address: str = ""
    recipients: str = ""


def _serialize(alert: Alert, *, caller: User, db: Session | None = None) -> dict:
    """Serialize an alert with gated endpoint-bearing metadata.

    Same predicate as every other endpoint-address face
    (``can_see_endpoint_address``): structured metadata can carry endpoint
    addresses, so undesignated viewers receive ``None``. Title/message stay
    visible so an administrator can still tell which model is unhealthy.
    """
    show_metadata = can_see_endpoint_address(db, caller)
    return {
        "id": alert.id,
        "category": alert.category,
        "severity": alert.severity,
        "source_type": alert.source_type,
        "source_id": alert.source_id,
        "title": alert.title,
        "message": alert.message,
        "status": alert.status,
        "metadata": (
            parse_alert_metadata(alert.metadata_json) if show_metadata else None
        ),
        "first_seen_at": alert.first_seen_at,
        "last_seen_at": alert.last_seen_at,
        "acknowledged_at": alert.acknowledged_at,
        "acknowledged_by_user_id": alert.acknowledged_by_user_id,
        "acknowledged_by_username": alert.acknowledged_by.username if alert.acknowledged_by else None,
        "resolved_at": alert.resolved_at,
    }


@router.get("", response_model=list[AlertResponse])
def list_alerts(
    status: str | None = Query(None, regex="^(open|acknowledged|resolved)$"),
    severity: str | None = Query(None, regex="^(low|medium|high|critical)$"),
    category: str | None = None,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    query = db.query(Alert).order_by(Alert.last_seen_at.desc())
    if status:
        query = query.filter(Alert.status == status)
    if severity:
        query = query.filter(Alert.severity == severity)
    if category:
        query = query.filter(Alert.category == category)
    return [_serialize(alert, caller=admin, db=db) for alert in query.all()]


@router.get("/mail")
def get_alert_mail(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    row = ensure_settings(db)
    db.commit()
    return public_view(row)


@router.put("/mail")
def put_alert_mail(
    body: AlertMailUpdate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        row = save_mail_settings(
            db,
            enabled=body.enabled,
            smtp_host=body.smtp_host,
            smtp_port=body.smtp_port,
            security=body.security,
            username=body.username,
            password=body.password,
            password_set=bool(body.password),
            clear_password=body.clear_password,
            from_address=body.from_address,
            recipients=body.recipients,
            actor=admin,
        )
    except AlertMailConfigError as exc:
        raise HTTPException(status_code=400, detail=exc.public_message) from exc
    except UnsafeEndpointError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    db.refresh(row)
    return public_view(row)


@router.post("/mail/test")
def post_alert_test_mail(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    del admin
    try:
        error = send_test_mail(db)
    except AlertMailConfigError as exc:
        raise HTTPException(status_code=400, detail=exc.public_message) from exc
    except UnsafeEndpointError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    if error:
        return {"ok": False, "error": error}
    return {"ok": True, "error": None}


@router.get("/summary", response_model=AlertSummary)
def alert_summary(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return summarize_alerts(db)


@router.post("/{alert_id}/ack", response_model=AlertResponse)
def ack_alert(
    alert_id: int,
    request: AlertStatusUpdate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="告警不存在")
    acknowledge_alert(db, alert, actor=admin)
    log_audit_event(
        db,
        actor=admin,
        action="ack",
        resource_type="alert",
        resource_id=alert.id,
        detail=request.note or alert.title,
    )
    db.commit()
    db.refresh(alert)
    return _serialize(alert, caller=admin, db=db)


@router.post("/{alert_id}/resolve", response_model=AlertResponse)
def resolve_alert_manually(
    alert_id: int,
    request: AlertStatusUpdate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="告警不存在")
    resolve_alert(db, alert)
    log_audit_event(
        db,
        actor=admin,
        action="resolve",
        resource_type="alert",
        resource_id=alert.id,
        detail=request.note or alert.title,
    )
    db.commit()
    db.refresh(alert)
    return _serialize(alert, caller=admin, db=db)
