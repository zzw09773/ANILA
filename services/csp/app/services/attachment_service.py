"""Attachment upload/download service backed by local filesystem.

P1.5：上傳後非同步抽文字、估算 token。extract_status 只記抽取結果
（pending / ok / failed / unsupported / too_large）；是否塞進某次
對話的 context 由 attachment_context.admit() 在使用當下依模型預算推導。
"""
from __future__ import annotations

import logging
import mimetypes
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, UploadFile
from sqlalchemy.orm import Session, defer

from app.database import SessionLocal
from app.models.attachment import Attachment
from app.models.user import User
from app.services.attachment_context import (
    get_context_window,
    get_conversation_attachment_usage,
    max_stored_tokens,
)
from app.services.auth_service import is_admin_tier
from app.services.proxy import _estimate_token_count
from app.services.storage_paths import ATTACHMENT_STORAGE_ROOT

logger = logging.getLogger(__name__)

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
    # 純文字 / 程式碼（與 parser registry 文字類對齊；.zip 可上傳但解析仍 unsupported）
    ".html", ".htm", ".xml", ".yaml", ".yml", ".toml", ".ini",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".sql",
    ".sh", ".bash", ".c", ".cpp", ".h", ".hpp", ".rb", ".php", ".r", ".m", ".tex",
    # USAF Digital DATCOM / 工程純文字（.dat/.out 他工具也可能是二進位——靠內容閘拒絕）
    ".dcm", ".dat", ".inp", ".out",
    # 壓縮
    ".zip",
}

# 副檔名是真正的閘門；主流瀏覽器／Python mimetypes 對原始碼常送
# application/x-*、video/mp2t(.ts)、octet-stream 等。MIME prefix 檢查只套在
# 「有副檔名且非文字類」的上傳（PDF / Office / 圖 / zip）；文字類與無副檔名
# （for005 / README 等）跳過 MIME，由 extract 內容閘決定。無副檔名視為未定型
# 可接受——Content-Disposition: attachment 已關閉 XSS 路徑。
# 此集合＝ALLOWED 內會走 PlainTextParser 的文字類。
TEXT_CLASS_EXTENSIONS = frozenset({
    ".txt", ".md", ".csv", ".tsv", ".json", ".log",
    ".html", ".htm", ".xml", ".yaml", ".yml", ".toml", ".ini",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs", ".sql",
    ".sh", ".bash", ".c", ".cpp", ".h", ".hpp", ".rb", ".php", ".r", ".m", ".tex",
    ".dcm", ".dat", ".inp", ".out",
})

ALLOWED_MIME_PREFIXES = (
    "image/",
    "text/",
    "application/json",
    "application/pdf",
    "application/rtf",
    "application/zip",
    "application/vnd.openxmlformats-officedocument.",  # docx / pptx / xlsx
    "application/vnd.oasis.opendocument.",  # odt / ods / odp
    "application/msword",
    "application/vnd.ms-",  # .ppt / .xls 舊格式
    "application/x-yaml",
)


def _storage_root() -> Path:
    root = ATTACHMENT_STORAGE_ROOT
    root.mkdir(parents=True, exist_ok=True)
    return root


def _truncate_error(msg: str, limit: int = 500) -> str:
    msg = (msg or "").strip()
    if len(msg) <= limit:
        return msg
    return msg[: limit - 1] + "…"


def _record_extract_failure(
    db: Session,
    attachment_id: int,
    reason: str,
) -> None:
    """Best-effort: leave the row as failed, never as a false 'ok'."""
    try:
        db.rollback()
    except Exception:
        pass
    try:
        att = db.get(Attachment, attachment_id)
        if att is None:
            return
        att.extract_status = "failed"
        att.extract_error = _truncate_error(reason)
        att.extracted_text = None
        # Keep token_count / page_count if already measured; clear if still pending.
        if att.token_count is None:
            att.token_count = None
        db.commit()
    except Exception:
        logger.exception(
            "extract_attachment_text: failed to record failure id=%s",
            attachment_id,
        )


