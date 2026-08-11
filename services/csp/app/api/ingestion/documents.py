"""Ingestion documents endpoint group.

Sprint 1 ships:

- ``POST /api/ingestion/collections/{id}/documents`` — multipart upload,
  writes blob to UPLOAD_DIR, INSERTs ingestion_documents row, enqueues
  ingest_document Arq job, INSERTs ingestion_jobs row tied to the job id.
- ``GET  /api/ingestion/collections/{id}/documents`` — paginated list of
  documents in a collection.
- ``GET  /api/ingestion/documents/{id}`` — detail row + last job row.

Document upload is the API end of the pipeline; the worker takes over
from the moment we enqueue. The dev sees the document in 'pending'
status immediately, then 'parsing' / 'chunking' / 'embedding' / 'indexed'
as the worker advances.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime, timezone

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from anila_core.ingestion.citation_extractor import normalize_title
from anila_core.storage.adapters.pg_pool import PgPool
from anila_core.storage.adapters.pgvector_store import CollectionScopedPgVectorStore

from app.api.ingestion.collections import _require_collection_access
from app.database import get_db
from app.models.ingestion import (
    IngestionCollection,
    IngestionDocument,
    IngestionJob,
)
from app.models.user import User
from app.services.audit_service import log_audit_event
from app.services.auth_service import get_current_user
from app.services.ingestion_classification import (
    effective_document_classification_level,
)
from app.services.ingestion_queue import enqueue_ingest_document
from app.schemas.base import ApiResponseModel

router = APIRouter(tags=["Ingestion / Documents"])


_UPLOAD_DIR = "/var/anila/ingestion-uploads"

# Sprint 1 hard cap. Larger files are a Sprint 2 concern (chunked upload,
# resumable, progress) — for now hard-fail with 413.
_MAX_BYTES = 50 * 1024 * 1024  # 50 MB

# Sprint 5 X / M2: zip 解壓總量上限。每檔 50MB × 200 檔 = 10GB 太寬鬆，
# 真正想處理的是「一次塞 200 個小檔」而不是「200 個極限大檔」；用
# total cap 1GB 限制磁碟寫入量，超過時直接停止後續解壓。
_ZIP_MAX_TOTAL_BYTES = 1 * 1024 * 1024 * 1024  # 1 GB

# 控制 zip 內檔名能用的字元 — 阻擋 NUL / CR / LF（Content-Disposition
# header 注入）以及任何 ``..`` segment（路徑遍歷顯示偽裝）。儲存路徑用
# sha256 不會受影響，但使用者下載時的 ``filename`` 一定要乾淨。
_FILENAME_BAD_CHARS = ("\x00", "\r", "\n")


def _zip_member_name(db: Session, member: zipfile.ZipInfo) -> str:
    """還原 zip 內檔名的正確編碼。

    ``zipfile`` 對「沒設 UTF-8 旗標(general-purpose bit 11 / 0x800)」的 entry
    一律用 CP437 解檔名;但 Windows 內建壓縮存的中文檔名其實是 CP950/Big5(或
    GBK)→ 被 CP437 解成亂碼。偵測到無 UTF-8 旗標時,把字串還原成原始 bytes 再用
    台灣常見編碼重解。單檔上傳沒這問題(檔名來自 multipart,本來就 UTF-8)。

    覆寫碼頁固定走 cp950 → gbk 的內建序，**每個成員都重解一次**。一次 zip 上限
    200 個成員,而每個
    成員本來就要落檔＋雜湊＋寫一列,一次主鍵查詢在這裡不是熱點;把它提到迴圈外
    先讀起來反而多一個「讀取時機」要解釋,而那正是本包在消滅的那種形狀。
    """
    name = member.filename
    if member.flag_bits & 0x800:
        return name  # entry 已標 UTF-8,zipfile 解對了
    try:
        raw = name.encode("cp437")
    except UnicodeEncodeError:
        return name  # 不是 CP437 能表示的,維持原樣
    # 純 ASCII 檔名在 CP437/CP950/UTF-8 下位元組相同,zipfile 本來就解對 → 別動,
    # 避免把合法 ASCII 名誤判成需要轉碼。
    if all(b < 0x80 for b in raw):
        return name
    # 非 UTF-8 旗標 + 含非 ASCII bytes → 多半是本地碼頁存的 CJK 檔名被 zipfile 用
    # CP437 誤解。台灣內網固定優先 CP950(Big5 是其子集),再嘗試 GBK。⚠ 啟發式:各 CJK 碼頁 byte 範圍
    # 重疊,"decode 成功" 不保證 100% 正確,但對單一語系內網是合理預設。
    override = ""
    encs = [e for e in (override, "cp950", "gbk") if e]
    for enc in encs:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return name


def _sanitize_archive_filename(raw: str, *, preserve_folder_structure: bool) -> str:
    """Return a safe ``filename`` for documents pulled from an uploaded zip.

    - ``preserve_folder_structure=False``: keep basename only.
    - ``preserve_folder_structure=True``: keep relative path BUT collapse
      any ``..`` segments and reject control chars. We never use this name
      to build a filesystem path (that's ``storage_path`` derived from
      sha256), but it is echoed back in JSON responses and as the download
      ``Content-Disposition``, so we still need to keep it free of CRLF.
    """
    name = raw or "upload"
    if preserve_folder_structure:
        # Drop leading slashes / drive letters; collapse "..".
        parts = [p for p in name.replace("\\", "/").split("/") if p and p != "."]
        parts = [p for p in parts if p != ".."]
        name = "/".join(parts) or "upload"
    else:
        name = os.path.basename(name) or "upload"

    for ch in _FILENAME_BAD_CHARS:
        name = name.replace(ch, "_")
    # Cap the displayable length so a malicious 65k-char filename can't
    # be persisted (DB column is 1000 wide; trim safely below that).
    return name[:512]


def _derive_title(filename: str, explicit: str | None = None) -> tuple[str | None, str | None]:
    """Pick a document ``title`` + its ``normalized_title`` for citation resolution.

    Phase 1 of document-relations: citation targets resolve by matching a
    cited regulation name against ``ingestion_documents.normalized_title``, so
    every document needs a best-effort title. Source priority:

    1. ``explicit`` — the uploader knows the real regulation name (the most
       reliable source per design §13; ROC regs are often named by 字號 inside
       the file but the human knows the canonical name).
    2. filename stem — strip the extension and any path; a deterministic
       fallback so the column is never null.

    The worker (task #40) may later refine ``title`` from a parsed first
    heading. Returns ``(title, normalized_title)``; ``normalized_title`` is the
    durable join key (NFKC + brackets/whitespace stripped).
    """
    raw = (explicit or "").strip()
    if not raw:
        base = os.path.basename(filename or "")
        raw = os.path.splitext(base)[0].strip()
    if not raw:
        return None, None
    title = raw[:500]
    return title, (normalize_title(title) or None)


class DocumentResponse(ApiResponseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    collection_id: int
    filename: str
    title: str | None = None
    normalized_title: str | None = None
    sha256: str
    mime_type: str | None
    bytes: int | None
    status: str
    chunk_count: int
    error_message: str | None
    uploaded_by: int | None
    uploaded_at: datetime
    indexed_at: datetime | None
    # 有效密等（max(文件, 知識庫)）；由 _document_response 填入，
    # 不要直接從 ORM 欄位投影，以免讀到未級聯的較低值。
    classification_level: str = "無機密"


class DocumentDetailResponse(DocumentResponse):
    """Document row + last job row for status display."""

    latest_job_id: int | None = None
    latest_job_status: str | None = None
    latest_job_error_code: str | None = None
    arq_job_id: str | None = None


# ── Helpers ─────────────────────────────────────────────────────────────────


def _resolve_collection(
    db: Session, user: User, collection_id: int
) -> IngestionCollection:
    """Sprint 4: collection access keyed on ownership, not agent_id.

    ``_require_collection_access`` does its own row fetch + 404 + ACL —
    we just delegate. Returning the row keeps the existing call sites
    working unchanged.
    """
    return _require_collection_access(db, user, collection_id)


def _document_response(
    db: Session,
    doc: IngestionDocument,
    *,
    collection: IngestionCollection | None = None,
) -> DocumentResponse:
    """Project a document with effective classification (never the column alone)."""
    coll = collection
    if coll is None:
        coll = db.get(IngestionCollection, doc.collection_id)
    payload = DocumentResponse.model_validate(doc)
    payload.classification_level = effective_document_classification_level(
        doc, coll
    ).to_storage()
    return payload


def _persist_blob(content: bytes, sha256: str) -> str:
    """Write the upload to disk under a content-addressable path.

    Path = ``UPLOAD_DIR/<sha256[:2]>/<sha256>`` so we get a flat 2-deep
    directory structure (~256 entries per top-level dir even at scale).
    Same sha256 → same path → re-uploads are no-ops at the FS layer.
    The DB layer separately enforces the (collection_id, sha256) unique.
    """
    sub = os.path.join(_UPLOAD_DIR, sha256[:2])
    os.makedirs(sub, exist_ok=True)
    path = os.path.join(sub, sha256)
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(content)
    return path


# ── Endpoints ───────────────────────────────────────────────────────────────


@router.post(
    "/api/ingestion/collections/{collection_id}/documents",
    response_model=DocumentResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_document(
    collection_id: int,
    file: UploadFile = File(...),
    title: str | None = Form(
        default=None,
        description=(
            "Optional canonical regulation/document name used to resolve "
            "cross-document citation targets. Falls back to the filename stem."
        ),
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DocumentResponse:
    """Accept one file, persist, enqueue ingestion job.

    Returns 202 (Accepted) — the document row is written but indexing
    happens async. Caller polls ``GET /api/ingestion/documents/{id}``
    to watch status transitions.
    """
    coll = _resolve_collection(db, current_user, collection_id)

    # Auth / collection resolve done; release before reading the upload body
    # (up to 50 MB) so the pooled connection is not pinned across I/O.
    db.commit()

    # Read fully into memory — Sprint 1 caps uploads at 50 MB so this is
    # fine; Sprint 2 streaming upload will spool to disk in chunks.
    content = await file.read()
    size = len(content)
    if size == 0:
        raise HTTPException(status_code=400, detail="Empty file")
    if size > _MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({size:,} > {_MAX_BYTES:,} bytes)",
        )

    sha256 = hashlib.sha256(content).hexdigest()
    storage_path = _persist_blob(content, sha256)

    doc_title, doc_norm_title = _derive_title(file.filename or "", title)

    # Insert the document row. Uniqueness on (collection_id, sha256) gives
    # us cheap content-level dedup — re-uploading the same file just
    # returns the existing row.
    # Inherit the collection's level at insert so a document never starts
    # below its library (raise path cascades later via apply_classification).
    doc = IngestionDocument(
        collection_id=collection_id,
        filename=file.filename or sha256,
        title=doc_title,
        normalized_title=doc_norm_title,
        sha256=sha256,
        mime_type=file.content_type,
        bytes=size,
        storage_path=storage_path,
        status="pending",
        chunk_count=0,
        uploaded_by=current_user.id,
        classification_level=(
            getattr(coll, "classification_level", None) or "無機密"
        ),
    )
    db.add(doc)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # Same content already uploaded to this collection — return the
        # existing row instead of erroring. Idempotent uploads matter
        # for retry-prone clients.
        existing = (
            db.query(IngestionDocument)
            .filter(
                IngestionDocument.collection_id == collection_id,
                IngestionDocument.sha256 == sha256,
            )
            .first()
        )
        if existing is None:
            raise HTTPException(status_code=500, detail="Upload conflict")
        return _document_response(db, existing, collection=coll)
    db.refresh(doc)

    # Enqueue + create the matching jobs row. We do this in two steps
    # because Arq returns a job id only after enqueue, and we want the
    # row to carry that id from the start (no UPDATE-after-INSERT race).
    arq_job_id = await enqueue_ingest_document(doc.id)
    job = IngestionJob(
        arq_job_id=arq_job_id,
        collection_id=collection_id,
        document_id=doc.id,
        job_type="ingest",
        status="queued",
        progress_pct=0,
        enqueued_by=current_user.id,
    )
    db.add(job)
    db.commit()

    log_audit_event(
        db,
        commit=True,
        actor=current_user,
        action="ingestion_document_upload",
        resource_type="ingestion_document",
        resource_id=doc.id,
        metadata={
            "collection_id": collection_id,
            "filename": doc.filename,
            "size": size,
            "arq_job_id": arq_job_id,
        },
    )
    return _document_response(db, doc, collection=coll)


@router.post(
    "/api/ingestion/documents/{document_id}/reprocess",
    response_model=DocumentResponse,
)
async def reprocess_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DocumentResponse:
    """重新嵌入既有 document(通常是 parse/embedding 失敗、卡在 status='failed' 的)。

    失敗檔原本沒有重試入口,而 (collection_id, sha256) unique 也擋住重傳同一個檔
    → 資料卡死。這個端點讓 owner/admin 對既有 row 直接 re-enqueue ingest job,不必
    重傳;重設 status='pending' 並清掉上次的 error_message。進行中的檔拒絕重塞,
    避免同一檔並行兩個 job。
    """
    doc = db.get(IngestionDocument, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    # 透過所屬 collection 做存取控管(與上傳走同一條授權路徑)。
    _resolve_collection(db, current_user, doc.collection_id)
    # 只允許重試「失敗」的檔 — 其餘狀態各有正常流程,避免重複塞 job。
    if doc.status != "failed":
        raise HTTPException(
            status_code=409,
            detail=f"Only failed documents can be reprocessed (current: {doc.status})",
        )
    # 原子狀態轉移:conditional UPDATE failed→pending,只有真的搶到(rowcount==1)
    # 才往下 enqueue,擋住兩個並行請求同時 re-enqueue 同一份文件(競態 + 重複 job)。
    claimed = (
        db.query(IngestionDocument)
        .filter(
            IngestionDocument.id == document_id,
            IngestionDocument.status == "failed",
        )
        .update(
            {"status": "pending", "error_message": None},
            synchronize_session=False,
        )
    )
    db.commit()
    if claimed != 1:
        raise HTTPException(
            status_code=409, detail="Document is no longer in a failed state"
        )

    # enqueue 失敗就把狀態退回 failed,避免文件卡在「pending 但沒有 job」的死角。
    try:
        arq_job_id = await enqueue_ingest_document(document_id)
    except Exception:
        db.query(IngestionDocument).filter(
            IngestionDocument.id == document_id
        ).update({"status": "failed"}, synchronize_session=False)
        db.commit()
        raise HTTPException(
            status_code=502,
            detail="Failed to enqueue ingest job; document left as failed",
        )
    job = IngestionJob(
        arq_job_id=arq_job_id,
        collection_id=doc.collection_id,
        document_id=document_id,
        job_type="ingest",
        status="queued",
        progress_pct=0,
        enqueued_by=current_user.id,
    )
    db.add(job)
    db.commit()
    db.refresh(doc)

    log_audit_event(
        db,
        commit=True,
        actor=current_user,
        action="ingestion_document_reprocess",
        resource_type="ingestion_document",
        resource_id=doc.id,
        metadata={
            "collection_id": doc.collection_id,
            "filename": doc.filename,
            "arq_job_id": arq_job_id,
        },
    )
    return _document_response(db, doc)


class ZipUploadResult(BaseModel):
    """Per-file outcome of a multi-file zip upload."""

    filename: str
    document_id: int | None = None
    arq_job_id: str | None = None
    status: str  # 'enqueued' | 'duplicate' | 'skipped' | 'too_large' | 'error'
    detail: str | None = None


class ZipUploadResponse(BaseModel):
    """Aggregated outcome of a single zip upload."""

    files_in_archive: int
    enqueued: int
    duplicates: int
    skipped: int
    errors: int
    results: list[ZipUploadResult]


def _declared_zip_member_error(
    member,
    out_name: str,
    *,
    cumulative_bytes: int,
) -> ZipUploadResult | None:
    """Preflight zip metadata before inflating a member.

    ``ZipInfo.file_size`` is the declared uncompressed size. We still keep
    the post-read checks below as defense in depth, but this avoids inflating
    members that are already known to exceed per-file or archive-total caps.
    """
    declared_size = max(int(getattr(member, "file_size", 0) or 0), 0)
    if cumulative_bytes >= _ZIP_MAX_TOTAL_BYTES:
        return ZipUploadResult(
            filename=out_name,
            status="skipped",
            detail="archive total exceeds 1 GB cap",
        )
    if declared_size == 0:
        return ZipUploadResult(
            filename=out_name,
            status="skipped",
            detail="empty file",
        )
    if declared_size > _MAX_BYTES:
        return ZipUploadResult(
            filename=out_name,
            status="too_large",
            detail=f"{declared_size:,} bytes exceeds {_MAX_BYTES:,} limit",
        )
    if cumulative_bytes + declared_size > _ZIP_MAX_TOTAL_BYTES:
        return ZipUploadResult(
            filename=out_name,
            status="skipped",
            detail="archive total exceeds 1 GB cap (this file pushed over)",
        )
    return None


@router.post(
    "/api/ingestion/collections/{collection_id}/documents/zip",
    response_model=ZipUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_zip(
    collection_id: int,
    file: UploadFile = File(...),
    preserve_folder_structure: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ZipUploadResponse:
    """Bulk upload via zip archive.

    Each file in the zip becomes a document in the collection. Files
    that hit the per-file 50 MB cap are reported as ``too_large`` but
    don't fail the whole upload; same-sha256 duplicates report
    ``duplicate`` and skip enqueue. Folders / dotfiles (``__MACOSX/``,
    ``.DS_Store``) are filtered out.

    ``preserve_folder_structure``: when True, prepends the in-zip path
    to ``filename`` so the inspector can group by folder. When False
    (default), only the basename is kept — useful when the zip was
    created with a "compress everything in this folder" UI that
    introduces a useless top-level wrapper.

    Hard limit: 200 files per zip. Bigger archives should be split or
    use the future Sprint 4 streaming API.
    """
    import zipfile
    from io import BytesIO

    coll = _resolve_collection(db, current_user, collection_id)

    archive_bytes = await file.read()
    if len(archive_bytes) > 500 * 1024 * 1024:  # 500 MB cap on archive
        raise HTTPException(status_code=413, detail="Zip archive > 500 MB")
    try:
        zf = zipfile.ZipFile(BytesIO(archive_bytes))
    except zipfile.BadZipFile as e:
        raise HTTPException(status_code=400, detail=f"Not a valid zip: {e}") from e

    # Filter to actual file entries; reject anything that smells dodgy.
    members = [
        m for m in zf.infolist()
        if not m.is_dir()
        and not m.filename.startswith("__MACOSX/")
        and not os.path.basename(m.filename).startswith(".")
    ]
    if len(members) > 200:
        raise HTTPException(
            status_code=413,
            detail=f"{len(members)} files in archive; limit is 200 per zip",
        )

    results: list[ZipUploadResult] = []
    enqueued = duplicates = skipped = errors = 0
    # 累積解壓資料量；超過 _ZIP_MAX_TOTAL_BYTES 後續成員一律 skipped。
    cumulative_bytes = 0

    for member in members:
        # Choose the document filename based on preserve_folder_structure.
        # 先還原檔名編碼(zip 內非 UTF-8 旗標的中文檔名會被 CP437 解成亂碼)。
        in_zip_path = _zip_member_name(db, member)
        out_name = _sanitize_archive_filename(
            in_zip_path,
            preserve_folder_structure=preserve_folder_structure,
        )

        declared_error = _declared_zip_member_error(
            member,
            out_name,
            cumulative_bytes=cumulative_bytes,
        )
        if declared_error is not None:
            skipped += 1
            results.append(declared_error)
            continue

        try:
            content = zf.read(member)
        except Exception as e:
            errors += 1
            results.append(ZipUploadResult(
                filename=in_zip_path, status="error",
                detail=f"unzip failed: {type(e).__name__}",
            ))
            continue

        size = len(content)
        if size == 0:
            skipped += 1
            results.append(ZipUploadResult(
                filename=out_name, status="skipped", detail="empty file",
            ))
            continue
        if size > _MAX_BYTES:
            skipped += 1
            results.append(ZipUploadResult(
                filename=out_name, status="too_large",
                detail=f"{size:,} bytes exceeds {_MAX_BYTES:,} limit",
            ))
            continue
        cumulative_bytes += size
        if cumulative_bytes > _ZIP_MAX_TOTAL_BYTES:
            skipped += 1
            results.append(ZipUploadResult(
                filename=out_name, status="skipped",
                detail="archive total exceeds 1 GB cap (this file pushed over)",
            ))
            continue

        sha256 = hashlib.sha256(content).hexdigest()
        storage_path = _persist_blob(content, sha256)

        # Check for duplicate (same sha within collection).
        existing = (
            db.query(IngestionDocument)
            .filter(
                IngestionDocument.collection_id == collection_id,
                IngestionDocument.sha256 == sha256,
            )
            .first()
        )
        if existing is not None:
            duplicates += 1
            results.append(ZipUploadResult(
                filename=out_name, status="duplicate",
                document_id=existing.id,
                detail="same sha already in collection",
            ))
            continue

        # Sniff a MIME from the filename — UploadFile.content_type is the
        # zip's content_type, not per-member.
        import mimetypes
        mime, _ = mimetypes.guess_type(out_name)

        member_title, member_norm_title = _derive_title(out_name)
        doc = IngestionDocument(
            collection_id=collection_id,
            filename=out_name,
            title=member_title,
            normalized_title=member_norm_title,
            sha256=sha256,
            mime_type=mime,
            bytes=size,
            storage_path=storage_path,
            status="pending",
            chunk_count=0,
            uploaded_by=current_user.id,
            classification_level=(
                getattr(coll, "classification_level", None) or "無機密"
            ),
        )
        db.add(doc)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            duplicates += 1
            results.append(ZipUploadResult(
                filename=out_name, status="duplicate",
                detail="raced with concurrent upload",
            ))
            continue
        db.refresh(doc)

        try:
            arq_job_id = await enqueue_ingest_document(doc.id)
        except Exception as e:
            errors += 1
            results.append(ZipUploadResult(
                filename=out_name, status="error",
                document_id=doc.id,
                detail=f"enqueue failed: {type(e).__name__}",
            ))
            continue

        job = IngestionJob(
            arq_job_id=arq_job_id,
            collection_id=collection_id,
            document_id=doc.id,
            job_type="ingest",
            status="queued",
            progress_pct=0,
            enqueued_by=current_user.id,
        )
        db.add(job)
        db.commit()

        enqueued += 1
        results.append(ZipUploadResult(
            filename=out_name, status="enqueued",
            document_id=doc.id, arq_job_id=arq_job_id,
        ))

    log_audit_event(
        db,
        commit=True,
        actor=current_user,
        action="ingestion_document_upload_zip",
        resource_type="ingestion_collection",
        resource_id=collection_id,
        metadata={
            "files_in_archive": len(members),
            "enqueued": enqueued, "duplicates": duplicates,
            "skipped": skipped, "errors": errors,
        },
    )
    return ZipUploadResponse(
        files_in_archive=len(members),
        enqueued=enqueued,
        duplicates=duplicates,
        skipped=skipped,
        errors=errors,
        results=results,
    )


@router.get(
    "/api/ingestion/collections/{collection_id}/documents",
    response_model=list[DocumentResponse],
)
def list_documents(
    collection_id: int,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[DocumentResponse]:
    coll = _resolve_collection(db, current_user, collection_id)
    rows = (
        db.query(IngestionDocument)
        .filter(IngestionDocument.collection_id == collection_id)
        .order_by(IngestionDocument.id.desc())
        .limit(limit)
        .offset(offset)
        .all()
    )
    return [_document_response(db, r, collection=coll) for r in rows]


@router.get(
    "/api/ingestion/documents/{document_id}",
    response_model=DocumentDetailResponse,
)
def get_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DocumentDetailResponse:
    """Document row + most recent job row joined.

    The inspector polls this endpoint to render the parse → chunk →
    embed → indexed timeline. We always fetch the *latest* job because
    a re-ingest creates a new row; the older ones stay for audit but
    aren't UI-relevant.
    """
    doc = (
        db.query(IngestionDocument)
        .filter(IngestionDocument.id == document_id)
        .first()
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    coll = _resolve_collection(db, current_user, doc.collection_id)

    latest_job = (
        db.query(IngestionJob)
        .filter(IngestionJob.document_id == document_id)
        .order_by(IngestionJob.id.desc())
        .first()
    )

    base = _document_response(db, doc, collection=coll)
    payload = DocumentDetailResponse(**base.model_dump())
    if latest_job is not None:
        payload.latest_job_id = latest_job.id
        payload.latest_job_status = latest_job.status
        payload.latest_job_error_code = latest_job.error_code
        payload.arq_job_id = latest_job.arq_job_id
    return payload


@router.delete(
    "/api/ingestion/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
def delete_document(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Response:
    """Delete a document, its chunks (CASCADE), and its blob if unique.

    Three layers to clean up:
      1. ``ingestion_documents`` row — straight DELETE.
      2. ``document_chunks`` rows — handled by ``ON DELETE CASCADE`` on
         the FK (see migration 0014); pgvector + tsv index entries fall
         out automatically.
      3. The on-disk blob at ``UPLOAD_DIR/<sha256[:2]>/<sha256>`` — the
         storage layer is content-addressable, so the SAME file may
         back multiple ``ingestion_documents`` rows (uploading the same
         PDF to two collections shares the bytes). We only ``unlink``
         when no other row references that ``sha256``. This keeps the
         FS lean for the common case while staying correct under
         deduped re-uploads.

    Job rows in ``ingestion_jobs`` keep their FK pointing at the doc id
    via ``ON DELETE CASCADE`` too, so worker history disappears with the
    document. If you want to retain audit trail beyond the row, the
    ``audit_log`` entry below is the durable record.
    """
    doc = (
        db.query(IngestionDocument)
        .filter(IngestionDocument.id == document_id)
        .first()
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")

    # Auth: must be admin or own the parent collection.
    _resolve_collection(db, current_user, doc.collection_id)

    snapshot = {
        "filename": doc.filename,
        "sha256": doc.sha256,
        "collection_id": doc.collection_id,
        "bytes": doc.bytes,
    }
    blob_path = doc.storage_path
    sha256 = doc.sha256

    db.delete(doc)
    db.commit()

    # Refcount the blob: only unlink if nothing else points at this sha.
    # Done AFTER commit so a concurrent upload that lands the same sha
    # on disk doesn't lose its file (the new row is visible to this
    # query). The race window is tiny — ingestion_documents.sha256 has a
    # uniqueness constraint per collection, so we'd have to commit a new
    # doc in another collection between the commit above and the count
    # below. In that race we'd unlink and the new doc's worker would
    # repopulate via _persist_blob (idempotent: writes only if absent).
    still_referenced = (
        db.query(IngestionDocument)
        .filter(IngestionDocument.sha256 == sha256)
        .count()
    )
    if still_referenced == 0 and blob_path:
        try:
            if os.path.exists(blob_path):
                os.unlink(blob_path)
        except OSError:
            # FS errors are not fatal — the row is gone, the orphan
            # blob is at worst a cleanup chore handled by a sweeper job.
            pass

    log_audit_event(
        db,
        commit=True,
        actor=current_user,
        action="ingestion_document_delete",
        resource_type="ingestion_document",
        resource_id=document_id,
        metadata=snapshot,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Inspector endpoints (Sprint 2 Chunk H) ──────────────────────────────────


class ChunkRow(ApiResponseModel):
    """Inspector-facing chunk row.

    Embedding is omitted by default because the inspector list view
    doesn't render the 4000-d vector. Vector debug info goes through
    the dedicated ``/embedding-debug`` endpoint behind a UI toggle.
    """

    id: int
    chunk_key: str
    content: str
    metadata: dict
    token_count: int | None
    created_at: datetime
    # Sprint 9 X / parent-child RAG (migration 0028). All optional;
    # legacy rows that predate the new chunker default to
    # ``chunk_type='leaf'`` / ``parent_chunk_id=None`` so existing UIs
    # see no behaviour change unless they look at the new fields.
    parent_chunk_id: int | None = None
    chunk_type: str = "leaf"
    chunk_level: int = 0


class ChunkEmbeddingDebug(BaseModel):
    """Vector-debug summary for a single chunk.

    Only ``dim`` and ``norm`` are returned — never the full vector.
    Bandwidth: 30 bytes per chunk vs ~16 KB if the raw embedding shipped.
    Useful to confirm chunks were actually embedded (norm ≈ 1 means
    L2-normalised; embedding pipelines that drop normalisation surface
    here as norm ≠ 1).
    """

    chunk_id: int
    dim: int
    norm: float


@router.get(
    "/api/ingestion/documents/{document_id}/chunks",
    response_model=list[ChunkRow],
)
async def list_document_chunks(
    document_id: int,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[ChunkRow]:
    """Inspector chunk-list — agent-scoped via AgentScopedPgVectorStore.

    Goes through the central SDK so RLS auto-filters even if the API
    layer's own auth check (``_resolve_collection``) is buggy. Belt-
    and-suspenders security.
    """
    from app.services.ingestion_pool import get_pool

    doc = (
        db.query(IngestionDocument)
        .filter(IngestionDocument.id == document_id)
        .first()
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    coll = _resolve_collection(db, current_user, doc.collection_id)
    collection_id = coll.id
    # Auth done; release before asyncpg store await (no SQLAlchemy work during it).
    db.commit()

    try:
        pool = get_pool()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    store = CollectionScopedPgVectorStore(pool, collection_id=collection_id)
    chunks = await store.list_by_document(
        document_id=document_id,
        limit=limit,
        offset=offset,
        include_embedding=False,
    )
    return [
        ChunkRow(
            id=c.id,
            chunk_key=c.chunk_key,
            content=c.content,
            metadata=c.metadata or {},
            token_count=c.token_count,
            created_at=c.created_at,
            parent_chunk_id=getattr(c, "parent_chunk_id", None),
            chunk_type=getattr(c, "chunk_type", "leaf") or "leaf",
            chunk_level=getattr(c, "chunk_level", 0) or 0,
        )
        for c in chunks
    ]


@router.get(
    "/api/ingestion/documents/{document_id}/chunks/{chunk_id}/embedding-debug",
    response_model=ChunkEmbeddingDebug,
)
async def get_chunk_embedding_debug(
    document_id: int,
    chunk_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ChunkEmbeddingDebug:
    """Return ``(dim, L2 norm)`` for one chunk — the "Show vector debug"
    payload behind the Inspector toggle.

    The route is keyed on ``(document_id, chunk_id)`` rather than
    ``chunk_id`` alone because the Layer 2 RLS policy default-denies
    every ``document_chunks`` row when no GUC is set, which means we
    can't look up the owning agent_id directly from the chunks table.
    Resolving via ``ingestion_documents`` → ``ingestion_collections``
    (regular non-RLS tables) is the clean way through. The frontend
    always has both ids in hand from the chunks-list endpoint anyway.

    Wire payload is 2 scalars (~30 bytes); the full halfvec stays
    server-side. Used behind the inspector's "Show vector debug"
    toggle so the page render isn't paying for it by default.
    """
    import math

    from app.services.ingestion_pool import get_pool

    doc = (
        db.query(IngestionDocument)
        .filter(IngestionDocument.id == document_id)
        .first()
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    coll = _resolve_collection(db, current_user, doc.collection_id)
    collection_id = coll.id
    # Auth done; release before asyncpg fetch.
    db.commit()

    try:
        pool = get_pool()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    store = CollectionScopedPgVectorStore(pool, collection_id=collection_id)
    async with store._acquire() as conn:  # noqa: SLF001
        row = await conn.fetchrow(
            """
            SELECT id, embedding
              FROM document_chunks
             WHERE id = $1 AND document_id = $2
            """,
            chunk_id,
            document_id,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="Chunk not found")

    emb = row["embedding"]
    # ``HalfVector`` from pgvector exposes ``.to_list()``; raw lists
    # iterate directly. Both shapes appear depending on codec version.
    components = list(emb.to_list()) if hasattr(emb, "to_list") else list(emb)
    norm = math.sqrt(sum(c * c for c in components)) if components else 0.0
    return ChunkEmbeddingDebug(
        chunk_id=int(row["id"]),
        dim=len(components),
        norm=norm,
    )


@router.get("/api/ingestion/documents/{document_id}/blob")
def download_document_blob(
    document_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> FileResponse:
    """Stream the raw uploaded file back to the inspector.

    Auth: standard collection-scope check. The blob lives at
    ``UPLOAD_DIR/<sha[:2]>/<sha>`` — we don't do path traversal because
    we read storage_path from the DB row, not from a query string.
    """
    doc = (
        db.query(IngestionDocument)
        .filter(IngestionDocument.id == document_id)
        .first()
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    _resolve_collection(db, current_user, doc.collection_id)  # auth check

    if not doc.storage_path or not os.path.exists(doc.storage_path):
        raise HTTPException(status_code=410, detail="Blob no longer on disk")
    return FileResponse(
        path=doc.storage_path,
        media_type=doc.mime_type or "application/octet-stream",
        filename=doc.filename,
    )
