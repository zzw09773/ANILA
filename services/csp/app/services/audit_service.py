import json
import logging
from sqlalchemy.orm import Session
from app.models.audit_log import AuditLog
from app.models.user import User


logger = logging.getLogger(__name__)

# High-risk callers (approve / deactivate / API key issue+revoke / grants)
# use log_audit_event_or_raise so a failed write becomes HTTP 500 and
# the mutation stays in the same uncommitted session. Low-risk callers keep
# the historic fail-soft log_audit_event.
AUDIT_WRITE_FAILED_DETAIL = "稽核紀錄寫入失敗，變更未生效"


class AuditWriteError(RuntimeError):
    """Raised when strict=True audit logging fails after session rollback."""


# Owner-only fields. Admins see the audit trail for moderation but the
# IP address and request metadata can leak deployment topology / token
# remnants and are reserved for the platform owner. Non-owner viewers
# get a literal sentinel so the column doesn't silently look "always
# blank" — they can still see who/what/when, just not where/how.
SENSITIVE_REDACTED = "<owner-only>"


def serialize_audit_log(
    log: AuditLog, *, caller: User, db: Session | None = None
) -> dict:
    """Serialize an audit row with gated IP / endpoint-bearing metadata.

    Shared by ``GET /api/audit-logs`` and message-action audit export so
    the security-relevant rule has a single source (service layer).

    * ``ip_address`` remains owner-only.
    * ``metadata`` (and any endpoint sentinel embedded in ``detail``) follow
      ``can_see_endpoint_address`` — the same predicate as every other
      endpoint-address face.

    ``is_owner`` / ``can_see_endpoint_address`` are imported lazily to avoid
    a cycle with ``auth_service`` / ``endpoint_author_service`` (which call
    ``log_audit_event``).
    """
    from app.services.auth_service import is_owner
    from app.services.endpoint_author_service import (
        ENDPOINT_INTERNAL,
        ENDPOINT_REDACTED,
        can_see_endpoint_address,
    )

    show_ip = is_owner(caller)
    show_endpoint = can_see_endpoint_address(db, caller)
    meta = parse_metadata(log.metadata_json) if show_endpoint else None
    detail = log.detail
    if (
        show_endpoint
        and detail
        and meta
        and isinstance(meta.get("endpoint_url"), str)
        and meta["endpoint_url"]
    ):
        real = meta["endpoint_url"]
        for sentinel in (ENDPOINT_INTERNAL, ENDPOINT_REDACTED):
            if sentinel in detail:
                detail = detail.replace(sentinel, real)
    return {
        "id": log.id,
        "actor_user_id": log.actor_user_id,
        "actor_username": log.actor_username,
        "action": log.action,
        "resource_type": log.resource_type,
        "resource_id": log.resource_id,
        "status": log.status,
        "detail": detail,
        "ip_address": log.ip_address if show_ip else SENSITIVE_REDACTED,
        "metadata": meta,
        "created_at": log.created_at,
    }


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
    strict: bool = False,
) -> AuditLog | None:
    """寫一筆 audit 事件到 DB。

    Failure mode (db.add / db.commit() / session 異常):

    * default **fail-soft**:捕捉、rollback、log 出完整事件 metadata,**不**
      re-raise。理由:caller (api/auth.py 多處) 在 audit 後緊接
      ``raise HTTPException(401)`` 之類,audit 失敗 cascade 成 500 會掩蓋
      user 該看到的真正錯誤碼;反正 nginx access log + 此處 logger 都能補出
      事發 trace。回 None 給 caller 明確訊號。
    * ``strict=True`` **fail-closed**:同樣 rollback + log,然後 raise
      ``AuditWriteError``。給「事後查帳會痛」的 mutation 用 — 呼叫端不得在
      audit 成功前進 commit。
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
    except Exception as exc:
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
            # fail-soft 路徑只要不 re-raise 就好。
            pass
        if strict:
            raise AuditWriteError(AUDIT_WRITE_FAILED_DETAIL) from exc
        return None


def log_audit_event_or_raise(
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
    """Fail-closed audit write for high-risk mutations.

    Always ``strict=True``. On failure the session is rolled back and this
    raises HTTP 500 so the caller must not leave a silent data change.
    Prefer ``commit=False`` and let the caller commit mutation + audit together.
    """
    from fastapi import HTTPException

    try:
        row = log_audit_event(
            db,
            action=action,
            resource_type=resource_type,
            actor=actor,
            resource_id=resource_id,
            status=status,
            detail=detail,
            ip_address=ip_address,
            metadata=metadata,
            commit=commit,
            strict=True,
        )
    except AuditWriteError as exc:
        raise HTTPException(
            status_code=500, detail=AUDIT_WRITE_FAILED_DETAIL
        ) from exc
    if row is None:
        raise HTTPException(status_code=500, detail=AUDIT_WRITE_FAILED_DETAIL)
    return row


def parse_metadata(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
