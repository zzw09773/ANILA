"""Attachment upload/download endpoints."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session, defer

from app.api.auth import get_current_user
from app.api.conversations import router as conversations_router
from app.database import get_db
from app.middleware.caller import Caller
from app.models.attachment import Attachment
from app.models.user import User
from app.schemas.attachment import (
    AttachmentBindOut,
    AttachmentBindRequest,
    AttachmentOut,
    ConversationAttachmentsOut,
    ConversationCapacity,
)
from app.services.attachment_service import (
    bind_attachments,
    capacity_for_conversation,
    delete_attachment,
    extract_attachment_text,
    get_attachment,
    upload_attachment,
)

router = APIRouter(prefix="/api/attachments", tags=["attachments"])


def _capacity_model(raw: dict) -> ConversationCapacity:
    return ConversationCapacity(
        used_tokens=raw["used_tokens"],
        budget_tokens=raw["budget_tokens"],
        remaining_tokens=raw["remaining_tokens"],
        over_budget_tokens=raw.get("over_budget_tokens", 0),
        percent=raw["percent"],
        attachment_count=raw["attachment_count"],
    )


def _attachment_out(
    att: Attachment,
    *,
    capacity: dict | None = None,
    budget_admitted: bool = False,
) -> AttachmentOut:
    return AttachmentOut(
        reference_id=att.reference_id,
        filename=att.filename,
        content_type=att.content_type,
        size_bytes=att.size_bytes,
        conversation_id=att.conversation_id,
        message_id=att.message_id,
        created_at=att.created_at,
        extract_status=att.extract_status or "pending",
        budget_admitted=budget_admitted,
        token_count=att.token_count,
        extract_error=att.extract_error,
        conversation_capacity=_capacity_model(capacity) if capacity else None,
    )


def _admitted_set(capacity: dict | None) -> set[int]:
    if not capacity:
        return set()
    return set(capacity.get("admitted_ids") or [])


def _require_attachment_write_target(
    db: Session,
    current_user: User,
    *,
    conversation_id: Optional[int],
    message_id: Optional[int],
) -> tuple[Optional[int], Optional[int]]:
    """Resolve upload target to a conversation the caller may write.

    Same predicate as chat / list (``proxy._require_conversation_access``).
    Access is checked before mismatch reporting so a foreign ``message_id``
    cannot be distinguished from a missing one via 400 vs 404.
    """
    from app.api.proxy import _require_conversation_access
    from app.models.message import Message

    caller = Caller(user=current_user, api_key_id=None)
    if message_id is not None:
        msg = db.get(Message, message_id)
        if msg is None:
            raise HTTPException(status_code=404, detail="找不到此訊息")
        try:
            _require_conversation_access(db, caller, msg.conversation_id)
        except HTTPException:
            # Collapse unauthorised ↔ missing (no message-id oracle).
            raise HTTPException(status_code=404, detail="找不到此訊息") from None
        if (
            conversation_id is not None
            and msg.conversation_id != conversation_id
        ):
            raise HTTPException(
                status_code=400,
                detail="message_id 與 conversation_id 不屬於同一對話",
            )
        return msg.conversation_id, message_id
    if conversation_id is not None:
        _require_conversation_access(db, caller, conversation_id)
    return conversation_id, message_id


@router.post("", response_model=AttachmentOut, status_code=201)
async def upload(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    conversation_id: Optional[int] = Form(None),
    message_id: Optional[int] = Form(None),
    # Optional: client names the conversation's selected model so the meter
    # matches admission. Absent → configured default window.
    model: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Same conversation-access rule as the chat path / list endpoint —
    # do not invent a second rule. Missing this check lets user A place
    # extracted text into user B's model prompt via conversation_id or
    # message_id.
    conversation_id, message_id = _require_attachment_write_target(
        db, current_user,
        conversation_id=conversation_id,
        message_id=message_id,
    )
    att = await upload_attachment(
        db, file, current_user,
        conversation_id=conversation_id,
        message_id=message_id,
    )
    # Schedule extraction after commit; BackgroundTasks runs post-response.
    # Tests call extract_attachment_text(id, db=...) directly against the
    # fixture session (SessionLocal points at a different SQLite URL).
    background_tasks.add_task(extract_attachment_text, att.id)

    capacity = None
    if att.conversation_id is not None:
        capacity = capacity_for_conversation(
            db, att.conversation_id, model_name=model,
        )
    # Fresh upload is still pending → not admitted yet.
    return _attachment_out(att, capacity=capacity, budget_admitted=False)


@router.post("/bind", response_model=AttachmentBindOut)
def bind(
    body: AttachmentBindRequest,
    model: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Attach orphan (or already-this-conversation) files to a conversation.

    Must be declared before ``/{reference_id}`` so ``bind`` is not captured
    as a download id. Write access reuses the chat-path predicate.
    """
    from app.api.proxy import _require_conversation_access

    _require_conversation_access(
        db, Caller(user=current_user, api_key_id=None), body.conversation_id,
    )
    rows = bind_attachments(
        db, current_user, body.conversation_id, body.reference_ids,
        message_id=body.message_id,
    )
    capacity = capacity_for_conversation(
        db, body.conversation_id, model_name=model,
    )
    admitted = _admitted_set(capacity)
    return AttachmentBindOut(
        conversation_id=body.conversation_id,
        attachments=[
            _attachment_out(a, budget_admitted=a.id in admitted)
            for a in rows
        ],
        conversation_capacity=_capacity_model(capacity),
    )