def extract_attachment_text(
    attachment_id: int,
    db: Session | None = None,
    model_name: str | None = None,
) -> None:
    """Extract text for one attachment; never raises to the caller.

    Uses its own SessionLocal when ``db`` is omitted (BackgroundTasks path).
    Tests may pass an explicit session bound to the in-memory fixture engine.

    extract_status records extraction outcome only. Budget admission is
    derived later by ``admit()`` at meter / inject / API time.
    """
    own_session = db is None
    if own_session:
        db = SessionLocal()
    try:
        att = db.get(Attachment, attachment_id)
        if att is None:
            logger.warning(
                "extract_attachment_text: attachment_id=%s not found",
                attachment_id,
            )
            return

        full_path = _storage_root() / att.storage_path
        try:
            content = full_path.read_bytes()
        except OSError as exc:
            att.extract_status = "failed"
            att.extract_error = _truncate_error(f"無法讀取附件檔案：{exc}")
            att.token_count = None
            att.extracted_text = None
            db.commit()
            return

        try:
            from anila_core.ingestion.errors import ParseError
            from anila_core.ingestion.parsers import extract_text
        except ImportError as exc:
            att.extract_status = "failed"
            att.extract_error = _truncate_error(
                f"文件解析元件不可用：{exc}"
            )
            att.token_count = None
            db.commit()
            return

        try:
            text, metadata, _images = extract_text(
                att.filename, content, att.content_type,
            )
        except ParseError as exc:
            code = getattr(exc, "code", "") or ""
            user_msg = getattr(exc, "user_message", None) or str(exc)
            if code == "E_PARSE_FORMAT_UNSUPPORTED":
                att.extract_status = "unsupported"
            else:
                att.extract_status = "failed"
            att.extract_error = _truncate_error(user_msg)
            att.token_count = None
            att.extracted_text = None
            att.page_count = None
            db.commit()
            return
        except Exception as exc:
            logger.exception(
                "extract_attachment_text: unexpected error id=%s",
                attachment_id,
            )
            att.extract_status = "failed"
            att.extract_error = _truncate_error(
                f"附件解析失敗：{type(exc).__name__}"
            )
            att.token_count = None
            att.extracted_text = None
            db.commit()
            return

        page_count = metadata.get("page_count") if isinstance(metadata, dict) else None
        if page_count is not None:
            try:
                att.page_count = int(page_count)
            except (TypeError, ValueError):
                att.page_count = None

        raw_tokens = int(_estimate_token_count(None, text))
        att.token_count = raw_tokens
        att.extracted_at = datetime.now(timezone.utc)
        att.extract_error = None

        # 儲存上限是絕對值,與模型無關:抽取當下不知道會用哪個模型,若拿
        # 預算推導上限,大 context 模型本來吃得下的文件會在這裡被永久丟棄。
        store_cap = max_stored_tokens(db)

        if raw_tokens > store_cap:
            # Too large to persist — keep token_count for metering / notices,
            # withhold text so a 50 MB upload cannot write an unbounded row.
            # This IS an extraction-time outcome (we did not store the text).
            att.extracted_text = None
            att.extract_status = "too_large"
            att.extract_error = _truncate_error(
                f"抽取約 {raw_tokens} tokens，超過單份附件儲存上限"
                f"（{store_cap} tokens）"
            )
        else:
            att.extracted_text = text
            att.extract_status = "ok"

        try:
            db.commit()
        except Exception as exc:
            logger.exception(
                "extract_attachment_text: commit failed id=%s",
                attachment_id,
            )
            _record_extract_failure(
                db,
                attachment_id,
                f"附件抽取結果寫入失敗：{type(exc).__name__}",
            )
    except Exception:
        logger.exception(
            "extract_attachment_text: outer failure id=%s", attachment_id,
        )
        _record_extract_failure(db, attachment_id, "附件處理發生未預期錯誤")
    finally:
        if own_session:
            db.close()


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

    # Extension is the gate for text-class uploads. Browsers send
    # application/x-shellscript, video/mp2t (.ts), application/javascript,
    # etc. — chasing application/x-* prefixes forever is the wrong model.
    # Extensionless (for005/for006, README, Makefile, …): allow upload so
    # extract can decide by content guards — same honesty as text-class
    # (binary still lands unsupported; MIME must not block the path).
    # Keep the MIME prefix check for non-text-class typed extensions
    # (PDFs / Office / images / zip).
    declared_mime = (file.content_type or "").split(";")[0].strip().lower()
    if declared_mime and ext and ext not in TEXT_CLASS_EXTENSIONS:
        if not any(
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

    # No byte→token precheck: ASCII is ~4 bytes/token, so size//2 falsely
    # refuses the large-but-usable band. MAX_FILE_SIZE is the only upload
    # gate; budget admission is derived after extraction at use time.
    content_type = (
        file.content_type
        or mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )

    ref_id = str(uuid.uuid4())
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
        extract_status="pending",
    )
    db.add(att)
    db.commit()
    db.refresh(att)
    return att


def get_attachment(db: Session, reference_id: str, user: User) -> tuple[Attachment, Path]:
    # 三個消費端(下載 / meta / 刪除)都用不到 extracted_text,而它可能到數 MB。
    att = (
        db.query(Attachment)
        .options(defer(Attachment.extracted_text))
        .filter(Attachment.reference_id == reference_id)
        .first()
    )
    if not att:
        raise HTTPException(status_code=404, detail="找不到此附件")
    # uploader or admin-tier (admin/owner) may download
    if not is_admin_tier(user) and att.uploaded_by != user.id:
        raise HTTPException(status_code=403, detail="無權存取此附件")
    full_path = _storage_root() / att.storage_path
    if not full_path.is_file():
        raise HTTPException(status_code=404, detail="附件檔案不存在")
    return att, full_path


def delete_attachment(db: Session, reference_id: str, user: User) -> Optional[int]:
    """Delete attachment; return conversation_id (if any) for capacity response.

    No budget recheck: admission is re-derived on the next read / inject.
    """
    att, full_path = get_attachment(db, reference_id, user)
    conversation_id = att.conversation_id
    try:
        full_path.unlink(missing_ok=True)
    except OSError:
        pass
    db.delete(att)
    db.commit()
    return conversation_id


def capacity_for_conversation(
    db: Session,
    conversation_id: int,
    model_name: str | None = None,
) -> dict:
    # model_name None → fall back to the built-in context window
    # (client omitted ?model=; same helper as admission — no second budget).
    window = get_context_window(db, model_name)
    return get_conversation_attachment_usage(db, conversation_id, window)
