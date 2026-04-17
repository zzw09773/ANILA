from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.alert import Alert
from app.models.user import User
from app.schemas.alert import AlertResponse, AlertStatusUpdate, AlertSummary
from app.services.alert_service import (
    acknowledge_alert,
    parse_alert_metadata,
    resolve_alert,
    summarize_alerts,
)
from app.services.audit_service import log_audit_event
from app.services.auth_service import require_admin

router = APIRouter(prefix="/api/alerts", tags=["告警中心"])


def _serialize(alert: Alert) -> dict:
    return {
        "id": alert.id,
        "category": alert.category,
        "severity": alert.severity,
        "source_type": alert.source_type,
        "source_id": alert.source_id,
        "title": alert.title,
        "message": alert.message,
        "status": alert.status,
        "metadata": parse_alert_metadata(alert.metadata_json),
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
    return [_serialize(alert) for alert in query.all()]


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
    return _serialize(alert)


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
    return _serialize(alert)