@router.get("/{reference_id}")
def download(
    reference_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    att, path = get_attachment(db, reference_id, current_user)
    media = att.content_type or "application/octet-stream"
    return FileResponse(
        str(path),
        media_type=media,
        filename=att.filename,
        content_disposition_type=(
            "inline" if media.startswith("image/") else "attachment"
        ),
    )


@router.get("/{reference_id}/meta", response_model=AttachmentOut)
def get_meta(
    reference_id: str,
    # Optional: client names the conversation's selected model so the meter
    # matches admission. Absent → configured default window.
    model: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    att, _ = get_attachment(db, reference_id, current_user)
    capacity = None
    admitted = set()
    if att.conversation_id is not None:
        capacity = capacity_for_conversation(
            db, att.conversation_id, model_name=model,
        )
        admitted = _admitted_set(capacity)
    return _attachment_out(
        att,
        capacity=capacity,
        budget_admitted=att.id in admitted,
    )


@router.delete("/{reference_id}", response_model=ConversationCapacity)
def delete(
    reference_id: str,
    # Optional: client names the conversation's selected model so the meter
    # matches admission. Absent → configured default window.
    model: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    conversation_id = delete_attachment(db, reference_id, current_user)
    if conversation_id is None:
        # Orphan attachment (no conversation) — return an empty capacity.
        return ConversationCapacity(
            used_tokens=0,
            budget_tokens=0,
            remaining_tokens=0,
            over_budget_tokens=0,
            percent=0,
            attachment_count=0,
        )
    return _capacity_model(
        capacity_for_conversation(db, conversation_id, model_name=model),
    )


@conversations_router.get(
    "/{conversation_id}/attachments",
    response_model=ConversationAttachmentsOut,
    tags=["attachments"],
)
def list_conversation_attachments(
    conversation_id: int,
    # Optional: client names the conversation's selected model so the meter
    # matches admission. Absent → configured default window.
    model: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List a conversation's attachments + capacity meter.

    Authorisation reuses the chat-path rule
    (``proxy._require_conversation_access``): owner or admin-tier.

    ``budget_admitted`` on each row is derived for this request's model —
    independent of the persisted ``extract_status``.
    """
    # Lazy import avoids attachments ↔ proxy cycle at module load.
    from app.api.proxy import _require_conversation_access

    _require_conversation_access(
        db, Caller(user=current_user, api_key_id=None), conversation_id,
    )
    # 回應不含 extracted_text(可能數 MB)。用 defer 排除該欄位而非 load_only
    # 逐一列舉:列舉一旦漏欄位,序列化會靠 lazy load 補撈成 N+1,實體脫離
    # session 時還會直接失敗。
    rows = (
        db.query(Attachment)
        .options(defer(Attachment.extracted_text))
        .filter(Attachment.conversation_id == conversation_id)
        .order_by(Attachment.created_at.asc(), Attachment.id.asc())
        .all()
    )
    capacity = capacity_for_conversation(
        db, conversation_id, model_name=model,
    )
    admitted = _admitted_set(capacity)
    return ConversationAttachmentsOut(
        attachments=[
            _attachment_out(a, budget_admitted=a.id in admitted)
            for a in rows
        ],
        conversation_capacity=_capacity_model(capacity),
    )
