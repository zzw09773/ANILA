import json
import logging
from sqlalchemy.orm import Session
from app.models.audit_log import AuditLog
from app.models.user import User


logger = logging.getLogger(__name__)


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
) -> AuditLog | None:
    """寫一筆 audit 事件到 DB。

    Failure mode (db.commit() / session 異常) 採 **fail-soft**:捕捉、rollback、
    log 出完整事件 metadata,**不** re-raise。理由:caller (api/auth.py 多處)
    在 audit 後緊接 ``raise HTTPException(401)`` 之類,audit 失敗 cascade 成
    500 會掩蓋 user 該看到的真正錯誤碼;反正 nginx access log + 此處 logger
    都能補出事發 trace。回 None 給 caller 明確訊號,目前所有 caller 都丟棄
    回傳值,影響範圍 0。
    """
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
    try:
        db.add(event)
        if commit:
            db.commit()
            db.refresh(event)
        return event
    except Exception:
        logger.exception(
            "audit_log 寫入失敗:action=%s resource=%s status=%s actor=%s detail=%s",
            action, resource_type, status,
            actor.username if actor else None,
            detail,
        )
        try:
            db.rollback()
        except Exception:
            # session 可能已不可用;FastAPI 結束 request 時 dependency 會 cleanup,
            # 這裡只要不 re-raise 就好。
            pass
        return None


def parse_metadata(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
