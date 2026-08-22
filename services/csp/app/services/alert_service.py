import json
from datetime import datetime, timezone
from sqlalchemy import func
from sqlalchemy.orm import Session
from app.models.alert import Alert
from app.models.user import User


def upsert_alert(
    db: Session,
    *,
    fingerprint: str,
    category: str,
    severity: str,
    title: str,
    message: str,
    source_type: str | None = None,
    source_id: str | int | None = None,
    metadata: dict | None = None,
) -> Alert:
    now = datetime.now(timezone.utc)
    alert = db.query(Alert).filter(Alert.fingerprint == fingerprint).first()
    if alert:
        alert.category = category
        alert.severity = severity
        alert.title = title
        alert.message = message
        alert.source_type = source_type
        alert.source_id = str(source_id) if source_id is not None else None
        alert.metadata_json = json.dumps(metadata, ensure_ascii=False) if metadata else None
        alert.last_seen_at = now
        if alert.status == "resolved":
            alert.status = "open"
            alert.resolved_at = None
            alert.acknowledged_at = None
            alert.acknowledged_by_user_id = None
        return alert

    alert = Alert(
        fingerprint=fingerprint,
        category=category,
        severity=severity,
        title=title,
        message=message,
        source_type=source_type,
        source_id=str(source_id) if source_id is not None else None,
        metadata_json=json.dumps(metadata, ensure_ascii=False) if metadata else None,
        first_seen_at=now,
        last_seen_at=now,
    )
    db.add(alert)
    return alert


def acknowledge_alert(db: Session, alert: Alert, actor: User | None = None) -> Alert:
    alert.status = "acknowledged"
    alert.acknowledged_at = datetime.now(timezone.utc)
    alert.acknowledged_by_user_id = actor.id if actor else None
    return alert


def resolve_alert(db: Session, alert: Alert) -> Alert:
    alert.status = "resolved"
    alert.resolved_at = datetime.now(timezone.utc)
    alert.last_seen_at = alert.resolved_at
    return alert


def resolve_alert_by_fingerprint(db: Session, fingerprint: str) -> Alert | None:
    alert = (
        db.query(Alert)
        .filter(Alert.fingerprint == fingerprint, Alert.status != "resolved")
        .first()
    )
    if not alert:
        return None
    return resolve_alert(db, alert)


def summarize_alerts(db: Session) -> dict:
    rows = (
        db.query(Alert.status, func.count(Alert.id))
        .group_by(Alert.status)
        .all()
    )
    counts = {status: count for status, count in rows}
    # 只有「未處理」(open) 的高嚴重度才算「待立即處理」。
    # 已確認 (acknowledged)＝使用者已接手靜音，不該再被首頁逼著「立即處理」——
    # DK-4 那 2 筆已確認的陳年模型離線告警正是被算進「請立即處理」的元兇。
    high_count = (
        db.query(func.count(Alert.id))
        .filter(Alert.status == "open", Alert.severity.in_(["high", "critical"]))
        .scalar()
    )
    return {
        "open_count": counts.get("open", 0),
        "acknowledged_count": counts.get("acknowledged", 0),
        "resolved_count": counts.get("resolved", 0),
        "high_count": high_count or 0,
    }


def parse_alert_metadata(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
