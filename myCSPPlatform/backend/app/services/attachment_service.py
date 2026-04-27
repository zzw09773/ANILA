"""Attachment upload/download service backed by local filesystem."""
from __future__ import annotations

import mimetypes
import os
import uuid
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.config import settings
from app.models.attachment import Attachment
from app.models.user import User

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB

# L5: 改用 allow-list — 只允許平台明確支援的文件 / 圖檔型別。其餘一律
# 拒絕，比 deny-list 更不易因新副檔名漏網。
ALLOWED_EXTENSIONS = {
    # 文件
    ".pdf", ".txt", ".md", ".csv", ".tsv", ".json", ".log",
    ".doc", ".docx", ".odt", ".rtf",
    ".ppt", ".pptx", ".odp",
    ".xls", ".xlsx", ".ods",
    # 圖檔（聊天附件可能會貼）
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg",
    # 純文字 / 程式碼
    ".html", ".htm", ".xml", ".yaml", ".yml", ".toml", ".ini",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".sql",
    # 壓縮
    ".zip",
}

ALLOWED_MIME_PREFIXES = (
    "image/",
    "text/",
    "application/json",
    "application/pdf",
    "application/zip",
    "application/vnd.openxmlformats-officedocument.",  # docx / pptx / xlsx
    "application/vnd.oasis.opendocument.",  # odt / ods / odp
    "application/msword",
    "application/vnd.ms-",  # .ppt / .xls 舊格式
    "application/x-yaml",
)


def _storage_root() -> Path:
    root = Path(settings.ATTACHMENT_STORAGE_PATH)
    root.mkdir(parents=True, exist_ok=True)
    return root


async def upload_attachment(
    db: Session,
    file: UploadFile,
    user: User,
    conversation_id: Optional[int] = None,
    message_id: Optional[int] = None,
) -> Attachment:
    filename = file.filename or "upload"
    ext = Path(filename).suffix.lower()
    if ext and ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"不允許上傳 {ext} 類型的檔案")

    declared_mime = (file.content_type or "").split(";")[0].strip().lower()
    if declared_mime and not any(
        declared_mime == m or declared_mime.startswith(m)
        for m in ALLOWED_MIME_PREFIXES
    ):
        raise HTTPException(
            status_code=400,
            detail=f"不允許上傳 {declared_mime!r} 類型的檔案",
        )

    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"檔案超過大小限制 ({MAX_FILE_SIZE // 1024 // 1024} MB)",
        )

    ref_id = str(uuid.uuid4())
    content_type = (
        file.content_type
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )
    # Store under <root>/<user_id>/<ref_id><ext>
    subdir = _storage_root() / str(user.id)
    subdir.mkdir(parents=True, exist_ok=True)
    storage_path = str(subdir / f"{ref_id}{ext}")
    with open(storage_path, "wb") as f:
        f.write(content)

    # Store relative path only
    rel_path = os.path.relpath(storage_path, str(_storage_root()))

    att = Attachment(
        reference_id=ref_id,
        conversation_id=conversation_id,
        message_id=message_id,
        uploaded_by=user.id,
        filename=filename,
        content_type=content_type,
        size_bytes=len(content),
        storage_path=rel_path,
    )
    db.add(att)
    db.commit()
    db.refresh(att)
    return att


def get_attachment(db: Session, reference_id: str, user: User) -> tuple[Attachment, Path]:
    att = db.query(Attachment).filter(Attachment.reference_id == reference_id).first()
    if not att:
        raise HTTPException(status_code=404, detail="找不到此附件")
    # Only uploader or admin may download
    if user.role != "admin" and att.uploaded_by != user.id:
        raise HTTPException(status_code=403, detail="無權存取此附件")
    full_path = _storage_root() / att.storage_path
    if not full_path.is_file():
        raise HTTPException(status_code=404, detail="附件檔案不存在")
    return att, full_path


def delete_attachment(db: Session, reference_id: str, user: User) -> None:
    att, full_path = get_attachment(db, reference_id, user)
    try:
        full_path.unlink(missing_ok=True)
    except OSError:
        pass
    db.delete(att)
    db.commit()
