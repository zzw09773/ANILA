import json
import logging

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.models.audit_log import AuditLog
from app.models.user import User


logger = logging.getLogger(__name__)


def _persist_audit_row(db: Session, event: AuditLog, *, commit: bool) -> AuditLog:
    """實際寫入一列治理稽核。

    抽成獨立函式的唯一理由是**失敗注入的 patch point**(比照
    ``inference_audit._persist_inference_audit_row``)—— 沒有這個接縫,
    strict 模式的 503 行為就只能靠去 monkeypatch ``Session.commit``
    這種會波及全測試的手段來驗。
    """
    db.add(event)
    if commit:
        db.commit()
        db.refresh(event)
    return event


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

    Failure mode (db.commit() / session 異常) 由 ``ANILA_AUDIT_STRICT`` 決定,
    語意與 ``inference_audit.record_inference_audit`` 一致(同一張表、同一個
    旗標,不該有兩套失敗政策):

    * ``ANILA_AUDIT_STRICT=0``(預設,dev/非涉密)採 **fail-soft**:捕捉、
      rollback、log 出完整事件 metadata,**不** re-raise,回 ``None``。理由:
      caller (api/auth.py 多處) 在 audit 後緊接 ``raise HTTPException(401)``
      之類,audit 失敗 cascade 成 500 會掩蓋 user 該看到的真正錯誤碼;反正
      nginx access log + 此處 logger 都能補出事發 trace。
    * ``ANILA_AUDIT_STRICT=1``(formal posture 契約要求值,見
      ``startup_security._FORMAL_PROFILE_POSTURES``)採 **fail-closed**:拋
      ``HTTPException(503)``,請求整個被拒。理由:使用者建立/刪除、權限變更、
      card 登入拒絕這類治理事件靜默丟失,等於稽核軌跡有洞而沒人知道 —— 涉密
      部署寧可拒絕服務也不能少一列。
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
        return _persist_audit_row(db, event, commit=commit)
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
        if settings.ANILA_AUDIT_STRICT:
            # ``from None``:上游 DB 例外已完整 log 過,不要再把 driver 訊息
            # (可能含連線字串片段)串進 HTTP 回應的 __cause__ chain。
            raise HTTPException(
                status_code=503,
                detail="審計紀錄寫入失敗，請求已拒絕",
            ) from None
        return None


def parse_metadata(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
