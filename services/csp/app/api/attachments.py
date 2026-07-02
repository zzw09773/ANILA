"""Attachment upload/download endpoints."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.database import get_db
from app.models.user import User
from app.services.attachment_service import (
    delete_attachment,
    get_attachment,
    upload_attachment,
)

router = APIRouter(prefix="/api/attachments", tags=["attachments"])


class AttachmentOut(BaseModel):
    reference_id: str
    filename: str
    content_type: str
    size_bytes: int
    conversation_id: Optional[int]
    message_id: Optional[int]
    created_at: datetime
    model_config = {"from_attributes": True}


@router.post("", response_model=AttachmentOut, status_code=201)
async def upload(
    file: UploadFile = File(...),
    conversation_id: Optional[int] = Form(None),
    message_id: Optional[int] = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await upload_attachment(
        db, file, current_user,
        conversation_id=conversation_id,
        message_id=message_id,
    )


@router.get("/{reference_id}")
def download(
    reference_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    att, path = get_attachment(db, reference_id, current_user)
    return FileResponse(
        str(path),
        media_type=att.content_type,
        filename=att.filename,
    )


@router.get("/{reference_id}/meta", response_model=AttachmentOut)
def get_meta(
    reference_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    att, _ = get_attachment(db, reference_id, current_user)
    return att


@router.delete("/{reference_id}", status_code=204)
def delete(
    reference_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    delete_attachment(db, reference_id, current_user)
