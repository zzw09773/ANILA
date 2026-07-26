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
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.user import User
from app.services.auth_service import is_admin_tier

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


def _assert_binding_ownership(
    db: Session,
    user: User,
    conversation_id: Optional[int],
    message_id: Optional[int],
) -> None:
    """確認要綁定的 conversation / message 都屬於 `user`,否則 403。

    W3-12f。缺這一段的話,`conversation_id` / `message_id` 是**從 form 直接收下
    的攻擊者可控值**,寫進 `Attachment` 列不經任何檢查 → A 可以把附件綁到 B 的
    對話上。下載端有擋(`get_attachment` 比對 `uploaded_by`),所以 bytes 不洩漏
    —— 這個缺口的方向是**寫入**:

    - B 的對話從此多出一列 `attachments`,而 `filename` 是 A 控制的字串 → 前端
      附件 chip(W3-12d)會把它渲染在 B 的對話裡,是跨使用者內容注入。
    - 若 B 的對話是機密等級,這是一筆未經稽核、未經分級判定的寫入,直接落進
      受管容器 —— 而附件本身還在分類/retention 體系外(W3-12e)。

    三件事都要驗,少一件就能繞過:
    1. `conversation_id` 屬於自己;
    2. `message_id` 所屬的 conversation 屬於自己(**只給 message_id 是第二個
       入口,而且更隱蔽**);
    3. 兩者同時給時要**一致** —— 否則用自己的 `conversation_id` 配別人的
       `message_id` 就繞過了前兩項。

    找不到的 id 一律回 403 而非 404:回 404 會讓這支端點變成「某 id 是否存在」
    的 oracle,而 conversation id 是連號整數。

    **沒有 admin bypass**,這是刻意的:`api/conversations.py:277` 硬綁
    `user_id == current_user.id`,admin 連讀別人的對話都不行,寫入自然更不該開。
    """
    if conversation_id is None and message_id is None:
        return

    if conversation_id is not None:
        owns_conversation = (
            db.query(Conversation.id)
            .filter(
                Conversation.id == conversation_id,
                Conversation.user_id == user.id,
            )
            .first()
        )
        if owns_conversation is None:
            raise HTTPException(status_code=403, detail="無權將附件綁定至此對話")

    if message_id is not None:
        row = (
            db.query(Message.conversation_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .filter(Message.id == message_id, Conversation.user_id == user.id)
            .first()
        )
        if row is None:
            raise HTTPException(status_code=403, detail="無權將附件綁定至此訊息")
        if conversation_id is not None and row[0] != conversation_id:
            raise HTTPException(
                status_code=400,
                detail="附件的 conversation_id 與 message_id 不一致",
            )


async def upload_attachment(
    db: Session,
    file: UploadFile,
    user: User,
    conversation_id: Optional[int] = None,
    message_id: Optional[int] = None,
) -> Attachment:
    # 擁有權先驗 —— 在讀檔與落地之前,不要為一個必然被拒的請求收 50MB 進記憶體。
    _assert_binding_ownership(db, user, conversation_id, message_id)

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
    # uploader or admin-tier (admin/owner) may download
    if not is_admin_tier(user) and att.uploaded_by != user.id:
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
